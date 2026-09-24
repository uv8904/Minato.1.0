""""Coming Soon" – upcoming releases with a live countdown (Stream Mode).

What it is
----------
The web pages show a second rail next to *Newly Uploaded Movies*: movies that
are **not out yet**, each with its release day and a countdown that ticks in
the browser (``12d 04h 33m``) without a reload.  On the bot side
``/comingsoon`` lists the same releases and every row carries a
🔔 **Notify me** button that registers the title with the existing
``title_notify`` pipeline – so the moment somebody uploads the movie,
``title_notify.check_new_file()`` already PMs everyone who asked.

::

    TMDB /3/movie/upcoming ─► coming_soon.refresh_if_stale()
                                      │  (only when the cache is stale)
                                      ▼
                        upcoming_movies (Mongo, same DATABASE_URI)
                                      │
              GET /api/movies/upcoming ─► "Coming Soon" rail + countdown
                                      │
              /comingsoon (bot) ─► 🔔 Notify me ─► title_notify
                                      │
              file indexed ─► check_new_file() ─► users are PM'd

Why the countdown is split
--------------------------
The *label* (``"in 12 days"``, ``"releases today"``) is rendered server side so
the page is meaningful with JavaScript disabled; the *ticking numbers* are
computed in ``coming_soon.js`` from the same ISO ``release_date``.  Nothing is
guessed twice and the two can never disagree about the day.

Everything here is **best effort**: any failure is logged and swallowed, so a
TMDB outage or a missing key can never break a page view or the bot.

Reference: docs/COMING_SOON.md
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from dreamxbotz.util.movie_titles import (
    MAX_TITLE_LENGTH,
    clean_title_text,
    movie_id_for,
    sanitize_movie_id,
    truncate,
    utcnow,
)

logger = logging.getLogger(__name__)

UPCOMING_URL = "https://api.themoviedb.org/3/movie/upcoming"
#: Posters are proxied through our own origin (``/api/movies/upcoming/poster/…``)
#: exactly like the "Newly Uploaded" rail does – the browser never talks to
#: image.tmdb.org and never sees the API key.
POSTER_SIZE = "w500"
DEFAULT_TIMEOUT = 8.0
USER_AGENT = "DreamXBotz-ComingSoon/1.0 (+https://t.me)"

__all__ = [
    "countdown_parts",
    "countdown_label",
    "days_until",
    "is_enabled",
    "notify_key_for",
    "parse_release_date",
    "public_upcoming_movie",
    "public_upcoming_movies",
    "refresh_if_stale",
    "release_cutoff",
    "release_date_label",
    "seconds_until",
    "upcoming_from_payload",
    "upcoming_from_tmdb",
    "upcoming_list_text",
    "urgency",
]


def _cfg(name: str, default):
    """Read a setting from ``info.py`` without hard-failing when it is absent."""
    try:
        import info  # type: ignore

        return getattr(info, name, default)
    except Exception:
        return default


# --------------------------------------------------------------------------- #
# Configuration (all of it optional – sane defaults keep tests dependency-free)
# --------------------------------------------------------------------------- #
def is_enabled() -> bool:
    """Master switch: ``COMING_SOON`` (default ``True``)."""
    return bool(_cfg("COMING_SOON", True))


def limit() -> int:
    """Cards in the rail.  ``COMING_SOON_LIMIT`` (default 12, hard cap 24)."""
    try:
        return max(1, min(int(_cfg("COMING_SOON_LIMIT", 12)), 24))
    except (TypeError, ValueError):
        return 12


def refresh_ttl() -> int:
    """Seconds between two upstream refreshes.  ``COMING_SOON_REFRESH_HOURS``."""
    try:
        hours = max(1, min(int(_cfg("COMING_SOON_REFRESH_HOURS", 6)), 168))
    except (TypeError, ValueError):
        hours = 6
    return hours * 3600


def grace_days() -> int:
    """Days a released movie stays in the rail ("now released").

    Uploads normally land a day or two after the release day, so a movie that
    *just* came out is the most interesting card in the whole section – it is
    the one about to appear in "Newly Uploaded".  ``COMING_SOON_GRACE_DAYS``.
    """
    try:
        return max(0, min(int(_cfg("COMING_SOON_GRACE_DAYS", 3)), 30))
    except (TypeError, ValueError):
        return 3


def soon_days() -> int:
    """Within how many days a card gets the gold "releasing soon" chip."""
    try:
        return max(0, min(int(_cfg("COMING_SOON_SOON_DAYS", 7)), 60))
    except (TypeError, ValueError):
        return 7


def cache_ttl() -> int:
    """Browser cache for ``/api/movies/upcoming`` (seconds)."""
    try:
        return max(0, min(int(_cfg("COMING_SOON_CACHE_TTL", 600)), 86400))
    except (TypeError, ValueError):
        return 600


# --------------------------------------------------------------------------- #
# Pure date / countdown helpers (no I/O – unit tested)
# --------------------------------------------------------------------------- #
def _as_utc(value) -> Optional[datetime]:
    """Any datetime/date/ISO string -> aware UTC datetime (or ``None``)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        moment = parse_release_date(value)
        if moment is None:
            try:  # full ISO timestamps also work
                moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
    else:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def parse_release_date(value) -> Optional[datetime]:
    """``"2026-10-05"`` -> midnight UTC (``None`` when unusable).

    Upstream sends a plain ``YYYY-MM-DD`` string; some sources send ``""`` or
    ``"0000-00-00"`` for an unannounced date, which must not crash the rail.
    """
    if isinstance(value, (datetime, date)):
        return _as_utc(value)
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        parts = text.split("-")
        if len(parts) != 3:
            return None
        year, month, day = (int(p) for p in parts)
        if not (1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 2200):
            return None
        return datetime(year, month, day, tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def days_until(release, now=None) -> Optional[int]:
    """Whole days between today and the release day.

    ``0`` means "releases today", ``-2`` means "released 2 days ago" (still
    shown while inside the grace window).  Calendar days, not 24-hour
    intervals – that is what a visitor means by "in 3 days".
    """
    moment = _as_utc(release)
    if moment is None:
        return None
    reference = _as_utc(now) or utcnow()
    return (moment.date() - reference.date()).days


def countdown_parts(seconds: float) -> Dict[str, int]:
    """Seconds -> ``{"days","hours","minutes","seconds"}`` (never negative).

    Split out from the label so the JavaScript ticker and the server-side
    rendering share one definition of the breakdown.
    """
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        total = 0
    return {
        "days": total // 86400,
        "hours": (total // 3600) % 24,
        "minutes": (total // 60) % 60,
        "seconds": total % 60,
    }


def seconds_until(release, now=None) -> Optional[int]:
    """Exact seconds left until midnight of the release day (0 once passed)."""
    moment = _as_utc(release)
    if moment is None:
        return None
    reference = _as_utc(now) or utcnow()
    return max(0, int((moment - reference).total_seconds()))


def urgency(release, now=None, soon=None, grace=None) -> str:
    """Which chip a card wears: ``released`` / ``soon`` / ``upcoming`` / ``""``."""
    left = days_until(release, now)
    if left is None:
        return ""
    soon = soon_days() if soon is None else soon
    grace = grace_days() if grace is None else grace
    if left < 0:
        return "released" if left >= -grace else ""
    if left <= soon:
        return "soon"
    return "upcoming"


def release_date_label(release) -> str:
    """``"05 Oct 2026"`` – the day the card promises."""
    moment = _as_utc(release)
    if moment is None:
        return ""
    return moment.strftime("%d %b %Y")


def countdown_label(release, now=None) -> str:
    """Human label rendered server side: ``"in 12 days"`` / ``"today"`` / …"""
    left = days_until(release, now)
    if left is None:
        return ""
    if left < -1:
        return f"released {abs(left)} days ago"
    if left == -1:
        return "released yesterday"
    if left == 0:
        return "releases today"
    if left == 1:
        return "releases tomorrow"
    return f"in {left} days"


def release_cutoff(now=None, grace=None) -> Optional[datetime]:
    """Oldest release day still worth showing (drives ``purge_old``/queries)."""
    grace = grace_days() if grace is None else grace
    reference = _as_utc(now) or utcnow()
    return datetime(
        reference.year, reference.month, reference.day, tzinfo=timezone.utc
    ) - timedelta(days=int(grace))


# --------------------------------------------------------------------------- #
# Upstream -> rows
# --------------------------------------------------------------------------- #
def _poster(path) -> Optional[str]:
    if not path or not isinstance(path, str):
        return None
    from dreamxbotz.util.tmdb_direct import IMAGE_BASE

    return f"{IMAGE_BASE}{POSTER_SIZE}{path if path.startswith('/') else '/' + path}"


def upcoming_from_payload(payload, now=None) -> List[Dict[str, Any]]:
    """Turn a TMDB ``/movie/upcoming`` body into ``save_movies()`` rows.

    Pure: no network, no database – so it can be unit tested with a canned
    payload.  Rows without a usable title **or** without a release date are
    dropped (a countdown needs a date to count down to).
    """
    results = (payload or {}).get("results")
    if not isinstance(results, list):
        return []

    moment = _as_utc(now) or utcnow()
    rows: List[Dict[str, Any]] = []
    seen = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        title = clean_title_text(
            item.get("title") or item.get("original_title") or "",
            limit=MAX_TITLE_LENGTH,
        )
        raw_date = str(item.get("release_date") or "").strip()[:10]
        release = parse_release_date(raw_date)
        if not title or release is None:
            continue
        # A release further back than "now - grace" is not coming any more.
        if release < release_cutoff(moment):
            continue

        year = release.year
        movie_id = movie_id_for(title, year)
        if not movie_id or movie_id in seen:
            continue
        seen.add(movie_id)

        try:
            popularity = float(item.get("popularity") or 0.0)
        except (TypeError, ValueError):
            popularity = 0.0
        try:
            tmdb_id = int(item.get("id"))
        except (TypeError, ValueError):
            tmdb_id = None

        rows.append(
            {
                "title": title,
                "year": year,
                "release_date": release,
                "release_date_raw": raw_date,
                "poster_url": _poster(item.get("poster_path")),
                "poster_source": "tmdb" if item.get("poster_path") else None,
                "overview": truncate(str(item.get("overview") or "").strip(), 300) or None,
                "tmdb_id": tmdb_id,
                "popularity": popularity,
            }
        )
    return rows


async def upcoming_from_tmdb(api_key: str = "", session=None, timeout: float = DEFAULT_TIMEOUT,
                             page: int = 1) -> List[Dict[str, Any]]:
    """One page of TMDB's upcoming releases as ``save_movies()`` rows.

    Returns ``[]`` when there is no usable key, the request fails, or the body
    is empty – the caller then simply keeps serving the cached rail.
    """
    try:
        from dreamxbotz.util.tmdb_direct import auth_for, key_problem
    except Exception as exc:  # pragma: no cover - defensive
        logger.info("Coming soon: TMDB helpers unavailable: %s", exc)
        return []

    key = api_key or str(_cfg("TMDB_API_KEY", "") or "")
    if key_problem(key):
        logger.info("Coming soon: no usable TMDB_API_KEY – skipping the refresh.")
        return []
    params, headers = auth_for(key)
    if not params and not headers:
        return []

    from aiohttp import ClientSession, ClientTimeout

    headers = dict(headers)
    headers.setdefault("Accept", "application/json")
    headers.setdefault("User-Agent", USER_AGENT)

    own_session = session is None
    if own_session:
        session = ClientSession(timeout=ClientTimeout(total=max(2.0, float(timeout))))
    try:
        request_params = dict(params)
        request_params.update(
            {"language": "en-US", "page": str(max(1, int(page or 1))), "region": ""}
        )
        try:
            async with session.get(UPCOMING_URL, params=request_params,
                                   headers=headers) as response:
                if response.status == 401:
                    logger.warning("Coming soon: TMDB rejected the API key (401).")
                    return []
                if response.status != 200:
                    logger.info("Coming soon: TMDB answered HTTP %s.", response.status)
                    return []
                payload = await response.json(content_type=None)
        except Exception as exc:
            logger.info("Coming soon: TMDB request failed: %s", exc)
            return []
    finally:
        if own_session:
            await session.close()

    return upcoming_from_payload(payload)


async def refresh_if_stale(store=None, *, force: bool = False, api_key: str = "") -> int:
    """Refresh the cache when it is older than ``COMING_SOON_REFRESH_HOURS``.

    Best effort and safe to call on every request: the staleness check is one
    tiny ``find_one``, and a failed upstream call leaves the old rows in place.
    Returns how many rows were written (0 when skipped or failed).
    """
    if not is_enabled():
        return 0
    if store is None:
        from database.upcoming_db import UpcomingMoviesStore

        store = UpcomingMoviesStore()
    try:
        if not force and not await store.is_stale(refresh_ttl()):
            return 0
    except Exception as exc:
        logger.info("Coming soon: staleness check failed: %s", exc)
        return 0

    rows = await upcoming_from_tmdb(api_key=api_key)
    if not rows:
        return 0
    try:
        saved = await store.save_movies(rows)
        # Housekeeping so the collection cannot grow without bound.
        await store.purge_old(release_cutoff() - timedelta(days=30))
        await store.mark_fetched(saved)
        if saved:
            logger.info("Coming soon: refreshed %d upcoming release(s).", saved)
        return saved
    except Exception as exc:
        logger.warning("Coming soon: refresh failed: %s", exc)
        return 0


# --------------------------------------------------------------------------- #
# Public JSON shape
# --------------------------------------------------------------------------- #
def public_upcoming_movie(doc: Dict[str, Any], bot_username: str = "", now=None) -> Dict[str, Any]:
    """Database document -> the whitelisted JSON the browser may see.

    Anything not copied here never reaches the browser, so a leaked upstream
    poster URL or API key is impossible by construction – the poster is served
    as bytes from our own origin instead.
    """
    from dreamxbotz.util.movie_titles import build_deeplink

    movie_id = sanitize_movie_id(doc.get("_id") or doc.get("id") or "")
    title = clean_title_text(doc.get("title") or "", limit=MAX_TITLE_LENGTH)
    release = _as_utc(doc.get("release_date"))
    moment = _as_utc(now) or utcnow()

    year = doc.get("year")
    try:
        year = int(year) if year else (release.year if release else None)
    except (TypeError, ValueError):
        year = release.year if release else None

    left = days_until(release, moment)
    state = urgency(release, moment)
    remaining = seconds_until(release, moment)
    try:
        waiting = int(doc.get("notify_total") or 0)
    except (TypeError, ValueError):
        waiting = 0

    return {
        "id": movie_id,
        "title": title,
        "year": year,
        "release_date": release.isoformat().replace("+00:00", "Z") if release else None,
        "release_label": release_date_label(release),
        "days_left": left,
        "seconds_left": remaining,
        "countdown": countdown_label(release, moment),
        "state": state,
        "has_poster": bool(doc.get("poster_url")),
        "poster": f"/api/movies/upcoming/poster/{movie_id}" if movie_id else "",
        "waiting": waiting,
        "deeplink": build_deeplink(bot_username, movie_id),
    }


def public_upcoming_movies(docs, bot_username: str = "", now=None) -> List[Dict[str, Any]]:
    """Whitelist a whole page; documents with no usable id are dropped."""
    moment = _as_utc(now) or utcnow()
    return [
        movie
        for movie in (public_upcoming_movie(doc, bot_username, moment) for doc in docs or [])
        if movie.get("id")
    ]


# --------------------------------------------------------------------------- #
# /comingsoon (bot) text
# --------------------------------------------------------------------------- #
#: Telegram has no number emoji past ten, so the rest falls back to "11.".
_NUMBER_EMOJI = (
    "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣",
    "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟",
)


def _bullet(index: int) -> str:
    """Rank marker for one line of the list."""
    return _NUMBER_EMOJI[index] if index < len(_NUMBER_EMOJI) else f"<b>{index + 1}.</b>"


def notify_key_for(movie_id: str) -> str:
    """Short, stable callback key for one movie's 🔔 button.

    Telegram caps ``callback_data`` at 64 bytes and a ``MOVIE_ID`` can already
    be 64 characters on its own, so the id is hashed.  Hashing (rather than a
    counter) keeps the key **stable**: the same movie always maps to the same
    key, so repeated ``/comingsoon`` calls overwrite one ``PENDING_TEXT`` entry
    instead of adding a new one every time.
    """
    import hashlib

    digest = hashlib.md5(str(movie_id or "").encode("utf-8")).hexdigest()[:10]
    return f"cs-{digest}"


#: notify key -> ``MOVIE_ID``.  Lets a 🔔 tap be attributed back to the card
#: whose "🔥 N waiting" badge it should raise.  In-memory on purpose: it has
#: exactly the lifetime of ``title_notify.PENDING_TEXT`` (the title it keys),
#: so after a restart the tap still registers the alert – it just cannot be
#: counted against a card any more.
NOTIFY_MOVIE_IDS: Dict[str, str] = {}


def remember_upcoming(movie_id: str, title: str) -> str:
    """Remember the title behind a ``/comingsoon`` 🔔 button.

    Stores the search text where ``title_notify`` expects it (that is what
    fires the alert when a file is indexed) *and* the card id, so the notify
    tap can also raise the "waiting" counter.  Returns the callback key.
    """
    from dreamxbotz.util.title_notify import remember_search

    key = notify_key_for(movie_id)
    remember_search(key, title)
    if movie_id:
        NOTIFY_MOVIE_IDS[key] = str(movie_id)
    return key


def upcoming_movie_id_for(key) -> str:
    """``MOVIE_ID`` behind a ``/comingsoon`` notify key (``""`` when unknown)."""
    return NOTIFY_MOVIE_IDS.get(str(key or ""), "")


def upcoming_list_text(docs, now=None, limit: int = 10) -> str:
    """HTML text of the ``/comingsoon`` list.

    Pure – no database, no pyrogram – so the exact wording a user sees can be
    unit tested.  Returns ``""`` when there is nothing to show, and the caller
    decides what to say instead.
    """
    moment = _as_utc(now) or utcnow()
    movies = public_upcoming_movies(docs, "", moment)[: max(1, int(limit))]
    if not movies:
        return ""

    lines = [
        f"<b>⏳ Cᴏᴍɪɴɢ ꜱᴏᴏɴ</b> — ᴛʜᴇ ɴᴇxᴛ {len(movies)} ʀᴇʟᴇᴀꜱᴇꜱ",
        "",
    ]
    for index, movie in enumerate(movies):
        year = f" ({movie['year']})" if movie.get("year") else ""
        bits = [movie["release_label"]] if movie.get("release_label") else []
        if movie.get("countdown"):
            bits.append(movie["countdown"])
        if movie.get("waiting"):
            bits.append(f"🔥 {movie['waiting']} waiting")
        meta = " · ".join(bit for bit in bits if bit)
        lines.append(f"{_bullet(index)} <b>{movie['title']}</b>{year}")
        if meta:
            lines.append(f"    📅 {meta}")
    lines += [
        "",
        "ᴛᴀᴘ <b>🔔 ɴᴏᴛɪꜰʏ ᴍᴇ</b> ᴏɴ ᴀ ᴛɪᴛʟᴇ ᴀɴᴅ ɪ'ʟʟ ᴘᴍ ʏᴏᴜ ᴛʜᴇ ᴍᴏᴍᴇɴᴛ ɪᴛ'ꜱ ᴜᴘʟᴏᴀᴅᴇᴅ.",
    ]
    return "\n".join(lines)

