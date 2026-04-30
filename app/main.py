"""
main.py — Evolution API Proxy · HuggingFace Space entrypoint
"""

import os
import secrets
import logging
import asyncio
import uvicorn
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)

# ── Auto-generate secrets ─────────────────────────────────────────────────────
for key, label in [
    ("JWT_SECRET",     "JWT_SECRET"),
    ("WEBHOOK_SECRET", "WEBHOOK_SECRET"),
    ("SESSION_SECRET", "SESSION_SECRET"),
]:
    if not os.environ.get(key):
        os.environ[key] = secrets.token_hex(32)
        logger.warning(f"{label} not set — auto-generated (resets on restart). Set in Space secrets.")

if not os.environ.get("DASHBOARD_PASSWORD"):
    pwd = secrets.token_urlsafe(16)
    os.environ["DASHBOARD_PASSWORD"] = pwd
    print(f"\n{'='*62}")
    print(f"  ⚠  DASHBOARD_PASSWORD not set.")
    print(f"  Auto-generated password: {pwd}")
    print(f"{'='*62}\n")

if not os.environ.get("EVOLUTION_API_KEY"):
    logger.error("EVOLUTION_API_KEY is not set — Evolution API will reject all requests.")

if not os.environ.get("ADMIN_KEY"):
    logger.warning("ADMIN_KEY not set — instance create/delete will require only EVOLUTION_API_KEY.")

# ── Wait for Evolution API to be ready ───────────────────────────────────────
async def wait_for_evolution():
    import httpx
    evo_url = f"http://localhost:{os.environ.get('EVOLUTION_PORT', '8080')}"
    logger.info("Waiting for Evolution API to start …")
    for attempt in range(90):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{evo_url}/", timeout=3)
                if r.status_code < 500:
                    logger.info("Evolution API is ready ✓")
                    return
        except Exception:
            pass
        await asyncio.sleep(2)
    logger.warning("Evolution API did not respond in time — proxy will retry on each request.")

from server import app   # noqa: E402

@app.on_event("startup")
async def startup():
    asyncio.create_task(wait_for_evolution())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
