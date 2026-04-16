"""Session cookie auth helpers — HMAC-signed, stateless, no server-side storage.

Cookie value format: {timestamp_int}.{hmac_hex[:32]}
HMAC key: the configured api_key (same secret, different channel).

Properties:
- HttpOnly  — JS cannot read it
- SameSite=Lax — CSRF protection for browser flows
- Secure    — set only when request arrived over HTTPS
- Expiry    — configurable via NANIO_ORCHESTRATOR_SESSION_TTL (default 8h)
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

from fastapi import Request, Response

COOKIE_NAME = "nanio_session"
_HEX_LEN = 32  # first 32 hex chars of HMAC-SHA256 = 128 bits


def _sign(api_key: str, timestamp: int) -> str:
    """Return the HMAC-SHA256 signature (truncated to _HEX_LEN hex chars)."""
    msg = f"{timestamp}".encode()
    key = api_key.encode()
    digest = hmac.new(key, msg, hashlib.sha256).hexdigest()
    return digest[:_HEX_LEN]


def make_session_token(api_key: str) -> str:
    """Generate a signed session token for the current time."""
    ts = int(time.time())
    sig = _sign(api_key, ts)
    return f"{ts}.{sig}"


def verify_session_token(token: str, api_key: str, ttl: int) -> bool:
    """Return True if the token is valid and not expired."""
    try:
        ts_str, sig = token.split(".", 1)
        ts = int(ts_str)
    except (ValueError, AttributeError):
        return False

    # Timing-safe comparison
    expected = _sign(api_key, ts)
    if not hmac.compare_digest(sig, expected):
        return False

    # Check TTL
    now = int(time.time())
    if now - ts > ttl:
        return False

    return True


def get_session_token(request: Request) -> Optional[str]:
    """Extract the session token from the request cookie, or None."""
    return request.cookies.get(COOKIE_NAME)


def is_authenticated(request: Request, api_key: str, ttl: int) -> bool:
    """Check if the request has a valid session cookie."""
    token = get_session_token(request)
    if not token:
        return False
    return verify_session_token(token, api_key, ttl)


def set_session_cookie(response: Response, api_key: str, ttl: int, secure: bool) -> None:
    """Attach a fresh signed session cookie to the response."""
    token = make_session_token(api_key)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=ttl,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Expire the session cookie."""
    response.delete_cookie(key=COOKIE_NAME, path="/")


def is_https(request: Request) -> bool:
    """Detect HTTPS — handles direct TLS and proxied requests."""
    if request.url.scheme == "https":
        return True
    if request.headers.get("X-Forwarded-Proto", "").lower() == "https":
        return True
    return False
