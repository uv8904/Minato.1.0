#!/usr/bin/env python3
"""Local preview for the Stream Mode web pages.

Renders the **real** templates (``dl.html`` for the "Newly Uploaded Movies"
rail, ``req.html`` for the watch page with its movie hero) and serves the
**real** assets, with fake JSON feeds instead of MongoDB – so the design, the
UI states and the deep links can be checked in a browser without a running bot.

Usage::

    python tools/preview_section.py            # http://127.0.0.1:8080
    python tools/preview_section.py --port 9000

Handy URLs::

    /                         rail page, normal feed (12 sample movies)
    /?state=empty             "No new movies uploaded yet"
    /?state=error             error state + retry button
    /?state=loading           keeps the skeleton cards visible
    /?state=hostile           feed full of malicious values (hardening demo)
    /?limit=6                 fewer cards

    /watch/demo               watch page + movie hero (poster, backdrop,
                              chips, Telegram deep link, copy button)
    /watch/demo?state=error   hero when the artwork API is down
    /watch/demo?state=noart   hero when no poster exists (placeholder card)
    /watch/demo?state=hostile hero against a hostile artwork API

This file is a **development tool only** – it is not imported by the bot.
"""
import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dreamxbotz.server import movie_api, static_assets  # noqa: E402
from dreamxbotz.util.movie_titles import utcnow  # noqa: E402
from dreamxbotz.util.watch_hero import build_context as build_hero  # noqa: E402

BOT_USERNAME = "MyMovieBot"
DEFAULT_LIMIT = 20
TEMPLATE = ROOT / "dreamxbotz" / "template" / "dl.html"
TEMPLATE_WATCH = ROOT / "dreamxbotz" / "template" / "req.html"
#: The file name of the "movie" that is being streamed on /watch/demo.
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


def _doc(title, year, qualities, hue, age_hours):
    """Build a database-shaped document (only the fields the API reads)."""
    uploaded = utcnow() - timedelta(hours=age_hours)
    movie_id = f"{title.lower().replace(' ', '-').replace('&', 'and')}-{year}"
    return {
        "_id": movie_id,
        "title": title,
        "year": year,
        "qualities": qualities,
        "search_query": f"{title} {year}",
        "poster_url": f"demo://poster/{hue}" if hue else None,
        "uploaded_at": uploaded,
        "last_upload_at": uploaded,
        "updated_at": uploaded,
        # internal fields – public_movie() must drop these
        "file_ids": ["internal-file-id"],
        "file_names": [f"{title}.{year}.1080p.mkv"],
    }


DOCS = [_doc(*row) for row in SAMPLE_MOVIES]


def demo_poster_svg(title, hue, quality):
    """A stand-in poster so the grid looks like a real poster wall."""
    safe_title = title.replace("&", "and")
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
  </defs>
  <rect width="480" height="720" fill="url(#p)"/>
  <circle cx="392" cy="120" r="180" fill="#ffffff" opacity="0.06"/>
  <circle cx="70" cy="330" r="150" fill="#000000" opacity="0.18"/>
  <rect y="360" width="480" height="360" fill="url(#v)"/>
  <text x="40" y="548" font-family="Sora, Segoe UI, system-ui, sans-serif" font-size="42"
        font-weight="700" fill="#f7f8fb">{safe_title}</text>
  <text x="40" y="590" font-family="Inter, Segoe UI, system-ui, sans-serif" font-size="19"
        fill="#e7eaf3">{quality} · demo poster</text>
  <text x="40" y="640" font-family="Inter, Segoe UI, system-ui, sans-serif" font-size="13"
        letter-spacing="3" fill="#f5c518">MINATOVERSE PREVIEW</text>
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
        font-weight="700" fill="#f7f8fb" opacity="0.92">{safe_title}</text>
  <text x="82" y="430" font-family="Inter, Segoe UI, system-ui, sans-serif" font-size="22"
        fill="#e7eaf3" opacity="0.8">{quality or "HD"} · demo backdrop · 16:9</text>
