"""MongoDB storage for the "Daily Index Report" feature.

Every file that ``database.ia_filterdb.save_file()`` successfully indexes also
drops one tiny document here.  A background loop
(:mod:`dreamxbotz.util.index_report`) reads the previous day's documents each
morning and posts the full list – with details – to ``LOG_CHANNEL``.

Collection: ``INDEX_LOG_COLLECTION`` (default ``index_log``) inside the bot's
own ``DATABASE_NAME`` – i.e. the same ``DATABASE_URI`` the rest of the bot
already uses, so no extra database has to be provisioned.

Document schema
============ ======== ========================================================
field        type     notes
============ ======== ========================================================
``_id``      ObjectId auto-generated
``d``        str      index date (Asia/Kolkata) ``YYYY-MM-DD`` – report bucket
``n``        str      original file name, exactly as uploaded in the channel
``s``        int      file size in bytes
``t``        str      file type (``video`` / ``audio`` / ``document``)
``db``       str      which media DB received the file (``Primary`` etc.)
``src``      str      ``manual`` (``/index``) or ``channel`` (auto-indexing)
``ts``       datetime UTC timestamp of the index operation
============ ======== ========================================================

A single meta document ``{"_id": "__report_meta__", "last_sent": "YYYY-MM-DD"}``
remembers which date's report was already delivered, so a bot restart never
re-sends the same report – and never skips it when the bot was down at the
scheduled send time (startup catch-up).
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

#: Reports are scheduled and bucketed in IST (no DST, so a fixed offset is exact).
IST = timezone(timedelta(hours=5, minutes=30))

META_ID = "__report_meta__"
#: Old day-buckets are pruned after this many days (see the daily report loop).
RETENTION_DAYS = 10


def ist_date_str(when: Optional[datetime] = None) -> str:
    """``YYYY-MM-DD`` in Asia/Kolkata – the day bucket a file indexes into."""
    if when is None:
        return datetime.now(IST).strftime("%Y-%m-%d")
    return when.astimezone(IST).strftime("%Y-%m-%d")


class IndexLogStore:
    """Thin, testable wrapper around the ``index_log`` collection."""

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
            from info import INDEX_LOG_COLLECTION  # type: ignore

            return INDEX_LOG_COLLECTION
        except Exception:
            return "index_log"

    @property
    def col(self):
        """The motor collection (memoised)."""
        if self._collection is None:
            if self._database is None:
                self._database = _default_database()
            self._collection = self._database[self.collection_name]
        return self._collection

    async def ensure_indexes(self) -> None:
        """Create the date-bucket index.  Never breaks bot startup."""
        if self._indexes_ready:
            return
        try:
            await self.col.create_index("d", name="il_date", background=True)
            self._indexes_ready = True
            logger.info("Index log: indexes ready on '%s'.", self.collection_name)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Index log: could not create indexes: %s", exc)

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    async def record(
        self,
        *,
        file_name: str,
        file_size: int = 0,
        file_type: str = "?",
        db_name: str = "Primary",
        source: str = "auto",
        when: Optional[datetime] = None,
    ) -> None:
        """Store one indexed file.  One small insert per save – never raises."""
        ts = when or datetime.now(timezone.utc)
        doc = {
            "d": ist_date_str(ts),
            "n": file_name or "?",
            "s": int(file_size or 0),
            "t": file_type or "?",
            "db": db_name,
            "src": source,
            "ts": ts.replace(tzinfo=None),
        }
        try:
            await self.col.insert_one(doc)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Index log: could not record '%s': %s", file_name, exc)

    async def set_last_sent(self, date_str: str) -> None:
        """Remember that the report for ``date_str`` has been delivered."""
        try:
            await self.col.update_one(
                {"_id": META_ID},
                {"$set": {"last_sent": date_str, "ts": datetime.now(timezone.utc).replace(tzinfo=None)}},
                upsert=True,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Index log: could not store report meta: %s", exc)

    async def prune_before(self, cutoff_date: str) -> int:
        """Drop day-buckets older than ``cutoff_date`` (ISO strings compare fine)."""
        try:
            result = await self.col.delete_many({"d": {"$lt": cutoff_date}})
            return int(getattr(result, "deleted_count", 0))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Index log: prune failed: %s", exc)
            return 0

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    async def get_last_sent(self) -> Optional[str]:
        """The last date for which a report was delivered (``None`` = never)."""
        try:
            meta = await self.col.find_one({"_id": META_ID})
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Index log: could not read report meta: %s", exc)
            return None
        return (meta or {}).get("last_sent")

    async def list_for_date(self, date_str: str, limit: int = 0) -> List[Dict[str, Any]]:
        """All indexed files of one day-bucket, oldest first."""
        try:
            cursor = self.col.find({"d": date_str}, {"_id": 0}).sort("ts", 1)
            if limit:
                cursor = cursor.limit(limit)
            return [doc async for doc in cursor]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Index log: could not read date %s: %s", date_str, exc)
            return []

    async def stats_for_date(self, date_str: str) -> Dict[str, int]:
        """``{"files", "size", "manual", "channel"}`` for one day-bucket."""
        pipeline = [
            {"$match": {"d": date_str}},
            {
                "$group": {
                    "_id": None,
                    "files": {"$sum": 1},
                    "size": {"$sum": "$s"},
                    "manual": {"$sum": {"$cond": [{"$eq": ["$src", "manual"]}, 1, 0]}},
                }
            },
        ]
        try:
            rows = [row async for row in self.col.aggregate(pipeline)]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Index log: stats failed for %s: %s", date_str, exc)
            rows = []
        if not rows:
            return {"files": 0, "size": 0, "manual": 0, "channel": 0}
        row = rows[0]
        files = int(row.get("files", 0))
        manual = int(row.get("manual", 0))
        return {
            "files": files,
            "size": int(row.get("size", 0)),
            "manual": manual,
            "channel": files - manual,
        }


def _default_database():
    """Motor database built from the bot's existing ``DATABASE_URI``."""
    from info import DATABASE_NAME, DATABASE_URI  # type: ignore

    client = AsyncIOMotorClient(DATABASE_URI)
    return client[DATABASE_NAME]


#: Shared instance used by the indexing hook, the command and the report task.
index_store = IndexLogStore()
