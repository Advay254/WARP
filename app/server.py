"""
server.py — Evolution API Proxy FastAPI application

Proxy routes  (Bearer protected):
  ALL  /api/*                     → Evolution API internal (authenticated proxy)
  POST /api/instance/create       → guarded by ADMIN_KEY too
  POST /api/message/safe/{inst}   → safe send with rate limiting + anti-ban

Webhook receiver:
  POST /webhook/{instance}        → receives Evolution events, routes to configured webhooks

Dashboard (session-cookie protected):
  GET  /dashboard                 → main overview
  GET  /dashboard/instance/{n}    → per-instance detail
  POST /dashboard/instance/create → create new instance (phone number)
  POST /dashboard/instance/{n}/connect  → request pairing code (phone number method)
  POST /dashboard/instance/{n}/disconnect
  DELETE /dashboard/instance/{n}
  GET  /dashboard/webhooks/{n}    → webhook config for instance
  POST /dashboard/webhooks/{n}    → add webhook
  PATCH /dashboard/webhooks/{n}/{id}   → update webhook
  DELETE /dashboard/webhooks/{n}/{id} → delete webhook
  POST /dashboard/message/send    → send test message

Auth:
  GET/POST /login                 → dashboard login
  GET  /logout                    → clear session
  GET  /health                    → public health check
"""

import os
import json
import time
import logging
import hmac as hmac_lib
import hashlib
from typing import Optional

import httpx
from fastapi import (
    FastAPI, Request, Response, Depends,
    HTTPException, Form, status, Body,
)
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from auth import (
    verify_api_key, verify_admin_key, verify_session,
    create_session_token, _LoginRedirect, register_handlers,
)
from webhook_manager import webhook_manager, ALL_EVENTS
from safety import (
    send_message_safe, check_rate_limit, is_bot_message,
    is_self_message, reset_turns, WARMUP_MODE,
)

logger    = logging.getLogger(__name__)
EVO_BASE  = f"http://localhost:{os.environ.get('EVOLUTION_PORT', '8080')}"
EVO_KEY   = os.environ.get("EVOLUTION_API_KEY", "")
START_TIME = time.time()

app = FastAPI(
    title="Evolution API Gateway",
    description="Multi-instance WhatsApp gateway with anti-ban safety layer",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
)

BASE_DIR  = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
register_handlers(app)


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

async def _evo(method: str, path: str, **kwargs) -> dict:
    """Internal call to Evolution API."""
    headers = kwargs.pop("headers", {})
    headers["apikey"] = EVO_KEY
    try:
        async with httpx.AsyncClient(base_url=EVO_BASE, timeout=30) as client:
            r = await client.request(method, path, headers=headers, **kwargs)
            return r.json() if r.content else {}
    except Exception as exc:
        logger.error(f"Evolution API call failed [{method} {path}]: {exc}")
        return {"error": str(exc)}


async def _get_instances() -> list:
    data = await _evo("GET", "/instance/fetchInstances")
    if isinstance(data, list):
        return data
    return []


# ═════════════════════════════════════════════════════════════════════════════
# PUBLIC
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    instances = await _get_instances()
    connected = sum(1 for i in instances if i.get("instance", {}).get("state") == "open")
    return {
        "status":         "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "instances":      len(instances),
        "connected":      connected,
        "warmup_mode":    WARMUP_MODE,
    }

@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard", status_code=302)


# ═════════════════════════════════════════════════════════════════════════════
# AUTHENTICATED API PROXY
# ═════════════════════════════════════════════════════════════════════════════

@app.api_route("/api/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE"])
async def proxy_to_evolution(
    path:    str,
    request: Request,
    _:       None = Depends(verify_api_key),
):
    """
    Transparent proxy to Evolution API internal.
    Destructive paths also require x-admin-key.
    """
    destructive = any(x in path for x in ["create", "delete", "logout"])
    if destructive:
        await verify_admin_key(request)

    body    = await request.body()
    headers = {"apikey": EVO_KEY, "Content-Type": "application/json"}

    async with httpx.AsyncClient(base_url=EVO_BASE, timeout=30) as client:
        r = await client.request(
            request.method,
            f"/{path}",
            content=body,
            headers=headers,
            params=dict(request.query_params),
        )

    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json"),
    )


