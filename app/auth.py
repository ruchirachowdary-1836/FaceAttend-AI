"""Tiny stateless admin auth: HMAC-signed bearer tokens (no external dependency)."""
import base64
import hashlib
import hmac
import time

from fastapi import Header, HTTPException

from .config import ADMIN_PASSWORD, SECRET_KEY, TOKEN_TTL_HOURS


def required():
    return bool(ADMIN_PASSWORD)


def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    exp = str(int(time.time()) + TOKEN_TTL_HOURS * 3600)
    return base64.urlsafe_b64encode(f"{exp}.{_sign(exp)}".encode()).decode()


def valid(token: str | None) -> bool:
    if not required():
        return True
    try:
        exp, sig = base64.urlsafe_b64decode((token or "").encode()).decode().split(".")
        return hmac.compare_digest(sig, _sign(exp)) and int(exp) > time.time()
    except Exception:
        return False


def check_password(pw: str) -> bool:
    return hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode())


def admin(authorization: str | None = Header(default=None)):
    """FastAPI dependency guarding every state-changing route."""
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    if not valid(token):
        raise HTTPException(401, "Admin login required")
