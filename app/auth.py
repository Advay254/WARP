"""
auth.py — Bearer token + session cookie auth
"""

import os
import hmac
import logging
from typing import Optional

from fastapi import Request, HTTPException, status
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

logger = logging.getLogger(__name__)
SESSION_MAX_AGE = 60 * 60 * 8


def _serializer():
    return URLSafeTimedSerializer(
        os.environ.get("JWT_SECRET", "change-me"),
        salt="evolution-dashboard-session",
    )


def create_session_token(username: str) -> str:
    return _serializer().dumps({"user": username})


def decode_session_token(token: str) -> Optional[str]:
    try:
        return _serializer().loads(token, max_age=SESSION_MAX_AGE).get("user")
    except (BadSignature, SignatureExpired):
        return None


async def verify_api_key(request: Request) -> None:
    """Validates bearer token — used for /api/* proxy routes."""
    required = os.environ.get("EVOLUTION_API_KEY", "").strip()
    if not required:
        return
    header = request.headers.get("apikey", "") or request.headers.get("Authorization", "")
    # Support both Evolution's native apikey header and Bearer scheme
    provided = header.replace("Bearer ", "").strip()
    if not provided or not hmac.compare_digest(provided, required):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key. Use apikey header or Bearer token.",
        )


async def verify_admin_key(request: Request) -> None:
    """Extra layer for destructive operations (create/delete instance)."""
    admin_key = os.environ.get("ADMIN_KEY", "").strip()
    if not admin_key:
        await verify_api_key(request)
        return
    header = request.headers.get("x-admin-key", "").strip()
    if not header or not hmac.compare_digest(header, admin_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin key required for this operation. Use x-admin-key header.",
        )


class _LoginRedirect(Exception):
    pass


async def verify_session(request: Request) -> str:
    token = request.cookies.get("session", "")
    user  = decode_session_token(token) if token else None
    if not user:
        raise _LoginRedirect()
    return user


def register_handlers(app):
    from fastapi.responses import RedirectResponse

    @app.exception_handler(_LoginRedirect)
    async def _(request: Request, exc: _LoginRedirect):
        return RedirectResponse(url="/login", status_code=302)