# ── Safe message send (with anti-ban measures) ────────────────────────────────

class SafeSendRequest(BaseModel):
    number:  str
    text:    str
    options: dict = {}

@app.post("/api/message/safe/{instance}")
async def safe_send(
    instance: str,
    body:     SafeSendRequest,
    _:        None = Depends(verify_api_key),
):
    """Rate-limited, delay-jittered, presence-simulated message send."""
    result = await send_message_safe(
        instance=instance,
        jid=body.number,
        message={"text": body.text, "options": body.options},
    )
    if "error" in result:
        raise HTTPException(status_code=429, detail=result["error"])
    return result


# ═════════════════════════════════════════════════════════════════════════════
# WEBHOOK RECEIVER
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/webhook/{instance}")
async def receive_webhook(instance: str, request: Request):
    """
    Receives all Evolution API events for an instance.
    Validates HMAC signature, applies safety checks, routes to configured webhooks.
    """
    payload_bytes = await request.body()
    signature     = request.headers.get("X-Evolution-Signature", "")

    # Validate signature
    if not webhook_manager.validate_signature(payload_bytes, signature):
        logger.warning(f"[{instance}] Webhook signature validation failed")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload    = json.loads(payload_bytes)
        event_type = payload.get("event", "UNKNOWN")
        data       = payload.get("data", {})
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # ── Safety checks on inbound messages ────────────────────────────────────
    if event_type == "MESSAGES_UPSERT":
        key      = data.get("key", {})
        msg_id   = key.get("id", "")
        from_me  = key.get("fromMe", False)

        # Drop bot's own messages (reply loop prevention)
        if from_me or is_bot_message(msg_id):
            return JSONResponse({"status": "ignored", "reason": "bot_message"})

        # Drop self-messages
        sender_jid = key.get("remoteJid", "")
        instances  = await _get_instances()
        own_number = next(
            (i.get("instance", {}).get("owner", "") for i in instances
             if i.get("instance", {}).get("instanceName") == instance),
            ""
        )
        if own_number and is_self_message(sender_jid, own_number):
            return JSONResponse({"status": "ignored", "reason": "self_message"})

    # ── Route to configured webhooks ──────────────────────────────────────────
    await webhook_manager.route_event(instance, event_type, payload)

    return JSONResponse({"status": "received", "event": event_type})


# ═════════════════════════════════════════════════════════════════════════════
# AUTH
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == os.environ.get("DASHBOARD_USERNAME", "admin") and \
       password == os.environ.get("DASHBOARD_PASSWORD", ""):
        token    = create_session_token(username)
        response = RedirectResponse(url="/dashboard", status_code=302)
        response.set_cookie("session", token, httponly=True, samesite="lax", max_age=60*60*8)
        return response
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Invalid credentials."},
        status_code=401,
    )


@app.get("/logout")
async def logout():
    r = RedirectResponse(url="/login", status_code=302)
    r.delete_cookie("session")
    return r


# ═════════════════════════════════════════════════════════════════════════════
# DASHBOARD API  (session-cookie auth)
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(verify_session)):
    instances      = await _get_instances()
    webhook_config = webhook_manager.get_all_config()
    server_url     = os.environ.get("SERVER_URL", request.base_url._url.rstrip("/"))

    return templates.TemplateResponse("dashboard.html", {
        "request":       request,
        "user":          user,
        "instances":     instances,
        "webhook_config": webhook_config,
        "all_events":    ALL_EVENTS,
        "server_url":    server_url,
        "api_key_set":   bool(EVO_KEY),
        "admin_key_set": bool(os.environ.get("ADMIN_KEY")),
        "warmup_mode":   WARMUP_MODE,
        "uptime":        int(time.time() - START_TIME),
    })


# ── Instance management ───────────────────────────────────────────────────────

@app.post("/dashboard/instance/create")
async def dashboard_create_instance(
    request: Request,
    user:    str = Depends(verify_session),
):
    body = await request.json()
    name = body.get("instanceName", "").strip().replace(" ", "_")
    if not name:
        raise HTTPException(400, "instanceName is required")

    result = await _evo("POST", "/instance/create", json={
        "instanceName":   name,
        "integration":    "WHATSAPP-BAILEYS",
        "qrcode":         False,
        "number":         body.get("number", "").strip(),
    })
    return JSONResponse(result)


