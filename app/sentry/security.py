"""Webhook signature verification (Sentry Internal Integration Client Secret)."""
import hmac
import hashlib

from app.config import CLIENT_SECRET, log


def verify(body: bytes, sig) -> bool:
    if not CLIENT_SECRET:
        return True
    if not sig:
        log.warning("verify: no sentry-hook-signature header")
        return False
    expected = hmac.new(CLIENT_SECRET.encode(), body, hashlib.sha256).hexdigest()
    ok = hmac.compare_digest(expected, sig)
    if not ok:
        # Sentry sends a signature, not the secret. If these differ, either our
        # secret is wrong or the body doesn't match what Sentry signed.
        log.warning(
            "verify FAIL: received=%s expected=%s (secret_len=%d head=%s tail=%s, body_bytes=%d)",
            sig, expected, len(CLIENT_SECRET), CLIENT_SECRET[:4], CLIENT_SECRET[-4:], len(body),
        )
    return ok
