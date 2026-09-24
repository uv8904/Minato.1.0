#!/usr/bin/env python3
"""Local preview + **upload simulator** for the Stream Mode web pages.

Renders the **real** templates (``req.html`` – the Stream Mode player page
with the movie hero and the "Newly Uploaded Movies" spotlight + rail,
``dl.html`` – the download page) and serves the **real** assets and the
**real** ``recent_movies`` / ``upcoming_movies`` code paths, only with
in-memory collections instead of MongoDB – so the whole "upload a movie → it
shows up on the website with a poster" flow, and the whole "Coming Soon"
countdown rail, can be watched in a browser without a running bot, Telegram
or a database.

Usage::

    python tools/preview_section.py            # http://127.0.0.1:8080
    python tools/preview_section.py --port 9000

Handy URLs::

    /                         Stream Mode page (player + hero + spotlight + rail)
                              with the floating **Upload simulator** panel
    /download                 the download page (same rail, same panel)
    /?state=empty             "No new movies uploaded yet"
    /?state=error             error state + retry button
    /?state=loading           keeps the skeleton cards visible
    /?state=hostile           feed full of malicious values (hardening demo)
    /?limit=6                 fewer cards

    /?cs=empty                the same four states for the **Coming Soon** rail
    /?cs=error                only – so one rail can be broken while the other
    /?cs=loading              keeps rendering normally (``?state=`` moves both)
    /?cs=hostile

    /watch/demo               alias of "/" (kept for older bookmarks)
    /watch/demo?state=error   movie hero when the artwork API is down
    /watch/demo?state=noart   movie hero when no poster exists (placeholder card)
    /watch/demo?state=hostile movie hero against a hostile artwork API

Simulating an upload (what the bot does when a movie lands in the channel)::

    curl -s -X POST localhost:8080/demo/upload \\
         -H 'Content-Type: application/json' \\
         -d '{"file_name": "Hmm (2024) 1080p WEB-DL Hindi.mkv"}' | python -m json.tool

    curl -s -X POST localhost:8080/demo/reset          # back to the sample data

The endpoint runs the *same* functions the bot runs after ``save_file()``:
``parse_release_name()`` → ``RecentMoviesStore.register_upload()`` →
(poster worker) → ``GET /api/movies/new`` → the page.  Only the Telegram part
and the TMDB/IMDb poster lookup are simulated (there is no network here).

This file is a **development tool only** – it is not imported by the bot.
"""
import argparse
import asyncio
import json
import secrets
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.recent_movies_db import SORT, RecentMoviesStore  # noqa: E402
from database.upcoming_db import UpcomingMoviesStore  # noqa: E402
from dreamxbotz.server import movie_api, static_assets  # noqa: E402
from dreamxbotz.util import coming_soon  # noqa: E402
from dreamxbotz.util.movie_titles import (  # noqa: E402
    build_deeplink,
    looks_like_series,
    looks_like_video,
    parse_release_name,
    utcnow,
)
from dreamxbotz.util.watch_hero import build_context as build_hero  # noqa: E402

BOT_USERNAME = "MyMovieBot"
DEFAULT_LIMIT = 20
#: Cards in the demo "Coming Soon" rail.
COMING_SOON_LIMIT = 12
#: Live refresh interval used by the demo pages (production default: 60 s).
DEMO_POLL_SECONDS = 10
#: How long the simulated poster worker "searches TMDB/IMDb" before the poster
#: shows up – long enough to watch the placeholder → poster transition.
DEMO_POSTER_DELAY = 2.5
TEMPLATE = ROOT / "dreamxbotz" / "template" / "dl.html"
TEMPLATE_WATCH = ROOT / "dreamxbotz" / "template" / "req.html"
#: The file name of the "movie" that is being streamed on the player page.
WATCH_FILE = "[CK] - Marco (2024) Malayalam HQ 1080p BR-Rip - x264 - (AA.mkv"
WATCH_SIZE = "475.31 MiB"

SAMPLE_MOVIES = [
    ("Jawan", 2023, ["1080p", "720p", "480p"], "#f5c518", 0.2),
    ("Pushpa 2 The Rule", 2024, ["1080p", "720p"], "#ff8a4c", 1.5),
    ("Kalki 2898 AD", 2024, ["2160p", "1080p"], "#54c1ff", 3.1),
    ("Stree 2", 2024, ["1080p", "480p"], "#b46cff", 5.4),
    ("Deadpool & Wolverine", 2024, ["1080p"], "#ff4d6d", 9.0),
    ("Fighter", 2024, ["720p", "480p"], "#38e8b0", 14.0),
    ("Tiger 3", 2023, ["1080p", "720p"], "#ffd166", 26.0),
    ("Animal", 2023, ["1080p"], "#8ea2ff", 40.0),
    ("Leo", 2023, ["1080p", "720p", "480p"], "#ff7ab8", 72.0),
    ("Oppenheimer", 2023, ["2160p", "1080p"], "#c9d4ff", 120.0),
    ("12th Fail", 2023, ["1080p", "720p"], "#7ce0a3", 200.0),
    ("No poster yet", 2025, ["480p"], None, 1.0),  # exercises the placeholder
]

PALETTE = ["#f5c518", "#ff8a4c", "#54c1ff", "#b46cff", "#ff4d6d", "#38e8b0", "#ff7ab8", "#ffd166"]


