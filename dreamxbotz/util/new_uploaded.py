"""Newly-uploaded-movie tracking + poster worker (Stream Mode web section).

This module is the single funnel between the indexing pipeline and the
``recent_movies`` collection that powers the website section.

Flow
----
::

    plugins/channel.py (auto-index)  ┐
    plugins/index.py  (bulk index)   ├─► database.ia_filterdb.save_file()
    admin /index requests            ┘            │
                                                  ▼
                                   new_uploaded.notify_new_file()   (non-blocking)
                                                  │  asyncio.Queue
                                                  ▼
                                       worker → recent_movies.register_upload()
                                                  │
                                                  ▼
                                    poster worker → TMDB / IMDb poster lookup
                                                  │
                                                  ▼
                              GET /api/movies/new  ──►  "Newly Uploaded Movies"

Everything here is **best effort**: any failure is logged and swallowed so the
indexing pipeline can never be slowed down or broken by this feature.
"""
import asyncio
import logging
import re
from datetime import timedelta
from typing import Any, Dict, Optional

from dreamxbotz.util.movie_titles import (
    MAX_STORED_NAME_LENGTH,
    as_utc,
    looks_like_series,
    looks_like_video,
    parse_release_name,
    sanitize_movie_id,
    truncate,
    utcnow,
)

logger = logging.getLogger(__name__)

#: Bounded queues keep a huge bulk index from eating all the RAM.
UPLOAD_QUEUE_SIZE = 5000
POSTER_QUEUE_SIZE = 200
#: Seconds between two poster lookups (upstream APIs are rate limited).
POSTER_POLL_INTERVAL = 1.0


def _cfg(name: str, default):
    """Read a setting from ``info.py`` without hard-failing when it is absent."""
    try:
        import info  # type: ignore

        return getattr(info, name, default)
    except Exception:
        return default


def is_enabled() -> bool:
    """Master switch: ``NEW_UPLOADED_MOVIES`` (default ``True``)."""
    return bool(_cfg("NEW_UPLOADED_MOVIES", True))


def _limit() -> int:
    try:
        return max(1, min(int(_cfg("NEW_UPLOADED_LIMIT", 20)), 20))
    except (TypeError, ValueError):
        return 20


def _poster_fetch_enabled() -> bool:
    return bool(_cfg("NEW_UPLOADED_POSTER_FETCH", True))


def _poster_retry_hours() -> int:
    try:
        return max(1, int(_cfg("NEW_UPLOADED_POSTER_RETRY_HOURS", 48)))
    except (TypeError, ValueError):
        return 48


def _poster_timeout() -> float:
    try:
        return max(5.0, float(_cfg("NEW_UPLOADED_POSTER_TIMEOUT", 25)))
    except (TypeError, ValueError):
        return 25.0


def _poster_lookup_limit() -> int:
    """How many of the newest movies we are willing to backfill posters for."""
    try:
        return max(_limit(), min(int(_cfg("NEW_UPLOADED_POSTER_LOOKUPS", 60)), 200))
    except (TypeError, ValueError):
        return 60


# --------------------------------------------------------------------------- #
# Queues / worker state
# --------------------------------------------------------------------------- #
_UPLOAD_QUEUE: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=UPLOAD_QUEUE_SIZE)
_POSTER_QUEUE: "asyncio.Queue[str]" = asyncio.Queue(maxsize=POSTER_QUEUE_SIZE)
_WORKER_TASK: Optional[asyncio.Task] = None
_POSTER_TASK: Optional[asyncio.Task] = None
_POSTER_PENDING: set = set()
_DROPPED = 0
_PROCESSED = 0
_TRIM_EVERY = 100
_STARTED = False


def _ensure_workers() -> None:
    """Start the background consumers on the running event loop (idempotent)."""
    global _WORKER_TASK, _POSTER_TASK
    if _WORKER_TASK is None or _WORKER_TASK.done():
        _WORKER_TASK = asyncio.get_running_loop().create_task(_upload_worker())
    if _POSTER_TASK is None or _POSTER_TASK.done():
        if _poster_fetch_enabled():
            _POSTER_TASK = asyncio.get_running_loop().create_task(_poster_worker())


