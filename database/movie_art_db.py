"""Poster/backdrop cache for the watch-page movie hero (Stream Mode).

Collection: ``MOVIE_ART_COLLECTION`` (default ``movie_art``) inside the bot's
own ``DATABASE_NAME`` – i.e. the same ``DATABASE_URI`` (``DATABASE_CONNECTION``)
the rest of the bot uses, so nothing extra has to be provisioned.

Why a separate collection
-------------------------
``recent_movies`` is the "Newly Uploaded Movies" rail: only the newest 20
movies, trimmed over time, and anything written there shows up in that rail.
The hero, however, is opened for *any* streamed file – including movies that
are years old.  Keeping their artwork here means:

* the rail keeps its meaning (a hero lookup never bumps an old movie into it),
* the artwork survives the rail's housekeeping,
* a failed lookup is remembered (``checked_at``) so the upstream TMDB/IMDb APIs
  are not hammered on every page view.

Document schema
---------------
=============== ========== ==================================================
field           type       notes
=============== ========== ==================================================
``_id``         str        ``MOVIE_ID`` – same deterministic id as everywhere
``title``       str        clean movie title
``year``        int|None   release year (when known)
``poster_url``  str|None   upstream 2:3 artwork – **never** sent to the browser
``backdrop_url``str|None   upstream 16:9 artwork – **never** sent to the browser
``poster_source``str|None  ``"tmdb"`` / ``"imdb"``
``checked_at``  datetime   last lookup attempt (success **or** miss)
``updated_at``  datetime   last write
=============== ========== ==================================================
"""
import logging
from typing import Any, Dict, Optional

from motor.motor_asyncio import AsyncIOMotorClient

from dreamxbotz.util.movie_titles import (
    movie_id_for,
    normalize_title,
    sanitize_movie_id,
    truncate,
    utcnow,
)

logger = logging.getLogger(__name__)

#: Longest upstream artwork URL we keep (same cap as the rail uses).
MAX_URL_LENGTH = 600
#: Never load the internal artwork URLs when only the public shape is needed.
INTERNAL_FIELDS = {"poster_url": 0, "backdrop_url": 0}


class MovieArtStore:
    """Tiny, testable wrapper around the ``movie_art`` collection."""

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
            from info import MOVIE_ART_COLLECTION  # type: ignore

            return MOVIE_ART_COLLECTION
        except Exception:
            return "movie_art"

    @property
    def col(self):
        """The motor collection (memoised)."""
        if self._collection is None:
            if self._database is None:
                self._database = _default_database()
            self._collection = self._database[self.collection_name]
        return self._collection

    async def ensure_indexes(self) -> None:
        """Create the lookup index (never fatal for bot startup)."""
        if self._indexes_ready:
            return
        try:
            await self.col.create_index("checked_at", name="ma_checked", background=True)
            self._indexes_ready = True
            logger.info("Watch hero: indexes ready on '%s'.", self.collection_name)
        except Exception as exc:
            logger.warning("Watch hero: could not create indexes: %s", exc)

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    async def set_art(
        self,
        movie_id: str,
        *,
        title: Optional[str] = None,
        year: Optional[int] = None,
        poster_url: Optional[str] = None,
        backdrop_url: Optional[str] = None,
        source: Optional[str] = None,
        now=None,
    ) -> bool:
        """Remember the artwork of one movie (``None`` values clear it).

        A stored document without any URL is still meaningful: ``checked_at``
        records that the lookup already failed, so the API will not retry it on
        every page view.
        """
        safe_id = sanitize_movie_id(movie_id)
        if not safe_id:
            return False
        moment = now or utcnow()
        fields: Dict[str, Any] = {
            "poster_url": truncate(poster_url, MAX_URL_LENGTH) if poster_url else None,
            "backdrop_url": truncate(backdrop_url, MAX_URL_LENGTH) if backdrop_url else None,
            "poster_source": truncate(source, 24) if source else None,
            "checked_at": moment,
            "updated_at": moment,
        }
        if title:
            clean, parsed_year, _ = normalize_title(title, year)
            fields["title"] = truncate(clean or title, 200)
            fields["year"] = int(parsed_year) if parsed_year else None
        try:
            await self.col.update_one({"_id": safe_id}, {"$set": fields}, upsert=True)
            return True
        except Exception as exc:
            logger.debug("Watch hero: could not store artwork for %s: %s", safe_id, exc)
            return False

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    async def get(self, movie_id: str) -> Optional[Dict[str, Any]]:
        """One record by ``MOVIE_ID`` – ``None`` when it was never looked up."""
        safe_id = sanitize_movie_id(movie_id)
        if not safe_id:
            return None
        try:
            return await self.col.find_one({"_id": safe_id})
        except Exception as exc:
            logger.debug("Watch hero: artwork lookup failed for %s: %s", safe_id, exc)
            return None

    async def count(self) -> int:
        try:
            return int(await self.col.count_documents({}))
        except Exception:
            return 0

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def movie_id(title: str, year: Optional[int] = None) -> str:
        """Convenience wrapper so callers never build ids by hand."""
        return movie_id_for(title, year)


def _default_database():
    """Motor database built from the bot's existing ``DATABASE_URI``.

    The client is created with a short server-selection timeout: the artwork
    cache sits on a **page view** path, so a database outage must degrade the
    hero quickly instead of stalling the request.
    """
    from info import DATABASE_NAME, DATABASE_URI  # type: ignore

    client = AsyncIOMotorClient(DATABASE_URI, serverSelectionTimeoutMS=2000)
    return client[DATABASE_NAME]


#: Shared instance used by the API handlers.
movie_art = MovieArtStore()
