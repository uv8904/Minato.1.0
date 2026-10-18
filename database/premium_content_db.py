"""
Premium Exclusive Content store — "Premium Early Access" feature.

Owner/Admin marks certain movies/series as premium-only.
- Premium users: instant search results, no verification, normal file delivery.
- Normal users: lock screen with Hindi+English message, buy button, notify option.
- Owner decides when to release to public (or schedule).

Schema (premium_contents):
  _id: keyword_lower (str) — normalized keyword, e.g. "jawan", "kalki 2898 ad"
  keyword: original display keyword
  display_title: pretty title shown on lock screen
  type: "movie" | "series" | "keyword"
  added_by: int (admin id)
  added_at: datetime
  is_released: bool — False = locked for normals, True = public
  released_at: datetime | None
  release_at: datetime | None — scheduled auto-release
  hit_count: int — how many normals tried to access
  file_ids: list[str] — optional explicit file ids locked (file-name fallback)
  subscribers: list[int] — user ids waiting for public release
"""
import asyncio
import datetime
import re
import time
from typing import Optional, List, Dict, Any

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError

from info import DATABASE_URI, DATABASE_NAME

DEFAULT_COLLECTION = "premium_contents"

#: Search must stay snappy even when Mongo is slow/down: the lock lookup gets
#: this budget and then fails open (treat as "nothing locked"). The result is
#: cached in-process for LOCKED_CACHE_TTL seconds so a hot search never pays
#: the round trip twice.
LOCK_LOOKUP_TIMEOUT = 0.5
LOCKED_CACHE_TTL = 30.0


