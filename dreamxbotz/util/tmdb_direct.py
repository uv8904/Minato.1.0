"""Direct TMDB lookup (official ``api.themoviedb.org`` v3 API).

The bot's regular poster helper (``plugins/Dreamxfutures/Imdbposter.py``)
talks to a hosted wrapper in front of TMDB.  When that wrapper is slow, down
or rate-limited the "Newly Uploaded Movies" rail and the watch-page hero
would silently end up with placeholder artwork – so the poster worker asks
TMDB **directly** with the owner's own ``TMDB_API_KEY`` before it falls back
to IMDb.

Works with both kinds of TMDB credentials:

* **v3 API key** (32 hex characters) → sent as ``?api_key=`` – the one shown
  on https://www.themoviedb.org/settings/api under "API Key",
* **v4 read access token** (long ``eyJ…`` JWT) → sent as
  ``Authorization: Bearer`` header.

Nothing here raises: every problem is logged and an empty result is returned.
"""
import logging
import re
from typing import Any, Dict, Optional, Tuple

from aiohttp import ClientSession, ClientTimeout

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
IMAGE_BASE = "https://image.tmdb.org/t/p/"
POSTER_SIZE = "w780"
BACKDROP_SIZE = "w1280"
DEFAULT_TIMEOUT = 8.0
USER_AGENT = "MinatoVerse-Poster-Worker/1.0 (+https://t.me)"

_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
_V3_KEY_RE = re.compile(r"^[A-Za-z0-9]{20,64}$")


def split_title_year(query: str) -> Tuple[str, Optional[int]]:
    """``"Jawan 2023"`` → ``("Jawan", 2023)``; a query without a year keeps ``None``."""
    text = re.sub(r"\s+", " ", str(query or "")).strip()
    if not text:
        return "", None
    match = None
    for match in _YEAR_RE.finditer(text):  # last year-like token wins
        pass
    if not match:
        return text, None
    year = int(match.group(1))
    title = (text[: match.start()] + text[match.end():]).strip(" -–·()[]")
    title = re.sub(r"\s+", " ", title).strip()
    return title or text, year


def auth_for(api_key: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """``(params, headers)`` for a v3 key or a v4 bearer token."""
    key = str(api_key or "").strip().strip('"').strip("'")
    if not key:
        return {}, {}
    if key.count(".") >= 2 and len(key) > 64:  # v4 read access token (JWT)
        return {}, {"Authorization": f"Bearer {key}"}
    return {"api_key": key}, {}


def looks_like_key(api_key: str) -> bool:
    """Cheap sanity check used by the ``/posters`` diagnostics."""
    key = str(api_key or "").strip()
    if not key:
        return False
    if key.count(".") >= 2 and len(key) > 64:
        return True
    return bool(_V3_KEY_RE.match(key))


def _image(path: Optional[str], size: str) -> Optional[str]:
    if not path or not isinstance(path, str):
        return None
    return f"{IMAGE_BASE}{size}{path if path.startswith('/') else '/' + path}"


def _pick(results, title: str) -> Optional[dict]:
    """First result with a poster, preferring an (almost) exact title match."""
    if not isinstance(results, list):
        return None
    wanted = re.sub(r"[^a-z0-9]+", " ", str(title).lower()).strip()
    with_poster = [row for row in results if isinstance(row, dict) and row.get("poster_path")]
    for row in with_poster:
        for key in ("title", "original_title"):
            got = re.sub(r"[^a-z0-9]+", " ", str(row.get(key) or "").lower()).strip()
            if got and got == wanted:
                return row
    if with_poster:
        return with_poster[0]
    return results[0] if results and isinstance(results[0], dict) else None


async def search_movie(
    query: str,
    *,
    api_key: str,
    year: Optional[int] = None,
    timeout: float = DEFAULT_TIMEOUT,
    session: Optional[ClientSession] = None,
    language: str = "en-US",
) -> Dict[str, Any]:
    """Poster + backdrop for one movie straight from TMDB.

    Returns ``{"poster", "backdrop", "title", "year", "tmdb_id", "source"}``
    (values ``None`` when unknown) or ``{}`` when nothing was found / no key.
    A query that contains a year (``"Jawan 2023"``) is split automatically;
    when the year-filtered search has no hits, the search is repeated without
    the year (release years in file names are often off by one).
    """
    params, headers = auth_for(api_key)
    if not params and not headers:
        return {}
    title, parsed_year = split_title_year(query)
    year = year or parsed_year
    if not title:
        return {}

    headers = dict(headers)
    headers.setdefault("Accept", "application/json")
    headers.setdefault("User-Agent", USER_AGENT)

    own_session = session is None
    if own_session:
        session = ClientSession(timeout=ClientTimeout(total=max(2.0, float(timeout))))
    try:
        attempts = [year, None] if year else [None]
        for attempt_year in attempts:
            query_params = dict(params)
            query_params.update({"query": title, "include_adult": "false", "language": language, "page": "1"})
            if attempt_year:
                query_params["year"] = str(attempt_year)
            try:
                async with session.get(SEARCH_URL, params=query_params, headers=headers) as response:
                    if response.status == 401:
                        logger.warning("TMDB rejected the API key (401) – check TMDB_API_KEY.")
                        return {}
                    if response.status != 200:
                        logger.info("TMDB search failed with HTTP %s for '%s'.", response.status, title)
                        continue
                    payload = await response.json(content_type=None)
            except Exception as exc:
                logger.info("TMDB search error for '%s': %s", title, exc)
                continue
            picked = _pick((payload or {}).get("results"), title)
            if not picked:
                continue
            release = str(picked.get("release_date") or "")
            found_year = int(release[:4]) if release[:4].isdigit() else None
            return {
                "poster": _image(picked.get("poster_path"), POSTER_SIZE),
                "backdrop": _image(picked.get("backdrop_path"), BACKDROP_SIZE),
                "title": picked.get("title") or picked.get("original_title") or title,
                "year": found_year,
                "tmdb_id": picked.get("id"),
                "source": "tmdb",
            }
        return {}
    finally:
        if own_session:
            await session.close()
