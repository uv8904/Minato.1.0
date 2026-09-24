"""MongoDB storage for the Stream Mode "Coming Soon" section.

Collection: ``COMING_SOON_COLLECTION`` (default ``upcoming_movies``) inside the
bot's own ``DATABASE_NAME`` – i.e. the same ``DATABASE_URI``
(``DATABASE_CONNECTION``) the rest of the bot already uses, so no extra
database has to be provisioned.

A second, one-document collection (``COMING_SOON_META_COLLECTION``, default
``upcoming_meta``) remembers **when** the list was last refreshed from TMDB.
Keeping the timestamp out of the movie collection means a refresh can never
accidentally show up as a movie card, and ``list_upcoming()`` stays a plain
query with no extra filter.

Document schema (``upcoming_movies``)
-------------------------------------
==================== ========= ===============================================
field                type      notes
==================== ========= ===============================================
``_id``              str       ``MOVIE_ID`` – the same deterministic id the
                               "Newly Uploaded" rail uses, so a deep link built
                               here keeps working after the movie is uploaded
``title``            str       clean movie title shown on the card
``title_key``        str       ``"title year"`` de-duplication key
``year``             int|None  release year (when known)
``release_date``     datetime  theatrical/streaming release day (00:00 UTC)
``release_date_raw`` str       ``"YYYY-MM-DD"`` exactly as upstream sent it
``poster_url``       str|None  upstream poster – **never** given to the browser
``poster_source``    str|None  ``"tmdb"`` / ``"imdb"``
``overview``         str|None  short plot blurb (capped)
``tmdb_id``          int|None  upstream id (used for the trailer/links later)
``popularity``       float     upstream popularity – tie-breaker when two
                               movies share a release day
``notify_total``     int       users waiting for this movie ("🔥 N waiting")
``fetched_at``       datetime  when this row came in from upstream
``updated_at``       datetime  last write
==================== ========= ===============================================

Meta document (``upcoming_meta``)
---------------------------------
``{"_id": "refresh", "fetched_at": <datetime>, "movies": <int>}``

Nothing here ever holds a file id or a Telegram credential: an unreleased
movie has no file, and the poster bytes are proxied through our own origin.
"""
import logging
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError

from dreamxbotz.util.movie_titles import (
    MAX_STORED_NAME_LENGTH,
    movie_id_for,
    normalize_title,
    truncate,
    utcnow,
)

logger = logging.getLogger(__name__)

#: The section shows the next 20 releases; keep a little extra so the rail can
#: be re-sorted/filtered without another upstream call.
DEFAULT_MAX_MOVIES = 120
#: Newest-first is not what we want here – the countdown sorts by date ascending.
SORT = [("release_date", 1), ("popularity", -1), ("title", 1)]
#: Fields the public API never needs, so they are not loaded at all.
#:
#: Note what is *not* here: ``poster_url``.  It must stay in the projection
#: because ``public_upcoming_movie()`` reads it to compute ``has_poster``.
#: Keeping the upstream URL away from the browser is the job of that
#: whitelisting function (which emits our own ``/api/movies/upcoming/poster/…``
#: path instead) – not of the query.  Stripping it here would silently report
#: every single movie as poster-less.
INTERNAL_FIELDS = {"overview": 0}
#: ``_id`` of the single refresh-timestamp document.
REFRESH_ID = "refresh"


def _default_database():
    """The bot's own database – the same one ``ia_filterdb`` uses."""
    from info import DATABASE_NAME, DATABASE_URI

    client = AsyncIOMotorClient(DATABASE_URI)
    return client[DATABASE_NAME]


