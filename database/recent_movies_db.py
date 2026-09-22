"""MongoDB storage for the Stream Mode "Newly Uploaded Movies" section.

Collection: ``NEW_UPLOADED_COLLECTION`` (default ``recent_movies``) inside the
bot's own ``DATABASE_NAME`` – i.e. the same ``DATABASE_URI``
(``DATABASE_CONNECTION``) the rest of the bot already uses, so no extra
database has to be provisioned.

Document schema
---------------
==================== ========= ===============================================
field                type      notes
==================== ========= ===============================================
``_id``              str       ``MOVIE_ID`` – deterministic ``slug``/``slug-year``
``title``            str       clean movie title shown on the card
``title_key``        str       ``"title year"`` de-duplication key
``year``             int|None  release year (when known)
``search_query``     str       what the bot searches for ``/start movie_<id>``
``qualities``        list[str] every quality seen for this movie
``poster_url``       str|None  upstream poster (TMDB/IMDb) – never given to the browser
``poster_source``    str|None  ``"tmdb"`` / ``"imdb"``
``poster_checked_at``datetime  last poster lookup attempt (avoids re-hammering APIs)
``file_ids``         list[str] **internal** bot file ids, capped, never exposed
``file_names``       list[str] **internal** original names, capped, never exposed
``file_total``       int       number of distinct files registered
``uploaded_at``      datetime  first time this movie appeared
``last_upload_at``   datetime  newest file of this movie (sort key, "newest first")
``updated_at``       datetime  last write
==================== ========= ===============================================
"""
import logging
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError

from dreamxbotz.util.movie_titles import (
    MAX_STORED_NAME_LENGTH,
    canonical_quality,
    movie_id_for,
    normalize_title,
    sanitize_movie_id,
    truncate,
    utcnow,
)

logger = logging.getLogger(__name__)

#: Safety rails – the arrays are internal caches, not the source of truth.
MAX_TRACKED_FILES = 40
#: The web section shows the newest 20 movies; keep a little history around.
DEFAULT_MAX_MOVIES = 500
#: Newest-first sorting: ``last_upload_at`` first, then the initial upload date.
SORT = [("last_upload_at", -1), ("uploaded_at", -1), ("title", 1)]
#: Never load internal fields into memory when serving the public API.
INTERNAL_FIELDS = {"file_ids": 0, "file_names": 0}


