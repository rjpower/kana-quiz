"""Cookie-based password gate.

If ``KANA_AUTH_PASSWORD`` is set, all ``/api/*`` requests (except the
``/api/auth/*`` endpoints themselves) require a signed ``kana_auth``
cookie. The cookie is HMAC-signed with ``KANA_AUTH_SECRET`` (or the
password itself if no secret is configured) so we can verify it without
storing session state server-side.

If the env var is unset the gate is disabled — handy for local dev where
adding a password every reload is friction.

The SPA shell (``/index.html``, ``/static/*``, manifest, icons, sw.js) is
NOT gated. Hiding the shell would also hide the manifest from PWA
installers, and the shell on its own reveals nothing — all the data is
behind ``/api``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

COOKIE_NAME = "kana_auth"
COOKIE_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days


def _password() -> str | None:
    return os.environ.get("KANA_AUTH_PASSWORD")


def _secret_key() -> bytes:
    raw = os.environ.get("KANA_AUTH_SECRET") or _password() or ""
    return hashlib.sha256(raw.encode()).digest()


def _sign(payload: str) -> str:
    sig = hmac.new(_secret_key(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


def _verify(token: str) -> bool:
    try:
        payload, sig_b64 = token.rsplit(".", 1)
    except ValueError:
        return False
    expected = hmac.new(_secret_key(), payload.encode(), hashlib.sha256).digest()
    try:
        actual = base64.urlsafe_b64decode(sig_b64 + "==")
    except Exception:
        return False
    if not hmac.compare_digest(expected, actual):
        return False
    try:
        issued_at = int(payload)
    except ValueError:
        return False
    return (time.time() - issued_at) < COOKIE_TTL_SECONDS


def auth_required() -> bool:
    return _password() is not None


def is_authed(request: Request) -> bool:
    if not auth_required():
        return True
    token = request.cookies.get(COOKIE_NAME)
    return bool(token and _verify(token))


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    password: str


def _set_cookie(response: Response, token: str) -> None:
    # secure=True means the cookie is only sent over HTTPS — fine in prod
    # behind Caddy. For local http dev the cookie still gets set if the
    # browser treats localhost as secure (Chrome/Safari do).
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


@router.post("/login")
def login(body: LoginIn, response: Response):
    pw = _password()
    if pw is None:
        return {"ok": True, "auth_required": False}
    if not hmac.compare_digest(body.password, pw):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong password")
    _set_cookie(response, _sign(str(int(time.time()))))
    return {"ok": True, "auth_required": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/status")
def status_(request: Request):
    return {
        "authed": is_authed(request),
        "auth_required": auth_required(),
    }
