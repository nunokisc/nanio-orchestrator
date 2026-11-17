"""Session endpoints for the web panel.

The panel is a single-page application: its shell and assets are public (they hold no
data) and it signs in here. A session is the same signed cookie the API has always
accepted next to the ``X-Orchestrator-Key`` header.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from nanio_orchestrator import __version__
from nanio_orchestrator.auth import clear_session_cookie, is_authorized, is_https, key_matches, set_session_cookie
from nanio_orchestrator.config import get_settings
from nanio_orchestrator.models import LoginRequest, SessionOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _session(authenticated: bool) -> SessionOut:
    return SessionOut(authenticated=authenticated, dev_mode=get_settings().dev, version=__version__)


@router.post("/login", response_model=SessionOut)
async def login(body: LoginRequest, request: Request, response: Response):
    """Exchange the API key for a session cookie. 401 on a wrong key."""
    s = get_settings()
    if not key_matches(body.key, s.api_key):
        raise HTTPException(401, "Invalid API key")
    set_session_cookie(response, s.api_key, s.session_ttl, secure=is_https(request))
    return _session(True)


@router.post("/logout", response_model=SessionOut)
async def logout(response: Response):
    """Expire the session cookie."""
    clear_session_cookie(response)
    return _session(False)


@router.get("/session", response_model=SessionOut)
async def session(request: Request):
    """Whether this browser is signed in. Never 401: the panel asks this before anything else."""
    s = get_settings()
    return _session(is_authorized(request, s.api_key, s.session_ttl))
