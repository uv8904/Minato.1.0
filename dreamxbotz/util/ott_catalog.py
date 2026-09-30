"""The OTT storefront brain: rails, search, trailer/genre metadata.

Everything the JioHotstar-style homepage (``/home``, ``/search``) renders comes
from here.  The module is deliberately split into **pure** functions (given a
list of movies + metadata, produce rails/cards/results – no I/O at all) and a
thin **async** layer that reads the MongoDB stores and the TMDB metadata cache.

Why the split
-------------
A homepage request must never be able to fail because MongoDB hiccuped, TMDB
was slow or a document had a weird field.  The pure half is exhaustively
testable without a database, and the async half treats every store as
"best effort": it returns an empty list rather than raising.

Data flow
---------
``recent_movies`` (which movie exists + quality + upload date)
        + ``ott_meta`` (genres, trailer, rating, backdrop – from TMDB)
        + ``upcoming_movies`` (for the "Coming Soon" rail)
                 ↓
          ``Catalog`` → rails ("New on MinatoVerse", one per genre, …)
                     → search/filter results for ``/search``
                     → cards handed to the browser (never a file id)

Reference: docs/OTT_HOMEPAGE.md
"""
import asyncio
import html
import logging
import re
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from dreamxbotz.util.movie_titles import (
    MAX_TITLE_LENGTH,
    build_deeplink,
    canonical_quality,
    clean_title_text,
    looks_like_series,
    primary_quality,
    quality_label,
    relative_time_label,
    sanitize_movie_id,
    utcnow,
)

logger = logging.getLogger(__name__)

__all__ = [
    "GENRE_LABELS",
    "GENRE_ORDER",
    "Catalog",
    "build_catalog",
    "build_home",
    "card_for",
    "enrich_pending",
    "kick_enrichment",
    "match_trending",
    "normalize_query",
    "quality_bucket",
    "search_catalog",
    "suggest_titles",
]

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
#: TMDB genre names in the order a JioHotstar-like storefront shows its rails.
GENRE_ORDER: Tuple[str, ...] = (
    "Action",
    "Thriller",
    "Drama",
    "Comedy",
    "Romance",
    "Science Fiction",
    "Crime",
    "Mystery",
    "Adventure",
    "Horror",
    "Family",
    "Animation",
    "Fantasy",
    "Biography",
    "Documentary",
    "History",
    "War",
    "Music",
    "Western",
    "TV Movie",
)
#: Rail/section titles – the storefront sound, not the database sound.
GENRE_LABELS: Dict[str, str] = {
    "Action": "Action & Blast",
    "Adventure": "Adventure & Quest",
    "Animation": "Animated",
    "Biography": "True Stories",
    "Comedy": "Comedy",
    "Crime": "Crime & Underworld",
    "Documentary": "Documentaries",
    "Drama": "Drama",
    "Family": "Family Time",
    "Fantasy": "Fantasy Worlds",
    "History": "History & Period",
    "Horror": "Horror & Spooky",
    "Music": "Music & Dance",
    "Mystery": "Mystery",
    "Romance": "Romance",
    "Science Fiction": "Sci-Fi & Future",
    "TV Movie": "TV Movies",
    "Thriller": "Thriller",
    "War": "War & Action",
    "Western": "Western",
}
GENRE_ICONS: Dict[str, str] = {
    "Action": "💥",
    "Adventure": "🧭",
    "Animation": "🎨",
    "Comedy": "😂",
    "Crime": "🕵️",
    "Documentary": "📰",
    "Drama": "🎭",
    "Family": "👨‍👩‍👧",
    "Fantasy": "🪄",
    "History": "🏛️",
    "Horror": "👻",
    "Music": "🎵",
    "Mystery": "🔍",
    "Romance": "💖",
    "Science Fiction": "🚀",
    "TV Movie": "📺",
    "Thriller": "🔪",
    "War": "⚔️",
    "Western": "🤠",
}

#: Quality buckets used by the /search filter chips (label, member qualities).
QUALITY_BUCKETS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("4K", ("2160p", "1440p")),
    ("1080p", ("1080p",)),
    ("720p", ("720p",)),
    ("480p", ("480p", "360p")),
)

MIN_MATCH_RATIO = 0.72
DEFAULT_RAIL_MIN_ITEMS = 4
#: How long a page render may wait for the search-trend table (seconds).
TRENDING_TIMEOUT = 1.5
#: Rails rendered into the HTML of the homepage (the rest arrive via the JSON
#: bootstrap / the live API, which keeps the first paint small and fast).
SERVER_RENDERED_RAILS = 4


def _cfg(name: str, default):
    """Read a setting from ``info.py`` without breaking when it is missing."""
    try:
        import info  # type: ignore

        return getattr(info, name, default)
    except Exception:
        return default


