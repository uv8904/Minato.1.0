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
#: "Validate key" endpoint – answers 200 for a good credential and 401 for a bad one.
AUTH_URL = "https://api.themoviedb.org/3/authentication"
IMAGE_BASE = "https://image.tmdb.org/t/p/"
POSTER_SIZE = "w780"
BACKDROP_SIZE = "w1280"
DEFAULT_TIMEOUT = 8.0
USER_AGENT = "MinatoVerse-Poster-Worker/1.0 (+https://t.me)"

_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
#: A v3 API key is exactly 32 hexadecimal characters.
V3_KEY_LENGTH = 32
_V3_KEY_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_V3_BAD_CHARS_RE = re.compile(r"[^0-9a-fA-F]")
#: Characters people accidentally paste around a key (markdown, quotes, angle brackets).
_KEY_JUNK = " \t\r\n*`\"'<>«»“”‘’"
_KEY_PREFIX_RE = re.compile(r"^(?:tmdb[_ -]?api[_ -]?key|api[_ -]?key|bearer)\s*[:=]?\s*", re.IGNORECASE)


def clean_key(raw: Any) -> str:
    """Strip the junk that ends up around a pasted key (quotes, ``*``, ``TMDB_API_KEY=`` …)."""
    key = str(raw or "").strip().strip(_KEY_JUNK)
    for _ in range(2):  # "TMDB_API_KEY=\"abc\"" → abc
        key = _KEY_PREFIX_RE.sub("", key).strip(_KEY_JUNK)
    return key


def key_problem(api_key: Any) -> str:
    """Why ``api_key`` cannot be a TMDB credential – ``""`` when it looks fine.

    Written for the ``/posters`` report, so the text tells the owner what to
    fix ("24 characters instead of 32", "contains 'r'") instead of a bare
    "invalid".
    """
    key = clean_key(api_key)
    if not key:
        return "missing"
    if re.search(r"\s", key):
        return "contains spaces or line breaks – paste it as one word"
    if key.startswith("eyJ") or key.count(".") >= 2:  # v4 read access token (JWT)
        if key.count(".") < 2 or len(key) <= 64:
            return "the v4 token is cut off – copy the whole eyJ… token"
        return ""
    bad = sorted(set(_V3_BAD_CHARS_RE.findall(key)))
    if bad:
        shown = ", ".join(repr(ch) for ch in bad[:5])
        return f"contains {shown} – a v3 key only has the digits 0-9 and letters a-f (copy/paste typo?)"
    if len(key) != V3_KEY_LENGTH:
        return f"{len(key)} characters instead of {V3_KEY_LENGTH} – the key is cut off or mistyped"
    return ""


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
    key = clean_key(api_key)
    if not key:
        return {}, {}
    if key.count(".") >= 2 and len(key) > 64:  # v4 read access token (JWT)
        return {}, {"Authorization": f"Bearer {key}"}
    return {"api_key": key}, {}


def looks_like_key(api_key: str) -> bool:
    """Cheap sanity check used by the ``/posters`` diagnostics (see :func:`key_problem`)."""
    return not key_problem(api_key)


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


async def validate_key(
    api_key: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    session: Optional[ClientSession] = None,
) -> Tuple[Optional[bool], str]:
    """Ask TMDB itself whether ``api_key`` is accepted.

    Returns ``(True, "accepted")``, ``(False, "<TMDB's reason>")`` when the key
    is rejected (HTTP 401 – usually one mistyped digit) or ``(None, "<why>")``
    when TMDB could not be reached, so the caller can tell "wrong key" from
    "no internet".
    """
    params, headers = auth_for(api_key)
    if not params and not headers:
        return False, "no key configured"
    headers = dict(headers)
    headers.setdefault("Accept", "application/json")
    headers.setdefault("User-Agent", USER_AGENT)

    own_session = session is None
    if own_session:
        session = ClientSession(timeout=ClientTimeout(total=max(2.0, float(timeout))))
    try:
        async with session.get(AUTH_URL, params=params, headers=headers) as response:
            try:
                payload = await response.json(content_type=None)
            except Exception:
                payload = None
            payload = payload if isinstance(payload, dict) else {}
            if response.status == 200 and payload.get("success", True):
                return True, "accepted"
            reason = str(payload.get("status_message") or f"HTTP {response.status}").strip()
            if response.status in (401, 403):
                return False, reason
            return None, reason
    except Exception as exc:  # DNS / firewall / timeout – not the key's fault
        return None, f"could not reach api.themoviedb.org ({type(exc).__name__})"
    finally:
        if own_session:
            await session.close()


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