# --------------------------------------------------------------------------- #
# Public API used by the indexing pipeline
# --------------------------------------------------------------------------- #
def notify_new_file(
    file_name: str,
    file_id: Optional[str] = None,
    *,
    mime_type: str = "",
    file_type: str = "",
    file_size: Optional[int] = None,
) -> bool:
    """Queue one freshly indexed file for the "Newly Uploaded Movies" section.

    Called from :func:`database.ia_filterdb.save_file` right after a successful
    commit.  Returns ``True`` when the file was queued, ``False`` when the
    feature is off or the input is not a movie file.  **Never raises.**
    """
    try:
        if not is_enabled() or not file_name:
            return False
        if _cfg("NEW_UPLOADED_ONLY_MOVIES", True) and looks_like_series(file_name):
            return False
        if not looks_like_video(file_name, mime_type, file_type):
            return False

        item: Dict[str, Any] = {
            "file_name": truncate(file_name, MAX_STORED_NAME_LENGTH),
            "file_id": file_id or None,
            "file_size": file_size,
        }
        try:
            _UPLOAD_QUEUE.put_nowait(item)
        except asyncio.QueueFull:
            global _DROPPED
            _DROPPED += 1
            if _DROPPED % 200 == 1:
                logger.warning(
                    "Newly-uploaded movies: queue full, dropped %s files so far.",
                    _DROPPED,
                )
            return False
        # Start the consumers (no-op when they already run).  Outside a running
        # loop – tooling, scripts – the item simply waits in the queue until the
        # bot's loop picks it up.
        try:
            _ensure_workers()
        except RuntimeError:
            pass
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("notify_new_file failed: %s", exc)
        return False


def register_movie_update(
    base_name: str,
    *,
    year: Optional[int] = None,
    quality: Optional[str] = None,
    poster_url: Optional[str] = None,
    poster_source: Optional[str] = None,
    file_names: Optional[list] = None,
) -> bool:
    """Enrich a movie entry with data already known to the movie-update flow.

    ``plugins/channel.py`` builds ``movie_updates`` documents with a curated
    title, the year, the quality list and a TMDB/IMDb poster – feed that in so
    the web section shows the same, high-quality information.
    """
    try:
        if not is_enabled() or not base_name:
            return False
        names = [name for name in (file_names or []) if name][:40]
        # Same rule as the indexing hook: series stay out of the movies rail
        # unless the owner switches NEW_UPLOADED_ONLY_MOVIES off.
        if _cfg("NEW_UPLOADED_ONLY_MOVIES", True) and any(
            looks_like_series(name) for name in names
        ):
            return False
        item = {
            "title": truncate(base_name, MAX_STORED_NAME_LENGTH),
            "year": year,
            "quality": quality,
            "poster_url": poster_url,
            "poster_source": poster_source,
            "file_names": names,
        }
        try:
            _UPLOAD_QUEUE.put_nowait(item)
        except asyncio.QueueFull:
            return False
        try:
            _ensure_workers()
        except RuntimeError:
            pass
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("register_movie_update failed: %s", exc)
        return False


async def start_worker() -> None:
    """Prepare indexes and backfill posters for the newest movies.

    Called once from ``bot.py`` during startup (after the Mongo indexes of the
    filter DB are ensured).  Safe to call when the feature is disabled.
    """
    global _STARTED
    if _STARTED or not is_enabled():
        return
    _STARTED = True
    try:
        from database.recent_movies_db import recent_movies

        await recent_movies.ensure_indexes()
        _ensure_workers()
        task = asyncio.get_running_loop().create_task(refresh_posters())
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
    except Exception as exc:
        logger.warning("Newly-uploaded movies: worker start failed: %s", exc)


_BACKGROUND: set = set()


async def refresh_posters(limit: Optional[int] = None) -> int:
    """Queue poster lookups for the newest movies that still lack one."""
    if not _poster_fetch_enabled():
        return 0
    try:
        from database.recent_movies_db import recent_movies

        window = _poster_lookup_limit()
        rows = await recent_movies.list_recent(
            limit or window, hard_limit=max(window, _limit())
        )
        queued = 0
        for row in rows:
            if row.get("poster_url"):
                continue
            if not _poster_check_expired(row.get("poster_checked_at")):
                continue
            if _enqueue_poster(row.get("_id")):
                queued += 1
        if queued:
            logger.info("Newly-uploaded movies: %s poster lookups queued.", queued)
        return queued
    except Exception as exc:
        logger.debug("refresh_posters failed: %s", exc)
        return 0


def _poster_check_expired(checked_at, now=None) -> bool:
    """``True`` when a poster lookup may be attempted again for this movie."""
    if not checked_at:
        return True
    moment = as_utc(checked_at)
    if moment is None:
        return True
    return ((as_utc(now) or utcnow()) - moment) > timedelta(hours=_poster_retry_hours())


def _enqueue_poster(movie_id: Optional[str]) -> bool:
    safe_id = sanitize_movie_id(movie_id)
    if not safe_id or safe_id in _POSTER_PENDING:
        return False
    try:
        _POSTER_QUEUE.put_nowait(safe_id)
    except asyncio.QueueFull:
        return False
    _POSTER_PENDING.add(safe_id)
    return True


# --------------------------------------------------------------------------- #
# Workers
# --------------------------------------------------------------------------- #
async def _upload_worker() -> None:
    """Sequentially persist queued uploads (keeps Mongo writes predictable)."""
    global _PROCESSED
    logger.info("Newly-uploaded movies: upload worker started.")
    try:
        while True:
            item = await _UPLOAD_QUEUE.get()
            try:
                await _persist(item)
            except Exception as exc:
                logger.debug("Newly-uploaded movies: persist failed: %s", exc)
            finally:
                _UPLOAD_QUEUE.task_done()
            _PROCESSED += 1
            if _PROCESSED % _TRIM_EVERY == 0:
                await _maybe_trim()
    except asyncio.CancelledError:  # pragma: no cover - shutdown
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Newly-uploaded movies: upload worker stopped: %s", exc)