# --------------------------------------------------------------------------- #
# In-memory stand-in for the ``recent_movies`` Mongo collection
# --------------------------------------------------------------------------- #
class MemoryCursor:
    """The subset of a motor cursor that ``RecentMoviesStore`` uses."""

    def __init__(self, docs, projection=None):
        self._docs = list(docs)
        self._projection = projection or {}

    def _project(self, doc):
        excluded = [key for key, flag in self._projection.items() if flag == 0]
        included = [key for key, flag in self._projection.items() if flag == 1]
        if included:
            keep = set(included) | {"_id"}
            return {key: value for key, value in doc.items() if key in keep}
        return {key: value for key, value in doc.items() if key not in excluded}

    def sort(self, spec):
        # Stable sort applied per key in reverse order → first key is primary.
        for key, direction in reversed(list(spec)):
            descending = direction == -1

            def sort_key(doc, key=key, descending=descending):
                value = doc.get(key)
                missing = value is None
                # Missing values sort last whatever the direction.
                return ((not missing) if descending else missing, 0 if missing else value)

            self._docs.sort(key=sort_key, reverse=descending)
        return self

    def skip(self, count):
        self._docs = self._docs[count:]
        return self

    def limit(self, count):
        self._docs = self._docs[:count]
        return self

    def __aiter__(self):
        self._iterator = iter([self._project(dict(doc)) for doc in self._docs])
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as stop:
            raise StopAsyncIteration from stop


class MemoryCollection:
    """Just enough MongoDB (``update_one`` operators, ``find``, …) for the demo."""

    def __init__(self):
        self.docs = {}

    async def create_index(self, *args, **kwargs):
        return "demo"

    def _matches(self, doc, filter_):
        for key, condition in (filter_ or {}).items():
            value = doc.get(key)
            if isinstance(condition, dict):
                if "$in" in condition and value not in condition["$in"]:
                    return False
                if "$exists" in condition and (key in doc) != bool(condition["$exists"]):
                    return False
                # The "Coming Soon" store queries by release date range.
                if "$ne" in condition and value == condition["$ne"]:
                    return False
                if "$gte" in condition and not (value is not None and value >= condition["$gte"]):
                    return False
                if "$lt" in condition and not (value is not None and value < condition["$lt"]):
                    return False
                if "$regex" in condition:
                    import re as _re

                    flags = _re.IGNORECASE if "i" in str(condition.get("$options", "")) else 0
                    if not isinstance(value, str) or not _re.search(condition["$regex"], value, flags):
                        return False
            elif value != condition:
                return False
        return True

    def find(self, filter_=None, projection=None):
        docs = [dict(doc) for doc in self.docs.values() if self._matches(doc, filter_)]
        return MemoryCursor(docs, projection)

    async def find_one(self, filter_, projection=None):
        for doc in self.docs.values():
            if self._matches(doc, filter_):
                return MemoryCursor([dict(doc)], projection)._project(dict(doc))
        return None

    async def count_documents(self, filter_=None, **kwargs):
        return sum(1 for doc in self.docs.values() if self._matches(doc, filter_))

    async def delete_many(self, filter_=None):
        doomed = [key for key, doc in self.docs.items() if self._matches(doc, filter_)]
        for key in doomed:
            del self.docs[key]
        return SimpleNamespace(deleted_count=len(doomed))

    async def update_many(self, filter_, update):
        matched = [doc["_id"] for doc in self.docs.values() if self._matches(doc, filter_)]
        for movie_id in matched:
            await self.update_one({"_id": movie_id}, update)
        return SimpleNamespace(matched_count=len(matched), modified_count=len(matched))

    async def update_one(self, filter_, update, upsert=False):
        movie_id = filter_["_id"]
        doc = self.docs.get(movie_id)
        inserted = False
        if doc is None:
            if not upsert:
                return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)
            doc = {"_id": movie_id}
            inserted = True
        if inserted:
            for key, value in (update.get("$setOnInsert") or {}).items():
                doc[key] = value
        for key, value in (update.get("$set") or {}).items():
            doc[key] = value
        for key, value in (update.get("$addToSet") or {}).items():
            current = list(doc.get(key) or [])
            values = value["$each"] if isinstance(value, dict) and "$each" in value else [value]
            for item in values:
                if item not in current:
                    current.append(item)
            doc[key] = current
        for key, value in (update.get("$inc") or {}).items():
            doc[key] = (doc.get(key) or 0) + value
        self.docs[movie_id] = doc
        return SimpleNamespace(
            matched_count=0 if inserted else 1,
            modified_count=0 if inserted else 1,
            upserted_id=movie_id if inserted else None,
        )


#: The *real* store class on top of the in-memory collection.
STORE = RecentMoviesStore(collection=MemoryCollection(), collection_name="recent_movies")
#: Demo artwork per MOVIE_ID (in production this is the TMDB/IMDb poster URL).
_POSTER_JOBS: set = set()

#: The *real* "Coming Soon" store on another in-memory collection.
UPCOMING_STORE = UpcomingMoviesStore(
    collection=MemoryCollection(),
    meta_collection=MemoryCollection(),
    collection_name="upcoming_movies",
    meta_collection_name="upcoming_meta",
)

#: (title, days from now, waiting users) – one row per demo countdown card.
#: A negative offset exercises the "releases today"/grace-window states.
SAMPLE_UPCOMING = [
    ("Releases Today", 0, 214),
    ("Border 2", 2, 168),
    ("Avatar 3", 6, 97),
    ("Dhurandhar", 11, 74),
    ("Toxic", 19, 61),
    ("Ramayana Part 1", 34, 55),
    ("Battle of Galwan", 58, 43),
    ("King", 96, 38),
    ("War 2", 141, 29),
    ("Alpha", 175, 22),
    ("No Poster Yet", 23, 0),  # exercises the poster placeholder
]


def _hue_for(value: str) -> str:
    """Stable demo colour per movie id."""
    return PALETTE[sum(ord(char) for char in str(value)) % len(PALETTE)]