def _normalize_keyword(raw: str) -> str:
    """Lowercase, strip, collapse spaces/punctuation for matching."""
    if not raw:
        return ""
    s = str(raw).strip().lower()
    # replace common separators with space
    s = re.sub(r"[_\-\.\+\[\]\(\)]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Tokens users type that are NOT part of a title: qualities, languages, common
# request words. Years/season tags are handled separately below.
_NOISE_TOKENS = {
    "p", "4k", "8k", "hd", "fhd", "uhd", "bluray", "brrip", "webrip", "web",
    "dl", "dual", "multi", "audio", "hindi", "english", "eng", "hin", "tam",
    "tamil", "tel", "telugu", "kan", "kannada", "mal", "malayalam", "guj",
    "mar", "pun", "dub", "dubbed", "sub", "subs", "esub", "hsub", "series",
    "movie", "movies", "season", "episode", "full", "download", "watch", "in",
    "the", "of", "a", "an", "and", "print", "file", "s", "e",
}
_QUALITY_RE = re.compile(r"^\d{3,4}p$|^4k$|^8k$")
_SEASON_EPISODE_RE = re.compile(r"^(s\d{1,2}e\d{1,2}|s\d{1,2}|e\d{1,2}|season\d{1,2}|episode\d{1,2})$")
_YEAR_RE = re.compile(r"^\d{4}$")


def _is_noise_token(token: str) -> bool:
    return (
        token in _NOISE_TOKENS
        or bool(_QUALITY_RE.match(token))
        or bool(_SEASON_EPISODE_RE.match(token))
        or bool(_YEAR_RE.match(token))
    )


def _is_subsequence(needles, haystack) -> bool:
    """True when every needle appears in haystack, in order (not gaps-free)."""
    it = iter(haystack)
    return all(any(h == n for h in it) for n in needles)


def _keyword_match(query: str, keyword_norm: str) -> bool:
    """
    Check if a premium keyword covers the search query.

    Matching rules (title-locking semantics):
    1. every keyword token appears in the query in order → the query is about
       this title (``kalki 2898 ad`` locks ``kalki 2898 ad 2024``);
    2. or the query's *content* tokens (years/quality/languages stripped) are
       all keyword tokens in order → the user typed a subset of the title
       (``jawan 1080p`` hits a ``jawan 2023`` lock, ``kalki 2898`` hits a
       ``kalki 2898 ad`` lock).
    Whole-token comparison keeps ``jawans`` from matching a ``jawan`` lock.
    """
    if not query or not keyword_norm:
        return False
    q_tokens = _normalize_keyword(query).split()
    k_tokens = keyword_norm.split()
    if not q_tokens or not k_tokens:
        return False
    if _is_subsequence(k_tokens, q_tokens):
        return True
    q_content = [t for t in q_tokens if not _is_noise_token(t)]
    if not q_content:
        return False
    return _is_subsequence(q_content, k_tokens)


class PremiumContentStore:
    def __init__(self, database=None, collection=None, collection_name: Optional[str] = None):
        self._database = database
        self._collection = collection
        self._collection_name = collection_name
        self._indexes_ready = False
        # process-local cache of locked docs: {docs, fetched_at}
        self._locked_cache = {"docs": None, "at": 0.0}

    def _invalidate_locked_cache(self):
        self._locked_cache = {"docs": None, "at": 0.0}

    async def _locked_docs(self) -> List[Dict[str, Any]]:
        """All locked docs, cached in-process (fail-open on DB problems)."""
        now = time.monotonic()
        cached = self._locked_cache
        if cached["docs"] is not None and now - cached["at"] < LOCKED_CACHE_TTL:
            return cached["docs"]

        async def _fetch():
            cursor = self.col.find({"is_released": False})
            return [d async for d in cursor]

        try:
            docs = await asyncio.wait_for(_fetch(), timeout=LOCK_LOOKUP_TIMEOUT)
        except Exception:
            # Mongo down/slow: keep search alive. Serve the previous cache
            # (even stale) or an empty list.
            docs = cached["docs"] or []
        self._locked_cache = {"docs": docs, "at": now}
        return docs

    @property
    def collection_name(self) -> str:
        if self._collection_name:
            return self._collection_name
        try:
            from info import PREMIUM_CONTENT_COLLECTION
            return PREMIUM_CONTENT_COLLECTION
        except Exception:
            return DEFAULT_COLLECTION

    @property
    def col(self):
        if self._collection is None:
            if self._database is None:
                self._database = AsyncIOMotorClient(DATABASE_URI)[DATABASE_NAME]
            self._collection = self._database[self.collection_name]
        return self._collection

    async def ensure_indexes(self):
        if self._indexes_ready:
            return
        try:
            await self.col.create_index("is_released", background=True)
            await self.col.create_index("added_at", background=True)
            await self.col.create_index("hit_count", background=True)
            self._indexes_ready = True
        except Exception:
            pass

    # ---------- CRUD ----------
    async def add_content(self, keyword: str, display_title: str = "", added_by: int = 0, content_type: str = "movie") -> Dict[str, Any]:
        norm = _normalize_keyword(keyword)
        if not norm:
            raise ValueError("keyword empty")
        if len(norm) < 2:
            raise ValueError("keyword too short")
        doc = {
            "_id": norm,
            "keyword": norm,
            "display_title": (display_title or keyword).strip() or keyword,
            "type": content_type,
            "added_by": int(added_by) if added_by else 0,
            "added_at": datetime.datetime.utcnow(),
            "is_released": False,
            "released_at": None,
            "release_at": None,
            "hit_count": 0,
            "file_ids": [],
            "subscribers": [],
            "total_subscribers": 0,
        }
        try:
            await self.col.insert_one(doc)
        except DuplicateKeyError:
            # update existing to re-lock if it was released
            await self.col.update_one(
                {"_id": norm},
                {"$set": {
                    "display_title": doc["display_title"],
                    "type": content_type,
                    "is_released": False,
                    "released_at": None,
                    "added_at": doc["added_at"],
                    "added_by": doc["added_by"],
                }}
            )
            doc = await self.col.find_one({"_id": norm})
        self._invalidate_locked_cache()
        return doc

    async def remove_content(self, keyword: str) -> bool:
        norm = _normalize_keyword(keyword)
        if not norm:
            return False
        res = await self.col.delete_one({"_id": norm})
        self._invalidate_locked_cache()
        return getattr(res, "deleted_count", 0) > 0

    async def get_content(self, keyword: str) -> Optional[Dict[str, Any]]:
        norm = _normalize_keyword(keyword)
        if not norm:
            return None
        return await self.col.find_one({"_id": norm})

    async def list_contents(self, include_released: bool = True, limit: int = 50) -> List[Dict[str, Any]]:
        filt = {}
        if not include_released:
            filt = {"is_released": False}
        cursor = self.col.find(filt).sort("added_at", -1).limit(limit)
        return [d async for d in cursor]

    async def release_content(self, keyword: str) -> Optional[Dict[str, Any]]:
        norm = _normalize_keyword(keyword)
        if not norm:
            return None
        doc = await self.get_content(norm)
        if not doc:
            return None
        await self.col.update_one(
            {"_id": norm},
            {"$set": {"is_released": True, "released_at": datetime.datetime.utcnow()}}
        )
        self._invalidate_locked_cache()
        return await self.get_content(norm)

    async def schedule_release(self, keyword: str, release_at: datetime.datetime) -> Optional[Dict[str, Any]]:
        norm = _normalize_keyword(keyword)
        if not norm:
            return None
        await self.col.update_one({"_id": norm}, {"$set": {"release_at": release_at}})
        return await self.get_content(norm)

    async def increment_hit(self, keyword_norm: str):
        try:
            await self.col.update_one({"_id": keyword_norm}, {"$inc": {"hit_count": 1}})
        except Exception:
            pass

    async def add_subscriber(self, keyword: str, user_id: int) -> bool:
        norm = _normalize_keyword(keyword)
        if not norm:
            return False
        # addToSet to avoid duplicates
        res = await self.col.update_one(
            {"_id": norm},
            {"$addToSet": {"subscribers": int(user_id)}}
        )
        if getattr(res, "modified_count", 0) > 0 or getattr(res, "matched_count", 0) > 0:
            # also update count for display
            doc = await self.get_content(norm)
            if doc:
                await self.col.update_one({"_id": norm}, {"$set": {"total_subscribers": len(doc.get("subscribers", []))}})
            return True
        return False

    async def get_subscribers(self, keyword: str) -> List[int]:
        doc = await self.get_content(keyword)
        if not doc:
            return []
        return list(doc.get("subscribers", []))

    async def clear_subscribers(self, keyword: str):
        norm = _normalize_keyword(keyword)
        await self.col.update_one({"_id": norm}, {"$set": {"subscribers": [], "total_subscribers": 0}})

    # ---------- Shortlink-verified free unlocks ----------
    async def add_verified_user(self, keyword: str, user_id: int) -> bool:
        """Remember that ``user_id`` passed the shortlink verification for this
        title and may now watch it without premium (until the owner releases)."""
        norm = _normalize_keyword(keyword)
        if not norm:
            return False
        await self.col.update_one(
            {"_id": norm},
            {"$addToSet": {"verified_users": int(user_id)}},
        )
        return True

    async def is_verified_user(self, keyword: str, user_id: int) -> bool:
        doc = await self.get_content(keyword)
        if not doc:
            return False
        try:
            return int(user_id) in {int(u) for u in doc.get("verified_users", [])}
        except (TypeError, ValueError):
            return False

    # ---------- Matching ----------
    async def find_locked_for_query(self, query: str) -> Optional[Dict[str, Any]]:
        """
        Check if query matches any *locked* (is_released=False) premium content.
        Returns the matched doc or None. If multiple match, returns longest keyword (most specific).
        """
        if not query or not query.strip():
            return None
        q_norm = _normalize_keyword(query)
        if not q_norm or len(q_norm) < 2:
            return None
        # Locked docs are a small set (tens) and cached in-process.
        candidates = await self._locked_docs()
        best = None
        best_len = -1
        for doc in candidates:
            kw = doc.get("keyword") or doc.get("_id") or ""
            if _keyword_match(query, kw):
                l = len(kw)
                if l > best_len:
                    best = doc
                    best_len = l
        return best

    async def is_file_locked(self, file_name: str) -> Optional[Dict[str, Any]]:
        """Check if a file's name matches any locked premium content."""
        if not file_name:
            return None
        return await self.find_locked_for_query(file_name)

    async def is_query_locked(self, query: str) -> Optional[Dict[str, Any]]:
        return await self.find_locked_for_query(query)

    async def total_locked_count(self) -> int:
        return await self.col.count_documents({"is_released": False})

    async def total_released_count(self) -> int:
        return await self.col.count_documents({"is_released": True})

# Global instance
premium_content_store = PremiumContentStore()

# Re-export helpers for tests
__all__ = ["PremiumContentStore", "premium_content_store", "_normalize_keyword", "_keyword_match"]