def _bool_cfg(name: str, default: bool = True) -> bool:
    value = _cfg(name, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return bool(value)


def _int_cfg(name: str, default: int, low: int = 1, high: int = 1000) -> int:
    try:
        return max(low, min(int(_cfg(name, default)), high))
    except (TypeError, ValueError):
        return default


def _clean(value: Any, limit: int = MAX_TITLE_LENGTH) -> str:
    return clean_title_text(value if value is not None else "", limit=limit)


def _esc(value: Any) -> str:
    """HTML-escape a value for the server-rendered first paint."""
    return html.escape(str(value if value is not None else ""), quote=True)


# --------------------------------------------------------------------------- #
# Query helpers (pure – used by /search and by inline mode)
# --------------------------------------------------------------------------- #
def normalize_query(text: Any) -> str:
    """Lower-case, accent-free, punctuation-free query used for matching."""
    raw = unicodedata.normalize("NFKD", str(text or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.lower()
    raw = re.sub(r"[^a-z0-9\s]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def query_tokens(text: Any) -> List[str]:
    """Meaningful tokens of a query – stop words such as ``the``/``movie`` dropped."""
    stop = {"the", "a", "an", "of", "and", "movie", "movies", "film", "full", "hd", "download"}
    return [token for token in normalize_query(text).split() if token and token not in stop]


#: Canonical quality -> filter bucket on the search page.
_BUCKET_MEMBERS: Dict[str, str] = {
    "2160p": "4K", "1440p": "4K", "4k": "4K",
    "1080p": "1080p",
    "720p": "720p", "576p": "720p", "540p": "720p",
    "480p": "480p", "360p": "480p", "240p": "480p", "140p": "480p",
}


def quality_bucket(card_or_movie: Dict[str, Any]) -> Optional[str]:
    """``"4K" | "1080p" | "720p" | "480p"`` for a card, ``None`` when unknown.

    The bucket is derived from the *best* quality the movie has, so a movie
    available in 1080p **and** 720p answers both ``?quality=1080p`` and is never
    listed under ``4K``.
    """
    qualities = list(card_or_movie.get("qualities") or [])
    if not qualities:
        single = card_or_movie.get("quality")
        qualities = [single] if single else []
    buckets = set()
    for quality in qualities:
        canonical = canonical_quality(quality)
        bucket = _BUCKET_MEMBERS.get(str(canonical or "").strip().lower())
        if not bucket and quality:
            bucket = _BUCKET_MEMBERS.get(str(quality).strip().lower())
        if bucket:
            buckets.add(bucket)
    if not buckets:
        return None
    for bucket in ("4K", "1080p", "720p", "480p"):
        if bucket in buckets:
            return bucket
    return None


def _quality_filter(card: Dict[str, Any], wanted: str) -> bool:
    """Does one card belong to a quality bucket (``4k`` / ``1080p`` / …)?"""
    wanted = normalize_query(wanted).replace(" ", "")
    if not wanted:
        return True
    bucket = quality_bucket(card)
    if not bucket:
        return False
    if wanted in ("4k", "2160p", "1440p"):
        return bucket == "4K"
    return bucket == wanted


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
def card_for(
    movie: Dict[str, Any],
    meta: Optional[Dict[str, Any]] = None,
    *,
    bot_username: str = "",
    search_path: str = "/search",
    now=None,
    with_overview: bool = False,
) -> Dict[str, Any]:
    """One browser-ready card (the only shape the frontend ever sees).

    Only whitelisted, already-public values are copied: the poster/backdrop are
    *our own* proxy URLs (``/api/movies/poster/<id>``), the action is the
    Telegram deep link built from the public ``BOT_USERNAME``.  File ids,
    download URLs and the bot token are impossible by construction.
    """
    meta = meta or {}
    movie_id = sanitize_movie_id(movie.get("_id") or movie.get("id") or "")
    title = _clean(meta.get("title") or movie.get("title") or "", limit=MAX_TITLE_LENGTH) or "Untitled"
    year = movie.get("year")
    if not isinstance(year, int):
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None

    qualities = list(movie.get("qualities") or [])
    if movie.get("quality"):
        qualities.append(movie["quality"])
    has_poster = bool(movie.get("poster_url")) or bool(meta.get("poster"))
    version = str(movie.get("updated_at") or "").replace(" ", "")[:24]
    token = f"?v={abs(hash(f'{movie_id}|{version}')) % 10_000_000:07d}" if has_poster else ""

    genres = [str(name) for name in (meta.get("genres") or []) if str(name).strip()][:4]
    rating = meta.get("rating")
    try:
        rating = round(float(rating), 1) if rating else None
    except (TypeError, ValueError):
        rating = None
    runtime = meta.get("runtime")
    try:
        runtime = int(runtime) if runtime else 0
    except (TypeError, ValueError):
        runtime = 0

    trailer_key = str(meta.get("trailer_key") or "").strip()
    search_url = f"{search_path}?q={_quote(title)}"
    deeplink = build_deeplink(bot_username, movie_id)
    card: Dict[str, Any] = {
        "id": movie_id,
        "title": title,
        "year": year,
        "quality": primary_quality(qualities) or "",
        "quality_label": quality_label(qualities) or "",
        "qualities": qualities[:8],
        "bucket": quality_bucket({"qualities": qualities}),
        "has_poster": has_poster,
        "poster": f"/api/movies/poster/{movie_id}{token}" if movie_id and has_poster else "",
        "backdrop": f"/api/movies/backdrop/{movie_id}{token}" if movie_id and has_poster else "",
        "genres": genres,
        "rating": rating,
        "runtime": runtime,
        "is_series": bool(movie.get("is_series")) or looks_like_series(movie.get("title") or title),
        "files": int(movie.get("file_total") or 0),
        "added": relative_time_label(movie.get("last_upload_at") or movie.get("uploaded_at"), now=now),
        "uploaded_at": str(movie.get("last_upload_at") or movie.get("uploaded_at") or "")[:19],
        "trailer": trailer_key if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", trailer_key or "") else "",
        "deeplink": deeplink,
        "search_url": search_url,
        # Navigational link that is never empty: without a BOT_USERNAME (the
        # bot is not fully configured yet) a card still opens *something* real
        # instead of rendering href="".
        "link": deeplink or search_url,
        "source": str(meta.get("source") or movie.get("poster_source") or "")[:24],
    }
    if with_overview and meta.get("overview"):
        card["overview"] = _clean(meta["overview"], limit=400)
    return card


def _quote(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(str(value or "")[:120])


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
class Catalog:
    """Movies + their metadata, plus the derived rails/results.

    ``cards`` keeps the *same* card object per movie id, so a movie appears
    once in the JSON payload even when several rails reference it.
    """

    def __init__(
        self,
        movies: Sequence[Dict[str, Any]],
        metas: Optional[Dict[str, Dict[str, Any]]] = None,
        *,
        bot_username: str = "",
        search_path: str = "/search",
        now=None,
    ):
        self.now = now or utcnow()
        self.metas = metas or {}
        self.cards: List[Dict[str, Any]] = []
        self.by_id: Dict[str, Dict[str, Any]] = {}
        self.trailers: List[str] = []
        for movie in movies or []:
            movie_id = sanitize_movie_id(movie.get("_id") or movie.get("id") or "")
            if not movie_id or movie_id in self.by_id:
                continue
            card = card_for(
                movie,
                self.metas.get(movie_id),
                bot_username=bot_username,
                search_path=search_path,
                now=self.now,
            )
            self.cards.append(card)
            self.by_id[movie_id] = card
            if card.get("trailer"):
                self.trailers.append(movie_id)

    # -- lookups -------------------------------------------------------- #
    def __len__(self) -> int:
        return len(self.cards)

    def get(self, movie_id: str) -> Optional[Dict[str, Any]]:
        return self.by_id.get(sanitize_movie_id(movie_id) or "")

    def many(self, movie_ids: Iterable[str]) -> List[Dict[str, Any]]:
        out = []
        for movie_id in movie_ids or []:
            card = self.get(movie_id)
            if card is not None:
                out.append(card)
        return out

    @property
    def genres(self) -> List[Tuple[str, int]]:
        """``[("Action", 12), ("Drama", 9), …]`` – most populated first."""
        counts: Dict[str, int] = {}
        for card in self.cards:
            for genre in card.get("genres") or []:
                counts[genre] = counts.get(genre, 0) + 1
        return sorted(counts.items(), key=lambda row: (-row[1], row[0]))

    # -- derived views -------------------------------------------------- #
    def hero(self, limit: int = 6) -> List[Dict[str, Any]]:
        """Trailer-first hero rotation (falls back to the newest uploads)."""
        with_trailer = [card for card in self.cards if card.get("trailer") and card.get("has_poster")]
        pool = with_trailer or [card for card in self.cards if card.get("has_poster")] or self.cards
        return pool[: max(1, int(limit))]

    def rails(
        self,
        *,
        limit: int = 18,
        max_rails: int = 12,
        min_items: int = DEFAULT_RAIL_MIN_ITEMS,
        preferred_genres: Sequence[str] = (),
        trending: Sequence[Dict[str, Any]] = (),
        upcoming: Sequence[Dict[str, Any]] = (),
        include_series: bool = True,
    ) -> List[Dict[str, Any]]:
        """The ordered list of rails of the storefront.

        Order: trending (when available) → new uploads → the preferred/genre
        rails (most populated first) → series → top rated → premium quality →
        the newest release year → coming soon.  A rail with fewer than
        ``min_items`` cards is dropped, so the page never shows a lonely row.
        """
        limit = max(1, int(limit))
        min_items = max(1, int(min_items))
        rails: List[Dict[str, Any]] = []

        def add(
            rail_id: str,
            title: str,
            items: Sequence[Dict[str, Any]],
            *,
            subtitle="",
            icon="",
            kind="rail",
            genre: str = "",
        ):
            items = [item for item in items if item][:limit]
            if len(items) < min_items:
                return
            rail = {
                "id": rail_id,
                "title": title,
                "subtitle": subtitle,
                "icon": icon,
                "kind": kind,
                "count": len(items),
                "cards": items,
            }
            if genre:
                rail["genre"] = genre
            rails.append(rail)

        if trending:
            add(
                "trending",
                "Trending now",
                trending,
                subtitle="What everyone is searching for in the bot right now",
                icon="🔥",
            )
        add("new", "New on MinatoVerse", self.cards, subtitle="Freshly indexed, straight from the upload channel", icon="✨")

        # --- genre rails ---------------------------------------------------
        genre_counts = dict(self.genres)
        preferred = [
            genre
            for genre in preferred_genres
            if genre and normalize_query(genre) in {normalize_query(name) for name in GENRE_ORDER}
        ]
        # Match the configured spelling onto the canonical TMDB name.
        canonical = {normalize_query(name): name for name in GENRE_ORDER}
        preferred = list(dict.fromkeys(canonical[normalize_query(genre)] for genre in preferred))
        remaining = sorted(
            (name for name in genre_counts if name not in preferred),
            key=lambda name: (-genre_counts[name], GENRE_ORDER.index(name) if name in GENRE_ORDER else 99, name),
        )
        for genre in preferred + remaining:
            if not genre_counts.get(genre):
                continue
            items = [card for card in self.cards if genre in (card.get("genres") or [])]
            add(
                f"genre-{normalize_query(genre).replace(' ', '-')}",
                GENRE_LABELS.get(genre, genre),
                items,
                subtitle=f"{genre_counts[genre]} titles · tap a card to open it in the bot",
                icon=GENRE_ICONS.get(genre, "🎬"),
                kind="genre",
                genre=genre,
            )
            if len([rail for rail in rails if rail["kind"] == "genre"]) >= max(1, int(max_rails) - 4):
                break

        if include_series:
            series = [card for card in self.cards if card.get("is_series")]
            add("series", "Web Series & Shows", series, subtitle="Binge a full season", icon="📺", kind="series")

        top_rated = sorted(
            (card for card in self.cards if (card.get("rating") or 0) >= 6.5),
            key=lambda card: (-(card.get("rating") or 0), -(card.get("year") or 0)),
        )
        add("top-rated", "Top rated on TMDB", top_rated, subtitle="Rated 6.5 and above", icon="⭐", kind="rating")

        premium = sorted(
            (card for card in self.cards if card.get("bucket") == "4K"),
            key=lambda card: (-(card.get("year") or 0), card.get("title") or ""),
        )
        add(
            "premium",
            "4K & Ultra HD",
            premium,
            subtitle="The sharpest copies in the library",
            icon="💎",
            kind="quality",
        )

        years = sorted({card.get("year") for card in self.cards if card.get("year")}, reverse=True)
        if years:
            newest_year = years[0]
            add(
                f"year-{newest_year}",
                f"Top picks of {newest_year}",
                [card for card in self.cards if card.get("year") == newest_year],
                subtitle="This year's releases",
                icon="📅",
                kind="year",
            )

        if upcoming:
            add(
                "coming-soon",
                "Coming soon",
                upcoming,
                subtitle="Not out yet — a tap sets a reminder in the bot",
                icon="⏳",
                kind="coming",
            )
        return rails[: max(1, int(max_rails))]

    # -- search --------------------------------------------------------- #
    def search(
        self,
        query: str = "",
        *,
        genre: str = "",
        year: Any = None,
        quality: str = "",
        sort: str = "relevance",
        limit: int = 24,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Filter + rank the catalog; returns results and facet counts."""
        return search_catalog(
            self.cards,
            query,
            genre=genre,
            year=year,
            quality=quality,
            sort=sort,
            limit=limit,
            offset=offset,
        )


def build_catalog(
    movies: Sequence[Dict[str, Any]],
    metas: Optional[Dict[str, Dict[str, Any]]] = None,
    **kwargs,
) -> Catalog:
    """Convenience factory (kept separate so tests can build catalogs cheaply)."""
    return Catalog(movies, metas, **kwargs)


# --------------------------------------------------------------------------- #
# Search / ranking (pure)
# --------------------------------------------------------------------------- #
def _score_card(card: Dict[str, Any], query: str) -> float:
    """Relevance of one card for a free-text query (``0`` = no match)."""
    tokens = query_tokens(query)
    if not tokens:
        return 0.5  # browse mode: everything matches, order comes from `sort`
    title = normalize_query(card.get("title"))
    title_tokens = title.split()
    joined = " ".join(tokens)
    score = 0.0
    if title == joined:
        score = 120.0
    elif title.startswith(joined):
        score = 90.0
    elif joined in title:
        score = 70.0
    else:
        matched = sum(1 for token in tokens if token in title_tokens or token in title)
        if matched:
            score = 40.0 + 20.0 * (matched / len(tokens))
        else:
            ratio = SequenceMatcher(None, joined, title).ratio()
            best_token = max((SequenceMatcher(None, token, title).ratio() for token in tokens), default=0.0)
            ratio = max(ratio, best_token)
            if ratio < MIN_MATCH_RATIO:
                return 0.0
            score = 30.0 * ratio
    # A year in the query is a strong signal (`jawan 2023`).
    year = card.get("year")
    if year and str(year) in tokens:
        score += 25.0
    if card.get("is_series"):
        score += 0.5
    return score


def _sort_results(rows: List[Dict[str, Any]], sort: str) -> List[Dict[str, Any]]:
    sort = str(sort or "relevance").strip().lower()
    if sort == "newest":
        return sorted(rows, key=lambda row: (row["card"].get("uploaded_at") or "", row["score"]), reverse=True)
    if sort == "rating":
        return sorted(rows, key=lambda row: (-(row["card"].get("rating") or 0), -row["score"]))
    if sort == "title":
        return sorted(rows, key=lambda row: normalize_query(row["card"].get("title")))
    if sort == "year":
        return sorted(rows, key=lambda row: (-(row["card"].get("year") or 0), -row["score"]))
    return sorted(rows, key=lambda row: (-row["score"], -(row["card"].get("year") or 0)))


def search_catalog(
    cards: Sequence[Dict[str, Any]],
    query: str = "",
    *,
    genre: str = "",
    year: Any = None,
    quality: str = "",
    sort: str = "relevance",
    limit: int = 24,
    offset: int = 0,
) -> Dict[str, Any]:
    """The ``/api/ott/search`` engine.

    Filters are applied first (genre / year / quality), then the query is
    scored, then the survivors are sorted and paginated.  Facets are counted on
    the *filtered set without the query*, which is what makes the filter chips
    on the search page stable while the user types.
    """
    started = time.perf_counter()
    query = str(query or "")[:120]
    wanted_genre = _clean(genre, limit=40)
    wanted_quality = _clean(quality, limit=12)
    wanted_year = None
    try:
        wanted_year = int(year) if str(year or "").strip() else None
    except (TypeError, ValueError):
        wanted_year = None

    def facet_source() -> List[Dict[str, Any]]:
        rows = list(cards)
        if wanted_genre:
            rows = [card for card in rows if wanted_genre in (card.get("genres") or [])]
        if wanted_year:
            rows = [card for card in rows if card.get("year") == wanted_year]
        if wanted_quality:
            rows = [card for card in rows if _quality_filter(card, wanted_quality)]
        return rows

    filtered = facet_source()
    scored = []
    for card in filtered:
        score = _score_card(card, query)
        if score > 0:
            scored.append({"card": card, "score": score})
    ranked = _sort_results(scored, sort)

    total = len(ranked)
    limit = max(1, int(limit))
    offset = max(0, int(offset))
    page = ranked[offset : offset + limit]
    facets = {
        "genres": _facet(filtered, "genres"),
        "years": sorted(
            {card.get("year") for card in filtered if card.get("year")}, reverse=True
        )[:12],
        "qualities": [
            {"name": label, "count": sum(1 for card in filtered if _quality_filter(card, label))}
            for label, _ in QUALITY_BUCKETS
        ],
    }
    return {
        "query": query,
        "results": [row["card"] for row in page],
        "total": total,
        "limit": limit,
        "offset": offset,
        "page": (offset // limit) + 1 if limit else 1,
        "pages": max(1, (total + limit - 1) // limit) if total else 0,
        "has_more": offset + len(page) < total,
        "facets": facets,
        "took_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _facet(cards: Sequence[Dict[str, Any]], field: str, limit: int = 14) -> List[Dict[str, Any]]:
    counts: Dict[str, int] = {}
    for card in cards:
        for value in card.get(field) or []:
            if value:
                counts[str(value)] = counts.get(str(value), 0) + 1
    ordered = sorted(counts.items(), key=lambda row: (-row[1], row[0]))
    return [{"name": name, "count": count} for name, count in ordered[:limit]]


def suggest_titles(cards: Sequence[Dict[str, Any]], query: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Type-ahead list for the search box: ``[{id, title, year, poster}]``."""
    query = str(query or "").strip()
    if not query:
        return [
            {"id": card["id"], "title": card["title"], "year": card.get("year"), "poster": card.get("poster") or ""}
            for card in list(cards)[: max(1, int(limit))]
        ]
    ranked = []
    for card in cards:
        score = _score_card(card, query)
        if score > 0:
            ranked.append((score, card))
    ranked.sort(key=lambda row: (-row[0], -(row[1].get("year") or 0)))
    return [
        {"id": card["id"], "title": card["title"], "year": card.get("year"), "poster": card.get("poster") or ""}
        for _, card in ranked[: max(1, int(limit))]
    ]


def match_trending(catalog: Catalog, queries: Sequence[str], limit: int = 12) -> List[Dict[str, Any]]:
    """Cards matching the live "top searches" table – newest match wins per query."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for query in queries or []:
        result = catalog.search(str(query), limit=3)
        for card in result["results"]:
            if card["id"] in seen:
                continue
            seen.add(card["id"])
            out.append(card)
            if len(out) >= max(1, int(limit)):
                return out
    return out


# --------------------------------------------------------------------------- #
# Async layer – reads the stores, never raises
# --------------------------------------------------------------------------- #
#: Store overrides used by ``tools/preview_section.py`` and the tests, so the
#: real catalog code can run on in-memory collections (no MongoDB needed).
_STORE_OVERRIDES: Dict[str, Any] = {}


def override_stores(*, recent=None, meta=None, upcoming=None, reset: bool = False) -> None:
    """Point the catalog at other stores (preview tool / tests).

    ``override_stores(reset=True)`` restores the real MongoDB-backed stores.
    """
    if reset:
        _STORE_OVERRIDES.clear()
        return
    if recent is not None:
        _STORE_OVERRIDES["recent"] = recent
    if meta is not None:
        _STORE_OVERRIDES["meta"] = meta
    if upcoming is not None:
        _STORE_OVERRIDES["upcoming"] = upcoming


#: The ``upcoming_movies`` store, built once per process (see ``_upcoming_store``).
_UPCOMING_STORE = None


def _recent_store():
    if _STORE_OVERRIDES.get("recent") is not None:
        return _STORE_OVERRIDES["recent"]
    from database.recent_movies_db import recent_movies

    return recent_movies


def _meta_store():
    if _STORE_OVERRIDES.get("meta") is not None:
        return _STORE_OVERRIDES["meta"]
    from database.ott_meta_db import ott_meta

    return ott_meta


def _upcoming_store():
    """The ``upcoming_movies`` store (memoised per process)."""
    global _UPCOMING_STORE
    if _STORE_OVERRIDES.get("upcoming") is not None:
        return _STORE_OVERRIDES["upcoming"]
    if _UPCOMING_STORE is None:
        from database.upcoming_db import UpcomingMoviesStore

        _UPCOMING_STORE = UpcomingMoviesStore()
    return _UPCOMING_STORE


def _store_or_none(getter, label: str):
    """Call ``getter()`` but return ``None`` (with a log line) when it fails.

    A store may be impossible to import or construct (broken install, missing
    configuration).  That must degrade to an empty storefront – never to an
    "unavailable" error page – so every getter call is funnelled through here.
    """
    try:
        return getter()
    except Exception as exc:
        logger.warning("OTT catalog: %s store unavailable: %s", label, exc)
        return None


async def _safe(coro, default):
    """Await ``coro`` but turn any failure into ``default`` (with a log line)."""
    try:
        return await coro
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("OTT catalog: %s", exc)
        return default


async def load_movies(limit: int = 500) -> List[Dict[str, Any]]:
    """Every tracked movie, newest first (the source of the whole storefront)."""
    store = _store_or_none(_recent_store, "recent movies")
    if store is None:
        return []
    movies = await _safe(store.list_recent(limit, hard_limit=max(limit, 500)), [])
    if not movies:
        # `list_recent` caps its own result; ask again without the web cap.
        movies = await _safe(_list_all(store, limit), [])
    return [movie for movie in movies if movie]


async def _list_all(store, limit: int) -> List[Dict[str, Any]]:
    cursor = store.col.find({}, {"file_ids": 0, "file_names": 0}).sort(
        [("last_upload_at", -1), ("uploaded_at", -1), ("title", 1)]
    ).limit(int(limit))
    return [doc async for doc in cursor]


async def load_metas(movie_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Genre/trailer metadata of a page of movies (one query)."""
    store = _store_or_none(_meta_store, "OTT meta")
    if store is None:
        return {}
    metas = await _safe(store.get_many(list(movie_ids)), {})
    return metas or {}


async def load_upcoming(limit: int = 12) -> List[Dict[str, Any]]:
    """Coming Soon entries rendered inside the storefront's own rail."""
    if not _bool_cfg("COMING_SOON", True):
        return []
    store = _store_or_none(_upcoming_store, "upcoming movies")
    if store is None:
        return []
    rows = await _safe(store.list_upcoming(limit), [])
    out = []
    for row in rows or []:
        movie_id = sanitize_movie_id(row.get("_id") or "")
        if not movie_id:
            continue
        release = row.get("release_date")
        out.append(
            {
                "id": movie_id,
                "title": _clean(row.get("title") or "", limit=MAX_TITLE_LENGTH) or "Untitled",
                "year": row.get("year"),
                "quality": "",
                "quality_label": "",
                "genres": row.get("genres") or [],
                "rating": row.get("rating"),
                "has_poster": True,
                "poster": f"/api/movies/upcoming/poster/{movie_id}",
                "backdrop": f"/api/movies/upcoming/poster/{movie_id}?w=1280",
                "trailer": "",
                "release_date": str(release or "")[:19],
                "release_label": str(row.get("release_label") or "")[:32],
                "days_left": row.get("days_left"),
                "seconds_left": row.get("seconds_left"),
                "state": str(row.get("state") or "upcoming")[:16],
                "waiting": int(row.get("waiting") or row.get("notify_count") or 0),
                "deeplink": build_deeplink(_bot_username(), movie_id),
                "upcoming": True,
            }
        )
    return out


def _bot_username() -> str:
    try:
        from utils import temp  # type: ignore

        name = getattr(temp, "U_NAME", None)
        if name:
            return str(name).strip().lstrip("@")
    except Exception:
        pass
    return ""


#: Overrides used by the preview tool / tests (``None`` = read the database).
_TRENDING_OVERRIDE: Optional[List[str]] = None


def override_trending(queries: Optional[Sequence[str]] = None, *, reset: bool = False) -> None:
    """Feed the "Trending now" rail without a database (preview tool / tests)."""
    global _TRENDING_OVERRIDE
    if reset:
        _TRENDING_OVERRIDE = None
    elif queries is not None:
        _TRENDING_OVERRIDE = [str(query) for query in queries]


async def load_trending_queries(limit: int = 12) -> List[str]:
    """The bot's own "top searches" table (best effort, never blocks a page).

    A homepage request must not wait for a slow/unreachable database, so the
    lookup is capped at ``TRENDING_TIMEOUT`` seconds and any failure simply
    means "no trending rail this time".
    """
    if _TRENDING_OVERRIDE is not None:
        return [query for query in _TRENDING_OVERRIDE][: max(1, int(limit))]
    if not _bool_cfg("OTT_TRENDING_RAIL", True):
        return []
    try:
        from database.config_db import mdb  # type: ignore

        rows = await _safe(
            asyncio.wait_for(mdb.get_top_messages(max(2, int(limit))), timeout=TRENDING_TIMEOUT), []
        )
    except Exception:
        return []
    out = []
    for row in rows or []:
        text = str(row or "").strip()
        if 2 <= len(text) <= 40 and not text.startswith(("/", "#", "http")):
            out.append(text)
    return out


async def build_home_catalog(*, movie_limit: int = 500) -> Tuple[Catalog, List[Dict[str, Any]]]:
    """Load the stores and return ``(catalog, upcoming_cards)``.

    This is the one place the homepage talks to the database; everything the
    handler does afterwards is pure computation on the returned objects.
    """
    movies = await load_movies(movie_limit)
    metas = await load_metas([movie.get("_id") for movie in movies])
    catalog = Catalog(
        movies,
        metas,
        bot_username=_bot_username(),
        search_path=str(_cfg("OTT_SEARCH_PATH", "/search") or "/search"),
    )
    upcoming = await load_upcoming(_int_cfg("COMING_SOON_LIMIT", 12, 1, 24))
    return catalog, upcoming


async def home_payload(
    *,
    rail_limit: Optional[int] = None,
    max_rails: Optional[int] = None,
    min_items: Optional[int] = None,
    hero_limit: Optional[int] = None,
    include_upcoming: bool = True,
    light: bool = False,
) -> Dict[str, Any]:
    """Everything ``/api/ott/home`` answers with (hero + rails)."""
    started = time.perf_counter()
    catalog, upcoming = await build_home_catalog()
    trending_cards: List[Dict[str, Any]] = []
    if _bool_cfg("OTT_TRENDING_RAIL", True):
        queries = await load_trending_queries(_int_cfg("OTT_TRENDING_LIMIT", 12, 2, 24))
        trending_cards = match_trending(catalog, queries, limit=_int_cfg("OTT_TRENDING_LIMIT", 12, 2, 24))

    rails = catalog.rails(
        limit=rail_limit or _int_cfg("OTT_RAIL_LIMIT", 18, 4, 40),
        max_rails=max_rails or _int_cfg("OTT_RAILS_MAX", 12, 1, 20),
        min_items=min_items or _int_cfg("OTT_RAIL_MIN_ITEMS", 4, 2, 12),
        preferred_genres=_preferred_genres(),
        trending=trending_cards,
        upcoming=upcoming if include_upcoming else [],
    )
    hero = catalog.hero(hero_limit or _int_cfg("OTT_HERO_LIMIT", 6, 1, 12))
    if light:
        hero = [_trim(card) for card in hero]
        rails = [{**rail, "cards": [_trim(card) for card in rail["cards"]]} for rail in rails]
    return {
        "ok": True,
        "hero": hero,
        "rails": rails,
        "genres": [{"name": name, "count": count, "label": GENRE_LABELS.get(name, name)} for name, count in catalog.genres],
        "counts": {
            "movies": len(catalog),
            "with_trailer": len(catalog.trailers),
            "rails": len(rails),
        },
        "updated_at": str(utcnow())[:19],
        "took_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _trim(card: Dict[str, Any]) -> Dict[str, Any]:
    """Cheaper card for ``?light=1`` (drops the fields only the hero needs)."""
    return {key: value for key, value in card.items() if key not in ("qualities", "source", "uploaded_at", "runtime")}


def light_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The same payload with trimmed cards (used for the embedded bootstrap).

    The server renders the first rails into the HTML; the bootstrap JSON only
    needs enough to render/replace the rest on the client, so it is trimmed to
    keep a storefront page small.
    """
    payload = payload or {}
    return {
        **payload,
        "hero": [_trim(card) for card in payload.get("hero") or []],
        "rails": [
            {**rail, "cards": [_trim(card) for card in rail.get("cards") or []]}
            for rail in payload.get("rails") or []
        ],
    }


def _preferred_genres() -> List[str]:
    raw = _cfg("OTT_GENRES", "") or ""
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


# --------------------------------------------------------------------------- #
# TMDB enrichment worker (genre + trailer metadata)
# --------------------------------------------------------------------------- #
_ENRICH_LOCK: Optional[asyncio.Lock] = None
_ENRICH_TASK: Optional["asyncio.Task"] = None


def _current_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _lock() -> asyncio.Lock:
    """The enrichment lock, recreated when its event loop went away (tests)."""
    global _ENRICH_LOCK
    loop = _current_loop()
    holder = getattr(_ENRICH_LOCK, "_loop", None) if _ENRICH_LOCK is not None else None
    if _ENRICH_LOCK is None or holder is not loop:
        _ENRICH_LOCK = asyncio.Lock()
    return _ENRICH_LOCK


def api_key() -> str:
    """``TMDB_API_KEY`` from the environment / ``info.py`` (empty when unset)."""
    try:
        from info import TMDB_API_KEY  # type: ignore

        return str(TMDB_API_KEY or "").strip()
    except Exception:
        return ""


async def enrich_movie(movie: Dict[str, Any], *, timeout: Optional[float] = None) -> bool:
    """Resolve genres/trailer of one movie through TMDB and cache them.

    Returns ``True`` when something was stored.  A movie whose lookup failed is
    still marked as checked (``checked_at``), so the storefront does not hammer
    TMDB for a title it does not know.
    """
    if not _bool_cfg("OTT_META_FETCH", True):
        return False
    from dreamxbotz.util import tmdb_direct

    key = api_key()
    if not key:
        return False
    store = _store_or_none(_meta_store, "OTT meta")
    if store is None:
        return False
    movie_id = sanitize_movie_id(movie.get("_id") or "")
    if not movie_id:
        return False
    query = str(movie.get("search_query") or movie.get("title") or "").strip()
    if not query:
        return False

    seconds = float(timeout if timeout is not None else _cfg("OTT_META_TIMEOUT", 8) or 8)
    details = await tmdb_direct.movie_details(
        query=query,
        api_key=key,
        year=movie.get("year"),
        timeout=max(2.0, seconds),
        with_videos=_bool_cfg("OTT_TRAILER_FETCH", True),
    )
    if not details:
        await store.mark_checked_many([movie_id])
        return False

    fields = {
        "tmdb_id": details.get("tmdb_id"),
        "genres": details.get("genres") or [],
        "genre_ids": details.get("genre_ids") or [],
        "trailer_key": details.get("trailer_key"),
        "trailer_name": (details.get("trailer") or {}).get("name"),
        "overview": details.get("overview"),
        "rating": details.get("rating"),
        "votes": details.get("votes"),
        "runtime": details.get("runtime"),
        "languages": details.get("languages"),
        "cast": details.get("cast"),
        "backdrop": details.get("backdrop"),
        "poster": details.get("poster"),
        "source": "tmdb",
    }
    # A hand-set poster must win over the TMDB one.
    if not movie.get("poster_url") and details.get("poster"):
        recent = _store_or_none(_recent_store, "recent movies")
        if recent is not None:
            await _safe(recent.set_poster(movie_id, details["poster"], "tmdb"), False)
    return bool(await store.upsert(movie_id, fields))


async def enrich_pending(limit: Optional[int] = None, *, retry_hours: Optional[int] = None) -> int:
    """Look up the metadata of movies that are missing it (bounded, serial).

    Called by the background worker and safe to run from a web request: one
    lock serialises the passes, the number of lookups is bounded by
    ``OTT_META_LOOKUPS`` and any exception is logged instead of raised.
    """
    key = api_key()
    if not key or not _bool_cfg("OTT_META_FETCH", True):
        return 0
    async with _lock():
        movies = await load_movies(500)
        if not movies:
            return 0
        store = _store_or_none(_meta_store, "OTT meta")
        if store is None:
            return 0
        metas = await _safe(store.get_many([movie.get("_id") for movie in movies]), {})
        hours = int(retry_hours if retry_hours is not None else _cfg("OTT_META_RETRY_HOURS", 72) or 72)
        budget = int(limit if limit is not None else _int_cfg("OTT_META_LOOKUPS", 40, 1, 200))
        pending = [
            movie
            for movie in movies
            if store.is_stale(metas.get(sanitize_movie_id(movie.get("_id") or "")), hours)
        ]
        done = 0
        for movie in pending[:budget]:
            try:
                if await enrich_movie(movie):
                    done += 1
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("OTT enrich failed for %s: %s", movie.get("_id"), exc)
        if done:
            logger.info("OTT meta: enriched %s movie(s) from TMDB.", done)
        return done


def kick_enrichment(limit: Optional[int] = None) -> bool:
    """Start one background enrichment pass (no-op when one is already running).

    Called when the storefront loads, so genres/trailers fill themselves in
    right after an upload without anything having to be scheduled.
    """
    global _ENRICH_TASK
    if not _bool_cfg("OTT_META_FETCH", True) or not api_key():
        return False
    loop = _current_loop()
    if loop is None:  # no running loop (CLI / unit test): nothing to schedule on
        return False
    if _ENRICH_TASK is not None and not _ENRICH_TASK.done() and _ENRICH_TASK.get_loop() is loop:
        return False
    _ENRICH_TASK = loop.create_task(_run_enrichment(limit))
    return True


async def _run_enrichment(limit: Optional[int]) -> None:
    try:
        await enrich_pending(limit)
    except asyncio.CancelledError:  # pragma: no cover - shutdown
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("OTT meta worker crashed: %s", exc)


def reset_worker() -> None:
    """Drop the worker handle (tests)."""
    global _ENRICH_TASK
    _ENRICH_TASK = None


# --------------------------------------------------------------------------- #
# Process-local caching
# --------------------------------------------------------------------------- #
#: The storefront recomputes rails only once per OTT_CACHE_TTL seconds, no
#: matter how many visitors / API calls arrive in between (the TTL cache also
#: coalesces parallel requests onto a single build).
def _cache_ttl() -> int:
    return _int_cfg("OTT_CACHE_TTL", 120, 0, 3600)


def _new_cache(maxsize: int = 64):
    from dreamxbotz.util.async_cache import AsyncTTLCache

    return AsyncTTLCache(maxsize, max(_cache_ttl(), 1))


_HOME_CACHE = _new_cache(4)
_SEARCH_CACHE = _new_cache(128)
_SUGGEST_CACHE = _new_cache(128)


def invalidate_cache() -> None:
    """Drop the caches (new upload, poster change, tests, ``/posters retry``)."""
    _HOME_CACHE.clear()
    _SEARCH_CACHE.clear()
    _SUGGEST_CACHE.clear()


async def cached_home_payload(
    *,
    rail_limit: Optional[int] = None,
    max_rails: Optional[int] = None,
    min_items: Optional[int] = None,
    hero_limit: Optional[int] = None,
    include_upcoming: bool = True,
    light: bool = False,
) -> Dict[str, Any]:
    """Cached :func:`home_payload` (the TTL is ``OTT_CACHE_TTL``, 0 = no cache)."""
    if not _cache_ttl():
        return await home_payload(
            rail_limit=rail_limit,
            max_rails=max_rails,
            min_items=min_items,
            hero_limit=hero_limit,
            include_upcoming=include_upcoming,
            light=light,
        )
    key = (rail_limit, max_rails, min_items, hero_limit, bool(include_upcoming), bool(light))
    return await _HOME_CACHE.get(
        key,
        lambda: home_payload(
            rail_limit=rail_limit,
            max_rails=max_rails,
            min_items=min_items,
            hero_limit=hero_limit,
            include_upcoming=include_upcoming,
            light=light,
        ),
    )


async def cached_search(query: str, **filters) -> Dict[str, Any]:
    """Cached free-text search over the whole catalog (not only the newest 20)."""
    if not _cache_ttl():
        return await search_whole_catalog(query, **filters)
    key = (str(query or "")[:120], tuple(sorted((str(k), str(v)) for k, v in filters.items())))
    return await _SEARCH_CACHE.get(key, lambda: search_whole_catalog(query, **filters))


async def search_whole_catalog(
    query: str,
    *,
    genre: str = "",
    year: Any = None,
    quality: str = "",
    sort: str = "relevance",
    limit: int = 24,
    offset: int = 0,
) -> Dict[str, Any]:
    """:meth:`Catalog.search` over the whole store (the /search page engine)."""
    catalog, _upcoming = await build_home_catalog()
    payload = catalog.search(
        query, genre=genre, year=year, quality=quality, sort=sort, limit=limit, offset=offset
    )
    payload["ok"] = True
    payload["catalog_size"] = len(catalog)
    return payload


async def cached_suggestions(query: str, limit: int = 8) -> Dict[str, Any]:
    """Cached type-ahead suggestions for the search box."""
    async def build() -> Dict[str, Any]:
        catalog, _upcoming = await build_home_catalog()
        return {"ok": True, "items": suggest_titles(catalog.cards, query, limit=limit)}

    if not _cache_ttl():
        return await build()
    return await _SUGGEST_CACHE.get((str(query or "")[:64], int(limit)), build)


async def cached_movie(movie_id: str) -> Optional[Dict[str, Any]]:
    """One movie as a detail-sheet card (``None`` when unknown / not public)."""
    catalog, _upcoming = await build_home_catalog()
    card = catalog.get(movie_id)
    if card is None:
        return None
    meta = (await load_metas([card["id"]])).get(card["id"]) or {}
    return card_for(
        {
            "_id": card["id"],
            "title": card["title"],
            "year": card.get("year"),
            "qualities": card.get("qualities"),
            "poster_url": "meta" if card.get("has_poster") else None,
            "file_total": card.get("files"),
        },
        meta,
        bot_username=_bot_username(),
        search_path=str(_cfg("OTT_SEARCH_PATH", "/search") or "/search"),
        with_overview=True,
    )


def site_context() -> Dict[str, Any]:
    """Paths/limits the templates and the browser need (all public values)."""
    search_path = str(_cfg("OTT_SEARCH_PATH", "/search") or "/search")
    home_path = str(_cfg("OTT_HOME_PATH", "/home") or "/home")
    return {
        "home_path": home_path,
        "search_path": search_path,
        "home_api": str(_cfg("OTT_HOME_API_PATH", "/api/ott/home") or "/api/ott/home"),
        "search_api": str(_cfg("OTT_SEARCH_API_PATH", "/api/ott/search") or "/api/ott/search"),
        "genres_api": str(_cfg("OTT_GENRES_API_PATH", "/api/ott/genres") or "/api/ott/genres"),
        "movie_api": str(_cfg("OTT_MOVIE_API_PATH", "/api/ott/movie") or "/api/ott/movie"),
        "suggest_api": str(_cfg("OTT_SUGGEST_API_PATH", "/api/ott/suggest") or "/api/ott/suggest"),
        "rail_limit": _int_cfg("OTT_RAIL_LIMIT", 18, 4, 40),
        "hero_limit": _int_cfg("OTT_HERO_LIMIT", 6, 1, 12),
        "search_limit": _int_cfg("OTT_SEARCH_LIMIT", 24, 6, 48),
        "search_max_limit": _int_cfg("OTT_SEARCH_MAX_LIMIT", 60, 6, 120),
        "suggest_limit": _int_cfg("OTT_SUGGEST_LIMIT", 8, 2, 20),
        "poll": _int_cfg("OTT_POLL", 90, 0, 3600),
        "browser_ttl": _int_cfg("OTT_BROWSER_CACHE_TTL", 60, 0, 3600),
        "trailer_volume": _bool_cfg("OTT_TRAILER_VOLUME", True),
        "bot_username": _bot_username(),
    }


# --------------------------------------------------------------------------- #
# Diagnostics (``/stats``, admin dashboard)
# --------------------------------------------------------------------------- #
async def diagnostics() -> Dict[str, Any]:
    """Counts shown by the admin dashboard / owner diagnostics."""
    store = _store_or_none(_meta_store, "OTT meta")
    stats = await _safe(store.stats(), {}) if store is not None else {}
    key = api_key()
    return {
        "enabled": _bool_cfg("OTT_HOME", True),
        "tmdb_key": bool(key),
        "meta_fetch": _bool_cfg("OTT_META_FETCH", True),
        "trailer_fetch": _bool_cfg("OTT_TRAILER_FETCH", True),
        **(stats or {}),
    }
