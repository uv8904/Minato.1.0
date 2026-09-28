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
    # "Coming Soon" rail (upcoming releases + live countdown)
    "coming_soon.css": "text/css; charset=utf-8",
    "coming_soon.js": "text/javascript; charset=utf-8",
    # watch-page movie hero (strip above the player)
    "watch_hero.css": "text/css; charset=utf-8",
    "watch_hero.js": "text/javascript; charset=utf-8",
    # Netflix Pack · Enhanced Player + Continue Watching + My List + Trailers
    "netflix_pack.css": "text/css; charset=utf-8",
    "netflix_pack.js": "text/javascript; charset=utf-8",
}

#: Poster files for the wall / trending / AI-recommendation rails, served from
#: ``/static/posters/<name>``.  Bundled with the app so every known title gets
#: its real poster even when third-party lookups (iTunes JSONP) are slow or
#: blocked – the page never sits on a blank gradient again.
POSTERS = {
    "3-idiots.jpg": "image/jpeg",
    "dune-part-two.jpg": "image/jpeg",
    "inception.jpg": "image/jpeg",
    "interstellar.jpg": "image/jpeg",
    "jailer.jpg": "image/jpeg",
    "jawan.jpg": "image/jpeg",
    "kalki-2898-ad.jpg": "image/jpeg",
    "leo.jpg": "image/jpeg",
    "marco.jpg": "image/jpeg",
    "oppenheimer.jpg": "image/jpeg",
    "pathaan.jpg": "image/jpeg",
    "pushpa-2.jpg": "image/jpeg",
    "rrr.jpg": "image/jpeg",
    "sholay.jpg": "image/jpeg",
    "stree-2.jpg": "image/jpeg",
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


@routes.get("/static/posters/{name}", allow_head=True)
async def static_poster(request: web.Request) -> web.StreamResponse:
    """Serve one whitelisted poster (traversal-proof, long-cached)."""
    name = request.match_info.get("name", "")
    content_type = POSTERS.get(name)
    if not content_type:
        raise web.HTTPNotFound(text="Not found")
    path = STATIC_DIR / "posters" / name
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
    """One token that changes whenever any section asset or poster changes."""
    names = sorted(list(ASSETS) + ["posters/" + name for name in POSTERS])
    return "-".join(asset_version(name) for name in names)
