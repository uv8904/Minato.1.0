"""JioHotstar OTT homepage · pages + JSON APIs (Option A).

Routes
------
``GET  /`` and ``GET /home``            the storefront (``OTT_HOME_PATH``)
``GET  /search``                        search + filter page (``OTT_SEARCH_PATH``)
``GET  /api/ott/home``                  hero + rails (what the page refreshes)
``GET  /api/ott/search``                search/filter/paginate the catalog
``GET  /api/ott/suggest``               type-ahead suggestions
``GET  /api/ott/genres``                genre list with counts (nav + filters)
``GET  /api/ott/movie/<MOVIE_ID>``      one movie (detail sheet)
``GET  /health``                        the old JSON probe of ``/``

Design notes
------------
* **Same origin by default.**  The pages call relative paths, so the browser
  never needs ``API_URL``; a second origin is supported through
  ``NEW_UPLOADED_CORS_ORIGIN`` exactly like the other movie APIs.
* **Nothing private leaks.**  A card carries only a title, year, quality, our
  own poster proxy path and the Telegram deep link built from the *public*
  ``BOT_USERNAME``.  File ids, download URLs and ``TELEGRAM_BOT_TOKEN`` are
  never part of any response.
* **Every failure is graceful.**  A database outage renders an empty storefront
  with an error hint (HTTP 200 for the pages, ``ok: false`` for the JSON), so a
  visitor never sees a stack trace.
* **Caching.**  Rails are computed once per ``OTT_CACHE_TTL`` seconds and sent
  with ``Cache-Control: public, max-age=OTT_BROWSER_CACHE_TTL``; the browser
  additionally refreshes them live every ``OTT_POLL`` seconds.

Reference: docs/OTT_HOMEPAGE.md
"""
import json
import logging
from typing import Any, Dict, Optional

from aiohttp import web

from dreamxbotz.util import ott_catalog, ott_pages
from dreamxbotz.util.movie_titles import sanitize_movie_id

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _cfg(name: str, default):
    try:
        import info  # type: ignore

        return getattr(info, name, default)
    except Exception:
        return default


def _enabled() -> bool:
    return ott_pages.ott_enabled()


