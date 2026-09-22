#!/usr/bin/env python3
"""Local preview for the Stream Mode "Newly Uploaded Movies" section.

Renders the **real** ``dreamxbotz/template/dl.html`` page (navbar, hero,
trending and poster wall included) and serves the **real** section assets, with
a fake ``/api/movies/new`` feed instead of MongoDB – so the design, the UI
states and the deep links can be checked in a browser without a running bot.

Usage::

    python tools/preview_section.py            # http://127.0.0.1:8080
    python tools/preview_section.py --port 9000

Handy URLs::

    /                     normal feed (12 sample movies)
    /?state=empty         "No new movies uploaded yet"
    /?state=error         error state + retry button
    /?state=loading       keeps the skeleton cards visible
    /?state=hostile       feed full of malicious values (hardening demo)
    /?limit=6             fewer cards

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

BOT_USERNAME = "MyMovieBot"
DEFAULT_LIMIT = 20
TEMPLATE = ROOT / "dreamxbotz" / "template" / "dl.html"

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
        return web.Response(status=404, body=movie_api.placeholder_svg(""), content_type="image/svg+xml")
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


def build_app(limit: int = DEFAULT_LIMIT) -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/movies/new", demo_feed)
    app.router.add_get("/api/movies/poster/{movie_id}", demo_poster)
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
    web.run_app(build_app(args.limit), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