async def seed_upcoming() -> None:
    """Fill the Coming Soon store through the real ``save_movie``."""
    UPCOMING_STORE.col.docs.clear()
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    for title, offset, waiting in SAMPLE_UPCOMING:
        release = today + timedelta(days=offset)
        movie_id = await UPCOMING_STORE.save_movie(
            title=title,
            year=release.year,
            release_date=release,
            release_date_raw=release.strftime("%Y-%m-%d"),
            # In production this is the TMDB poster URL; the demo proxy turns
            # the colour back into generated artwork.
            poster_url=None if title == "No Poster Yet" else f"demo://poster/{_hue_for(title).lstrip('#')}",
            poster_source=None if title == "No Poster Yet" else "demo",
            overview="Demo blurb for the preview harness.",
            popularity=100 - offset,
            now=utcnow(),
        )
        if movie_id and waiting:
            UPCOMING_STORE.col.docs[movie_id]["notify_total"] = waiting
    await UPCOMING_STORE.mark_fetched(len(SAMPLE_UPCOMING))


async def seed_store() -> None:
    """Fill the store through the real ``register_upload`` (like the bot does)."""
    STORE.col.docs.clear()
    for title, year, qualities, hue, age_hours in SAMPLE_MOVIES:
        uploaded = utcnow() - timedelta(hours=age_hours)
        movie_id = None
        for quality in qualities:
            movie_id = await STORE.register_upload(
                title=title,
                year=year,
                quality=quality,
                file_id=f"demo-{secrets.token_hex(3)}",
                file_name=f"{title}.{year}.{quality}.WEB-DL.mkv",
                now=uploaded,
            )
        if movie_id and hue:
            # What the poster worker stores once TMDB/IMDb answered.
            await STORE.set_poster(movie_id, f"demo://poster/{hue.lstrip('#')}", "demo")


async def simulated_poster_worker(movie_id: str, delay: float) -> None:
    """Stand-in for ``new_uploaded._poster_worker``: TMDB/IMDb → ``set_poster``."""
    try:
        await asyncio.sleep(max(0.0, delay))
        doc = await STORE.get(movie_id)
        if doc and not doc.get("poster_url"):  # same rule as the real worker
            await STORE.set_poster(movie_id, f"demo://poster/{_hue_for(movie_id).lstrip('#')}", "demo")
    finally:
        _POSTER_JOBS.discard(movie_id)


# --------------------------------------------------------------------------- #
# Demo artwork
# --------------------------------------------------------------------------- #
def demo_poster_svg(title, hue, quality, year=None):
    """A stand-in poster so the grid looks like a real poster wall."""
    safe_title = str(title).replace("&", "and")
    meta = " · ".join(part for part in (str(year) if year else "", quality or "HD") if part)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="480" height="720" viewBox="0 0 480 720">
  <defs>
    <linearGradient id="p" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{hue}"/>
      <stop offset="0.55" stop-color="#1b1f31"/>
      <stop offset="1" stop-color="#07080c"/>
    </linearGradient>
    <linearGradient id="v" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0.4" stop-color="#05060b" stop-opacity="0"/>
      <stop offset="1" stop-color="#05060b" stop-opacity="0.95"/>
    </linearGradient>
    <radialGradient id="s" cx="0.7" cy="0.2" r="0.6">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.35"/>
      <stop offset="1" stop-color="#ffffff" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="480" height="720" fill="url(#p)"/>
  <rect width="480" height="720" fill="url(#s)"/>
  <circle cx="392" cy="120" r="180" fill="#ffffff" opacity="0.06"/>
  <circle cx="70" cy="330" r="150" fill="#000000" opacity="0.18"/>
  <rect y="360" width="480" height="360" fill="url(#v)"/>
  <text x="40" y="548" font-family="Sora, Segoe UI, system-ui, sans-serif" font-size="42"
        font-weight="700" fill="#f7f8fb">{safe_title[:22]}</text>
  <text x="40" y="590" font-family="Inter, Segoe UI, system-ui, sans-serif" font-size="19"
        fill="#e7eaf3">{meta}</text>
  <text x="40" y="640" font-family="Inter, Segoe UI, system-ui, sans-serif" font-size="13"
        letter-spacing="3" fill="#f5c518">MINATOVERSE · DEMO POSTER</text>
</svg>
"""


def demo_backdrop_svg(title, hue, quality):
    """16:9 stand-in for a TMDB backdrop so the hero band looks real."""
    safe_title = str(title).replace("&", "and")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
  <defs>
    <linearGradient id="b" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{hue}"/>
      <stop offset="0.5" stop-color="#1b1f31"/>
      <stop offset="1" stop-color="#07080c"/>
    </linearGradient>
    <radialGradient id="g" cx="0.78" cy="0.18" r="0.7">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.22"/>
      <stop offset="1" stop-color="#ffffff" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="1280" height="720" fill="url(#b)"/>
  <rect width="1280" height="720" fill="url(#g)"/>
  <circle cx="1010" cy="150" r="230" fill="#000000" opacity="0.18"/>
  <circle cx="210" cy="560" r="260" fill="#000000" opacity="0.16"/>
  <text x="80" y="386" font-family="Sora, Segoe UI, system-ui, sans-serif" font-size="60"
        font-weight="700" fill="#f7f8fb" opacity="0.92">{safe_title[:26]}</text>
  <text x="82" y="430" font-family="Inter, Segoe UI, system-ui, sans-serif" font-size="22"
        fill="#e7eaf3" opacity="0.8">{quality or "HD"} · demo backdrop · 16:9</text>
</svg>
"""


def limit_default(request, default: int = 20, cap: int = 20) -> int:
    """``?limit=`` clamped into range (the real API caps it the same way)."""
    try:
        return max(1, min(int(request.rel_url.query.get("limit", default)), cap))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# API (same JSON contracts as the bot's endpoints)
