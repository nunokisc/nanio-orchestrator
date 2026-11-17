"""Serves the web panel: a single-page application built from ``ui/`` into ``web/dist``.

The build output is shipped inside the wheel, so an installed package needs no Node. A
source checkout has no ``dist`` until ``make ui`` has run; the panel then says so instead
of failing.

Everything served here is public: the shell and its assets contain no data. The data is
behind ``/api/*``, which answers 401 until the panel has signed in.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from starlette.convertors import PathConvertor, register_url_convertor

router = APIRouter(include_in_schema=False)

UI_DIR = Path(__file__).parent / "dist"

_NOT_BUILT = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>nanio-orchestrator</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 4rem auto; padding: 0 1rem">
<h1>The web panel is not built</h1>
<p>This is a source checkout without the panel's build output. Build it once with:</p>
<pre>make ui</pre>
<p>(needs Node.js; the result goes to <code>nanio_orchestrator/web/dist</code>). Packages
installed from PyPI and the Docker image already contain it.</p>
<p>The API is running: see <a href="/api/docs">/api/docs</a>.</p>
</body>
</html>
"""


class _PanelPath(PathConvertor):
    """Any path that is not the API's. Leaving /api/* unmatched keeps its own answers: 404 for
    an unknown endpoint and 405 for a wrong method, in JSON, never the HTML shell."""

    regex = r"(?!api(?:/|$)).*"


register_url_convertor("panel", _PanelPath())


@router.get("/{path:panel}")
async def panel(path: str):
    """A file of the build if there is one at this path, the application shell otherwise
    (the application does its own routing)."""
    root = UI_DIR.resolve()
    index = root / "index.html"
    if not index.is_file():
        return HTMLResponse(_NOT_BUILT, status_code=503)

    if path:
        candidate = (root / path).resolve()
        if root in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        if path.startswith("assets/"):
            # a missing script must not be answered with the HTML shell
            raise HTTPException(404, "Not found")
    # The shell names the hashed assets of this build: always revalidate it.
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
