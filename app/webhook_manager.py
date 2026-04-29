"""
webhook_manager.py — Per-instance webhook configuration, HMAC validation, event routing.

Each instance can have multiple webhooks. Each webhook can be configured to:
  - Listen to specific event types (or all)
  - Enable/disable independently
  - Forward to any URL (n8n, custom endpoint, etc.)

Webhook configs are stored in /app/data/webhooks.json and survive container restarts
as long as the HF Space isn't fully rebuilt. For persistence across rebuilds, back up
this file to your Supabase instance.
"""

import os
import json
import hmac
import hashlib
import logging
import asyncio
import time
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DATA_FILE  = Path("/app/data/webhooks.json")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# ── All Evolution API event types ─────────────────────────────────────────────
ALL_EVENTS = [
    "APPLICATION_STARTUP",
    "QRCODE_UPDATED",
    "MESSAGES_SET",
    "MESSAGES_UPSERT",
    "MESSAGES_UPDATE",
    "MESSAGES_DELETE",
    "SEND_MESSAGE",
    "CONTACTS_SET",
    "CONTACTS_UPSERT",
    "CONTACTS_UPDATE",
    "PRESENCE_UPDATE",
    "CHATS_SET",
    "CHATS_UPSERT",
    "CHATS_UPDATE",
    "CHATS_DELETE",
    "GROUPS_UPSERT",
    "GROUP_UPDATE",
    "GROUP_PARTICIPANTS_UPDATE",
    "CONNECTION_UPDATE",
    "CALL",
    "TYPEBOT_START",
    "TYPEBOT_CHANGE_FLOW",
]

# ── Webhook config shape ──────────────────────────────────────────────────────
# {
#   "instance_name": {
#     "webhooks": [
#       {
#         "id": "uuid",
#         "url": "https://...",
#         "label": "n8n main",
#         "enabled": true,
#         "events": ["MESSAGES_UPSERT", "CONNECTION_UPDATE"],  # [] = all events
#         "created_at": 1234567890,
#       }
#     ]
#   }
# }


class WebhookManager:

    def __init__(self):
        self._config: dict = {}
        self._load()

    def _load(self):
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        if DATA_FILE.exists():
            try:
                self._config = json.loads(DATA_FILE.read_text())
                logger.info(f"Loaded webhook config ({len(self._config)} instances)")
            except Exception as exc:
                logger.error(f"Failed to load webhook config: {exc}")
                self._config = {}
        else:
            self._config = {}

    def _save(self):
        try:
            DATA_FILE.write_text(json.dumps(self._config, indent=2))
        except Exception as exc:
            logger.error(f"Failed to save webhook config: {exc}")

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def get_instance_webhooks(self, instance: str) -> list:
        return self._config.get(instance, {}).get("webhooks", [])

    def get_all_config(self) -> dict:
        return self._config

    def add_webhook(
        self,
        instance: str,
        url:      str,
        label:    str   = "",
        events:   list  = None,
        enabled:  bool  = True,
    ) -> dict:
        import uuid
        webhook = {
            "id":         str(uuid.uuid4())[:8],
            "url":        url,
            "label":      label or url,
            "enabled":    enabled,
            "events":     events or [],   # empty = all events
            "created_at": int(time.time()),
        }
        inst = self._config.setdefault(instance, {"webhooks": []})
        inst["webhooks"].append(webhook)
        self._save()
        return webhook

    def update_webhook(self, instance: str, webhook_id: str, **kwargs) -> Optional[dict]:
        webhooks = self.get_instance_webhooks(instance)
        for wh in webhooks:
            if wh["id"] == webhook_id:
                wh.update({k: v for k, v in kwargs.items() if k in ("url","label","enabled","events")})
                self._save()
                return wh
        return None

    def delete_webhook(self, instance: str, webhook_id: str) -> bool:
        webhooks = self.get_instance_webhooks(instance)
        before   = len(webhooks)
        self._config[instance]["webhooks"] = [w for w in webhooks if w["id"] != webhook_id]
        self._save()
        return len(self._config[instance]["webhooks"]) < before

    def delete_instance_config(self, instance: str):
        self._config.pop(instance, None)
        self._save()

    # ── Signature validation ──────────────────────────────────────────────────

    def validate_signature(self, payload: bytes, signature: str) -> bool:
        """Validate X-Evolution-Signature header (HMAC-SHA256)."""
        if not WEBHOOK_SECRET:
            return True   # No secret configured → accept all (warn in startup)
        expected = hmac.new(
            WEBHOOK_SECRET.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()
        provided = signature.replace("sha256=", "").strip()
        return hmac.compare_digest(expected, provided)

    # ── Event routing ─────────────────────────────────────────────────────────

    async def route_event(self, instance: str, event_type: str, payload: dict):
        """
        Route an incoming Evolution API event to all matching webhooks for this instance.
        Runs webhook deliveries concurrently.
        """
        webhooks = self.get_instance_webhooks(instance)
        if not webhooks:
            return

        tasks = []
        for wh in webhooks:
            if not wh.get("enabled", True):
                continue
            # Check event filter — empty list means "all events"
            wh_events = wh.get("events", [])
            if wh_events and event_type not in wh_events:
                continue
            tasks.append(self._deliver(wh, instance, event_type, payload))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _deliver(self, webhook: dict, instance: str, event_type: str, payload: dict):
        """Deliver a single event to a webhook URL with retries."""
        url = webhook["url"]
        body = {
            "instance":   instance,
            "event":      event_type,
            "data":       payload,
            "webhook_id": webhook["id"],
        }

        # Sign the outgoing payload
        body_bytes = json.dumps(body).encode()
        sig = hmac.new(
            WEBHOOK_SECRET.encode() if WEBHOOK_SECRET else b"unsigned",
            body_bytes,
            hashlib.sha256,
        ).hexdigest()

        headers = {
            "Content-Type":          "application/json",
            "X-Evolution-Signature": f"sha256={sig}",
            "X-Evolution-Event":     event_type,
            "X-Evolution-Instance":  instance,
        }

        for attempt in range(3):
            try:
                async with httpx.AsyncClient() as client:
                    r = await client.post(url, content=body_bytes, headers=headers, timeout=10)
                    if r.status_code < 300:
                        logger.debug(f"[{instance}] Webhook {webhook['id']} → {url} ✓ ({r.status_code})")
                        return
                    logger.warning(f"[{instance}] Webhook {webhook['id']} → {url} got {r.status_code}")
            except Exception as exc:
                logger.warning(f"[{instance}] Webhook delivery attempt {attempt+1} failed: {exc}")
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)   # exponential backoff: 0s, 2s, 4s

        logger.error(f"[{instance}] Webhook {webhook['id']} → {url} failed after 3 attempts")


# Singleton
webhook_manager = WebhookManager()
