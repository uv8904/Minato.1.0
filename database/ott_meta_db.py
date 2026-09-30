"""MongoDB cache behind the JioHotstar OTT homepage (Option A).

Collection: ``OTT_META_COLLECTION`` (default ``ott_meta``) inside the bot's own
``DATABASE_NAME`` – the same ``DATABASE_URI`` (``DATABASE_CONNECTION``) the rest
of the bot already uses, so nothing extra has to be provisioned.

Why a separate collection?  ``recent_movies`` (the "Newly Uploaded" section)
only knows what the *file name* said.  Genres, trailers, ratings and the
overview come from TMDB and are **expensive** (one HTTP round trip each), so
they are resolved once in the background and cached here:

======================= ========= =============================================
field                   type      notes
======================= ========= =============================================
``_id``                 str       ``MOVIE_ID`` (same deterministic id as everywhere)
``tmdb_id``             int|None  TMDB movie id – needed for later lookups
``genres``              list[str] TMDB genre names (``["Action", "Thriller"]``)
``genre_ids``           list[int] TMDB genre ids (language independent)
``trailer_key``         str|None  YouTube id of the best trailer
``trailer_name``        str|None  e.g. ``"Official Trailer"``
``overview``            str|None  short synopsis shown in the detail sheet
``rating``              float|None TMDB vote average (0–10)
``runtime``             int       minutes (0 = unknown)
``backdrop``            str|None  ️upstream 16:9 artwork URL (never sent to the browser as-is)
``poster``              str|None  upstream 2:3 artwork URL
``source``              str       ``"tmdb"`` (or ``"manual"`` when an admin set it)
``checked_at``          datetime  last successful/attempted lookup
``updated_at``          datetime  last write
======================= ========= =============================================

The store never raises: every method swallows database errors and returns an
empty/neutral value, because it is called from a web request path and from a
background worker that must not be able to break the bot.
"""
import logging
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

from motor.motor_asyncio import AsyncIOMotorClient

from dreamxbotz.util.movie_titles import sanitize_movie_id, utcnow

logger = logging.getLogger(__name__)

#: Fields copied from a TMDB answer (everything else is dropped).
#: ``videos``/``credits`` are never stored – only the picked trailer key.
STORED_FIELDS = (
    "tmdb_id",
    "genres",
    "genre_ids",
    "trailer_key",
    "trailer_name",
    "overview",
    "rating",
    "votes",
    "runtime",
    "languages",
    "cast",
    "backdrop",
    "poster",
    "source",
)
#: Longest strings we keep, so a hostile/odd TMDB answer cannot bloat the db.
FIELD_LIMITS = {
    "trailer_key": 24,
    "trailer_name": 120,
    "overview": 600,
    "backdrop": 600,
    "poster": 600,
    "source": 24,
}
MAX_LIST = 12
MAX_GENRES = 6


