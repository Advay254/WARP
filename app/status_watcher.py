"""
status_watcher.py — Automatic WhatsApp status viewer and liker.

For each enabled instance:
- Polls Evolution API for new statuses every POLL_INTERVAL minutes
- Queues each unseen status with a random delay (min_delay – max_delay minutes)
- At scheduled time (within active hours), marks the status as seen
- With like_probability chance, sends a ❤️ reaction to the status
- Tracks seen statuses in Redis (with in-memory fallback)
"""

import os
import json
import time
import random
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

EVO_BASE = f"http://localhost:{os.environ.get('EVOLUTION_PORT', '8080')}"
EVO_KEY  = os.environ.get("EVOLUTION_API_KEY", "")

POLL_INTERVAL = 10  # minutes between status fetches per instance

# In-memory fallback when Redis unavailable
_mem_seen: dict  = {}   # instance -> set of seen status IDs
_mem_queue: dict = {}   # instance -> list of {id, jid, key, due, like}
_configs: dict   = {}   # instance -> config dict


# ── Redis helpers ──────────────────────────────────────────────────────────────

def _redis():
    try:
        import redis as _r
        uri = os.environ.get("CACHE_REDIS_URI", "")
        if not uri:
            return None
        r = _r.from_url(uri, decode_responses=True, socket_connect_timeout=2)
        r.ping()
        return r
    except Exception:
        return None


def _mark_seen(instance: str, status_id: str):
    r = _redis()
    if r:
        r.sadd(f"warp:seen:{instance}", status_id)
        r.expire(f"warp:seen:{instance}", 86400 * 7)
    else:
        _mem_seen.setdefault(instance, set()).add(status_id)


def _is_seen(instance: str, status_id: str) -> bool:
    r = _redis()
    if r:
        return bool(r.sismember(f"warp:seen:{instance}", status_id))
    return status_id in _mem_seen.get(instance, set())


def _save_queue(instance: str, queue: list):
    r = _redis()
    if r:
        r.set(f"warp:queue:{instance}", json.dumps(queue), ex=86400)
    else:
        _mem_queue[instance] = queue


def _load_queue(instance: str) -> list:
    r = _redis()
    if r:
        raw = r.get(f"warp:queue:{instance}")
        return json.loads(raw) if raw else []
    return _mem_queue.get(instance, [])


# ── Config ─────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "enabled":            False,
    "min_delay_minutes":  15,
    "max_delay_minutes":  240,
    "like_probability":   0.30,
    "active_hours_start": 7,
    "active_hours_end":   23,
}


def get_config(instance: str) -> dict:
    r = _redis()
    if r:
        raw = r.get(f"warp:status_cfg:{instance}")
        if raw:
            return {**DEFAULT_CONFIG, **json.loads(raw)}
    return {**DEFAULT_CONFIG, **_configs.get(instance, {})}


def save_config(instance: str, cfg: dict):
    merged = {**DEFAULT_CONFIG, **cfg}
    r = _redis()
    if r:
        r.set(f"warp:status_cfg:{instance}", json.dumps(merged))
    _configs[instance] = merged
    return merged


# ── Activity log ───────────────────────────────────────────────────────────────

_activity: list = []  # last 100 entries


def _log_activity(instance: str, action: str, detail: str):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "instance": instance, "action": action, "detail": detail}
    _activity.append(entry)
    if len(_activity) > 100:
        _activity.pop(0)
    logger.info(f"[StatusWatcher][{instance}] {action}: {detail}")


def get_activity_log() -> list:
    return list(reversed(_activity))


# ── Evolution API helpers ──────────────────────────────────────────────────────

async def _evo(method: str, path: str, **kwargs) -> dict:
    headers = kwargs.pop("headers", {})
    headers["apikey"] = EVO_KEY
    try:
        async with httpx.AsyncClient(base_url=EVO_BASE, timeout=20) as client:
            r = await client.request(method, path, headers=headers, **kwargs)
            return r.json() if r.content else {}
    except Exception as exc:
        logger.debug(f"[EVO] {method} {path} failed: {exc}")
        return {}