@app.post("/dashboard/instance/{instance}/connect")
async def dashboard_connect_instance(
    instance: str,
    request:  Request,
    user:     str = Depends(verify_session),
):
    """Request a pairing code for phone number connection (no QR scan needed)."""
    body   = await request.json()
    number = body.get("number", "").strip().replace("+", "").replace(" ", "")
    if not number:
        raise HTTPException(400, "Phone number is required")

    # First connect the instance, then request pairing code
    await _evo("GET", f"/instance/connect/{instance}")
    result = await _evo("POST", f"/instance/connect/{instance}", json={}, params={"number": number})
    return JSONResponse(result)


@app.post("/dashboard/instance/{instance}/disconnect")
async def dashboard_disconnect(instance: str, user: str = Depends(verify_session)):
    result = await _evo("DELETE", f"/instance/logout/{instance}")
    return JSONResponse(result)


@app.post("/dashboard/instance/{instance}/restart")
async def dashboard_restart(instance: str, user: str = Depends(verify_session)):
    result = await _evo("PUT", f"/instance/restart/{instance}")
    return JSONResponse(result)


@app.delete("/dashboard/instance/{instance}")
async def dashboard_delete_instance(instance: str, user: str = Depends(verify_session)):
    result = await _evo("DELETE", f"/instance/delete/{instance}")
    webhook_manager.delete_instance_config(instance)
    return JSONResponse(result)


@app.get("/dashboard/instance/{instance}/status")
async def dashboard_instance_status(instance: str, user: str = Depends(verify_session)):
    result = await _evo("GET", f"/instance/connectionState/{instance}")
    return JSONResponse(result)


@app.get("/dashboard/instance/{instance}/messages")
async def dashboard_messages(
    instance: str,
    user:     str = Depends(verify_session),
    count:    int = 20,
):
    result = await _evo("GET", f"/chat/findMessages/{instance}", params={"count": count})
    return JSONResponse(result)


# ── Webhook management ────────────────────────────────────────────────────────

@app.get("/dashboard/webhooks/{instance}")
async def get_webhooks(instance: str, user: str = Depends(verify_session)):
    return JSONResponse({"webhooks": webhook_manager.get_instance_webhooks(instance)})


@app.post("/dashboard/webhooks/{instance}")
async def add_webhook(instance: str, request: Request, user: str = Depends(verify_session)):
    body   = await request.json()
    url    = body.get("url", "").strip()
    if not url:
        raise HTTPException(400, "url is required")
    wh = webhook_manager.add_webhook(
        instance=instance,
        url=url,
        label=body.get("label", ""),
        events=body.get("events", []),
        enabled=body.get("enabled", True),
    )
    # Register the webhook URL with Evolution API for this instance
    server_url = os.environ.get("SERVER_URL", "")
    if server_url:
        await _evo("POST", f"/webhook/set/{instance}", json={
            "url":     f"{server_url}/webhook/{instance}",
            "enabled": True,
            "events":  ALL_EVENTS,
        })
    return JSONResponse(wh)


@app.patch("/dashboard/webhooks/{instance}/{webhook_id}")
async def update_webhook(
    instance: str, webhook_id: str,
    request: Request, user: str = Depends(verify_session),
):
    body   = await request.json()
    result = webhook_manager.update_webhook(instance, webhook_id, **body)
    if not result:
        raise HTTPException(404, "Webhook not found")
    return JSONResponse(result)


@app.delete("/dashboard/webhooks/{instance}/{webhook_id}")
async def delete_webhook(instance: str, webhook_id: str, user: str = Depends(verify_session)):
    ok = webhook_manager.delete_webhook(instance, webhook_id)
    if not ok:
        raise HTTPException(404, "Webhook not found")
    return JSONResponse({"deleted": True})


# ── Send test message ─────────────────────────────────────────────────────────

@app.post("/dashboard/message/send")
async def dashboard_send_message(request: Request, user: str = Depends(verify_session)):
    body     = await request.json()
    instance = body.get("instance", "")
    number   = body.get("number", "")
    text     = body.get("text", "")
    if not all([instance, number, text]):
        raise HTTPException(400, "instance, number, and text are required")

    result = await send_message_safe(
        instance=instance,
        jid=number,
        message={"text": text},
    )
    return JSONResponse(result)
