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
import time
from collections import OrderedDict
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
)

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

# --------------------------------------------------------------------------- #
# Tunables (overridable through info.py / environment)
# --------------------------------------------------------------------------- #
DEFAULT_LIMIT = 20
MAX_LIMIT = 20
POSTER_PATH = "/api/movies/poster"
ALLOWED_POSTER_WIDTHS = (200, 320, 480, 640)
MAX_POSTER_BYTES = 8 * 1024 * 1024
POSTER_CACHE_BYTES = 24 * 1024 * 1024
POSTER_TTL = 86400  # seconds – posters are immutable in practice
PLACEHOLDER_TTL = 300
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
    poster = f"{POSTER_PATH}/{movie_id}?v={_poster_version(doc)}" if movie_id else ""
    uploaded_at = as_utc(doc.get("last_upload_at") or doc.get("uploaded_at"))
    deeplink = build_deeplink(bot_username, movie_id)

    return {
        "id": movie_id,
        "title": title,
        "year": year,
        "quality": badge,
        "quality_label": label,
        "poster": poster,
        "has_poster": has_poster,
        "uploaded_at": uploaded_at.isoformat().replace("+00:00", "Z") if uploaded_at else None,
        "added": relative_time_label(uploaded_at),
        "deeplink": deeplink,
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


@routes.get(POSTER_PATH + "/{movie_id}", allow_head=True)
async def movie_poster(request: web.Request) -> web.Response:
    """Poster bytes for one movie, or a clean SVG placeholder."""
    movie_id = sanitize_movie_id(request.match_info.get("movie_id"))
    if not movie_id:
        return _not_found_poster(request)

    width = _safe_width(request.rel_url.query.get("w"))
    cache_key = f"{movie_id}:{width}"
    cached = _cache_get(cache_key)
    if cached:
        payload, content_type, ttl = cached
        return _image_response(payload, content_type, request=request, ttl=ttl)

    try:
        doc = await _store().get(movie_id)
    except Exception as exc:
        logger.error("Poster lookup failed for %s: %s", movie_id, exc)
        doc = None

    if not doc:
        return _not_found_poster(request)

    poster_url = doc.get("poster_url")
    if not poster_url:
        payload = (placeholder_svg(doc.get("title")), "image/svg+xml")
        _cache_put(cache_key, payload[0], payload[1], PLACEHOLDER_TTL)
        return _image_response(payload[0], payload[1], request=request)

    image = await _fetch_poster(cache_key, poster_url, width, _session(request))
    if image is None:
        payload = (placeholder_svg(doc.get("title")), "image/svg+xml")
        _cache_put(cache_key, payload[0], payload[1], PLACEHOLDER_TTL)
        return _image_response(payload[0], payload[1], request=request)

    return _image_response(image[0], image[1], request=request)


def _safe_width(value) -> int:
    try:
        width = int(value)
    except (TypeError, ValueError):
        return ALLOWED_POSTER_WIDTHS[1]
    if width < ALLOWED_POSTER_WIDTHS[0]:
        return ALLOWED_POSTER_WIDTHS[0]
    for allowed in ALLOWED_POSTER_WIDTHS:
        if width <= allowed:
            return allowed
    return ALLOWED_POSTER_WIDTHS[-1]


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


async def _fetch_poster(cache_key: str, poster_url: str, width: int, session: ClientSession):
    """Download + resize a poster; returns ``(bytes, content_type)`` or ``None``.

    Concurrent requests for the same poster share one upstream download.
    """
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