class RecentMoviesStore:
    """Thin, testable wrapper around the ``recent_movies`` collection."""

    def __init__(self, database=None, collection=None, collection_name: Optional[str] = None):
        self._database = database
        self._collection = collection
        self._collection_name = collection_name
        self._indexes_ready = False

    # ------------------------------------------------------------------ #
    # Connection helpers
    # ------------------------------------------------------------------ #
    @property
    def collection_name(self) -> str:
        if self._collection_name:
            return self._collection_name
        try:  # imported lazily so the module stays import-safe without info.py
            from info import NEW_UPLOADED_COLLECTION  # type: ignore

            return NEW_UPLOADED_COLLECTION
        except Exception:
            return "recent_movies"

    @property
    def col(self):
        """The motor collection (memoised)."""
        if self._collection is None:
            if self._database is None:
                self._database = _default_database()
            self._collection = self._database[self.collection_name]
        return self._collection

    async def ensure_indexes(self) -> None:
        """Create the indexes used for "newest first" and de-duplication."""
        if self._indexes_ready:
            return
        try:
            await self.col.create_index(
                [("last_upload_at", -1)], name="nu_last_upload", background=True
            )
            await self.col.create_index("title_key", name="nu_title_key", background=True)
            await self.col.create_index("updated_at", name="nu_updated", background=True)
            self._indexes_ready = True
            logger.info(
                "Newly-uploaded movies: indexes ready on '%s'.", self.collection_name
            )
        except Exception as exc:  # never break bot startup because of an index
            logger.warning("Newly-uploaded movies: could not create indexes: %s", exc)

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    async def register_upload(
        self,
        *,
        title: str,
        year: Optional[int] = None,
        quality: Optional[str] = None,
        file_id: Optional[str] = None,
        file_name: Optional[str] = None,
        poster_url: Optional[str] = None,
        poster_source: Optional[str] = None,
        now=None,
    ) -> Optional[str]:
        """Upsert one uploaded file and return the movie's ``MOVIE_ID``.

        De-duplication happens on three levels:

        1. ``_id`` is *derived* from the title (+ year), so the same movie can
           never create two documents.
        2. A year-less document is adopted when the year becomes known
           (``Jawan`` and ``Jawan 2023`` collapse into one entry).
        3. ``file_ids``/``qualities`` are ``$addToSet``-ed, so re-indexing the
           same file changes nothing but the timestamps.
        """
        title, year, title_key = normalize_title(title, year)
        if not title:
            return None

        movie_id = movie_id_for(title, year)
        if year is not None:
            bare_id = movie_id_for(title, None)
            if bare_id and bare_id != movie_id and await self._exists(bare_id):
                # Reuse the entry created before we learned the release year.
                movie_id = bare_id

        moment = now or utcnow()
        if getattr(moment, "tzinfo", None) is None:  # keep stored values aware
            from datetime import timezone

            moment = moment.replace(tzinfo=timezone.utc)

        quality = canonical_quality(quality)
        search_parts = [title, str(year)] if year else [title]
        fields: Dict[str, Any] = {
            "title": title,
            "title_key": title_key,
            "year": int(year) if year else None,
            "search_query": " ".join(part for part in search_parts if part),
            "last_upload_at": moment,
            "updated_at": moment,
        }
        if poster_url:
            fields["poster_url"] = truncate(poster_url, 600)
            fields["poster_source"] = truncate(poster_source or "unknown", 24)
            fields["poster_checked_at"] = moment

        set_on_insert = {"uploaded_at": moment, "file_total": 0}
        add_to_set: Dict[str, Any] = {}
        if quality:
            add_to_set["qualities"] = quality
        if file_name:
            add_to_set["file_names"] = truncate(file_name, MAX_STORED_NAME_LENGTH)

        increments = {"file_total": 0}
        if file_id:
            existing = await self._internal_field(movie_id, "file_ids")
            tracked = existing or []
            if file_id not in tracked and len(tracked) < MAX_TRACKED_FILES:
                add_to_set["file_ids"] = file_id
                increments["file_total"] = 1

        update: Dict[str, Any] = {"$set": fields, "$setOnInsert": set_on_insert}
        if add_to_set:
            update["$addToSet"] = add_to_set
        update["$inc"] = increments

        try:
            await self.col.update_one({"_id": movie_id}, update, upsert=True)
        except DuplicateKeyError:
            # Lost an upsert race with a parallel worker – the document exists.
            await self.col.update_one({"_id": movie_id}, update)
        return movie_id

    async def set_poster(
        self,
        movie_id: str,
        poster_url: Optional[str],
        source: Optional[str] = None,
        *,
        checked: bool = True,
    ) -> bool:
        """Attach (or clear) the poster of a movie."""
        safe_id = sanitize_movie_id(movie_id)
        if not safe_id:
            return False
        moment = utcnow()
        fields: Dict[str, Any] = {"updated_at": moment}
        if checked:
            fields["poster_checked_at"] = moment
        if poster_url:
            fields["poster_url"] = truncate(poster_url, 600)
            fields["poster_source"] = truncate(source or "unknown", 24)
        else:
            fields["poster_url"] = None
        result = await self.col.update_one({"_id": safe_id}, {"$set": fields})
        return bool(getattr(result, "matched_count", 0))

    async def clear_poster(self, movie_id: str) -> bool:
        """Remove the poster and forget the last lookup (``/delposter``).

        The placeholder shows again and the poster worker may look the movie
        up afresh on the next run.
        """
        safe_id = sanitize_movie_id(movie_id)
        if not safe_id:
            return False
        result = await self.col.update_one(
            {"_id": safe_id},
            {
                "$set": {
                    "poster_url": None,
                    "poster_source": None,
                    "poster_checked_at": None,
                    "updated_at": utcnow(),
                }
            },
        )
        return bool(getattr(result, "matched_count", 0))

    async def trim(self, keep: int = DEFAULT_MAX_MOVIES) -> int:
        """Delete everything but the newest ``keep`` movies (housekeeping)."""
        try:
            keep = max(20, int(keep))
            cursor = self.col.find({}, {"_id": 1}).sort(SORT).skip(keep)
            stale = [doc["_id"] async for doc in cursor]
            if not stale:
                return 0
            result = await self.col.delete_many({"_id": {"$in": stale}})
            removed = int(getattr(result, "deleted_count", 0))
            if removed:
                logger.info("Newly-uploaded movies: trimmed %s old entries.", removed)
            return removed
        except Exception as exc:
            logger.warning("Newly-uploaded movies: trim failed: %s", exc)
            return 0

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    async def list_recent(
        self, limit: int = 20, *, raise_on_error: bool = False, hard_limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Newest uploads first, internal fields stripped.

        ``raise_on_error`` is used by the HTTP API so a database outage is
        reported as an error state instead of a misleading "no movies yet".
        ``hard_limit`` defaults to the 20 cards the website shows; the poster
        worker raises it to pre-fetch artwork for a few more entries.
        """
        try:
            limit = max(1, min(int(limit), max(1, int(hard_limit))))
        except (TypeError, ValueError):
            limit = 20
        try:
            cursor = self.col.find({}, INTERNAL_FIELDS).sort(SORT).limit(limit)
            return [doc async for doc in cursor]
        except Exception as exc:
            logger.error("Newly-uploaded movies: list failed: %s", exc)
            if raise_on_error:
                raise
            return []

    async def get(self, movie_id: str) -> Optional[Dict[str, Any]]:
        """One movie by ``MOVIE_ID`` (internal fields stripped)."""
        safe_id = sanitize_movie_id(movie_id)
        if not safe_id:
            return None
        try:
            return await self.col.find_one({"_id": safe_id}, INTERNAL_FIELDS)
        except Exception as exc:
            logger.error("Newly-uploaded movies: lookup failed: %s", exc)
            return None

    async def count(self) -> int:
        try:
            return int(await self.col.count_documents({}))
        except Exception:
            return 0

    async def missing_posters(self, limit: int = 60) -> List[Dict[str, Any]]:
        """Newest movies that still have no poster (diagnostics / retry)."""
        try:
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            limit = 60
        try:
            cursor = (
                self.col.find({"poster_url": {"$in": [None, ""]}}, INTERNAL_FIELDS)
                .sort(SORT)
                .limit(limit)
            )
            rows = [doc async for doc in cursor]
        except Exception as exc:
            logger.error("Newly-uploaded movies: missing-poster query failed: %s", exc)
            return []
        # Older documents may simply lack the field.
        if not rows:
            try:
                cursor = self.col.find({"poster_url": {"$exists": False}}, INTERNAL_FIELDS).sort(SORT).limit(limit)
                rows = [doc async for doc in cursor]
            except Exception:
                rows = []
        return rows

    async def find_by_title(self, text: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Resolve what an admin typed – a ``MOVIE_ID`` or a title – to documents.

        Order of attempts: exact ``_id`` → id derived from the text (with and
        without the year) → case-insensitive ``title_key`` search.
        """
        text = str(text or "").strip()
        if not text:
            return []
        seen: List[Dict[str, Any]] = []

        async def _add(doc):
            if doc and all(doc.get("_id") != other.get("_id") for other in seen):
                seen.append(doc)

        safe_id = sanitize_movie_id(text)
        if safe_id:
            await _add(await self.get(safe_id))
        title, year, title_key = normalize_title(text, None)
        if title:
            for candidate in (movie_id_for(title, year), movie_id_for(title, None)):
                if candidate and candidate != safe_id:
                    await _add(await self.get(candidate))
        if seen:
            return seen[:limit]
        if not title_key:
            return []
        try:
            import re as _re

            pattern = ".*".join(_re.escape(part) for part in title_key.split()[:6])
            cursor = (
                self.col.find({"title_key": {"$regex": pattern, "$options": "i"}}, INTERNAL_FIELDS)
                .sort(SORT)
                .limit(max(1, int(limit)))
            )
            return [doc async for doc in cursor]
        except Exception as exc:
            logger.error("Newly-uploaded movies: title search failed: %s", exc)
            return []

    async def reset_poster_checks(self, limit: int = 200) -> int:
        """Forget failed poster lookups so the worker retries them right away.

        Used by ``/posters retry`` – typically after ``TMDB_API_KEY`` was added.
        Returns the number of movies that will be looked up again.
        """
        rows = await self.missing_posters(limit)
        ids = [doc.get("_id") for doc in rows if doc.get("_id")]
        if not ids:
            return 0
        try:
            await self.col.update_many(
                {"_id": {"$in": ids}}, {"$set": {"poster_checked_at": None}}
            )
        except Exception as exc:
            logger.error("Newly-uploaded movies: reset of poster checks failed: %s", exc)
            return 0
        return len(ids)

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #
    async def _exists(self, movie_id: str) -> bool:
        try:
            return bool(await self.col.find_one({"_id": movie_id}, {"_id": 1}))
        except Exception:
            return False

    async def _internal_field(self, movie_id: str, field: str) -> Optional[list]:
        try:
            doc = await self.col.find_one({"_id": movie_id}, {field: 1})
        except Exception:
            return None
        if not doc:
            return None
        value = doc.get(field)
        return list(value) if value else []


def _default_database():
    """Motor database built from the bot's existing ``DATABASE_URI``."""
    from info import DATABASE_NAME, DATABASE_URI  # type: ignore

    client = AsyncIOMotorClient(DATABASE_URI)
    return client[DATABASE_NAME]


#: Shared instance used by the bot, the API handlers and the deep-link flow.
recent_movies = RecentMoviesStore()