</svg>
"""


def _hue_for(value: str) -> str:
    """Stable demo colour per movie id."""
    palette = ["#f5c518", "#ff8a4c", "#54c1ff", "#b46cff", "#ff4d6d", "#38e8b0", "#ff7ab8"]
    return palette[sum(ord(char) for char in str(value)) % len(palette)]


def limit_default(request) -> int:
    try:
        return max(1, min(int(request.rel_url.query.get("limit", 20)), 20))
    except (TypeError, ValueError):
        return 20


async def demo_feed(request):
    """Same JSON contract as ``GET /api/movies/new`` – sanitized by the real helper."""
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
                        "quality": "<b>1080p</b>",
                        "added": "now",
                        "year": 2024,
                        "deeplink": "javascript:alert(1)",
                    },
                    {
                        "id": "jawan-2023",
                        "title": "Jawan",
                        "poster": "//evil.example/tracker.jpg",
                        "deeplink": "https://evil.example/steal",
                        "added": "2 days ago",
                    },
                ],
            }
        )
    try:
        limit = max(1, min(int(request.rel_url.query.get("limit", 20)), 20))
    except (TypeError, ValueError):
        limit = 20
    movies = [movie_api.public_movie(doc, BOT_USERNAME) for doc in DOCS[:limit]]
    return web.json_response(
        {
            "ok": True,
            "count": len(movies),
            "limit": limit,
            "updated_at": utcnow().isoformat().replace("+00:00", "Z"),
            "bot_username": BOT_USERNAME,
            "movies": movies,
        }
    )


async def demo_poster(request):
    """Demo posters: gradient artwork, plus the real placeholder for one movie."""
    movie_id = request.match_info["movie_id"]
    doc = next((d for d in DOCS if d["_id"] == movie_id), None)
    if not doc:
        # The watch-page hero asks for its own movie – make it look real too.
        title = (request.rel_url.query.get("q") or WATCH_FILE).strip()[:60]
        quality = "1080p" if request.rel_url.query.get("y") else "BR-Rip"
        return web.Response(
            body=demo_poster_svg(title, _hue_for(movie_id), quality),
            content_type="image/svg+xml",
        )
    if not doc.get("poster_url"):
        return web.Response(
            body=movie_api.placeholder_svg(doc["title"]),
            content_type="image/svg+xml",
        )
    hue = doc["poster_url"].rsplit("/", 1)[-1]
    return web.Response(
        body=demo_poster_svg(doc["title"], hue, doc["qualities"][0]),
        content_type="image/svg+xml",
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
    """16:9 demo artwork for the hero band."""
    movie_id = request.match_info["movie_id"]
    title = (request.rel_url.query.get("q") or "Marco").strip()[:60]
    return web.Response(
        body=demo_backdrop_svg(title, _hue_for(movie_id), "1080p"),
        content_type="image/svg+xml",
    )


def render_watch_html(state: str = "") -> str:
    """Render the real ``req.html`` watch page together with its movie hero."""
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
        newly_uploaded_api="/api/movies/new",
        newly_uploaded_limit=DEFAULT_LIMIT,
        asset_version=static_assets.version_token(),
    )


def render_page_html(limit: int, api: str = "/api/movies/new") -> str:
    """Render the real template with representative placeholder values."""
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
        asset_version=static_assets.version_token(),
    )


def index(request):
    """The real page, with ?state=… pushed through to the demo feed."""
    state = request.rel_url.query.get("state")
    api = "/api/movies/new"
    if state in ("empty", "error", "loading", "hostile"):
        api += "?state=" + state
    try:
        limit = max(1, min(int(request.rel_url.query.get("limit", DEFAULT_LIMIT)), 20))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    return web.Response(text=render_page_html(limit, api), content_type="text/html")


def watch_index(request):
    """The real watch page (req.html) with the movie hero on top."""
    state = request.rel_url.query.get("state")
    return web.Response(
        text=render_watch_html(state if state in ("error", "noart", "hostile") else ""),
        content_type="text/html",
    )


def build_app(limit: int = DEFAULT_LIMIT) -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/watch/demo", watch_index)
    app.router.add_get("/api/movies/new", demo_feed)
    app.router.add_get("/api/movies/art/{movie_id}", demo_art)
    app.router.add_get("/api/movies/art-{state}/{movie_id}", demo_art)
    app.router.add_get("/api/movies/poster/{movie_id}", demo_poster)
    app.router.add_get("/api/movies/backdrop/{movie_id}", demo_backdrop)
    app.add_routes(static_assets.routes)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()

    print(f"Newly Uploaded Movies preview → http://{args.host}:{args.port}/")
    print("States: /?state=empty · /?state=error · /?state=loading · /?state=hostile")
    print(f"Watch page + movie hero      → http://{args.host}:{args.port}/watch/demo")
    print("Matches: /watch/demo?state=error · ?state=noart · ?state=hostile")
    web.run_app(build_app(args.limit), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
