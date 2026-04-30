"""
server.py — Evolution API Proxy FastAPI application
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
# PUBLIC ROUTES
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    uptime = int(time.time() - START_TIME)
    instances = await _get_instances()
    connected = sum(1 for i in instances if i.get("instance", {}).get("state") == "open")
    return {
        "status": "ok",
        "uptime_seconds": uptime,
        "instances_total": len(instances),
        "instances_connected": connected,
        "warmup_mode": WARMUP_MODE,
    }


@app.get("/")
async def root():
    return RedirectResponse("/dashboard", status_code=302)


# ═════════════════════════════════════════════════════════════════════════════
# API PROXY
# ═════════════════════════════════════════════════════════════════════════════

@app.api_route("/api/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE"])
async def proxy_to_evolution(
    path: str,
    request: Request,
    _: str = Depends(verify_api_key),
):
    if path == "instance/create" or path.startswith("instance/create/"):
        await verify_admin_key(request)

    body = await request.body()
    params = dict(request.query_params)
    headers = {"apikey": EVO_KEY, "Content-Type": "application/json"}

    async with httpx.AsyncClient(base_url=EVO_BASE, timeout=60) as client:
        r = await client.request(
            request.method, f"/{path}",
            content=body, params=params, headers=headers,
        )

    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json"),
    )


@app.post("/api/message/safe/{instance}")
async def safe_send(
    instance: str,
    request: Request,
    _: str = Depends(verify_api_key),
):
    body = await request.json()
    result = await send_message_safe(
        instance=instance,
        jid=body.get("number", ""),
        message=body.get("message", {}),
    )
    return JSONResponse(result)


# ═════════════════════════════════════════════════════════════════════════════
# WEBHOOK RECEIVER
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/webhook/{instance}")
async def receive_webhook(instance: str, request: Request):
    raw  = await request.body()
    sig  = request.headers.get("x-hub-signature-256", "")
    wh_secret = os.environ.get("WEBHOOK_SECRET", "")

    if wh_secret and sig:
        expected = "sha256=" + hmac_lib.new(
            wh_secret.encode(), raw, hashlib.sha256
        ).hexdigest()
        if not hmac_lib.compare_digest(sig, expected):
            raise HTTPException(403, "Invalid signature")

    try:
        payload = json.loads(raw)
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    event   = payload.get("event", "")
    data    = payload.get("data", {})
    sender  = data.get("key", {}).get("remoteJid", "")
    is_bot  = is_bot_message(payload)
    is_self = is_self_message(payload)

    if is_bot or is_self:
        return {"status": "ignored"}

    webhooks = webhook_manager.get_instance_webhooks(instance)
    async with httpx.AsyncClient(timeout=10) as client:
        for wh in webhooks:
            if not wh.get("enabled", True):
                continue
            allowed = wh.get("events", [])
            if allowed and event not in allowed:
                continue
            try:
                await client.post(wh["url"], json=payload, headers={
                    "Content-Type": "application/json",
                    "X-Instance": instance,
                    "X-Event": event,
                })
            except Exception as e:
                logger.warning(f"Webhook delivery failed [{wh['url']}]: {e}")

    return {"status": "received"}


# ═════════════════════════════════════════════════════════════════════════════
# AUTH
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})

@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    admin_user = os.environ.get("DASHBOARD_USERNAME", "admin")
    admin_pass = os.environ.get("DASHBOARD_PASSWORD", "")
    if username == admin_user and password == admin_pass:
        token = create_session_token(username)
        resp  = RedirectResponse("/dashboard", status_code=302)
        resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=86400*7)
        return resp
    return RedirectResponse("/login?error=Invalid+credentials", status_code=302)

@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("session")
    return resp


# ═════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(verify_session)):
    instances = await _get_instances()
    server_url = os.environ.get("SERVER_URL", str(request.base_url).rstrip("/"))
    uptime = int(time.time() - START_TIME)
    return templates.TemplateResponse("dashboard.html", {
        "request":    request,
        "user":       user,
        "instances":  instances,
        "server_url": server_url,
        "uptime":     uptime,
        "all_events": ALL_EVENTS,
        "warmup":     WARMUP_MODE,
    })


# ─── Instance CRUD ────────────────────────────────────────────────────────────

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
    """Pairing code connect — enter phone number, get 8-digit code."""
    body   = await request.json()
    number = body.get("number", "").strip().replace("+", "").replace(" ", "")
    if not number:
        raise HTTPException(400, "Phone number is required")

    await _evo("GET", f"/instance/connect/{instance}")
    result = await _evo("POST", f"/instance/connect/{instance}", json={}, params={"number": number})
    return JSONResponse(result)


@app.get("/dashboard/instance/{instance}/qrcode")
async def dashboard_qrcode(instance: str, user: str = Depends(verify_session)):
    """Get QR code for this instance. Returns base64 image."""
    result = await _evo("GET", f"/instance/connect/{instance}")
    return JSONResponse(result)


@app.post("/dashboard/instance/{instance}/import-session")
async def dashboard_import_session(
    instance: str,
    request:  Request,
    user:     str = Depends(verify_session),
):
    """Import a raw Baileys session credentials JSON to instantly restore a connection."""
    body = await request.json()
    creds = body.get("credentials", "")
    if not creds:
        raise HTTPException(400, "credentials JSON is required")

    try:
        creds_obj = json.loads(creds) if isinstance(creds, str) else creds
    except Exception:
        raise HTTPException(400, "credentials must be valid JSON")

    # Attempt to set session via Evolution API
    result = await _evo("POST", f"/instance/setPresence/{instance}", json=creds_obj)
    return JSONResponse({"status": "imported", "result": result})


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


# ─── Webhook management ────────────────────────────────────────────────────────

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


# ─── Send message ──────────────────────────────────────────────────────────────

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
