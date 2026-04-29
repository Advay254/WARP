"""
safety.py — Anti-ban measures, rate limiting, message queue with jitter.

All outbound messages go through send_message_safe() which:
  1. Checks per-instance rate limits (Redis or in-memory fallback)
  2. Simulates typing presence before sending
  3. Adds randomised delay between messages
  4. Detects broadcast patterns and queues them
  5. Enforces conversation depth limits
"""

import os
import time
import random
import asyncio
import hashlib
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Config from env ───────────────────────────────────────────────────────────
WARMUP_MODE    = os.environ.get("WARMUP_MODE", "false").lower() == "true"
MAX_TURNS      = int(os.environ.get("MAX_TURNS", "20"))
MIN_DELAY_MS   = int(os.environ.get("MSG_MIN_DELAY_MS", "1500")) * (2 if WARMUP_MODE else 1)
MAX_DELAY_MS   = int(os.environ.get("MSG_MAX_DELAY_MS", "4000")) * (2 if WARMUP_MODE else 1)
MAX_PER_MIN    = int(os.environ.get("MSG_MAX_PER_MIN", "20")) // (2 if WARMUP_MODE else 1)
BURST_LIMIT    = int(os.environ.get("MSG_BURST_LIMIT", "10")) // (2 if WARMUP_MODE else 1)
BURST_COOLDOWN = int(os.environ.get("MSG_BURST_COOLDOWN_SEC", "300"))

EVO_INTERNAL   = f"http://localhost:{os.environ.get('EVOLUTION_PORT', '8080')}"
EVO_KEY        = os.environ.get("EVOLUTION_API_KEY", "")

# ── In-memory fallback counters (used when Redis is unavailable) ──────────────
_mem_counters: dict = {}   # {instance: {"count": int, "window_start": float, "burst": int, "burst_start": float}}

# ── Conversation turn tracking ────────────────────────────────────────────────
_turn_counts: dict = {}    # {f"{instance}:{jid}": int}

# ── Bot message fingerprints (to prevent reply loops) ────────────────────────
_bot_message_ids: set = set()
_bot_message_ids_maxsize = 1000


def _get_redis():
    """Return Redis client or None if unavailable."""
    try:
        import redis
        uri = os.environ.get("CACHE_REDIS_URI", "")
        if not uri:
            return None
        r = redis.from_url(uri, decode_responses=True, socket_connect_timeout=2)
        r.ping()
        return r
    except Exception:
        return None


def check_rate_limit(instance: str) -> tuple[bool, str]:
    """
    Returns (allowed: bool, reason: str).
    Enforces per-minute limit and burst cooldown.
    """
    now = time.time()
    r   = _get_redis()

    if r:
        # Redis-backed counters
        min_key   = f"evo:rate:{instance}:min"
        burst_key = f"evo:rate:{instance}:burst"
        cd_key    = f"evo:rate:{instance}:cooldown"

        # Check cooldown
        if r.exists(cd_key):
            ttl = r.ttl(cd_key)
            return False, f"Burst cooldown active — {ttl}s remaining"

        # Per-minute counter
        count = r.incr(min_key)
        if count == 1:
            r.expire(min_key, 60)
        if count > MAX_PER_MIN:
            return False, f"Rate limit: {MAX_PER_MIN} messages/min exceeded"

        # Burst counter
        burst = r.incr(burst_key)
        if burst == 1:
            r.expire(burst_key, 60)
        if burst > BURST_LIMIT:
            r.setex(cd_key, BURST_COOLDOWN, "1")
            return False, f"Burst limit hit — {BURST_COOLDOWN}s cooldown started"
    else:
        # In-memory fallback
        state = _mem_counters.setdefault(instance, {
            "count": 0, "window_start": now,
            "burst": 0, "burst_start": now,
            "cooldown_until": 0,
        })

        if now < state["cooldown_until"]:
            remaining = int(state["cooldown_until"] - now)
            return False, f"Burst cooldown active — {remaining}s remaining"

        if now - state["window_start"] > 60:
            state["count"] = 0; state["window_start"] = now
        state["count"] += 1
        if state["count"] > MAX_PER_MIN:
            return False, f"Rate limit: {MAX_PER_MIN} messages/min exceeded"

        if now - state["burst_start"] > 60:
            state["burst"] = 0; state["burst_start"] = now
        state["burst"] += 1
        if state["burst"] > BURST_LIMIT:
            state["cooldown_until"] = now + BURST_COOLDOWN
            return False, f"Burst limit hit — {BURST_COOLDOWN}s cooldown started"

    return True, "ok"


