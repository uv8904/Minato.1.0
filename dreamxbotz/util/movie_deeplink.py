"""Bot side of the Stream Mode deep link: ``?start=movie_<MOVIE_ID>``.

When a visitor taps a poster on the website the browser opens

    https://t.me/<BOT_USERNAME>?start=movie_<MOVIE_ID>

Telegram delivers that payload to ``/start``.  This module turns the payload
back into the exact movie the user clicked, so the bot can show the matching
result **without the user typing or searching again**.

The resolver is deliberately free of Telegram imports (it only reads the
database) which keeps it easy to unit-test; ``plugins/commands.py`` owns the
messaging part.
"""
import logging
import time
from typing import Any, Dict, Optional, Tuple

from dreamxbotz.util.movie_titles import clean_title_text, sanitize_movie_id

logger = logging.getLogger(__name__)

#: Small in-process cache – a poster tap must not always hit MongoDB.
CACHE_TTL = 300.0
CACHE_MAX_ENTRIES = 500
_CACHE: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}

STATUS_OK = "ok"          # movie found in the database
STATUS_FALLBACK = "fallback"  # unknown id, but the slug still yields a title
STATUS_INVALID = "invalid"    # unusable payload


def _cache_get(movie_id: str) -> Tuple[bool, Optional[dict]]:
    entry = _CACHE.get(movie_id)
    if not entry:
        return False, None
    expires_at, value = entry
    if time.monotonic() > expires_at:
        _CACHE.pop(movie_id, None)
        return False, None
    return True, value


def _cache_put(movie_id: str, value: Optional[dict]) -> None:
    if len(_CACHE) >= CACHE_MAX_ENTRIES:
        # Cheap FIFO eviction – the cache only exists to soften repeated taps.
        for key in list(_CACHE)[: CACHE_MAX_ENTRIES // 4]:
            _CACHE.pop(key, None)
    _CACHE[movie_id] = (time.monotonic() + CACHE_TTL, value)


def clear_cache() -> None:
    """Drop cached lookups (used by tests and after a poster/name change)."""
    _CACHE.clear()


def title_from_movie_id(movie_id: str) -> str:
    """Best-effort title for an unknown id: ``jawan-2023`` -> ``Jawan 2023``."""
    safe_id = sanitize_movie_id(movie_id)
    if not safe_id:
        return ""
    words = [part for part in safe_id.replace("_", "-").split("-") if part]
    if not words or words[0] == "m":  # hash-only id: nothing human readable
        return ""
    return clean_title_text(" ".join(word.capitalize() for word in words), limit=100)


async def lookup_movie(movie_id: str) -> Optional[dict]:
    """Fetch a movie document by ``MOVIE_ID`` (cached, never raises)."""
    safe_id = sanitize_movie_id(movie_id)
    if not safe_id:
        return None
    hit, cached = _cache_get(safe_id)
    if hit:
        return cached
    try:
        from database.recent_movies_db import recent_movies

        movie = await recent_movies.get(safe_id)
    except Exception as exc:  # DB down / not configured – fall back to a search
        logger.debug("Movie deep-link lookup failed: %s", exc)
        movie = None
    _cache_put(safe_id, movie)
    return movie


async def resolve_movie_deeplink(payload: str) -> Dict[str, Any]:
    """Resolve a ``movie_<MOVIE_ID>`` start payload into a search request.

    Returns a dict with:

    ``status``   ``"ok"`` (found), ``"fallback"`` (unknown id but usable slug)
                 or ``"invalid"`` (nothing we can search for).
    ``movie_id`` the sanitized id.
    ``query``    the search text the bot should run (``""`` when invalid).
    ``movie``    the stored document (or ``None``).
    """
    raw = str(payload or "").strip()
    if raw.startswith("movie_"):
        raw = raw[len("movie_") :]
    movie_id = sanitize_movie_id(raw)
    if not movie_id:
        return {"status": STATUS_INVALID, "movie_id": "", "query": "", "movie": None}

    movie = await lookup_movie(movie_id)
    if movie:
        query = clean_title_text(
            movie.get("search_query") or movie.get("title") or "", limit=100
        )
        if query:
            return {
                "status": STATUS_OK,
                "movie_id": movie_id,
                "query": query,
                "movie": movie,
            }

    query = title_from_movie_id(movie_id)
    if not query:
        return {"status": STATUS_INVALID, "movie_id": movie_id, "query": "", "movie": None}
    return {
        "status": STATUS_FALLBACK,
        "movie_id": movie_id,
        "query": query,
        "movie": movie,
    }