# --------------------------------------------------------------------------- #
async def demo_feed(request):
    """Same JSON contract as ``GET /api/movies/new`` – served by the real store."""
    state = request.rel_url.query.get("state")
    if state == "error":
        return web.json_response({"ok": False, "error": "database_unavailable", "movies": []}, status=503)
    if state == "empty":
        return web.json_response({"ok": True, "count": 0, "limit": 20, "movies": []})
    if state == "loading":
        await asyncio.sleep(3600)
    if state == "hostile":
        # Security demo: the browser must survive a compromised/odd API.
        return web.json_response(
            {
                "ok": True,
                "count": 2,
                "limit": limit_default(request),
                "bot_username": BOT_USERNAME,
                "movies": [
                    {
                        "id": "<img src=x onerror=alert(1)>",
                        "title": "<script>alert('xss')</script>Evil Movie",
                        "poster": "https://evil.example/poster.jpg",
                        "backdrop": "https://evil.example/backdrop.jpg",
                        "quality": "<b>1080p</b>",
                        "added": "now",
                        "year": 2024,
                        "deeplink": "javascript:alert(1)",
                    },
                    {
                        "id": "jawan-2023",
                        "title": "Jawan",
                        "poster": "//evil.example/tracker.jpg",
                        "backdrop": "//evil.example/tracker-wide.jpg",
                        "deeplink": "https://evil.example/steal",
                        "added": "2 days ago",
                    },
                ],
            }
        )
    limit = limit_default(request)
    rows = await STORE.list_recent(limit)
    movies = [movie_api.public_movie(doc, BOT_USERNAME) for doc in rows]
    newest = rows[0].get("last_upload_at") if rows else None
    return web.json_response(
        {
            "ok": True,
            "count": len(movies),
            "limit": limit,
            "updated_at": (newest or utcnow()).isoformat().replace("+00:00", "Z"),
            "bot_username": BOT_USERNAME,
            "movies": movies,
        },
        headers={"Cache-Control": "no-cache"},
    )