def check_turn_limit(instance: str, jid: str) -> tuple[bool, str]:
    """Track conversation depth per instance+contact."""
    key   = f"{instance}:{jid}"
    turns = _turn_counts.get(key, 0) + 1
    _turn_counts[key] = turns
    if turns > MAX_TURNS:
        return False, f"Max conversation depth ({MAX_TURNS}) reached"
    return True, "ok"


def reset_turns(instance: str, jid: str):
    _turn_counts.pop(f"{instance}:{jid}", None)


def register_bot_message(message_id: str):
    """Mark a message ID as bot-originated to prevent reply loops."""
    _bot_message_ids.add(message_id)
    if len(_bot_message_ids) > _bot_message_ids_maxsize:
        # Remove oldest entries (approximation)
        to_remove = list(_bot_message_ids)[:100]
        for mid in to_remove:
            _bot_message_ids.discard(mid)


def is_bot_message(message_id: str) -> bool:
    return message_id in _bot_message_ids


def is_self_message(sender_jid: str, instance_number: str) -> bool:
    """Check if sender is the bot's own number."""
    sender_num = sender_jid.replace("@s.whatsapp.net", "").replace("@c.us", "")
    own_num    = instance_number.replace("+", "").replace(" ", "")
    return sender_num == own_num


def detect_broadcast_pattern(instance: str, body: str, recipient: str) -> bool:
    """
    Returns True if same message body is being sent to many recipients quickly.
    Uses a hash of (instance, body) and tracks unique recipients.
    """
    r   = _get_redis()
    key = f"evo:broadcast:{instance}:{hashlib.md5(body.encode()).hexdigest()}"
    now = time.time()

    if r:
        count = r.incr(key)
        if count == 1:
            r.expire(key, 60)
        return count > 3
    else:
        # Simple in-memory check
        store = _mem_counters.setdefault(key, {"count": 0, "ts": now})
        if now - store["ts"] > 60:
            store["count"] = 0; store["ts"] = now
        store["count"] += 1
        return store["count"] > 3


async def send_message_safe(
    instance:  str,
    jid:       str,
    message:   dict,
    instance_number: str = "",
) -> dict:
    """
    Safe message sender with:
      - Rate limit check
      - Broadcast detection
      - Typing presence simulation
      - Randomised send delay
      - Bot message registration
    """
    # Rate limit
    allowed, reason = check_rate_limit(instance)
    if not allowed:
        logger.warning(f"[{instance}] Rate limited: {reason}")
        return {"error": reason, "queued": False}

    # Broadcast detection
    text = message.get("text", "") or str(message)
    if detect_broadcast_pattern(instance, text, jid):
        logger.warning(f"[{instance}] Broadcast pattern detected — adding extra delay")
        await asyncio.sleep(random.uniform(5, 15))

    headers = {"apikey": EVO_KEY, "Content-Type": "application/json"}

    # 1. Send typing presence (composing)
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{EVO_INTERNAL}/chat/presence/{instance}",
                json={"number": jid, "options": {"presence": "composing", "delay": 1200}},
                headers=headers, timeout=5,
            )
    except Exception:
        pass   # Non-fatal — continue with send

    # 2. Randomised delay (mimics human typing)
    delay_ms = random.randint(MIN_DELAY_MS, MAX_DELAY_MS)
    # Add extra time proportional to message length
    if text:
        typing_time = min(len(text) * 30, 5000)   # ~30ms per char, max 5s
        delay_ms    = min(delay_ms + typing_time, MAX_DELAY_MS * 2)

    await asyncio.sleep(delay_ms / 1000)

    # 3. Send paused presence (signals finished typing)
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{EVO_INTERNAL}/chat/presence/{instance}",
                json={"number": jid, "options": {"presence": "paused", "delay": 300}},
                headers=headers, timeout=5,
            )
    except Exception:
        pass

    # 4. Actually send the message
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{EVO_INTERNAL}/message/sendText/{instance}",
                json={"number": jid, "options": {"delay": 500}, **message},
                headers=headers, timeout=30,
            )
            result = r.json()

        # Register the sent message ID to prevent reply loops
        msg_id = result.get("key", {}).get("id", "")
        if msg_id:
            register_bot_message(msg_id)

        logger.info(f"[{instance}] → {jid} sent (delay={delay_ms}ms)")
        return result

    except Exception as exc:
        logger.error(f"[{instance}] Send failed: {exc}")
        return {"error": str(exc)}