async def _persist(item: Dict[str, Any]) -> None:
    from database.recent_movies_db import recent_movies

    title = item.get("title")
    year = item.get("year")
    quality = item.get("quality")
    file_names = item.get("file_names") or []

    if not title:  # came from the file-indexing hook: parse the release name
        parsed = parse_release_name(item.get("file_name") or "")
        if _cfg("NEW_UPLOADED_ONLY_MOVIES", True) and parsed["is_series"]:
            return
        title = parsed["title"]
        year = parsed["year"]
        quality = quality or parsed["quality"]

    if not title:
        return

    movie_id = await recent_movies.register_upload(
        title=title,
        year=year,
        quality=quality,
        file_id=item.get("file_id"),
        file_name=(file_names[0] if file_names else item.get("file_name")),
    )
    for name in file_names[1:]:
        await recent_movies.register_upload(title=title, year=year, quality=quality, file_name=name)

    if movie_id:
        if item.get("poster_url"):
            await recent_movies.set_poster(
                movie_id, item["poster_url"], item.get("poster_source") or "movie_update"
            )
            return
        if _poster_fetch_enabled():
            _enqueue_poster(movie_id)


async def _maybe_trim() -> None:
    try:
        keep = int(_cfg("NEW_UPLOADED_MAX_MOVIES", 500))
    except (TypeError, ValueError):
        keep = 500
    if keep <= 0:
        return
    from database.recent_movies_db import recent_movies

    await recent_movies.trim(keep)


async def _poster_worker() -> None:
    """Resolve missing posters one at a time (TMDB first, then IMDb)."""
    logger.info("Newly-uploaded movies: poster worker started.")
    try:
        while True:
            movie_id = await _POSTER_QUEUE.get()
            try:
                await _resolve_and_store_poster(movie_id)
            except Exception as exc:
                logger.debug("Poster lookup failed for %s: %s", movie_id, exc)
            finally:
                _POSTER_PENDING.discard(movie_id)
                _POSTER_QUEUE.task_done()
            await asyncio.sleep(POSTER_POLL_INTERVAL)
    except asyncio.CancelledError:  # pragma: no cover - shutdown
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Newly-uploaded movies: poster worker stopped: %s", exc)


async def _resolve_and_store_poster(movie_id: str) -> None:
    from database.recent_movies_db import recent_movies

    doc = await recent_movies.get(movie_id)
    if not doc:
        return
    if doc.get("poster_url"):
        return

    title = doc.get("title") or ""
    year = doc.get("year")
    query = f"{title} {year}".strip() if year else title
    if not title:
        return

    poster_url, source = await _lookup_poster(query, title)
    await recent_movies.set_poster(movie_id, poster_url, source)
    if poster_url:
        logger.info("Newly-uploaded movies: poster found for '%s'.", title)
    else:
        logger.info("Newly-uploaded movies: no poster found for '%s'.", title)


async def _lookup_poster(query: str, title: str) -> tuple:
    """Return ``(poster_url, source)`` – never raises."""
    timeout = _poster_timeout()
    try:
        from plugins.Dreamxfutures.Imdbposter import (  # heavy imports kept lazy
            get_movie_details,
            get_movie_detailsx,
        )
    except Exception as exc:
        logger.debug("Poster helpers unavailable: %s", exc)
        return None, None

    use_tmdb = bool(_cfg("TMDB_POSTER", True))
    details: Optional[dict] = None

    if use_tmdb:
        try:
            details = await asyncio.wait_for(get_movie_detailsx(query), timeout=timeout)
        except Exception as exc:
            logger.debug("TMDB lookup failed for '%s': %s", title, exc)
        if details and details.get("error"):
            details = None

    if not (details or {}).get("poster_url"):
        try:
            details = await asyncio.wait_for(get_movie_details(title), timeout=timeout) or details
        except Exception as exc:
            logger.debug("IMDb lookup failed for '%s': %s", title, exc)

    if not details:
        return None, None
    poster = details.get("poster_url") or details.get("backdrop_url")
    if not poster or not re.match(r"^https://", str(poster)):
        return None, None
    source = "tmdb" if "tmdb" in str(poster).lower() or (use_tmdb and details.get("tmdb_url")) else "imdb"
    return str(poster), source


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
def stats() -> Dict[str, Any]:
    """Tiny snapshot used by ``/stats``-style commands and debugging."""
    return {
        "enabled": is_enabled(),
        "upload_queue": _UPLOAD_QUEUE.qsize(),
        "poster_queue": _POSTER_QUEUE.qsize(),
        "processed": _PROCESSED,
        "dropped": _DROPPED,
    }