async def _fetch_statuses(instance: str) -> list:
    """Returns list of status message objects."""
    res = await _evo("POST", f"/chat/findStatusMessage/{instance}", json={})
    if isinstance(res, list):
        return res
    if isinstance(res, dict) and "messages" in res:
        return res["messages"]
    return []


async def _view_status(instance: str, key: dict):
    """Mark a status as read/viewed."""
    await _evo("POST", f"/chat/readMessages/{instance}", json={
        "readMessages": [key]
    })


async def _like_status(instance: str, key: dict):
    """Send ❤️ reaction to a status."""
    await _evo("POST", f"/message/sendReaction/{instance}", json={
        "key":      key,
        "reaction": "❤️",
    })


# ── Active hours check ─────────────────────────────────────────────────────────

def _in_active_hours(cfg: dict) -> bool:
    hour = datetime.now().hour
    return cfg["active_hours_start"] <= hour < cfg["active_hours_end"]


# ── Core watcher loop ──────────────────────────────────────────────────────────

async def _process_instance(instance: str):
    cfg = get_config(instance)
    if not cfg.get("enabled"):
        return

    # Fetch current statuses
    statuses = await _fetch_statuses(instance)
    if not statuses:
        return

    queue = _load_queue(instance)
    queued_ids = {item["id"] for item in queue}
    now = time.time()
    added = 0

    for status in statuses:
        # Normalise key extraction across different response shapes
        key = status.get("key") or {}
        sid = key.get("id") or status.get("id")
        jid = key.get("remoteJid") or status.get("remoteJid") or "status@broadcast"

        if not sid:
            continue
        if _is_seen(instance, sid):
            continue
        if sid in queued_ids:
            continue

        # Schedule at a random time within the configured delay range
        delay_sec = random.uniform(
            cfg["min_delay_minutes"] * 60,
            cfg["max_delay_minutes"] * 60,
        )
        due = now + delay_sec
        do_like = random.random() < cfg["like_probability"]

        queue.append({
            "id":   sid,
            "jid":  jid,
            "key":  key,
            "due":  due,
            "like": do_like,
        })
        queued_ids.add(sid)
        added += 1

    if added:
        _log_activity(instance, "queued", f"{added} new status(es) scheduled")
        _save_queue(instance, queue)

    # Process due items (only during active hours)
    if not _in_active_hours(cfg):
        return

    remaining = []
    viewed = 0
    liked = 0

    for item in queue:
        if time.time() < item["due"]:
            remaining.append(item)
            continue

        # View the status
        await _view_status(instance, item["key"])
        _mark_seen(instance, item["id"])
        viewed += 1

        # Small human-like pause between actions
        await asyncio.sleep(random.uniform(1.5, 4.5))

        # Maybe like it
        if item.get("like"):
            await _like_status(instance, item["key"])
            liked += 1
            await asyncio.sleep(random.uniform(1.0, 3.0))

    if viewed:
        detail = f"viewed {viewed}"
        if liked:
            detail += f", liked {liked}"
        _log_activity(instance, "viewed", detail)

    _save_queue(instance, remaining)


async def _watcher_loop(get_instances_fn):
    """Main loop — runs forever, polls each enabled instance."""
    logger.info("[StatusWatcher] Started.")
    while True:
        try:
            instances = await get_instances_fn()
            for inst in instances:
                name = inst.get("instance", {}).get("instanceName")
                state = inst.get("instance", {}).get("state")
                if name and state == "open":
                    await _process_instance(name)
                    # Small gap between instances to avoid hammering Evo API
                    await asyncio.sleep(2)
        except Exception as exc:
            logger.error(f"[StatusWatcher] Loop error: {exc}")

        await asyncio.sleep(POLL_INTERVAL * 60)


def start_watcher(app, get_instances_fn):
    """Call this from FastAPI startup to launch the background task."""
    import asyncio

    @app.on_event("startup")
    async def _start():
        asyncio.create_task(_watcher_loop(get_instances_fn))
        logger.info("[StatusWatcher] Background task scheduled.")
