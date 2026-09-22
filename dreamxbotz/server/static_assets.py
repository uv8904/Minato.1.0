"""Static assets for the Stream Mode web pages.

Only the files whitelisted in :data:`ASSETS` are served – the directory is
never exposed wholesale – and they are sent with a one-year immutable cache
because the templates append a ``?v=<mtime>`` token (:func:`version_token`).
"""
import logging
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

#: filename -> Content-Type (anything not listed here is a 404)
ASSETS = {
    "newly_uploaded.css": "text/css; charset=utf-8",
    "newly_uploaded.js": "text/javascript; charset=utf-8",
}

routes = web.RouteTableDef()


@routes.get("/static/{name}", allow_head=True)
async def static_asset(request: web.Request) -> web.StreamResponse:
    """Serve one whitelisted asset (traversal-proof, long-cached)."""
    name = request.match_info.get("name", "")
    content_type = ASSETS.get(name)
    if not content_type:
        raise web.HTTPNotFound(text="Not found")
    path = STATIC_DIR / name
    if not path.is_file():
        raise web.HTTPNotFound(text="Not found")
    return web.FileResponse(
        path,
        headers={
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


def asset_version(name: str) -> str:
    """Cache-busting token (last modification time) for one asset."""
    try:
        return str(int((STATIC_DIR / name).stat().st_mtime))
    except Exception:
        return "1"


def version_token() -> str:
    """One token that changes whenever any section asset changes."""
    return "-".join(asset_version(name) for name in sorted(ASSETS))