async def demo_poster(request):
    """Demo posters: gradient artwork, plus the real placeholder for one movie."""
    movie_id = request.match_info["movie_id"]
    doc = await STORE.get(movie_id)
    if not doc:
        # The watch-page hero asks for its own movie – make it look real too.
        title = (request.rel_url.query.get("q") or WATCH_FILE).strip()[:60]
        quality = "1080p" if request.rel_url.query.get("y") else "BR-Rip"
        return web.Response(
            body=demo_poster_svg(title, _hue_for(movie_id), quality),
            content_type="image/svg+xml",
            headers={"Cache-Control": "no-cache"},
        )
    if not doc.get("poster_url"):
        # Exactly what production serves while the poster worker is still busy.
        return web.Response(
            body=movie_api.placeholder_svg(doc["title"]),
            content_type="image/svg+xml",
            headers={"Cache-Control": "no-cache"},
        )
    hue = "#" + doc["poster_url"].rsplit("/", 1)[-1]
    quality = (doc.get("qualities") or ["HD"])[0]
    return web.Response(
        body=demo_poster_svg(doc["title"], hue, quality, doc.get("year")),
        content_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def demo_upcoming(request):
    """Same contract as ``GET /api/movies/upcoming`` – served by the real store.

    Runs the *real* ``coming_soon.public_upcoming_movies()`` shaping code and
    the real ``movie_api._json_response`` (so ETag / 304 / CORS behave exactly
    as they do in production); only the TMDB call is replaced by the seeded
    in-memory collection.
    """
    state = request.rel_url.query.get("state")
    if state == "error":
        return movie_api._json_response(
            {"ok": False, "error": "database_unavailable", "movies": []},
            status=503, request=request,
        )
    if state == "loading":
        await asyncio.sleep(3600)
    if state == "hostile":
        # Security demo: a compromised feed must not reach the DOM as markup.
        return movie_api._json_response(
            {
                "ok": True,
                "count": 1,
                "limit": 12,
                "bot_username": BOT_USERNAME,
                "movies": [
                    {
                        "id": "<img src=x onerror=alert(1)>",
                        "title": "<script>alert('xss')</script>Evil Movie",
                        "poster": "https://evil.example/poster.jpg",
                        "release_date": "not-a-date",
                        "countdown": "<b>in 3 days</b>",
                        "deeplink": "javascript:alert(1)",
                        "waiting": 5,
                    }
                ],
            },
            ttl=0, request=request,
        )

    limit = limit_default(request, default=12, cap=24)
    rows = [] if state == "empty" else await UPCOMING_STORE.list_upcoming(
        limit, cutoff=coming_soon.release_cutoff()
    )
    movies = coming_soon.public_upcoming_movies(rows, BOT_USERNAME)
    newest = max(
        (movie["release_date"] for movie in movies if movie.get("release_date")),
        default=None,
    )
    return movie_api._json_response(
        {
            "ok": True,
            "count": len(movies),
            "limit": limit,
            "updated_at": newest,
            "bot_username": BOT_USERNAME,
            "movies": movies,
        },
        ttl=0, request=request,
    )


async def demo_upcoming_poster(request):
    """Poster bytes for one upcoming release (generated artwork, same origin)."""
    movie_id = request.match_info["movie_id"]
    doc = await UPCOMING_STORE.get(movie_id)
    if not doc:
        return movie_api._not_found_poster(request)
    if not doc.get("poster_url"):
        # Exactly what production serves while TMDB has no artwork for it.
        return web.Response(
            body=movie_api.placeholder_svg(doc["title"]),
            content_type="image/svg+xml",
            headers={"Cache-Control": "no-cache"},
        )
    hue = "#" + doc["poster_url"].rsplit("/", 1)[-1]
    return web.Response(
        body=demo_poster_svg(doc["title"], hue, "Coming soon", doc.get("year")),
        content_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def demo_art(request):
    """Same JSON contract as ``GET /api/movies/art/<MOVIE_ID>`` (watch-page hero)."""
    state = request.match_info.get("state") or request.rel_url.query.get("state")
    movie_id = request.match_info["movie_id"]
    if state == "error":
        return web.json_response({"ok": False, "error": "database_unavailable"}, status=503)
    if state == "hostile":
        # Security demo: every URL below must be rejected by watch_hero.js.
        return web.json_response(
            {
                "ok": True,
                "id": "<script>alert(1)</script>",
                "title": "<img src=x onerror=alert(1)>Evil",
                "year": 2024,
                "poster": "https://evil.example/poster.jpg",
                "backdrop": "//evil.example/tracker.jpg",
                "has_poster": True,
                "has_backdrop": True,
            }
        )
    if state == "noart":
        return web.json_response(
            {
                "ok": True,
                "id": movie_id,
                "title": "Marco",
                "year": 2024,
                "poster": f"/api/movies/poster/{movie_id}?v=demo",
                "backdrop": f"/api/movies/backdrop/{movie_id}?v=demo",
                "has_poster": False,
                "has_backdrop": False,
            }
        )
    return web.json_response(
        {
            "ok": True,
            "id": movie_id,
            "title": "Marco",
            "year": 2024,
            "poster": f"/api/movies/poster/{movie_id}?v=demo",
            "backdrop": f"/api/movies/backdrop/{movie_id}?v=demo",
            "has_poster": True,
            "has_backdrop": True,
            "source": "tmdb",
        }
    )


async def demo_backdrop(request):
    """16:9 demo artwork: the spotlight banner and the watch-page hero band."""
    movie_id = request.match_info["movie_id"]
    doc = await STORE.get(movie_id)
    if doc:
        if not doc.get("poster_url"):
            return web.Response(
                body=movie_api.placeholder_backdrop_svg(doc["title"]),
                content_type="image/svg+xml",
                headers={"Cache-Control": "no-cache"},
            )
        hue = "#" + doc["poster_url"].rsplit("/", 1)[-1]
        quality = (doc.get("qualities") or ["HD"])[0]
        return web.Response(
            body=demo_backdrop_svg(doc["title"], hue, quality),
            content_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    title = (request.rel_url.query.get("q") or "Marco").strip()[:60]
    return web.Response(
        body=demo_backdrop_svg(title, _hue_for(movie_id), "1080p"),
        content_type="image/svg+xml",
    )


# --------------------------------------------------------------------------- #
# Upload simulator
# --------------------------------------------------------------------------- #
async def demo_upload(request):
    """Run the bot's post-index pipeline for one file name and report each step.

    Mirrors ``new_uploaded.notify_new_file()`` + ``_persist()``: the same
    parser, the same de-duplicating upsert, the same (here: simulated) poster
    worker.  The response lists every step so the page can show what happened.
    """
    try:
        if request.content_type == "application/json":
            data = await request.json()
        else:
            data = dict(await request.post())
    except Exception:
        data = {}
    file_name = str(data.get("file_name") or request.rel_url.query.get("file_name") or "").strip()
    file_name = file_name[:300]
    try:
        poster_delay = float(data.get("poster_delay", request.rel_url.query.get("poster_delay", DEMO_POSTER_DELAY)))
    except (TypeError, ValueError):
        poster_delay = DEMO_POSTER_DELAY
    if not file_name:
        return web.json_response({"ok": False, "error": "file_name is required"}, status=400)

    steps = []
    steps.append(
        {
            "key": "telegram",
            "label": "Telegram → bot",
            "detail": f"“{file_name}” posted in the file channel → save_file() indexed it (simulated).",
            "ok": True,
        }
    )

    mime = "video/x-matroska" if file_name.lower().endswith(".mkv") else "video/mp4"
    if not looks_like_video(file_name, mime, "video"):
        steps.append(
            {
                "key": "filter",
                "label": "notify_new_file()",
                "detail": "Not a video file – the rail ignores subtitles, zips, images …",
                "ok": False,
            }
        )
        return web.json_response({"ok": False, "steps": steps, "error": "not_a_video"})

    if looks_like_series(file_name):
        steps.append(
            {
                "key": "filter",
                "label": "notify_new_file()",
                "detail": "Looks like a series episode (S01E02 …) – skipped because NEW_UPLOADED_ONLY_MOVIES=True.",
                "ok": False,
            }
        )
        return web.json_response({"ok": False, "steps": steps, "error": "series_skipped"})

    parsed = parse_release_name(file_name)
    steps.append(
        {
            "key": "parse",
            "label": "parse_release_name()",
            "detail": "title “{}”{}{}".format(
                parsed["title"],
                f" · year {parsed['year']}" if parsed["year"] else " · no year in the name",
                f" · quality {parsed['quality']}" if parsed["quality"] else "",
            ),
            "ok": True,
        }
    )

    count_before = await STORE.count()
    movie_id = await STORE.register_upload(
        title=parsed["title"],
        year=parsed["year"],
        quality=parsed["quality"],
        file_id=f"demo-{secrets.token_hex(4)}",
        file_name=file_name,
    )
    if not movie_id:
        steps.append(
            {
                "key": "db",
                "label": "recent_movies.register_upload()",
                "detail": "No usable title could be derived – nothing stored.",
                "ok": False,
            }
        )
        return web.json_response({"ok": False, "steps": steps, "error": "no_title"})

    merged = (await STORE.count()) == count_before
    steps.append(
        {
            "key": "db",
            "label": "recent_movies.register_upload()",
            "detail": (
                f"MOVIE_ID “{movie_id}” already existed → merged into it (quality added, no duplicate) "
                "and moved to the front"
                if merged
                else f"new document _id = “{movie_id}” (deterministic slug → the same movie can never appear twice)"
            ),
            "ok": True,
        }
    )

    doc = await STORE.get(movie_id)
    if doc.get("poster_url"):
        steps.append(
            {
                "key": "poster",
                "label": "poster worker",
                "detail": "poster already known for this movie → kept (no new lookup).",
                "ok": True,
                "done": True,
            }
        )
    else:
        if movie_id not in _POSTER_JOBS:
            _POSTER_JOBS.add(movie_id)
            asyncio.get_running_loop().create_task(simulated_poster_worker(movie_id, poster_delay))
        steps.append(
            {
                "key": "poster",
                "label": "poster worker",
                "detail": (
                    f"queued: TMDB → IMDb lookup for “{parsed['title']}” runs in the background "
                    f"(demo artwork arrives in ~{poster_delay:g}s; until then the branded placeholder shows). "
                    "In production this needs a free TMDB_API_KEY – or /setposter with a photo."
                ),
                "ok": True,
            }
        )

    movie = movie_api.public_movie(doc, BOT_USERNAME)
    steps.append(
        {
            "key": "api",
            "label": "GET /api/movies/new",
            "detail": f"“{movie['title']}” is now movies[0] – newest first, sanitized JSON (no file ids, no links).",
            "ok": True,
        }
    )
    steps.append(
        {
            "key": "page",
            "label": "Stream Mode page",
            "detail": f"spotlight + first card refreshed · deep link {build_deeplink(BOT_USERNAME, movie_id)}",
            "ok": True,
        }
    )
    return web.json_response(
        {
            "ok": True,
            "movie": movie,
            "movie_id": movie_id,
            "merged": merged,
            "poster_delay": poster_delay,
            "steps": steps,
        }
    )


async def demo_reset(request):
    """Back to the sample data."""
    await seed_store()
    return web.json_response({"ok": True, "count": await STORE.count()})


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
SIMULATOR_PANEL = r"""
<!-- ===================== demo only: upload simulator ===================== -->
<style>
    .ups { position: fixed; right: 18px; bottom: 18px; z-index: 90; width: min(400px, calc(100vw - 28px));
        font-family: "Inter", "Segoe UI", system-ui, sans-serif; color: #eef0f7; }
    .ups-card { border-radius: 18px; border: 1px solid rgba(245, 197, 24, 0.4); background: rgba(9, 10, 16, 0.94);
        box-shadow: 0 30px 70px -30px rgba(0, 0, 0, 1), 0 0 0 1px rgba(255, 255, 255, 0.04); backdrop-filter: blur(14px);
        overflow: hidden; }
    .ups-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 12px 14px;
        background: linear-gradient(135deg, rgba(245, 197, 24, 0.16), rgba(245, 197, 24, 0.04)); cursor: pointer; }
    .ups-head b { font-family: "Sora", "Segoe UI", system-ui, sans-serif; font-size: 13.5px; letter-spacing: -0.1px; }
    .ups-head small { display: block; color: #9aa1b9; font-size: 11px; margin-top: 2px; }
    .ups-toggle { border: 0; background: rgba(255, 255, 255, 0.08); color: #ffdd7a; width: 30px; height: 30px; border-radius: 9px;
        font-size: 16px; line-height: 1; cursor: pointer; }
    .ups-body { padding: 14px; display: grid; gap: 10px; }
    .ups.is-collapsed .ups-body { display: none; }
    .ups label { font-size: 11px; letter-spacing: 1.6px; text-transform: uppercase; color: #9aa1b9; font-weight: 600; }
    .ups-row { display: flex; gap: 8px; }
    .ups input { flex: 1; min-width: 0; padding: 11px 12px; border-radius: 11px; border: 1px solid rgba(255, 255, 255, 0.14);
        background: #0f1220; color: #eef0f7; font: inherit; font-size: 13.5px; }
    .ups input:focus { outline: 2px solid rgba(245, 197, 24, 0.55); border-color: transparent; }
    .ups-btn { padding: 11px 14px; border-radius: 11px; border: 0; font: inherit; font-weight: 700; font-size: 13px; cursor: pointer;
        background: linear-gradient(135deg, #f5c518, #ffdd7a); color: #20180a; white-space: nowrap; }
    .ups-btn[disabled] { opacity: 0.6; cursor: progress; }
    .ups-chips { display: flex; flex-wrap: wrap; gap: 6px; }
    .ups-chip { border: 1px solid rgba(255, 255, 255, 0.12); background: rgba(255, 255, 255, 0.05); color: #eef0f7; border-radius: 999px;
        padding: 5px 10px; font: inherit; font-size: 11.5px; cursor: pointer; }
    .ups-chip:hover { border-color: rgba(245, 197, 24, 0.6); color: #ffdd7a; }
    .ups-steps { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; max-height: 42vh; overflow: auto; }
    .ups-steps li { display: grid; grid-template-columns: 22px 1fr; gap: 8px; padding: 8px 10px; border-radius: 10px;
        background: rgba(255, 255, 255, 0.04); border: 1px solid rgba(255, 255, 255, 0.06); font-size: 12px; line-height: 1.45; }
    .ups-steps li i { display: grid; place-items: center; width: 20px; height: 20px; border-radius: 50%; font-style: normal; font-size: 11px;
        font-weight: 800; background: rgba(52, 211, 153, 0.18); color: #34d399; }
    .ups-steps li.is-bad i { background: rgba(248, 113, 113, 0.18); color: #f87171; }
    .ups-steps li.is-wait i { background: rgba(245, 197, 24, 0.18); color: #f5c518; }
    .ups-steps li b { display: block; color: #ffdd7a; font-weight: 600; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
    .ups-steps li span { color: #c9cede; overflow-wrap: anywhere; }
    .ups-foot { display: flex; justify-content: space-between; align-items: center; gap: 8px; font-size: 11px; color: #9aa1b9; }
    .ups-foot button { border: 0; background: none; color: #9aa1b9; font: inherit; font-size: 11px; cursor: pointer; text-decoration: underline; }
    .ups-foot button:hover { color: #ffdd7a; }
    @media (max-width: 640px) { .ups { right: 10px; bottom: 10px; left: 10px; width: auto; } }
</style>
<aside class="ups" id="uploadSimulator" aria-label="Upload simulator (demo)">
    <div class="ups-card">
        <div class="ups-head" id="upsHead">
            <div>
                <b>Upload simulator · demo only</b>
                <small>Type a movie file name → it goes through the bot's real pipeline → appears above.</small>
            </div>
            <button class="ups-toggle" type="button" id="upsToggle" aria-label="Collapse">–</button>
        </div>
        <div class="ups-body">
            <label for="upsName">Movie file name (as posted in your channel)</label>
            <form class="ups-row" id="upsForm">
                <input id="upsName" type="text" autocomplete="off" spellcheck="false"
                    value="Hmm (2024) 1080p WEB-DL Hindi.mkv">
                <button class="ups-btn" id="upsSend" type="submit">Upload to bot</button>
            </form>
            <div class="ups-chips" id="upsChips">
                <button class="ups-chip" type="button" data-name="hmm.mkv">hmm.mkv</button>
                <button class="ups-chip" type="button" data-name="Devara Part 1 (2024) 720p HDRip Telugu.mkv">Devara 720p</button>
                <button class="ups-chip" type="button" data-name="Bhool Bhulaiyaa 3 2024 1080p WEB-DL x264 ESub.mkv">Bhool Bhulaiyaa 3</button>
                <button class="ups-chip" type="button" data-name="Jawan 2023 2160p 4K HDR.mkv">Jawan again (merge)</button>
                <button class="ups-chip" type="button" data-name="Money Heist S01E02 720p.mkv">a series (skipped)</button>
            </div>
            <ol class="ups-steps" id="upsSteps" aria-live="polite"></ol>
            <div class="ups-foot">
                <span>In production the channel post triggers this — no button. Real posters need a free
                    TMDB_API_KEY (or <code>/setposter</code>).</span>
                <button type="button" id="upsReset">Reset demo</button>
            </div>
        </div>
    </div>
</aside>
<script>
(function () {
    var box = document.getElementById("uploadSimulator");
    var form = document.getElementById("upsForm");
    var input = document.getElementById("upsName");
    var send = document.getElementById("upsSend");
    var steps = document.getElementById("upsSteps");
    var chips = document.getElementById("upsChips");
    var reset = document.getElementById("upsReset");
    var toggle = document.getElementById("upsToggle");
    var head = document.getElementById("upsHead");

    function collapse(force) {
        var collapsed = force === undefined ? !box.classList.contains("is-collapsed") : force;
        box.classList.toggle("is-collapsed", collapsed);
        toggle.textContent = collapsed ? "+" : "–";
        toggle.setAttribute("aria-label", collapsed ? "Expand" : "Collapse");
    }
    toggle.addEventListener("click", function (event) { event.stopPropagation(); collapse(); });
    head.addEventListener("click", function () { collapse(); });

    function item(kind, label, text) {
        var li = document.createElement("li");
        li.className = kind === "bad" ? "is-bad" : kind === "wait" ? "is-wait" : "";
        var i = document.createElement("i");
        i.textContent = kind === "bad" ? "!" : kind === "wait" ? "…" : "✓";
        var body = document.createElement("div");
        var b = document.createElement("b");
        b.textContent = label;
        var span = document.createElement("span");
        span.textContent = text;
        body.appendChild(b);
        body.appendChild(span);
        li.appendChild(i);
        li.appendChild(body);
        steps.appendChild(li);
        return li;
    }

    function refreshRail() {
        if (window.MinatoNewlyUploaded) {
            return window.MinatoNewlyUploaded.refresh({ silent: true });
        }
        return Promise.resolve();
    }

    function scrollToSection() {
        var section = document.getElementById("newlyUploaded");
        if (section) {
            section.scrollIntoView({ behavior: "smooth", block: "start" });
        }
    }

    function upload(name) {
        while (steps.firstChild) { steps.removeChild(steps.firstChild); }
        send.disabled = true;
        var waiting = item("wait", "uploading…", "sending “" + name + "” through save_file() → notify_new_file()");
        return fetch("/demo/upload", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ file_name: name })
        })
            .then(function (response) { return response.json(); })
            .then(function (data) {
                steps.removeChild(waiting);
                (data.steps || []).forEach(function (step) {
                    var kind = !step.ok ? "bad" : (step.key === "poster" && !step.done) ? "wait" : "ok";
                    item(kind, step.label, step.detail);
                });
                if (!data.ok) {
                    if (data.error && !(data.steps || []).length) {
                        item("bad", "error", data.error);
                    }
                    return;
                }
                return refreshRail().then(function () {
                    scrollToSection();
                    var delay = Math.max(0, (data.poster_delay || 0) * 1000 + 600);
                    window.setTimeout(function () {
                        refreshRail().then(function () {
                            var pending = steps.querySelector("li.is-wait");
                            if (pending) {
                                pending.className = "";
                                pending.querySelector("i").textContent = "✓";
                                pending.querySelector("span").textContent =
                                    "poster found and cached → served from /api/movies/poster/" + data.movie_id + " (own origin, resized, no third-party call from the browser)";
                            }
                        });
                    }, delay);
                });
            })
            .catch(function (error) {
                item("bad", "request failed", String(error && error.message || error));
            })
            .then(function () { send.disabled = false; });
    }

    form.addEventListener("submit", function (event) {
        event.preventDefault();
        var name = input.value.trim();
        if (name) { upload(name); }
    });
    chips.addEventListener("click", function (event) {
        var chip = event.target.closest("[data-name]");
        if (!chip) { return; }
        input.value = chip.getAttribute("data-name");
        upload(input.value);
    });
    reset.addEventListener("click", function () {
        fetch("/demo/reset", { method: "POST" }).then(function () {
            while (steps.firstChild) { steps.removeChild(steps.firstChild); }
            item("ok", "reset", "sample data restored");
            return window.MinatoNewlyUploaded ? window.MinatoNewlyUploaded.refresh() : null;
        });
    });
})();
</script>
"""


def _inject_panel(html: str) -> str:
    """Add the simulator panel to a rendered page (demo only – not in the bot)."""
    marker = "</body>"
    if marker in html:
        return html.replace(marker, SIMULATOR_PANEL + marker, 1)
    return html + SIMULATOR_PANEL


def render_watch_html(state: str = "", api: str = "/api/movies/new", limit: int = DEFAULT_LIMIT,
                      upcoming_api: str = "/api/movies/upcoming") -> str:
    """Render the real ``req.html`` Stream Mode page together with its movie hero."""
    import jinja2

    hero = build_hero(
        WATCH_FILE,
        BOT_USERNAME,
        utcnow() - timedelta(hours=5),
        api_base="",
    )
    # The demo states live in the path (watch_hero.js appends the movie id to the
    # endpoint, so a query string would break the URL it builds).
    hero["watch_hero_art_url"] = "/api/movies/art" + (
        f"-{state}" if state in ("error", "noart", "hostile") else ""
    )

    template = jinja2.Template(TEMPLATE_WATCH.read_text(encoding="utf-8"))
    return template.render(
        **hero,
        file_name=WATCH_FILE.replace("_", " "),
        file_url="https://files.example/42/Marco.2024.1080p.mkv?hash=abcdef",
        file_size=WATCH_SIZE,
        file_unique_id="abcdef123456",
        update_channel_url="https://t.me/wanda_movies_update",
        bot_username=BOT_USERNAME,
        newly_uploaded_enabled=True,
        newly_uploaded_api=api,
        newly_uploaded_limit=limit,
        newly_uploaded_poll=DEMO_POLL_SECONDS,
        # "Coming Soon" rail – the countdown needs no polling at all.
        coming_soon_enabled=True,
        coming_soon_api=upcoming_api,
        coming_soon_limit=COMING_SOON_LIMIT,
        coming_soon_poll=0,
        asset_version=static_assets.version_token(),
    )


def render_page_html(limit: int, api: str = "/api/movies/new",
                     upcoming_api: str = "/api/movies/upcoming") -> str:
    """Render the real download page (``dl.html``) with representative values."""
    import jinja2

    template = jinja2.Template(TEMPLATE.read_text(encoding="utf-8"))
    return template.render(
        file_name="Jawan 2023 1080p WEB-DL HINDI x264",
        file_url="https://files.example/42/Jawan.2023.1080p.mkv?hash=abcdef",
        file_size="2.1 GB",
        file_unique_id="abcdef123456",
        update_channel_url="https://t.me/wanda_movies_update",
        bot_username=BOT_USERNAME,
        newly_uploaded_enabled=True,
        newly_uploaded_api=api,
        newly_uploaded_limit=limit,
        newly_uploaded_poll=DEMO_POLL_SECONDS,
        # "Coming Soon" rail – the countdown needs no polling at all.
        coming_soon_enabled=True,
        coming_soon_api=upcoming_api,
        coming_soon_limit=COMING_SOON_LIMIT,
        coming_soon_poll=0,
        asset_version=static_assets.version_token(),
    )


def _feed_api(request) -> str:
    state = request.rel_url.query.get("state")
    api = "/api/movies/new"
    if state in ("empty", "error", "loading", "hostile"):
        api += "?state=" + state
    return api


def _upcoming_api(request) -> str:
    """"Coming Soon" endpoint, honouring the same ``?state=`` demo switches.

    A separate ``?cs=`` lets one rail be pushed into a state while the other
    keeps rendering normally (e.g. ``/?state=error&cs=empty``).
    """
    state = request.rel_url.query.get("cs") or request.rel_url.query.get("state")
    api = "/api/movies/upcoming"
    if state in ("empty", "error", "loading", "hostile"):
        api += "?state=" + state
    return api


def _limit(request) -> int:
    try:
        return max(1, min(int(request.rel_url.query.get("limit", DEFAULT_LIMIT)), 20))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


async def index(request):
    """The Stream Mode page (req.html): player + hero + spotlight + rail + simulator."""
    state = request.rel_url.query.get("state")
    hero_state = state if state in ("error", "noart", "hostile") else ""
    html = render_watch_html(
        hero_state, _feed_api(request), _limit(request), _upcoming_api(request)
    )
    return web.Response(text=_inject_panel(html), content_type="text/html")


async def download_index(request):
    """The download page (dl.html) – same rail, same simulator."""
    html = render_page_html(
        _limit(request), _feed_api(request), _upcoming_api(request)
    )
    return web.Response(text=_inject_panel(html), content_type="text/html")


def build_app(limit: int = DEFAULT_LIMIT) -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/watch/demo", index)
    app.router.add_get("/download", download_index)
    app.router.add_get("/api/movies/new", demo_feed)
    app.router.add_get("/api/movies/upcoming", demo_upcoming)
    app.router.add_get("/api/movies/upcoming/poster/{movie_id}", demo_upcoming_poster)
    app.router.add_get("/api/movies/art/{movie_id}", demo_art)
    app.router.add_get("/api/movies/art-{state}/{movie_id}", demo_art)
    app.router.add_get("/api/movies/poster/{movie_id}", demo_poster)
    app.router.add_get("/api/movies/backdrop/{movie_id}", demo_backdrop)
    app.router.add_post("/demo/upload", demo_upload)
    app.router.add_get("/demo/upload", demo_upload)  # convenience: ?file_name=…
    app.router.add_post("/demo/reset", demo_reset)
    app.router.add_get("/demo/reset", demo_reset)
    app.add_routes(static_assets.routes)

    async def _seed(_app):
        await seed_store()
        await seed_upcoming()

    app.on_startup.append(_seed)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()

    print(f"Stream Mode preview + upload simulator → http://{args.host}:{args.port}/")
    print("States: /?state=empty · /?state=error · /?state=loading · /?state=hostile")
    print(f"Download page                        → http://{args.host}:{args.port}/download")
    print("Hero states: /watch/demo?state=error · ?state=noart · ?state=hostile")
    print('Simulate: curl -X POST -H "Content-Type: application/json" '
          f'-d \'{{"file_name": "Hmm (2024) 1080p.mkv"}}\' http://127.0.0.1:{args.port}/demo/upload')
    web.run_app(build_app(args.limit), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
