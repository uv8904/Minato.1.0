"""Read-only JSON API + poster proxy for the "Newly Uploaded Movies" section.

Endpoints
---------
``GET /api/movies/new?limit=20``
    Newest uploads first, already sanitized for the browser:

    .. code-block:: json

        {
          "ok": true,
          "count": 20,
          "limit": 20,
          "updated_at": "2026-09-22T09:12:04Z",
          "bot_username": "MyMovieBot",
          "movies": [
            {
              "id": "jawan-2023",
              "title": "Jawan",
              "year": 2023,
              "quality": "1080p",
              "quality_label": "1080p, 720p, 480p",
              "poster": "/api/movies/poster/jawan-2023?v=8f2c1d",
              "backdrop": "/api/movies/backdrop/jawan-2023?v=8f2c1d",
              "has_poster": true,
              "uploaded_at": "2026-09-20T10:11:12Z",
              "added": "2 days ago",
              "deeplink": "https://t.me/MyMovieBot?start=movie_jawan-2023"
            }
          ]
        }

``GET /api/movies/poster/<MOVIE_ID>?w=320``
    Poster bytes (JPEG, resized + re-encoded) served from our own origin, or an
    inline SVG placeholder when the movie has no poster / upstream fails.

``GET /api/movies/art/<MOVIE_ID>?q=<title>&y=<year>``
    Artwork URLs of one movie for the watch-page hero: the 2:3 poster path and
    the 16:9 backdrop path (both on our own origin, so the browser never talks
    to TMDB/IMDb directly):

    .. code-block:: json

        {
          "ok": true,
          "id": "marco-2024",
          "title": "Marco",
          "year": 2024,
          "poster": "/api/movies/poster/marco-2024?v=8f2c1d",
          "backdrop": "/api/movies/backdrop/marco-2024?v=8f2c1d",
          "has_poster": true,
          "has_backdrop": true,
          "source": "tmdb"
        }

``GET /api/movies/backdrop/<MOVIE_ID>?w=1280``
    Wide (16:9) artwork for the hero band – the TMDB backdrop when there is
    one, the poster otherwise, and a clean SVG placeholder as the last resort.

``GET /api/movies/upcoming?limit=12``
    The "Coming Soon" rail: releases that are **not out yet**, soonest first,
    each with its release day and a countdown the browser ticks locally:

    .. code-block:: json

        {
          "ok": true,
          "count": 12,
          "limit": 12,
          "updated_at": "2026-10-05T00:00:00Z",
          "bot_username": "MyMovieBot",
          "movies": [
            {
              "id": "avatar-3-2026",
              "title": "Avatar 3",
              "year": 2026,
              "release_date": "2026-12-18T00:00:00Z",
              "release_label": "18 Dec 2026",
              "days_left": 85,
              "seconds_left": 7344000,
              "countdown": "in 85 days",
              "state": "upcoming",
              "has_poster": true,
              "poster": "/api/movies/upcoming/poster/avatar-3-2026",
              "waiting": 37,
              "deeplink": "https://t.me/MyMovieBot?start=movie_avatar-3-2026"
            }
          ]
        }

    ``state`` is ``"released"`` (inside the grace window), ``"soon"`` (inside
    ``COMING_SOON_SOON_DAYS``) or ``"upcoming"`` – that is what paints the gold
    chip.  ``waiting`` is how many users asked to be notified, so a card can
    show "🔥 37 waiting".

``GET /api/movies/upcoming/poster/<MOVIE_ID>?w=320``
    Poster bytes of one upcoming release.  Separate from ``/api/movies/poster``
    on purpose: this one reads the ``upcoming_movies`` cache, that one the
    uploaded-movies rail, so a Coming Soon card can never look available.

Security notes
--------------
* The response never contains ``file_ids``, ``file_names``, download URLs or the
  Telegram ``TELEGRAM_BOT_TOKEN`` – users reach a movie only through the bot
  deep link built from the public ``BOT_USERNAME``.
* Every string is cleaned and length-capped (:func:`public_movie`), the id is
  validated against ``^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`` before it touches the
  database or a URL.
* Posters are only fetched from an allow-list of image hosts (TMDB / IMDb /
  Amazon …), over HTTPS, size limited, and re-encoded locally.
"""
import asyncio
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from datetime import timedelta
from io import BytesIO
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

from aiohttp import ClientSession, ClientTimeout, web

from dreamxbotz.util.movie_titles import (
    MAX_TITLE_LENGTH,
    as_utc,
    build_deeplink,
    clean_title_text,
    primary_quality,
    quality_label,
    relative_time_label,
    sanitize_movie_id,
    utcnow,
)

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

# --------------------------------------------------------------------------- #
# Tunables (overridable through info.py / environment)
# --------------------------------------------------------------------------- #
DEFAULT_LIMIT = 20
MAX_LIMIT = 20
POSTER_PATH = "/api/movies/poster"
ART_PATH = "/api/movies/art"
BACKDROP_PATH = "/api/movies/backdrop"
#: "Coming Soon" rail – releases that are not out yet, with a countdown.
UPCOMING_PATH = "/api/movies/upcoming"
UPCOMING_POSTER_PATH = "/api/movies/upcoming/poster"
UPCOMING_MAX_LIMIT = 24
#: 2:3 poster art (200 = thumbnail, 1600 = big desktop card).
ALLOWED_POSTER_WIDTHS = (200, 320, 480, 640, 800, 1600)
#: 16:9 hero band artwork.
ALLOWED_BACKDROP_WIDTHS = (480, 720, 960, 1280, 1920)
MAX_POSTER_BYTES = 8 * 1024 * 1024
POSTER_CACHE_BYTES = 24 * 1024 * 1024
POSTER_TTL = 86400  # seconds – posters are immutable in practice
PLACEHOLDER_TTL = 300
#: Remembering a miss keeps the upstream TMDB/IMDb APIs from being hammered.
ART_TTL = 3600
ART_MISS_TTL = 60
ART_RETRY_HOURS = 24
ART_LOOKUP_TIMEOUT = 8.0
POSTER_USER_AGENT = "MinatoVerse-Poster-Proxy/1.0 (+https://t.me)"
DEFAULT_POSTER_HOSTS = (
    "image.tmdb.org",
    "media.themoviedb.org",
    "www.themoviedb.org",
    "m.media-amazon.com",
    "ia.media-imdb.com",
    "images-na.ssl-images-amazon.com",
    "images-amazon.com",
    "graph.org",
    "telegra.ph",
    "i.imgur.com",
)

