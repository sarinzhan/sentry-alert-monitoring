"""Session tokens for the web UI — stdlib only (no extra dependencies).

A token is `username:expiry:hmac_sha256(secret, "username:expiry")`, base64-url
encoded and stored in an HttpOnly cookie. The signing secret is generated once
and persisted in the DB (UsersRepo.secret), so sessions survive restarts.
"""
import base64
import hashlib
import hmac
import time

from app.config import AUTH_SESSION_HOURS


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(username: str, secret: str) -> str:
    exp = int(time.time() + AUTH_SESSION_HOURS * 3600)
    payload = f"{username}:{exp}"
    raw = f"{payload}:{_sign(secret, payload)}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def parse_token(token: str, secret: str):
    """Return the username for a valid, unexpired token, else None."""
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        username, exp, sig = raw.rsplit(":", 2)   # username may contain ':'
    except Exception:
        return None
    if not hmac.compare_digest(sig, _sign(secret, f"{username}:{exp}")):
        return None
    if time.time() > int(exp):
        return None
    return username