def _flag(name: str, default: bool = True) -> bool:
    """Boolean setting that tolerates the usual env spellings."""
    value = _cfg(name, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return bool(value)


def _path(name: str, default: str) -> str:
    value = str(_cfg(name, default) or default).strip()
    if not value.startswith("/"):
        value = "/" + value
    return value


def _int_param(request: web.Request, name: str, default: int, low: int, high: int) -> int:
    raw = request.rel_url.query.get(name)
    try:
        value = int(str(raw).strip()) if raw not in (None, "") else int(default)
    except (TypeError, ValueError):
        value = int(default)
    return max(low, min(value, high))


def _json(payload: Dict[str, Any], *, ttl: Optional[int] = None, status: int = 200, request=None):
    """JSON response with the project's standard CORS + caching headers."""
    response = web.json_response(payload, status=status, dumps=json.dumps)
    if ttl is None:
        ttl = int(_cfg("OTT_BROWSER_CACHE_TTL", 60) or 0)
    if ttl > 0:
        response.headers["Cache-Control"] = f"public, max-age={max(0, int(ttl))}"
    else:
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    _apply_cors(response.headers, request)
    try:
        response.enable_compression()
    except Exception:  # pragma: no cover
        pass
    return response


def _apply_cors(headers: Dict[str, str], request=None) -> None:
    """Mirror ``movie_api``'s CORS behaviour for a separately hosted frontend."""
    allowed = str(_cfg("NEW_UPLOADED_CORS_ORIGIN", "") or "").strip()
    if not allowed:
        return
    origin = ""
    try:
        origin = (request.headers.get("Origin") if request is not None else "") or ""
    except Exception:
        origin = ""
    allowed_list = [part.strip() for part in allowed.split(",") if part.strip()]
    if "*" in allowed_list:
        headers["Access-Control-Allow-Origin"] = "*"
    elif origin and origin in allowed_list:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
    if headers.get("Access-Control-Allow-Origin"):
        headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        headers["Access-Control-Allow-Headers"] = "Content-Type"


def _html(text: str, *, status: int = 200, ttl: int = 0) -> web.Response:
    response = web.Response(text=text, content_type="text/html", status=status)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = f"public, max-age={int(ttl)}" if ttl > 0 else "no-store"
    # The storefront is very repetitive (poster markup + JSON bootstrap), so
    # gzip roughly thirds it. aiohttp only compresses when the client asks for it.
    try:
        response.enable_compression()
    except Exception:  # pragma: no cover - zlib missing / already encoded
        pass
    return response


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
async def _home_page(request: web.Request) -> web.Response:
    context = {"view": str(request.rel_url.query.get("view") or "")[:24]}
    html = await ott_pages.render_home(context)
    # The storefront must reflect a fresh upload quickly; keep the HTML itself
    # short-lived and let the JSON API be the long-cached part.
    return _html(html, ttl=min(30, max(0, int(_cfg("OTT_BROWSER_CACHE_TTL", 60) or 0) // 2)))


async def _search_page(request: web.Request) -> web.Response:
    context = {
        "query": str(request.rel_url.query.get("q") or "")[:120],
        "genre": str(request.rel_url.query.get("genre") or "")[:40],
        "quality": str(request.rel_url.query.get("quality") or "")[:12],
        "year": str(request.rel_url.query.get("year") or "")[:4],
        "sort": str(request.rel_url.query.get("sort") or "relevance")[:16],
    }
    html = await ott_pages.render_search(context)
    return _html(html)


@routes.get("/health", allow_head=True)
async def health(request: web.Request) -> web.Response:
    """Tiny JSON probe (the previous behaviour of ``/``)."""
    return _json({"ok": True, "service": "dreamxbotz", "ott": _enabled()}, ttl=0, request=request)


# --------------------------------------------------------------------------- #
# JSON APIs
# --------------------------------------------------------------------------- #
async def home_api(request: web.Request) -> web.Response:
    """Hero rotation + every rail (the storefront's refresh endpoint)."""
    if not _enabled():
        return _json({"ok": False, "error": "disabled", "hero": [], "rails": []}, ttl=0, request=request)
    try:
        payload = await ott_catalog.cached_home_payload(
            rail_limit=_int_param(request, "rail", int(_cfg("OTT_RAIL_LIMIT", 18) or 18), 4, 40),
            max_rails=_int_param(request, "rails", int(_cfg("OTT_RAILS_MAX", 12) or 12), 1, 20),
            hero_limit=_int_param(request, "hero", int(_cfg("OTT_HERO_LIMIT", 6) or 6), 1, 12),
            light=str(request.rel_url.query.get("light") or "") in ("1", "true", "yes"),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("OTT home API failed: %s", exc)
        return _json({"ok": False, "error": "unavailable", "hero": [], "rails": []}, ttl=0, request=request)
    # Fill in missing genres/trailers in the background (bounded + cached).
    ott_catalog.kick_enrichment()
    payload["bot_username"] = _public_username(request)
    return _json(payload, request=request)


async def search_api(request: web.Request) -> web.Response:
    """Search / filter / paginate the whole catalog."""
    if not _enabled():
        return _json(
            {"ok": False, "error": "disabled", "results": [], "total": 0}, ttl=0, request=request
        )
    query = str(request.rel_url.query.get("q") or "").strip()[:120]
    limit = _int_param(
        request, "limit", int(_cfg("OTT_SEARCH_LIMIT", 24) or 24), 1,
        int(_cfg("OTT_SEARCH_MAX_LIMIT", 60) or 60),
    )
    page = _int_param(request, "page", 1, 1, 500)
    sort = str(request.rel_url.query.get("sort") or "relevance")[:16]
    try:
        payload = await ott_catalog.cached_search(
            query,
            genre=str(request.rel_url.query.get("genre") or "")[:40],
            year=str(request.rel_url.query.get("year") or "")[:4],
            quality=str(request.rel_url.query.get("quality") or "")[:12],
            sort=sort,
            limit=limit,
            offset=(page - 1) * limit,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("OTT search API failed: %s", exc)
        return _json(
            {"ok": False, "error": "unavailable", "results": [], "total": 0}, ttl=0, request=request
        )
    # Deep pages of a growing catalogue may shift between requests: only the
    # first page is worth caching in the browser.
    return _json(payload, ttl=0 if page > 1 else None, request=request)


async def suggest_api(request: web.Request) -> web.Response:
    """Type-ahead suggestions (debounced by the browser)."""
    if not _enabled():
        return _json({"ok": False, "items": []}, ttl=0, request=request)
    query = str(request.rel_url.query.get("q") or "").strip()[:64]
    limit = _int_param(request, "limit", int(_cfg("OTT_SUGGEST_LIMIT", 8) or 8), 1, 20)
    try:
        payload = await ott_catalog.cached_suggestions(query, limit)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("OTT suggest API failed: %s", exc)
        return _json({"ok": False, "items": []}, ttl=0, request=request)
    return _json(payload, ttl=30, request=request)


async def genres_api(request: web.Request) -> web.Response:
    """Genre names with counts, for the nav dropdown and the filter chips."""
    if not _enabled():
        return _json({"ok": False, "genres": []}, ttl=0, request=request)
    try:
        payload = await ott_catalog.cached_home_payload(light=True)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("OTT genres API failed: %s", exc)
        return _json({"ok": False, "genres": []}, ttl=0, request=request)
    return _json(
        {
            "ok": True,
            "genres": payload.get("genres") or [],
            "counts": payload.get("counts") or {},
            "updated_at": payload.get("updated_at"),
        },
        request=request,
    )


async def movie_api(request: web.Request) -> web.Response:
    """One movie as a detail-sheet card."""
    if not _enabled():
        return _json({"ok": False, "error": "disabled"}, ttl=0, request=request)
    movie_id = sanitize_movie_id(request.match_info.get("movie_id") or "")
    if not movie_id:
        return _json({"ok": False, "error": "invalid_id"}, ttl=0, status=400, request=request)
    try:
        card = await ott_catalog.cached_movie(movie_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("OTT movie API failed: %s", exc)
        return _json({"ok": False, "error": "unavailable"}, ttl=0, request=request)
    if not card:
        return _json({"ok": False, "error": "not_found", "id": movie_id}, ttl=10, status=404, request=request)
    card["bot_username"] = _public_username(request)
    return _json({"ok": True, "movie": card}, ttl=30, request=request)


def _public_username(request=None) -> str:
    """The public ``BOT_USERNAME`` (never the token)."""
    override = request.app.get("nu_bot_username") if request is not None else None
    if override:
        return str(override).strip().lstrip("@")
    from dreamxbotz.util.ott_catalog import _bot_username

    return _bot_username()


# --------------------------------------------------------------------------- #
# Registration (called by plugins/__init__.py::web_server before the catch-all)
# --------------------------------------------------------------------------- #
def register(app: web.Application) -> None:
    """Attach the storefront routes to ``app`` (no-op when disabled).

    The catch-all ``/{path:\S+}`` stream route in ``plugins/route.py`` would
    swallow everything below, so the storefront is registered *before* it —
    exactly like the other Stream Mode APIs.
    """
    if not _enabled():
        logger.info("OTT homepage disabled (OTT_HOME=False).")
        return
    home_page_path = _path("OTT_HOME_PATH", "/home")
    search_page_path = _path("OTT_SEARCH_PATH", "/search")
    home_json_path = _path("OTT_HOME_API_PATH", "/api/ott/home")
    search_json_path = _path("OTT_SEARCH_API_PATH", "/api/ott/search")
    genres_json_path = _path("OTT_GENRES_API_PATH", "/api/ott/genres")
    suggest_json_path = _path("OTT_SUGGEST_API_PATH", "/api/ott/suggest")
    movie_json_path = _path("OTT_MOVIE_API_PATH", "/api/ott/movie")

    # JSON APIs first – they must never fall through to the stream handler.
    # (``add_get`` allows HEAD automatically – registering it twice raises.)
    app.router.add_get(home_json_path, home_api)
    app.router.add_get(search_json_path, search_api)
    app.router.add_get(genres_json_path, genres_api)
    app.router.add_get(suggest_json_path, suggest_api)
    app.router.add_get(f"{movie_json_path}/{{movie_id}}", movie_api)
    app.router.add_get("/health", health)

    # Pages.
    app.router.add_get(home_page_path, _home_page)
    app.router.add_get(search_page_path, _search_page)
    if home_page_path != "/" and _flag("OTT_HOME_AT_ROOT", True):
        app.router.add_get("/", _home_page)
    logger.info(
        "OTT homepage ready at %s%s · search %s · api %s",
        home_page_path,
        " (and /)" if home_page_path != "/" and _flag("OTT_HOME_AT_ROOT", True) else "",
        search_page_path,
        home_json_path,
    )


# Route table for tests that build their own small app (mirrors `register`).
#: ``[(method, path, handler), …]`` – kept in sync with :func:`register`.
#: ``add_get`` allows HEAD as well, so no separate HEAD row is needed.
ROUTE_TABLE = (
    ("GET", "/api/ott/home", home_api),
    ("GET", "/api/ott/search", search_api),
    ("GET", "/api/ott/genres", genres_api),
    ("GET", "/api/ott/suggest", suggest_api),
    ("GET", "/api/ott/movie/{movie_id}", movie_api),
    ("GET", "/health", health),
)


def build_routes(app: web.Application, *, with_pages: bool = True) -> None:
    """Small helper used by the preview tool and the tests."""
    for _method, path, handler in ROUTE_TABLE:
        app.router.add_get(path, handler)
    if with_pages:
        app.router.add_get("/home", _home_page)
        app.router.add_get("/search", _search_page)


def _sanity_check() -> bool:
    """Import-time self test: are the config paths usable?"""
    for name, default in (
        ("OTT_HOME_API_PATH", "/api/ott/home"),
        ("OTT_SEARCH_API_PATH", "/api/ott/search"),
    ):
        if not str(_cfg(name, default) or "").startswith("/"):
            logger.warning("OTT %s must start with '/' – falling back to %s", name, default)
    return True


_sanity_check()