#: Posters an admin attached with ``/setposter`` by replying to a photo are
#: stored as ``tg://file/<telegram_file_id>`` and downloaded through the bot
#: itself – no third-party image host involved.
TELEGRAM_SCHEME = "tg://file/"
_VERSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

_POSTER_CACHE: "OrderedDict[str, Tuple[bytes, str, float]]" = OrderedDict()
_POSTER_CACHE_BYTES = 0
_POSTER_INFLIGHT: Dict[str, "Any"] = {}
_POSTER_SESSION: Optional[ClientSession] = None


def _cfg(name: str, default):
    """Read a setting from ``info.py`` without breaking when it is missing."""
    try:
        import info  # type: ignore

        return getattr(info, name, default)
    except Exception:
        return default


def _feature_enabled() -> bool:
    return bool(_cfg("NEW_UPLOADED_MOVIES", True))


def _default_limit() -> int:
    try:
        return max(1, min(int(_cfg("NEW_UPLOADED_LIMIT", DEFAULT_LIMIT)), MAX_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def _list_cache_ttl() -> int:
    try:
        return max(0, min(int(_cfg("NEW_UPLOADED_CACHE_TTL", 60)), 3600))
    except (TypeError, ValueError):
        return 60


def _poster_hosts() -> Tuple[str, ...]:
    extra = _cfg("NEW_UPLOADED_POSTER_HOSTS", "")
    hosts = list(DEFAULT_POSTER_HOSTS)
    if isinstance(extra, str) and extra.strip():
        hosts.extend(part.strip().lower() for part in extra.split() if part.strip())
    elif isinstance(extra, (list, tuple)):
        hosts.extend(str(part).strip().lower() for part in extra if str(part).strip())
    return tuple(dict.fromkeys(hosts))


def _bot_username(request) -> str:
    """Public bot @username (never the token) used to build the deep links."""
    override = request.app.get("nu_bot_username") if request is not None else None
    if override:
        return str(override).strip().lstrip("@")
    try:
        from utils import temp  # type: ignore

        name = getattr(temp, "U_NAME", None)
        if name:
            return str(name).strip().lstrip("@")
    except Exception:
        pass
    try:
        from dreamxbotz.Bot import dreamxbotz  # type: ignore

        name = getattr(dreamxbotz, "username", None)
        if name:
            return str(name).strip().lstrip("@")
    except Exception:
        pass
    return ""


def _store():
    """The shared ``recent_movies`` store (imported lazily for testability)."""
    from database.recent_movies_db import recent_movies

    return recent_movies


# --------------------------------------------------------------------------- #
# Sanitizing
# --------------------------------------------------------------------------- #
def _poster_version(doc: Dict[str, Any]) -> str:
    raw = f"{doc.get('poster_url') or ''}|{doc.get('updated_at')}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]


def public_movie(doc: Dict[str, Any], bot_username: str = "") -> Dict[str, Any]:
    """Convert a database document into the whitelisted JSON shape.

    Anything not explicitly copied here never reaches the browser – so leaked
    file ids or download links are impossible by construction.
    """
    movie_id = sanitize_movie_id(doc.get("_id") or doc.get("id") or "")
    title = clean_title_text(doc.get("title") or "", limit=MAX_TITLE_LENGTH)
    qualities = list(doc.get("qualities") or [])
    single = doc.get("quality")
    if single:
        qualities.append(single)
    badge = primary_quality(qualities)
    label = quality_label(qualities)

    year = doc.get("year")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None

    has_poster = bool(doc.get("poster_url"))
    version = _poster_version(doc)
    poster = f"{POSTER_PATH}/{movie_id}?v={version}" if movie_id else ""
    # 16:9 artwork for the "just added" spotlight banner (TMDB backdrop when
    # known, the poster otherwise) – same origin, like the poster.
    backdrop = f"{BACKDROP_PATH}/{movie_id}?v={version}" if movie_id else ""
    uploaded_at = as_utc(doc.get("last_upload_at") or doc.get("uploaded_at"))
    deeplink = build_deeplink(bot_username, movie_id)

    return {
        "id": movie_id,
        "title": title,
        "year": year,
        "quality": badge,
        "quality_label": label,
        "poster": poster,
        "backdrop": backdrop,
        "has_poster": has_poster,
        "uploaded_at": uploaded_at.isoformat().replace("+00:00", "Z") if uploaded_at else None,
        "added": relative_time_label(uploaded_at),
        "deeplink": deeplink,
    }


# --------------------------------------------------------------------------- #
# Artwork for the watch-page movie hero
# --------------------------------------------------------------------------- #
def _art_fetch_enabled() -> bool:
    """May the hero look artwork up online?  ``WATCH_HERO_ART_FETCH`` (default on)."""
    return bool(_cfg("WATCH_HERO_ART_FETCH", True))


def _art_timeout() -> float:
    """Per-lookup timeout for an on-demand hero lookup (web requests must stay snappy)."""
    try:
        return max(2.0, float(_cfg("WATCH_HERO_ART_TIMEOUT", ART_LOOKUP_TIMEOUT)))
    except (TypeError, ValueError):
        return ART_LOOKUP_TIMEOUT


def _art_retry_hours() -> int:
    try:
        return max(1, int(_cfg("WATCH_HERO_ART_RETRY_HOURS", ART_RETRY_HOURS)))
    except (TypeError, ValueError):
        return ART_RETRY_HOURS


def _art_store():
    """The artwork cache (imported lazily so tests can swap it out)."""
    from database.movie_art_db import movie_art

    return movie_art


def _safe_year(value) -> Optional[int]:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2100 else None


def _art_version(movie_id: str, art: Dict[str, Any]) -> str:
    """Cache-busting token – changes as soon as better artwork is known."""
    raw = f"{movie_id}|{art.get('poster') or ''}|{art.get('backdrop') or ''}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]


def _art_checked_recently(doc, title: str = "") -> bool:
    """``True`` when a previous lookup may not be repeated yet (hit **or** miss).

    A *different* – usually better – title than the one that was looked up last
    time is a reason to try once more, so a slug-only attempt cannot block the
    real title for a whole day.
    """
    if not doc:
        return False
    cached_title = str(doc.get("title") or "").strip().lower()
    wanted = str(title or "").strip().lower()
    if wanted and cached_title and wanted != cached_title:
        return False
    checked = as_utc(doc.get("checked_at"))
    if checked is None:
        return False
    return (utcnow() - checked) < timedelta(hours=_art_retry_hours())


_HEX_TOKEN_RE = re.compile(r"^[0-9a-f]{8,}$")


def _title_hint_from_id(movie_id: str) -> str:
    """``"jawan-2023"`` -> ``"jawan 2023"``; ``""`` for hash-only ids."""
    parts = [part for part in str(movie_id or "").split("-") if part]
    if not parts or any(_HEX_TOKEN_RE.match(part) for part in parts):
        return ""
    words = [part for part in parts if len(part) > 1] or parts
    return " ".join(words).strip()


async def resolve_art(
    movie_id: str,
    title: str = "",
    year=None,
    *,
    want_backdrop: bool = True,
    allow_lookup: bool = True,
) -> Dict[str, Any]:
    """Poster/backdrop of one movie: rail → artwork cache → TMDB/IMDb.

    Returns ``{"poster", "backdrop", "source", "title", "year"}``; missing
    artwork is simply ``None`` (the hero then shows its placeholder).  Never
    raises – a lookup problem must not break a page view.

    ``want_backdrop=False`` is used by the poster proxy of the "Newly Uploaded
    Movies" rail, which only needs the 2:3 artwork: a movie that already has a
    poster then triggers no network call at all.  ``allow_lookup=False`` keeps
    that proxy from ever starting an upstream request – it serves what the rail
    or the artwork cache already knows, and the hero's JSON call is what warms
    the cache.
    """
    art: Dict[str, Any] = {"poster": None, "backdrop": None, "source": None}
    safe_id = sanitize_movie_id(movie_id)
    if not safe_id:
        return art

    # 1. the "Newly Uploaded Movies" rail may already know this movie's poster
    try:
        doc = await _store().get(safe_id)
    except Exception:
        doc = None
    if doc:
        art["poster"] = doc.get("poster_url") or None
        art["source"] = doc.get("poster_source") or None
        title = title or doc.get("title") or ""
        year = year or doc.get("year")

    # 2. artwork resolved on an earlier visit (also remembers failed lookups).
    #    The rail's poster path (want_backdrop=False) skips this read when the
    #    rail already has a poster, so a page view costs no extra round trip.
    cached = None
    if want_backdrop or not art["poster"]:
        try:
            cached = await _art_store().get(safe_id)
        except Exception:
            cached = None
    if cached:
        art["poster"] = art["poster"] or cached.get("poster_url")
        art["backdrop"] = cached.get("backdrop_url") or None
        art["source"] = art["source"] or cached.get("poster_source")
        title = title or cached.get("title") or ""
        year = year or cached.get("year")

    # 3. a fresh lookup – only when something is missing and it is due again
    needs_lookup = not art["poster"] or (want_backdrop and not art["backdrop"])
    if needs_lookup and allow_lookup and _art_fetch_enabled() and not _art_checked_recently(cached, title):
        query = f"{title} {year}".strip() if title else _title_hint_from_id(safe_id)
        if query:
            try:
                from dreamxbotz.util.new_uploaded import lookup_art  # lazy: heavy deps

                found = await lookup_art(query, title or query, timeout=_art_timeout())
            except Exception as exc:
                logger.debug("Hero artwork lookup failed for %s: %s", safe_id, exc)
                found = {}
            art["poster"] = found.get("poster") or art["poster"]
            art["backdrop"] = found.get("backdrop") or art["backdrop"]
            art["source"] = found.get("source") or art["source"]
            try:
                await _art_store().set_art(
                    safe_id,
                    title=title or "",
                    year=year,
                    poster_url=art["poster"],
                    backdrop_url=art["backdrop"],
                    source=art["source"],
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Could not cache hero artwork for %s: %s", safe_id, exc)

    art["title"] = clean_title_text(
        title or _title_hint_from_id(safe_id).title(), limit=MAX_TITLE_LENGTH
    )
    art["year"] = _safe_year(year)
    # "known" separates "this movie exists, it just has no artwork (yet)" from
    # "no idea what this id is" – the poster proxy answers 404 for the latter.
    art["known"] = bool(doc or cached or art["poster"] or art["backdrop"])
    return art


def public_art(movie_id: str, art: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelisted JSON shape for the hero – upstream URLs never leave the server."""
    safe_id = sanitize_movie_id(movie_id)
    if not safe_id:
        return {"ok": False, "error": "invalid_id"}
    version = _art_version(safe_id, art)
    return {
        "ok": True,
        "id": safe_id,
        "title": clean_title_text(art.get("title") or "", limit=MAX_TITLE_LENGTH),
        "year": _safe_year(art.get("year")),
        "poster": f"{POSTER_PATH}/{safe_id}?v={version}",
        "backdrop": f"{BACKDROP_PATH}/{safe_id}?v={version}",
        "has_poster": bool(art.get("poster")),
        "has_backdrop": bool(art.get("backdrop")),
        "source": clean_title_text(art.get("source") or "", limit=24),
    }


def _json_response(payload: Dict[str, Any], status: int = 200, ttl: int = 0, request=None):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
    }
    if ttl > 0:
        headers["Cache-Control"] = f"public, max-age={ttl}"
        etag = '"%s"' % hashlib.md5(body).hexdigest()
        headers["ETag"] = etag
        if request is not None and request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
    else:
        headers["Cache-Control"] = "no-store"
    _apply_cors(headers, request)
    return web.Response(body=body, status=status, headers=headers)


def _apply_cors(headers: Dict[str, str], request) -> None:
    """Send CORS headers only when the API is intentionally used cross-origin."""
    origin = _cfg("NEW_UPLOADED_CORS_ORIGIN", "")
    if not origin:
        return
    allowed = [p.strip() for p in str(origin).split(",") if p.strip()]
    if not allowed:
        return
    request_origin = request.headers.get("Origin") if request is not None else None
    if "*" in allowed:
        headers["Access-Control-Allow-Origin"] = "*"
    elif request_origin and request_origin in allowed:
        headers["Access-Control-Allow-Origin"] = request_origin
        headers["Vary"] = "Origin"


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@routes.get("/api/movies/new", allow_head=True)
async def newly_uploaded_movies(request: web.Request) -> web.Response:
    """Newest uploaded movies (max 20, newest first) for the website section."""
    try:
        raw_limit = request.rel_url.query.get("limit")
        try:
            limit = int(raw_limit) if raw_limit else _default_limit()
        except (TypeError, ValueError):
            limit = _default_limit()
        limit = max(1, min(limit, MAX_LIMIT))

        if not _feature_enabled():
            return _json_response(
                {"ok": True, "count": 0, "limit": limit, "movies": [], "disabled": True},
                ttl=0,
                request=request,
            )

        rows = await _store().list_recent(limit, raise_on_error=True)
        bot_username = _bot_username(request)
        # Freshness = newest upload in the payload (not "now"), so two requests
        # for unchanged data produce the same body and the ETag can answer 304.
        newest = max(
            (as_utc(row.get("last_upload_at") or row.get("uploaded_at")) for row in rows),
            default=None,
        )
        # public_movie() whitelists + sanitizes; documents whose id does not
        # survive validation are dropped instead of being served half-built.
        movies = [
            movie
            for movie in (public_movie(row, bot_username) for row in rows)
            if movie.get("id")
        ]
        payload = {
            "ok": True,
            "count": len(movies),
            "limit": limit,
            "updated_at": newest.isoformat().replace("+00:00", "Z") if newest else None,
            "bot_username": bot_username,
            "movies": movies,
        }
        return _json_response(payload, ttl=_list_cache_ttl(), request=request)
    except Exception as exc:
        logger.error("GET /api/movies/new failed: %s", exc)
        return _json_response(
            {"ok": False, "error": "database_unavailable", "movies": []},
            status=503,
            request=request,
        )


# --------------------------------------------------------------------------- #
# "Coming Soon" – upcoming releases with a live countdown
# --------------------------------------------------------------------------- #
#: Guards against piling up refresh tasks when many visitors hit the page.
_UPCOMING_REFRESH_RUNNING = False
_UPCOMING_STORE = None


def _upcoming_store():
    """The ``upcoming_movies`` store (memoised per process)."""
    global _UPCOMING_STORE
    if _UPCOMING_STORE is None:
        from database.upcoming_db import UpcomingMoviesStore

        _UPCOMING_STORE = UpcomingMoviesStore()
    return _UPCOMING_STORE


def _upcoming_enabled() -> bool:
    """``COMING_SOON`` master switch (default on)."""
    try:
        from dreamxbotz.util.coming_soon import is_enabled

        return bool(is_enabled())
    except Exception:  # pragma: no cover - defensive
        return True


def _upcoming_limit() -> int:
    try:
        from dreamxbotz.util.coming_soon import limit

        return max(1, min(int(limit()), UPCOMING_MAX_LIMIT))
    except Exception:
        return 12


def _upcoming_cache_ttl() -> int:
    try:
        from dreamxbotz.util.coming_soon import cache_ttl

        return max(0, int(cache_ttl()))
    except Exception:
        return 600


def _kick_upcoming_refresh() -> None:
    """Refresh the TMDB cache in the background – never blocks the response.

    The page keeps serving whatever is cached; the next visit sees the new
    rows.  A single in-flight task is enough, so a burst of visitors cannot
    spawn a burst of upstream calls.
    """
    global _UPCOMING_REFRESH_RUNNING
    if _UPCOMING_REFRESH_RUNNING:
        return
    _UPCOMING_REFRESH_RUNNING = True

    async def _run():
        global _UPCOMING_REFRESH_RUNNING
        try:
            from dreamxbotz.util.coming_soon import refresh_if_stale

            await refresh_if_stale(_upcoming_store())
        except Exception as exc:  # never surface a refresh failure to visitors
            logger.info("Coming soon: background refresh failed: %s", exc)
        finally:
            _UPCOMING_REFRESH_RUNNING = False

    try:
        asyncio.ensure_future(_run())
    except RuntimeError:  # pragma: no cover - no running loop (e.g. in tests)
        _UPCOMING_REFRESH_RUNNING = False


@routes.get(UPCOMING_PATH, allow_head=True)
async def upcoming_movies(request: web.Request) -> web.Response:
    """Movies that are not out yet, soonest first, with a countdown.

    Same contract as ``/api/movies/new``: whitelisted fields only, an ETag the
    browser can answer with 304, and a clean ``503`` when Mongo is down.
    """
    try:
        raw_limit = request.rel_url.query.get("limit")
        try:
            limit = int(raw_limit) if raw_limit else _upcoming_limit()
        except (TypeError, ValueError):
            limit = _upcoming_limit()
        limit = max(1, min(limit, UPCOMING_MAX_LIMIT))

        if not _upcoming_enabled():
            return _json_response(
                {"ok": True, "count": 0, "limit": limit, "movies": [], "disabled": True},
                ttl=0,
                request=request,
            )

        from dreamxbotz.util.coming_soon import public_upcoming_movies, release_cutoff

        store = _upcoming_store()
        # A stale cache is refreshed *after* the response is built, so the
        # visitor is never made to wait for TMDB.
        try:
            stale = await store.is_stale(_upcoming_refresh_seconds())
        except Exception:
            stale = False
        if stale:
            _kick_upcoming_refresh()

        rows = await store.list_upcoming(limit, cutoff=release_cutoff(), raise_on_error=True)
        bot_username = _bot_username(request)
        movies = public_upcoming_movies(rows, bot_username)
        # Freshness = the newest release date in the payload, so unchanged data
        # produces an unchanged body and the ETag can answer 304.
        newest = max(
            (row.get("release_date") for row in movies if row.get("release_date")),
            default=None,
        )
        payload = {
            "ok": True,
            "count": len(movies),
            "limit": limit,
            "updated_at": newest,
            "bot_username": bot_username,
            "movies": movies,
        }
        return _json_response(payload, ttl=_upcoming_cache_ttl(), request=request)
    except Exception as exc:
        logger.error("GET %s failed: %s", UPCOMING_PATH, exc)
        return _json_response(
            {"ok": False, "error": "database_unavailable", "movies": []},
            status=503,
            request=request,
        )


def _upcoming_refresh_seconds() -> int:
    try:
        from dreamxbotz.util.coming_soon import refresh_ttl

        return int(refresh_ttl())
    except Exception:
        return 6 * 3600


@routes.get(UPCOMING_POSTER_PATH + "/{movie_id}", allow_head=True)
async def upcoming_poster(request: web.Request) -> web.Response:
    """Poster bytes of one upcoming release, served from our own origin.

    Deliberately separate from ``/api/movies/poster/…``: that route reads the
    ``recent_movies`` rail (movies that *are* uploaded), while these posters
    come from the ``upcoming_movies`` cache filled by TMDB.  Keeping them apart
    means a Coming Soon card can never be mistaken for an available movie.
    """
    movie_id = sanitize_movie_id(request.match_info.get("movie_id"))
    if not movie_id:
        return _not_found_poster(request)

    width = _safe_width(request.rel_url.query.get("w"))
    cache_key = f"upcoming:{movie_id}:{width}:{_safe_version(request)}"
    cached = _cache_get(cache_key)
    if cached:
        payload, content_type, ttl = cached
        return _image_response(payload, content_type, request=request, ttl=ttl)

    try:
        doc = await _upcoming_store().get(movie_id)
    except Exception as exc:
        logger.error("Coming soon: poster lookup failed for %s: %s", movie_id, exc)
        doc = None

    poster_url = (doc or {}).get("poster_url")
    title = (doc or {}).get("title")
    if not poster_url:
        if doc is None:
            return _not_found_poster(request)
        payload = (placeholder_svg(title), "image/svg+xml")
        _cache_put(cache_key, payload[0], payload[1], PLACEHOLDER_TTL)
        return _image_response(payload[0], payload[1], request=request)

    image = await _fetch_poster(cache_key, poster_url, width, _session(request))
    if image is None:
        payload = (placeholder_svg(title), "image/svg+xml")
        _cache_put(cache_key, payload[0], payload[1], PLACEHOLDER_TTL)
        return _image_response(payload[0], payload[1], request=request)

    return _image_response(image[0], image[1], request=request)


@routes.get(POSTER_PATH + "/{movie_id}", allow_head=True)
async def movie_poster(request: web.Request) -> web.Response:
    """Poster bytes for one movie, or a clean SVG placeholder."""
    movie_id = sanitize_movie_id(request.match_info.get("movie_id"))
    if not movie_id:
        return _not_found_poster(request)

    width = _safe_width(request.rel_url.query.get("w"))
    cache_key = f"{movie_id}:{width}:{_safe_version(request)}"
    cached = _cache_get(cache_key)
    if cached:
        payload, content_type, ttl = cached
        return _image_response(payload, content_type, request=request, ttl=ttl)

    try:
        # The rail already knows most posters; resolve_art() only falls back to
        # the artwork cache / a lookup, and never asks for a 16:9 backdrop here.
        art = await resolve_art(movie_id, want_backdrop=False, allow_lookup=False)
    except Exception as exc:
        logger.error("Poster lookup failed for %s: %s", movie_id, exc)
        art = {}

    poster_url = art.get("poster")
    title = art.get("title")
    if not poster_url:
        if not art.get("known"):
            return _not_found_poster(request)
        payload = (placeholder_svg(title), "image/svg+xml")
        _cache_put(cache_key, payload[0], payload[1], PLACEHOLDER_TTL)
        return _image_response(payload[0], payload[1], request=request)

    image = await _fetch_poster(cache_key, poster_url, width, _session(request))
    if image is None:
        payload = (placeholder_svg(title), "image/svg+xml")
        _cache_put(cache_key, payload[0], payload[1], PLACEHOLDER_TTL)
        return _image_response(payload[0], payload[1], request=request)

    return _image_response(image[0], image[1], request=request)


@routes.get(ART_PATH + "/{movie_id}", allow_head=True)
async def movie_art_info(request: web.Request) -> web.Response:
    """Artwork URLs (poster + backdrop) of one movie for the watch-page hero."""
    movie_id = sanitize_movie_id(request.match_info.get("movie_id"))
    if not movie_id:
        return _json_response(
            {"ok": False, "error": "invalid_id"}, status=400, request=request
        )

    title = clean_title_text(request.rel_url.query.get("q") or "", limit=MAX_TITLE_LENGTH)
    year = _safe_year(request.rel_url.query.get("y"))
    try:
        art = await resolve_art(movie_id, title, year)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Artwork lookup failed for %s: %s", movie_id, exc)
        art = {}

    payload = public_art(movie_id, art)
    has_art = bool(art.get("poster") or art.get("backdrop"))
    return _json_response(
        payload, ttl=ART_TTL if has_art else ART_MISS_TTL, request=request
    )


@routes.get(BACKDROP_PATH + "/{movie_id}", allow_head=True)
async def movie_backdrop(request: web.Request) -> web.Response:
    """Wide (16:9) hero artwork: TMDB backdrop → poster → SVG placeholder."""
    movie_id = sanitize_movie_id(request.match_info.get("movie_id"))
    if not movie_id:
        return _not_found_art(request)

    width = _safe_width(request.rel_url.query.get("w"), ALLOWED_BACKDROP_WIDTHS)
    cache_key = f"backdrop:{movie_id}:{width}:{_safe_version(request)}"
    cached = _cache_get(cache_key)
    if cached:
        payload, content_type, ttl = cached
        return _image_response(payload, content_type, request=request, ttl=ttl)

    title = clean_title_text(request.rel_url.query.get("q") or "", limit=MAX_TITLE_LENGTH)
    year = _safe_year(request.rel_url.query.get("y"))
    try:
        art = await resolve_art(movie_id, title, year)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Backdrop lookup failed for %s: %s", movie_id, exc)
        art = {}

    source_url = art.get("backdrop") or art.get("poster")
    if not source_url:
        if not art.get("known"):
            return _not_found_art(request)
        payload = (placeholder_backdrop_svg(art.get("title")), "image/svg+xml")
        _cache_put(cache_key, payload[0], payload[1], PLACEHOLDER_TTL)
        return _image_response(payload[0], payload[1], request=request)

    image = await _fetch_poster(cache_key, source_url, width, _session(request))
    if image is None:
        payload = (placeholder_backdrop_svg(art.get("title")), "image/svg+xml")
        _cache_put(cache_key, payload[0], payload[1], PLACEHOLDER_TTL)
        return _image_response(payload[0], payload[1], request=request)

    return _image_response(image[0], image[1], request=request)


def _safe_version(request) -> str:
    """The ``?v=`` cache-busting token (part of the in-process cache key)."""
    try:
        value = str(request.rel_url.query.get("v") or "").strip()
    except Exception:
        return ""
    return value if _VERSION_RE.match(value) else ""


def evict_cached(movie_id: str) -> int:
    """Drop every cached rendition of one movie (after ``/setposter`` etc.)."""
    safe_id = sanitize_movie_id(movie_id)
    if not safe_id:
        return 0
    prefixes = (f"{safe_id}:", f"backdrop:{safe_id}:", f"upcoming:{safe_id}:")
    doomed = [key for key in _POSTER_CACHE if key.startswith(prefixes)]
    for key in doomed:
        payload, _, _ = _POSTER_CACHE.pop(key)
        _drop_cached_bytes(len(payload))
    return len(doomed)


def _safe_width(value, allowed=ALLOWED_POSTER_WIDTHS) -> int:
    try:
        width = int(value)
    except (TypeError, ValueError):
        return allowed[1]
    if width < allowed[0]:
        return allowed[0]
    for candidate in allowed:
        if width <= candidate:
            return candidate
    return allowed[-1]


def _image_response(payload: bytes, content_type: str, request=None, ttl: Optional[int] = None):
    headers = {
        "Content-Type": content_type,
        "Cache-Control": f"public, max-age={ttl if ttl is not None else POSTER_TTL}",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
        "Content-Length": str(len(payload) or 1),
    }
    _apply_cors(headers, request)
    if request is not None and request.method == "HEAD":
        return web.Response(status=200, headers=headers)
    return web.Response(body=payload, headers=headers)


def _not_found_poster(request=None):
    payload = placeholder_svg("")
    headers = {
        "Content-Type": "image/svg+xml",
        "Cache-Control": "public, max-age=60",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
    }
    _apply_cors(headers, request)
    if request is not None and request.method == "HEAD":
        return web.Response(status=404, headers=headers)
    return web.Response(body=payload, status=404, headers=headers)


def _not_found_art(request=None):
    """16:9 placeholder for an unusable movie id."""
    payload = placeholder_backdrop_svg("")
    headers = {
        "Content-Type": "image/svg+xml",
        "Cache-Control": "public, max-age=60",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
    }
    _apply_cors(headers, request)
    if request is not None and request.method == "HEAD":
        return web.Response(status=404, headers=headers)
    return web.Response(body=payload, status=404, headers=headers)


# --------------------------------------------------------------------------- #
# Placeholder artwork
# --------------------------------------------------------------------------- #
def placeholder_svg(title: Optional[str] = None) -> bytes:
    """Clean, on-brand SVG used when a movie has no poster (or it fails)."""
    label = clean_title_text(title or "", limit=28)
    lines = []
    if label:
        words = label.split()
        line = ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if len(candidate) > 18 and line:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
    lines = lines[:2]
    text_markup = ""
    if lines:
        start_y = 486 - (len(lines) - 1) * 16
        for index, line in enumerate(lines):
            text_markup += (
                f'<text x="240" y="{start_y + index * 32}" text-anchor="middle" '
                f'font-family="Sora, Segoe UI, system-ui, sans-serif" font-size="26" '
                f'font-weight="600" fill="#f4f6fb">{xml_escape(line)}</text>'
            )
    else:
        text_markup = (
            '<text x="240" y="496" text-anchor="middle" '
            'font-family="Sora, Segoe UI, system-ui, sans-serif" font-size="22" '
            'fill="#9aa1b9">Poster coming soon</text>'
        )

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="480" height="720" viewBox="0 0 480 720" role="img" aria-label="Movie poster placeholder">
  <defs>
    <linearGradient id="nu-bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#141726"/>
      <stop offset="1" stop-color="#080910"/>
    </linearGradient>
    <linearGradient id="nu-gold" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#f5c518"/>
      <stop offset="1" stop-color="#ffdd7a"/>
    </linearGradient>
    <linearGradient id="nu-fade" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0.45" stop-color="#05060b" stop-opacity="0"/>
      <stop offset="1" stop-color="#05060b" stop-opacity="0.92"/>
    </linearGradient>
  </defs>
  <rect width="480" height="720" fill="url(#nu-bg)"/>
  <circle cx="86" cy="96" r="150" fill="#f5c518" opacity="0.07"/>
  <circle cx="410" cy="190" r="120" fill="#f5c518" opacity="0.05"/>
  <g opacity="0.9" transform="translate(150 216)">
    <rect x="0" y="10" width="180" height="150" rx="16" fill="none" stroke="url(#nu-gold)" stroke-width="4"/>
    <rect x="-22" y="0" width="16" height="18" rx="4" fill="url(#nu-gold)"/>
    <rect x="-22" y="36" width="16" height="18" rx="4" fill="url(#nu-gold)"/>
    <rect x="-22" y="72" width="16" height="18" rx="4" fill="url(#nu-gold)"/>
    <rect x="-22" y="108" width="16" height="18" rx="4" fill="url(#nu-gold)"/>
    <rect x="-22" y="144" width="16" height="18" rx="4" fill="url(#nu-gold)"/>
    <rect x="186" y="0" width="16" height="18" rx="4" fill="url(#nu-gold)"/>
    <rect x="186" y="36" width="16" height="18" rx="4" fill="url(#nu-gold)"/>
    <rect x="186" y="72" width="16" height="18" rx="4" fill="url(#nu-gold)"/>
    <rect x="186" y="108" width="16" height="18" rx="4" fill="url(#nu-gold)"/>
    <rect x="186" y="144" width="16" height="18" rx="4" fill="url(#nu-gold)"/>
    <path d="M72 55 L124 85 L72 115 Z" fill="url(#nu-gold)"/>
  </g>
  <rect y="430" width="480" height="290" fill="url(#nu-fade)"/>
  {text_markup}
  <text x="240" y="640" text-anchor="middle" font-family="Inter, Segoe UI, system-ui, sans-serif" font-size="15" letter-spacing="3" fill="#f5c518">MINATOVERSE</text>
  <text x="240" y="666" text-anchor="middle" font-family="Inter, Segoe UI, system-ui, sans-serif" font-size="12" fill="#9aa1b9">open in Telegram to get it</text>
</svg>
"""
    return svg.encode("utf-8")


def placeholder_backdrop_svg(title: Optional[str] = None) -> bytes:
    """Clean, on-brand **16:9** placeholder for the watch-page hero band."""
    label = clean_title_text(title or "", limit=34)
    lines = []
    if label:
        words = label.split()
        line = ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if len(candidate) > 22 and line:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
    lines = lines[:2]
    if lines:
        start_y = 372 - (len(lines) - 1) * 20
        text_markup = "".join(
            f'<text x="640" y="{start_y + index * 42}" text-anchor="middle" '
            f'font-family="Sora, Segoe UI, system-ui, sans-serif" font-size="34" '
            f'font-weight="600" fill="#f4f6fb">{xml_escape(line)}</text>'
            for index, line in enumerate(lines)
        )
    else:
        text_markup = (
            '<text x="640" y="378" text-anchor="middle" '
            'font-family="Sora, Segoe UI, system-ui, sans-serif" font-size="30" '
            'fill="#9aa1b9">Artwork coming soon</text>'
        )

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720" role="img" aria-label="Movie artwork placeholder">
  <defs>
    <linearGradient id="mh-bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#171b2c"/>
      <stop offset="0.55" stop-color="#0d101b"/>
      <stop offset="1" stop-color="#07080d"/>
    </linearGradient>
    <linearGradient id="mh-gold" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#f5c518"/>
      <stop offset="1" stop-color="#ffdd7a"/>
    </linearGradient>
    <radialGradient id="mh-glow" cx="0.2" cy="0.15" r="0.8">
      <stop offset="0" stop-color="#f5c518" stop-opacity="0.22"/>
      <stop offset="1" stop-color="#f5c518" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="1280" height="720" fill="url(#mh-bg)"/>
  <rect width="1280" height="720" fill="url(#mh-glow)"/>
  <g opacity="0.85" transform="translate(518 150)">
    <rect x="0" y="12" width="244" height="196" rx="20" fill="none" stroke="url(#mh-gold)" stroke-width="5"/>
    <rect x="-26" y="0" width="18" height="22" rx="5" fill="url(#mh-gold)"/>
    <rect x="-26" y="48" width="18" height="22" rx="5" fill="url(#mh-gold)"/>
    <rect x="-26" y="96" width="18" height="22" rx="5" fill="url(#mh-gold)"/>
    <rect x="-26" y="144" width="18" height="22" rx="5" fill="url(#mh-gold)"/>
    <rect x="252" y="0" width="18" height="22" rx="5" fill="url(#mh-gold)"/>
    <rect x="252" y="48" width="18" height="22" rx="5" fill="url(#mh-gold)"/>
    <rect x="252" y="96" width="18" height="22" rx="5" fill="url(#mh-gold)"/>
    <rect x="252" y="144" width="18" height="22" rx="5" fill="url(#mh-gold)"/>
    <path d="M96 62 L166 110 L96 158 Z" fill="url(#mh-gold)"/>
  </g>
  {text_markup}
  <text x="640" y="486" text-anchor="middle" font-family="Inter, Segoe UI, system-ui, sans-serif" font-size="16" letter-spacing="6" fill="#f5c518">MINATOVERSE</text>
</svg>
"""
    return svg.encode("utf-8")


# --------------------------------------------------------------------------- #
# Poster fetching (allow-listed, size-capped, re-encoded)
# --------------------------------------------------------------------------- #
def poster_host_allowed(url: str) -> bool:
    """SSRF guard: HTTPS + allow-listed image host only."""
    if bool(_cfg("NEW_UPLOADED_POSTER_ANY_HOST", False)):
        return str(url).startswith("https://")
    try:
        parsed = urlparse(str(url))
    except Exception:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.lower()
    return any(host == allowed or host.endswith("." + allowed) for allowed in _poster_hosts())


def _session(request=None) -> ClientSession:
    """The shared upstream HTTP session (recreated if its loop went away).

    A module-level session avoids app cleanup hooks – those are frozen once the
    server has started – and is reused by every poster request.
    """
    global _POSTER_SESSION
    session = _POSTER_SESSION
    if session is not None:
        loop = getattr(session, "_loop", None)
        if session.closed or (loop is not None and loop.is_closed()):
            _POSTER_SESSION = None
            session = None
    if session is None:
        session = ClientSession(
            timeout=ClientTimeout(total=12),
            headers={"User-Agent": POSTER_USER_AGENT},
        )
        _POSTER_SESSION = session
    return session


async def close_session() -> None:
    """Close the upstream session (tests / graceful shutdown)."""
    global _POSTER_SESSION
    session, _POSTER_SESSION = _POSTER_SESSION, None
    if session is not None and not session.closed:
        await session.close()


async def download_telegram_file(file_id: str) -> Optional[bytes]:
    """Fetch a photo/document the bot has access to (``/setposter`` posters).

    Replaced in tests / tools; inside the bot it uses the running client.
    """
    from dreamxbotz.Bot import dreamxbotz  # lazy: only available inside the bot

    buffer = await dreamxbotz.download_media(file_id, in_memory=True)
    if buffer is None:
        return None
    data = buffer.getvalue() if hasattr(buffer, "getvalue") else buffer
    return bytes(data) if data else None


async def _fetch_telegram_poster(cache_key: str, file_id: str, width: int):
    """Poster bytes for a ``tg://file/<id>`` artwork (resized like any other).

    The original bytes are cached per file id, so the poster, the backdrop
    band and every width share a single download through the bot.
    """
    raw_key = f"tgraw:{file_id}"
    cached = _cache_get(raw_key)
    if cached:
        data = cached[0]
    else:
        try:
            data = await download_telegram_file(file_id)
        except Exception as exc:
            logger.warning("Telegram poster download failed (%s): %s", file_id[:16], exc)
            return None
        if not data or len(data) > MAX_POSTER_BYTES:
            return None
        _cache_put(raw_key, data, "application/octet-stream", POSTER_TTL)
    # Telegram photos are JPEG already – without Pillow serve them as they are.
    result = _resize_jpeg(data, width) or (data, "image/jpeg")
    _cache_put(cache_key, result[0], result[1], POSTER_TTL)
    return result


async def _fetch_poster(cache_key: str, poster_url: str, width: int, session: ClientSession):
    """Download + resize a poster; returns ``(bytes, content_type)`` or ``None``.

    Concurrent requests for the same poster share one upstream download.
    """
    if str(poster_url).startswith(TELEGRAM_SCHEME):
        return await _fetch_telegram_poster(cache_key, str(poster_url)[len(TELEGRAM_SCHEME):], width)
    if not poster_host_allowed(poster_url):
        logger.info("Poster host not allow-listed, serving placeholder: %s", poster_url)
        return None

    inflight = _POSTER_INFLIGHT.get(cache_key)
    if inflight is not None:
        try:
            return await inflight
        except Exception:  # pragma: no cover - defensive
            return None

    loop = asyncio.get_running_loop()
    future: "asyncio.Future" = loop.create_future()
    _POSTER_INFLIGHT[cache_key] = future
    try:
        result = await _download_and_encode(poster_url, width, session)
    except Exception as exc:
        logger.warning("Poster download failed (%s): %s", poster_url, exc)
        result = None
    finally:
        _POSTER_INFLIGHT.pop(cache_key, None)
        if not future.done():
            future.set_result(result)

    if result:
        _cache_put(cache_key, result[0], result[1], POSTER_TTL)
    return result


async def _download_and_encode(poster_url: str, width: int, session: ClientSession):
    """Fetch one poster over the shared session and normalise it to JPEG."""
    async with session.get(poster_url) as response:
        if response.status != 200:
            return None
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type and not content_type.startswith("image/"):
            return None
        declared = response.headers.get("Content-Length")
        if declared:
            try:
                if int(declared) > MAX_POSTER_BYTES:
                    return None
            except (TypeError, ValueError):
                pass
        data = await response.content.read(MAX_POSTER_BYTES + 1)
        if not data or len(data) > MAX_POSTER_BYTES:
            return None

    resized = _resize_jpeg(data, width)
    if resized:
        return resized
    if content_type.startswith("image/"):
        return data, content_type
    return None


def _resize_jpeg(data: bytes, width: int):
    """Resize + re-encode to progressive JPEG (strips EXIF, cuts bytes a lot)."""
    try:
        from PIL import Image
    except Exception:  # Pillow missing – serve the original bytes instead
        return None
    try:
        Image.MAX_IMAGE_PIXELS = 50_000_000
        with Image.open(BytesIO(data)) as img:
            img = img.convert("RGB") if img.mode not in ("RGB", "L") else img
            if img.width > width:
                height = max(1, round(img.height * (width / img.width)))
                img = img.resize((width, height), Image.LANCZOS)
            out = BytesIO()
            img.save(out, format="JPEG", quality=82, optimize=True, progressive=True)
        return out.getvalue(), "image/jpeg"
    except Exception as exc:
        logger.debug("Poster resize failed: %s", exc)
        return None


def _cache_get(key: str):
    entry = _POSTER_CACHE.get(key)
    if not entry:
        return None
    payload, content_type, expires_at = entry
    if expires_at and time.monotonic() > expires_at:
        _POSTER_CACHE.pop(key, None)
        _drop_cached_bytes(len(payload))
        return None
    _POSTER_CACHE.move_to_end(key)
    ttl = max(1, int(expires_at - time.monotonic())) if expires_at else POSTER_TTL
    return payload, content_type, ttl


def _cache_put(key: str, payload: bytes, content_type: str, ttl: int) -> None:
    global _POSTER_CACHE_BYTES
    if not payload or len(payload) > POSTER_CACHE_BYTES:
        return
    previous = _POSTER_CACHE.pop(key, None)
    if previous:
        _POSTER_CACHE_BYTES -= len(previous[0])
    _POSTER_CACHE[key] = (payload, content_type, time.monotonic() + ttl)
    _POSTER_CACHE_BYTES += len(payload)
    while _POSTER_CACHE_BYTES > POSTER_CACHE_BYTES and len(_POSTER_CACHE) > 1:
        _, (evicted, _, _) = _POSTER_CACHE.popitem(last=False)
        _POSTER_CACHE_BYTES -= len(evicted)


def _drop_cached_bytes(size: int) -> None:
    global _POSTER_CACHE_BYTES
    _POSTER_CACHE_BYTES = max(0, _POSTER_CACHE_BYTES - size)


def cache_stats() -> Dict[str, Any]:
    """Diagnostics for ``/stats``-style commands."""
    return {
        "entries": len(_POSTER_CACHE),
        "bytes": _POSTER_CACHE_BYTES,
        "inflight": len(_POSTER_INFLIGHT),
    }