class UpcomingMoviesStore:
    """Thin, testable wrapper around the ``upcoming_movies`` collection."""

    def __init__(self, database=None, collection=None, collection_name: Optional[str] = None,
                 meta_collection=None, meta_collection_name: Optional[str] = None):
        self._database = database
        self._collection = collection
        self._collection_name = collection_name
        self._meta_collection = meta_collection
        self._meta_collection_name = meta_collection_name
        self._indexes_ready = False

    # ------------------------------------------------------------------ #
    # Connection helpers
    # ------------------------------------------------------------------ #
    @property
    def collection_name(self) -> str:
        if self._collection_name:
            return self._collection_name
        try:  # imported lazily so the module stays import-safe without info.py
            from info import COMING_SOON_COLLECTION  # type: ignore

            return COMING_SOON_COLLECTION
        except Exception:
            return "upcoming_movies"

    @property
    def meta_collection_name(self) -> str:
        if self._meta_collection_name:
            return self._meta_collection_name
        try:
            from info import COMING_SOON_META_COLLECTION  # type: ignore

            return COMING_SOON_META_COLLECTION
        except Exception:
            return "upcoming_meta"

    @property
    def col(self):
        """The motor collection (memoised)."""
        if self._collection is None:
            if self._database is None:
                self._database = _default_database()
            self._collection = self._database[self.collection_name]
        return self._collection

    @property
    def meta(self):
        """The one-document refresh-timestamp collection (memoised)."""
        if self._meta_collection is None:
            if self._database is None:
                self._database = _default_database()
            self._meta_collection = self._database[self.meta_collection_name]
        return self._meta_collection

    async def ensure_indexes(self) -> None:
        """Create the indexes used for the countdown sort and de-duplication."""
        if self._indexes_ready:
            return
        try:
            await self.col.create_index(
                [("release_date", 1)], name="cs_release_date", background=True
            )
            await self.col.create_index("title_key", name="cs_title_key", background=True)
            self._indexes_ready = True
            logger.info("Coming soon: indexes ready on '%s'.", self.collection_name)
        except Exception as exc:  # never break bot startup because of an index
            logger.warning("Coming soon: could not create indexes: %s", exc)

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    async def save_movie(
        self,
        *,
        title: str,
        year: Optional[int] = None,
        release_date=None,
        release_date_raw: str = "",
        poster_url: Optional[str] = None,
        poster_source: Optional[str] = None,
        overview: Optional[str] = None,
        tmdb_id: Optional[int] = None,
        popularity: float = 0.0,
        now=None,
    ) -> Optional[str]:
        """Upsert one upcoming release and return its ``MOVIE_ID``.

        ``_id`` is *derived* from the title (+ year) exactly like
        ``recent_movies`` does, which is what makes the two sections agree:
        a deep link built from a Coming Soon card still resolves once the
        movie is uploaded and the rail picks it up.
        """
        if release_date is None:
            return None
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
        fields: Dict[str, Any] = {
            "title": title,
            "title_key": title_key,
            "year": int(year) if year else None,
            "release_date": release_date,
            "release_date_raw": truncate(release_date_raw or "", 16),
            "updated_at": moment,
            "fetched_at": moment,
        }
        if poster_url:
            fields["poster_url"] = truncate(poster_url, 600)
            fields["poster_source"] = truncate(poster_source or "unknown", 24)
        if overview:
            fields["overview"] = truncate(overview, 300)
        if tmdb_id:
            try:
                fields["tmdb_id"] = int(tmdb_id)
            except (TypeError, ValueError):
                pass
        try:
            fields["popularity"] = max(0.0, float(popularity or 0.0))
        except (TypeError, ValueError):
            fields["popularity"] = 0.0

        try:
            await self.col.update_one(
                {"_id": movie_id},
                {"$set": fields, "$setOnInsert": {"notify_total": 0}},
                upsert=True,
            )
        except DuplicateKeyError:
            # Lost an upsert race with a parallel refresh – the document exists.
            await self.col.update_one(
                {"_id": movie_id}, {"$set": fields}
            )
        return movie_id

    async def save_movies(self, movies: List[Dict[str, Any]], now=None) -> int:
        """Upsert a whole upstream page. Returns how many rows were kept."""
        saved = 0
        for row in movies or []:
            try:
                if await self.save_movie(now=now, **row):
                    saved += 1
            except Exception as exc:  # one bad row must not kill the refresh
                logger.warning("Coming soon: skipped a row (%s): %s",
                               (row or {}).get("title"), exc)
        return saved

    async def mark_fetched(self, movies: int = 0, now=None) -> None:
        """Remember that the list was refreshed (drives ``is_stale``)."""
        moment = now or utcnow()
        try:
            await self.meta.update_one(
                {"_id": REFRESH_ID},
                {"$set": {"fetched_at": moment, "movies": int(movies or 0)}},
                upsert=True,
            )
        except Exception as exc:
            logger.warning("Coming soon: could not store the refresh time: %s", exc)

    async def add_notify(self, movie_id: str) -> int:
        """Count one more user waiting for this movie. Returns the new total."""
        if not movie_id:
            return 0
        try:
            await self.col.update_one(
                {"_id": movie_id}, {"$inc": {"notify_total": 1}}
            )
            doc = await self.col.find_one({"_id": movie_id}, {"notify_total": 1})
            return int((doc or {}).get("notify_total") or 0)
        except Exception as exc:
            logger.warning("Coming soon: could not count a notify for %s: %s",
                           movie_id, exc)
            return 0

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    async def _exists(self, movie_id: str) -> bool:
        try:
            return await self.col.find_one({"_id": movie_id}, {"_id": 1}) is not None
        except Exception:
            return False

    async def last_fetched(self):
        """When the list was last refreshed upstream (``None`` when never)."""
        try:
            doc = await self.meta.find_one({"_id": REFRESH_ID}, {"fetched_at": 1})
        except Exception as exc:
            logger.warning("Coming soon: could not read the refresh time: %s", exc)
            return None
        return (doc or {}).get("fetched_at")

    async def is_stale(self, ttl_seconds: int, now=None) -> bool:
        """Should a background refresh be kicked off?"""
        if not ttl_seconds or ttl_seconds <= 0:
            return True
        fetched = await self.last_fetched()
        if fetched is None:
            return True
        moment = now or utcnow()
        try:
            age = (moment - fetched).total_seconds()
        except TypeError:  # naive vs aware datetimes
            return True
        return age >= float(ttl_seconds)

    async def list_upcoming(self, limit: int = 20, cutoff=None, projection=None,
                            raise_on_error: bool = False) -> List[Dict[str, Any]]:
        """Upcoming releases, soonest first.

        ``cutoff`` drops movies that released long enough ago that nobody is
        waiting for them any more (see ``COMING_SOON_RELEASED_GRACE_DAYS``).
        """
        query: Dict[str, Any] = {"release_date": {"$ne": None}}
        if cutoff is not None:
            query["release_date"] = {"$gte": cutoff}
        fields = dict(projection) if projection else INTERNAL_FIELDS
        try:
            cursor = self.col.find(query, fields).sort(SORT).limit(max(1, int(limit)))
            return [doc async for doc in cursor]
        except Exception as exc:
            if raise_on_error:
                raise
            logger.error("Coming soon: list_upcoming failed: %s", exc)
            return []

    async def notify_count(self, movie_id: str) -> int:
        """How many users are waiting for this movie (0 on any failure)."""
        if not movie_id:
            return 0
        try:
            doc = await self.col.find_one({"_id": movie_id}, {"notify_total": 1})
            return int((doc or {}).get("notify_total") or 0)
        except Exception:
            return 0

    async def count(self) -> int:
        """How many releases are tracked (0 on any failure)."""
        try:
            return int(await self.col.count_documents({}))
        except Exception as exc:
            logger.warning("Coming soon: count failed: %s", exc)
            return 0

    async def purge_old(self, before, batch: int = DEFAULT_MAX_MOVIES) -> int:
        """Housekeeping: drop releases older than ``before``. Returns removed."""
        if before is None:
            return 0
        try:
            result = await self.col.delete_many({"release_date": {"$lt": before}})
            removed = int(getattr(result, "deleted_count", 0) or 0)
            if removed:
                logger.info("Coming soon: dropped %d expired release(s).", removed)
            return removed
        except Exception as exc:
            logger.warning("Coming soon: purge_old failed: %s", exc)
            return 0

    async def get(self, movie_id: str) -> Optional[Dict[str, Any]]:
        """One full document (internal fields included) – for the poster proxy."""
        if not movie_id:
            return None
        try:
            return await self.col.find_one({"_id": movie_id})
        except Exception as exc:
            logger.warning("Coming soon: get(%s) failed: %s", movie_id, exc)
            return None


__all__ = [
    "UpcomingMoviesStore",
    "DEFAULT_MAX_MOVIES",
    "INTERNAL_FIELDS",
    "MAX_STORED_NAME_LENGTH",
    "REFRESH_ID",
    "SORT",
]