def _clean_meta(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist + trim one metadata dict before it is stored."""
    clean: Dict[str, Any] = {}
    for key in STORED_FIELDS:
        if key not in fields:
            continue
        value = fields[key]
        if isinstance(value, str):
            limit = FIELD_LIMITS.get(key, 120)
            value = value.replace("\x00", "").strip()[:limit] or None
        elif isinstance(value, (list, tuple)):
            rows = []
            for item in list(value)[:MAX_LIST]:
                if isinstance(item, (int, float)) and not isinstance(item, bool):
                    rows.append(int(item))
                elif isinstance(item, str) and item.strip():
                    rows.append(item.strip()[:40])
            value = rows
        elif isinstance(value, bool):
            value = bool(value)
        elif isinstance(value, (int, float)):
            value = value
        else:
            continue
        clean[key] = value
    if clean.get("genres"):
        clean["genres"] = list(clean["genres"])[:MAX_GENRES]
    return clean


class OttMetaStore:
    """Thin, testable wrapper around the ``ott_meta`` collection."""

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
            from info import OTT_META_COLLECTION  # type: ignore

            return OTT_META_COLLECTION
        except Exception:
            return "ott_meta"

    @property
    def col(self):
        """The motor collection (memoised)."""
        if self._collection is None:
            if self._database is None:
                self._database = _default_database()
            self._collection = self._database[self.collection_name]
        return self._collection

    async def ensure_indexes(self) -> None:
        """Index the fields the worker queries (missing/stale lookups)."""
        if self._indexes_ready:
            return
        try:
            await self.col.create_index("checked_at", name="ott_checked", background=True)
            await self.col.create_index("tmdb_id", name="ott_tmdb", background=True)
            await self.col.create_index("updated_at", name="ott_updated", background=True)
            self._indexes_ready = True
        except Exception as exc:  # never break startup because of an index
            logger.warning("OTT meta: could not create indexes: %s", exc)

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    async def upsert(self, movie_id: str, fields: Dict[str, Any], *, now=None) -> bool:
        """Store (or update) the metadata of one movie."""
        safe_id = sanitize_movie_id(movie_id)
        if not safe_id or not isinstance(fields, dict):
            return False
        moment = now or utcnow()
        update = {"$set": {**_clean_meta(fields), "checked_at": moment, "updated_at": moment}}
        if not len(update["$set"]) > 2:  # only the timestamps -> nothing to store
            return False
        try:
            await self.col.update_one({"_id": safe_id}, update, upsert=True)
            return True
        except Exception as exc:
            logger.warning("OTT meta: upsert failed for %s: %s", safe_id, exc)
            return False

    async def mark_checked_many(self, movie_ids: Iterable[str], *, now=None) -> int:
        """Remember an unsuccessful lookup so it is not retried on every render."""
        ids = [sanitize_movie_id(movie_id) for movie_id in movie_ids]
        ids = [movie_id for movie_id in ids if movie_id]
        if not ids:
            return 0
        moment = now or utcnow()
        try:
            result = await self.col.update_many(
                {"_id": {"$in": ids}},
                {"$set": {"checked_at": moment, "updated_at": moment}},
                upsert=False,
            )
            return int(getattr(result, "modified_count", 0))
        except Exception as exc:
            logger.warning("OTT meta: mark_checked failed: %s", exc)
            return 0

    async def reset_checks(self, limit: int = 200) -> int:
        """Forget failed lookups so the worker retries them (``/posters retry``-style)."""
        try:
            cursor = self.col.find(
                {"$or": [{"tmdb_id": None}, {"tmdb_id": {"$exists": False}}, {"genres": {"$in": [None, [], ""]}}]},
                {"_id": 1},
            ).limit(max(1, int(limit)))
            ids = [doc["_id"] async for doc in cursor]
        except Exception as exc:
            logger.warning("OTT meta: reset query failed: %s", exc)
            return 0
        if not ids:
            return 0
        try:
            await self.col.update_many({"_id": {"$in": ids}}, {"$set": {"checked_at": None}})
        except Exception as exc:
            logger.warning("OTT meta: reset write failed: %s", exc)
            return 0
        return len(ids)

    async def delete(self, movie_id: str) -> bool:
        safe_id = sanitize_movie_id(movie_id)
        if not safe_id:
            return False
        try:
            result = await self.col.delete_one({"_id": safe_id})
            return bool(getattr(result, "deleted_count", 0))
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    async def get(self, movie_id: str) -> Optional[Dict[str, Any]]:
        """One movie's metadata (``None`` when unknown)."""
        safe_id = sanitize_movie_id(movie_id)
        if not safe_id:
            return None
        try:
            return await self.col.find_one({"_id": safe_id})
        except Exception as exc:
            logger.debug("OTT meta: lookup failed for %s: %s", safe_id, exc)
            return None

    async def get_many(self, movie_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """``{MOVIE_ID: meta}`` for a page of movies – one query, not N."""
        ids = []
        for movie_id in movie_ids or []:
            safe_id = sanitize_movie_id(movie_id)
            if safe_id and safe_id not in ids:
                ids.append(safe_id)
        if not ids:
            return {}
        try:
            cursor = self.col.find({"_id": {"$in": ids}})
            rows = [doc async for doc in cursor]
        except Exception as exc:
            logger.debug("OTT meta: bulk lookup failed: %s", exc)
            return {}
        return {doc["_id"]: doc for doc in rows if doc.get("_id")}

    async def count(self) -> int:
        try:
            return int(await self.col.count_documents({}))
        except Exception:
            return 0

    def is_stale(self, meta: Optional[Dict[str, Any]], retry_hours: int = 72, *, now=None) -> bool:
        """Should this movie be looked up again?

        ``True`` when there is no metadata, no TMDB id (a failed search is worth
        one more try after the retry window) or the entry is older than
        ``retry_hours``.
        """
        if not meta:
            return True
        moment = now or utcnow()
        checked = meta.get("checked_at")
        if checked is None:
            return True
        try:
            if getattr(checked, "tzinfo", None) is None:
                from datetime import timezone

                checked = checked.replace(tzinfo=timezone.utc)
            return checked < moment - timedelta(hours=max(1, int(retry_hours)))
        except Exception:
            return True

    async def stats(self) -> Dict[str, Any]:
        """Small diagnostics dict for the admin dashboard / ``/stats``."""
        try:
            total = await self.count()
            with_genres = await self.col.count_documents({"genres": {"$exists": True, "$ne": []}})
            with_trailer = await self.col.count_documents({"trailer_key": {"$nin": [None, ""]}})
            return {
                "collection": self.collection_name,
                "movies": total,
                "with_genres": with_genres,
                "with_trailer": with_trailer,
            }
        except Exception as exc:
            return {"collection": self.collection_name, "error": str(exc)}


def _default_database():
    """Motor database built from the bot's existing ``DATABASE_URI``."""
    from info import DATABASE_NAME, DATABASE_URI  # type: ignore

    client = AsyncIOMotorClient(DATABASE_URI)
    return client[DATABASE_NAME]


#: Shared instance used by the catalog, the API handlers and the worker.
ott_meta = OttMetaStore()
