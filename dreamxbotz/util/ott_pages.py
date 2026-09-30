"""Server-side rendering of the JioHotstar OTT pages (Option A).

Two templates are served by :mod:`dreamxbotz.server.ott_api`:

``dreamxbotz/template/ott_home.html``    → ``/home`` (and ``/``)
``dreamxbotz/template/ott_search.html``  → ``/search``

Both are *server rendered first*.  The hero, the first rails and the filter
chips are already in the HTML that leaves the bot, so the storefront paints
instantly (and still means something with JavaScript disabled); the JSON
bootstrap block at the end of the document lets ``/static/ott_home.js``
hydrate, add the trailer, My List, Continue Watching and live refresh on top.

The renderer is defensive by design: if the database is unreachable the page
still renders with a friendly empty state instead of a 500.
"""
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "template"
HOME_TEMPLATE = "ott_home.html"
SEARCH_TEMPLATE = "ott_search.html"

#: Compiled templates are cached; the mtime is part of the key so editing the
#: file while the bot runs (dev) still shows up on the next request.
_TEMPLATE_CACHE: Dict[str, Any] = {}


def _cfg(name: str, default):
    try:
        import info  # type: ignore

        return getattr(info, name, default)
    except Exception:
        return default


def ott_enabled() -> bool:
    """Master switch for the storefront (``OTT_HOME``, default ``True``)."""
    value = _cfg("OTT_HOME", True)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return bool(value)


def asset_version() -> str:
    """Cache-busting token shared with the other Stream Mode assets."""
    try:
        from dreamxbotz.server.static_assets import version_token  # type: ignore
        from dreamxbotz.zzint import __version__  # type: ignore

        return f"{__version__}-{version_token()}"
    except Exception:
        return "1"


