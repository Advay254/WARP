"""
server.py — WARP WhatsApp Gateway · FastAPI application
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
    HTTPException, Form, status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from auth import (
    verify_api_key, verify_admin_key, verify_session,
    create_session_token, _LoginRedirect, register_handlers,
)
from webhook_manager import webhook_manager, ALL_EVENTS
from safety import send_message_safe, is_bot_message, is_self_message, WARMUP_MODE
from status_watcher import get_config as sw_get_config, save_config as sw_save_config, get_activity_log as sw_get_log, start_watcher

logger   = logging.getLogger(__name__)
EVO_BASE = f"http://localhost:{os.environ.get('EVOLUTION_PORT', '8080')}"
EVO_KEY  = os.environ.get("EVOLUTION_API_KEY", "")
START_TS = time.time()

app = FastAPI(title="WARP", docs_url=None, redoc_url=None)

BASE_DIR  = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
register_handlers(app)
start_watcher(app, _get_instances)


# ── Internal Evolution API helper ─────────────────────────────────────────────

async def _evo(method: str, path: str, **kwargs) -> dict:
    headers = kwargs.pop("headers", {})
    headers["apikey"] = EVO_KEY
    try:
        async with httpx.AsyncClient(base_url=EVO_BASE, timeout=30) as client:
            r = await client.request(method, path, headers=headers, **kwargs)
            if r.content:
                return r.json()
            return {}
    except Exception as exc:
        logger.error(f"Evolution API call failed [{method} {path}]: {exc}")
        return {"error": str(exc)}


async def _get_instances() -> list:
    data = await _evo("GET", "/instance/fetchInstances")
    return data if isinstance(data, list) else []


# ── Public ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    instances  = await _get_instances()
    connected  = sum(1 for i in instances if i.get("instance", {}).get("state") == "open")
    return {
        "status":               "ok",
        "uptime_seconds":       int(time.time() - START_TS),
        "instances_total":      len(instances),
        "instances_connected":  connected,
        "warmup_mode":          WARMUP_MODE,
    }


@app.get("/")
async def root():
    return RedirectResponse("/dashboard", status_code=302)


# ── API proxy ─────────────────────────────────────────────────────────────────

@app.api_route("/api/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE"])
async def proxy(path: str, request: Request, _: None = Depends(verify_api_key)):
    if "instance/create" in path:
        await verify_admin_key(request)
    body    = await request.body()
    params  = dict(request.query_params)
    headers = {"apikey": EVO_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(base_url=EVO_BASE, timeout=60) as client:
        r = await client.request(request.method, f"/{path}",
                                 content=body, params=params, headers=headers)
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))


@app.post("/api/message/safe/{instance}")
async def safe_send(instance: str, request: Request, _: None = Depends(verify_api_key)):
    body = await request.json()
    return JSONResponse(await send_message_safe(
        instance=instance,
        jid=body.get("number", ""),
        message=body.get("message", {}),
    ))


# ── Webhook receiver ──────────────────────────────────────────────────────────

@app.post("/webhook/{instance}")
async def receive_webhook(instance: str, request: Request):
    raw = await request.body()
    sig = request.headers.get("x-hub-signature-256", "")
    secret = os.environ.get("WEBHOOK_SECRET", "")
    if secret and sig:
        expected = "sha256=" + hmac_lib.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac_lib.compare_digest(sig, expected):
            raise HTTPException(403, "Invalid signature")
    try:
        payload = json.loads(raw)
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    if is_bot_message(payload) or is_self_message(payload):
        return {"status": "ignored"}

    event = payload.get("event", "")
    await webhook_manager.route_event(instance, event, payload)
    return {"status": "received"}


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
async def login_submit(request: Request,
                       username: str = Form(...),
                       password: str = Form(...)):
    ok_user = os.environ.get("DASHBOARD_USERNAME", "admin")
    ok_pass = os.environ.get("DASHBOARD_PASSWORD", "")
    if username == ok_user and password == ok_pass:
        token = create_session_token(username)
        resp  = RedirectResponse("/dashboard", status_code=302)
        resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=86400 * 7)
        return resp
    return RedirectResponse("/login?error=Invalid+credentials", status_code=302)


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("session")
    return resp


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(verify_session)):
    instances  = await _get_instances()
    server_url = os.environ.get("SERVER_URL", str(request.base_url).rstrip("/"))
    # Serialize to JSON strings once — avoids any Jinja2/JS conflict
    return templates.TemplateResponse("dashboard.html", {
        "request":          request,
        "user":             user,
        "instances_json":   json.dumps(instances),
        "all_events_json":  json.dumps(ALL_EVENTS),
        "server_url":       server_url,
        "uptime":           int(time.time() - START_TS),
        "warmup":           WARMUP_MODE,
        "connected_count":  sum(1 for i in instances if i.get("instance", {}).get("state") == "open"),
        "total_count":      len(instances),
    })


# ── Dashboard instance endpoints ──────────────────────────────────────────────

@app.post("/dashboard/instance/create")
async def dash_create(request: Request, _: str = Depends(verify_session)):
    body   = await request.json()
    name   = body.get("instanceName", "").strip().replace(" ", "_")
    number = body.get("number", "").strip().replace("+", "").replace(" ", "")
    if not name:
        raise HTTPException(400, "instanceName required")
    result = await _evo("POST", "/instance/create", json={
        "instanceName": name,
        "integration":  "WHATSAPP-BAILEYS",
        "qrcode":       False,
        "number":       number,
    })
    return JSONResponse(result)


@app.get("/dashboard/instance/{instance}/qrcode")
async def dash_qr(instance: str, _: str = Depends(verify_session)):
    result = await _evo("GET", f"/instance/connect/{instance}")
    return JSONResponse(result)


@app.post("/dashboard/instance/{instance}/connect")
async def dash_connect(instance: str, request: Request, _: str = Depends(verify_session)):
    body   = await request.json()
    number = body.get("number", "").strip().replace("+", "").replace(" ", "")
    if not number:
        raise HTTPException(400, "number required")
    # Connect then request pairing code
    await _evo("GET", f"/instance/connect/{instance}")
    result = await _evo("POST", f"/instance/connect/{instance}",
                        json={}, params={"number": number})
    return JSONResponse(result)


@app.post("/dashboard/instance/{instance}/import-session")
async def dash_import(instance: str, request: Request, _: str = Depends(verify_session)):
    body = await request.json()
    raw  = body.get("credentials", "")
    if not raw:
        raise HTTPException(400, "credentials required")
    try:
        creds = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        raise HTTPException(400, "credentials must be valid JSON")
    result = await _evo("POST", f"/instance/setPresence/{instance}", json=creds)
    return JSONResponse({"status": "imported", "result": result})


@app.post("/dashboard/instance/{instance}/disconnect")
async def dash_disconnect(instance: str, _: str = Depends(verify_session)):
    return JSONResponse(await _evo("DELETE", f"/instance/logout/{instance}"))


@app.post("/dashboard/instance/{instance}/restart")
async def dash_restart(instance: str, _: str = Depends(verify_session)):
    return JSONResponse(await _evo("PUT", f"/instance/restart/{instance}"))


@app.delete("/dashboard/instance/{instance}")
async def dash_delete(instance: str, _: str = Depends(verify_session)):
    result = await _evo("DELETE", f"/instance/delete/{instance}")
    webhook_manager.delete_instance_config(instance)
    return JSONResponse(result)


@app.get("/dashboard/instance/{instance}/status")
async def dash_status(instance: str, _: str = Depends(verify_session)):
    return JSONResponse(await _evo("GET", f"/instance/connectionState/{instance}"))


# ── Webhook management endpoints ──────────────────────────────────────────────

@app.get("/dashboard/webhooks/{instance}")
async def dash_wh_list(instance: str, _: str = Depends(verify_session)):
    return JSONResponse({"webhooks": webhook_manager.get_instance_webhooks(instance)})


@app.post("/dashboard/webhooks/{instance}")
async def dash_wh_add(instance: str, request: Request, _: str = Depends(verify_session)):
    body = await request.json()
    url  = body.get("url", "").strip()
    if not url:
        raise HTTPException(400, "url required")
    wh = webhook_manager.add_webhook(
        instance=instance, url=url,
        label=body.get("label", ""),
        events=body.get("events", []),
        enabled=body.get("enabled", True),
    )
    # Register webhook URL in Evolution API as well
    server_url = os.environ.get("SERVER_URL", "")
    if server_url:
        await _evo("POST", f"/webhook/set/{instance}", json={
            "url":     f"{server_url}/webhook/{instance}",
            "enabled": True,
            "events":  ALL_EVENTS,
        })
    return JSONResponse(wh)


@app.patch("/dashboard/webhooks/{instance}/{wh_id}")
async def dash_wh_update(instance: str, wh_id: str,
                          request: Request, _: str = Depends(verify_session)):
    body   = await request.json()
    result = webhook_manager.update_webhook(instance, wh_id, **body)
    if not result:
        raise HTTPException(404, "Webhook not found")
    return JSONResponse(result)


@app.delete("/dashboard/webhooks/{instance}/{wh_id}")
async def dash_wh_delete(instance: str, wh_id: str, _: str = Depends(verify_session)):
    if not webhook_manager.delete_webhook(instance, wh_id):
        raise HTTPException(404, "Webhook not found")
    return JSONResponse({"deleted": True})


# ── Send message ──────────────────────────────────────────────────────────────

@app.post("/dashboard/send")
async def dash_send(request: Request, _: str = Depends(verify_session)):
    body     = await request.json()
    instance = body.get("instance", "")
    number   = body.get("number", "").replace("+", "").replace(" ", "")
    text     = body.get("text", "")
    if not all([instance, number, text]):
        raise HTTPException(400, "instance, number and text required")
    return JSONResponse(await send_message_safe(instance=instance, jid=number,
                                                 message={"text": text}))


# ── Status Automation endpoints ───────────────────────────────────────────────

@app.get("/dashboard/status/{instance}/config")
async def dash_status_get_cfg(instance: str, _: str = Depends(verify_session)):
    return JSONResponse(sw_get_config(instance))


@app.post("/dashboard/status/{instance}/config")
async def dash_status_save_cfg(instance: str, request: Request, _: str = Depends(verify_session)):
    body = await request.json()
    return JSONResponse(sw_save_config(instance, body))


@app.get("/dashboard/status/activity")
async def dash_status_activity(_: str = Depends(verify_session)):
    return JSONResponse({"log": sw_get_log()})