def _load_template(name: str):
    """Read + compile one template (never raises for a missing file: empty)."""
    from jinja2 import Template

    path = TEMPLATE_DIR / name
    try:
        stamp = path.stat().st_mtime
    except OSError:
        logger.warning("OTT template %s is missing.", name)
        return None
    cached = _TEMPLATE_CACHE.get(name)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        template = Template(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("OTT template %s could not be compiled: %s", name, exc)
        return None
    _TEMPLATE_CACHE[name] = (stamp, template)
    return template


def _json_for_script(payload: Any) -> str:
    """JSON safe to embed inside ``<script type="application/json">``."""
    text = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"), default=str)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _open_url(site: Dict[str, Any]) -> str:
    """The public Telegram link of the bot (or the updates channel as fallback)."""
    username = str(site.get("bot_username") or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}?start=ott"
    return str(_cfg("UPDATE_CHNL_LNK", "") or "https://telegram.org")


def _updates_url() -> str:
    return str(_cfg("UPDATE_CHNL_LNK", "") or "https://t.me")


def config_payload(site: Dict[str, Any]) -> Dict[str, Any]:
    """The public runtime config the browser reads (never a secret).

    ``keys`` are the ``localStorage`` keys the watch page's Netflix Pack
    already writes, so My List / Continue Watching are *shared* between the
    player page and the storefront instead of being two separate lists.
    """
    return {
        "bot": str(site.get("bot_username") or ""),
        "open_url": _open_url(site),
        "home_path": site.get("home_path"),
        "search_path": site.get("search_path"),
        "home_api": site.get("home_api"),
        "search_api": site.get("search_api"),
        "suggest_api": site.get("suggest_api"),
        "movie_api": site.get("movie_api"),
        "genres_api": site.get("genres_api"),
        "poll": site.get("poll"),
        "trailer_volume": site.get("trailer_volume"),
        "limits": {
            "rail": site.get("rail_limit"),
            "hero": site.get("hero_limit"),
            "search": site.get("search_limit"),
            "suggest": site.get("suggest_limit"),
        },
        "keys": {"continue": "dx:cw:list", "mylist": "dx:mylist", "resume": "dx:resume:"},
    }


def _nav_links(context: Dict[str, Any]) -> List[Dict[str, str]]:
    return [
        {"label": "Home", "href": context.get("home_path") or "/home", "icon": "🏠", "key": "home"},
        {"label": "Movies", "href": f"{context.get('search_path') or '/search'}?sort=newest", "icon": "🎬", "key": "movies"},
        {"label": "Web Series", "href": f"{context.get('search_path') or '/search'}?q=season", "icon": "📺", "key": "series"},
        {"label": "My List", "href": f"{context.get('home_path') or '/home'}?view=mylist", "icon": "❤️", "key": "mylist"},
    ]


async def render_home(context: Optional[Dict[str, Any]] = None) -> str:
    """Render the storefront page (hero + rails + bootstrap payload)."""
    from dreamxbotz.util import ott_catalog

    site = ott_catalog.site_context()
    site.update(context or {})
    started = time.perf_counter()
    payload: Dict[str, Any] = {"ok": False, "hero": [], "rails": [], "genres": [], "counts": {}}
    error = ""
    try:
        payload = await ott_catalog.cached_home_payload(
            rail_limit=site["rail_limit"],
            hero_limit=site["hero_limit"],
            max_rails=int(_cfg("OTT_RAILS_MAX", 12) or 12),
        )
    except Exception as exc:  # pragma: no cover - the page must still render
        logger.warning("OTT home payload failed: %s", exc)
        error = "The library is taking a moment to answer — tap refresh in a second."

    template = _load_template(HOME_TEMPLATE)
    if template is None:
        return _fallback_page("MinatoVerse", error or "The storefront template is missing.")

    # First paint = hero + the first few rails (fast, meaningful without JS);
    # the remaining rails arrive with the bootstrap JSON and are mounted by
    # ott_home.js, which keeps the HTML of a big library small.
    all_rails = payload.get("rails") or []
    visible = all_rails[: max(1, int(_cfg("OTT_SERVER_RAILS", ott_catalog.SERVER_RENDERED_RAILS) or 4))]
    html = template.render(
        **site,
        site=site,
        nav=_nav_links(site),
        hero=payload.get("hero") or [],
        hero_count=len(payload.get("hero") or []),
        rails=visible,
        rail_count=len(all_rails),
        genres=payload.get("genres") or [],
        counts=payload.get("counts") or {},
        boot=_json_for_script(ott_catalog.light_payload(payload)),
        config=_json_for_script(config_payload(site)),
        open_url=_open_url(site),
        update_channel_url=_updates_url(),
        error=error,
        ok=bool(payload.get("ok")),
        updated_at=payload.get("updated_at") or "",
        asset_version=asset_version(),
        site_year=time.gmtime().tm_year,
    )
    logger.debug("OTT home rendered in %.1f ms", (time.perf_counter() - started) * 1000)
    return html


async def render_search(context: Optional[Dict[str, Any]] = None) -> str:
    """Render the /search page shell (chips + filters + first results)."""
    from dreamxbotz.util import ott_catalog

    # NOTE: the active filters are *not* merged into `site`: they are passed as
    # their own template variables (`query`, `genre`, …), and merging them would
    # raise "multiple values for keyword argument" in `template.render(**site)`.
    site = ott_catalog.site_context()
    filters = dict(context or {})
    query = str(filters.get("query") or "")[:120]
    genre = str(filters.get("genre") or "")[:40]
    quality = str(filters.get("quality") or "")[:12]
    sort = str(filters.get("sort") or "relevance")[:16]
    year = str(filters.get("year") or "")[:4]

    payload: Dict[str, Any] = {
        "ok": False,
        "results": [],
        "total": 0,
        "facets": {"genres": [], "qualities": [], "years": []},
    }
    error = ""
    try:
        if query or genre or quality or year:
            payload = await ott_catalog.cached_search(
                query,
                genre=genre,
                quality=quality,
                year=year,
                sort=sort,
                limit=site["search_limit"],
                offset=0,
            )
        else:
            catalog, _upcoming = await ott_catalog.build_home_catalog()
            browse = catalog.search("", sort=sort or "newest", limit=site["search_limit"], offset=0)
            browse["ok"] = True
            browse["catalog_size"] = len(catalog)
            payload = browse
    except Exception as exc:  # pragma: no cover - the page must still render
        logger.warning("OTT search payload failed: %s", exc)
        error = "Search is warming up — try again in a second."

    template = _load_template(SEARCH_TEMPLATE)
    if template is None:
        return _fallback_page("Search · MinatoVerse", error or "The search template is missing.")

    html = template.render(
        **site,
        site=site,
        nav=_nav_links(site),
        results=payload.get("results") or [],
        total=payload.get("total") or 0,
        facets=payload.get("facets") or {"genres": [], "qualities": [], "years": []},
        query=query,
        genre=genre,
        quality=quality,
        year=year,
        sort=sort,
        boot=_json_for_script(
            {
                "ok": payload.get("ok"),
                "query": query,
                "genre": genre,
                "quality": quality,
                "year": year,
                "sort": sort,
                "total": payload.get("total") or 0,
                "catalog_size": payload.get("catalog_size") or 0,
                "results": payload.get("results") or [],
                "facets": payload.get("facets") or {},
            }
        ),
        config=_json_for_script(config_payload(site)),
        open_url=_open_url(site),
        update_channel_url=_updates_url(),
        error=error,
        ok=bool(payload.get("ok")),
        asset_version=asset_version(),
        site_year=time.gmtime().tm_year,
    )
    return html


def _fallback_page(title: str, message: str) -> str:
    """Last-resort page when a template cannot be read (never a blank screen)."""
    from markupsafe import escape

    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex, nofollow\">"
        f"<title>{escape(title)}</title></head>"
        "<body style=\"margin:0;background:#0b0b16;color:#eef0f7;font:16px system-ui;"
        "display:grid;place-items:center;min-height:100vh;text-align:center\">"
        f"<div><h1 style=\"font-size:20px\">{escape(title)}</h1>"
        f"<p style=\"color:#9aa1b9\">{escape(message)}</p></div></body></html>"
    )
