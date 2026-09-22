"""End-to-end check of the Stream Mode **upload simulator** (``tools/preview_section.py``).

The tool runs the bot's real post-index pipeline on top of an in-memory
collection, so this is the closest thing to "upload a movie, look at the
website" that can run without Telegram or MongoDB:

    POST /demo/upload {"file_name": "Hmm (2024) 1080p WEB-DL.mkv"}
        → parse_release_name()                 (real)
        → RecentMoviesStore.register_upload()  (real, in-memory collection)
        → poster worker                        (simulated, delayed)
        → GET /api/movies/new                  (real public_movie() shape)
        → the page: spotlight + first card

Run with the project dependencies installed::

    pytest tests/test_preview_upload_simulator.py -q
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import preview_section as tool  # noqa: E402


def run(coro):
    return asyncio.run(coro)


async def _client():
    return TestClient(TestServer(tool.build_app()))


def test_seed_is_served_newest_first_through_the_real_store():
    async def scenario():
        async with await _client() as client:
            response = await client.get("/api/movies/new?limit=3")
            payload = await response.json()
            return response.status, payload

    status, payload = run(scenario())
    assert status == 200
    assert payload["ok"] is True
    assert payload["bot_username"] == tool.BOT_USERNAME
    ids = [movie["id"] for movie in payload["movies"]]
    assert ids[0] == "jawan-2023"  # uploaded 0.2 h ago in SAMPLE_MOVIES
    assert len(ids) == 3
    first = payload["movies"][0]
    assert first["poster"].startswith("/api/movies/poster/jawan-2023?v=")
    assert first["backdrop"].startswith("/api/movies/backdrop/jawan-2023?v=")
    assert first["deeplink"] == "https://t.me/MyMovieBot?start=movie_jawan-2023"
    assert first["quality_label"] == "1080p, 720p, 480p"  # three files merged into one card


def test_uploading_hmm_puts_it_first_with_a_poster_after_the_worker_ran():
    async def scenario():
        async with await _client() as client:
            response = await client.post(
                "/demo/upload",
                json={"file_name": "Hmm (2024) 1080p WEB-DL Hindi.mkv", "poster_delay": 0.2},
            )
            upload = await response.json()

            feed = await (await client.get("/api/movies/new?limit=2")).json()
            newest = feed["movies"][0]
            poster_before = await client.get(newest["poster"] + "&w=320")
            poster_before_body = await poster_before.read()

            await asyncio.sleep(0.5)  # let the simulated poster worker finish

            feed_after = await (await client.get("/api/movies/new?limit=2")).json()
            newest_after = feed_after["movies"][0]
            poster_after = await client.get(newest_after["poster"] + "&w=480")
            poster_after_body = await poster_after.read()
            backdrop = await client.get(newest_after["backdrop"] + "&w=1280")
            backdrop_body = await backdrop.read()
            return (
                upload,
                newest,
                (poster_before.status, poster_before.headers["Content-Type"], poster_before_body),
                newest_after,
                (poster_after.status, poster_after_body),
                (backdrop.status, backdrop.headers["Content-Type"], backdrop_body),
            )

    upload, newest, before, newest_after, after, backdrop = run(scenario())

    # 1. the pipeline report
    assert upload["ok"] is True
    assert upload["movie_id"] == "hmm-2024"
    assert upload["merged"] is False
    labels = [step["label"] for step in upload["steps"]]
    assert labels == [
        "Telegram → bot",
        "parse_release_name()",
        "recent_movies.register_upload()",
        "poster worker",
        "GET /api/movies/new",
        "Stream Mode page",
    ]
    assert all(step["ok"] for step in upload["steps"])
    assert "title “Hmm” · year 2024 · quality 1080p" in upload["steps"][1]["detail"]
    assert "https://t.me/MyMovieBot?start=movie_hmm-2024" in upload["steps"][-1]["detail"]

    # 2. straight away: first in the feed, placeholder poster while the worker runs
    assert newest["id"] == "hmm-2024"
    assert newest["title"] == "Hmm"
    assert newest["year"] == 2024
    assert newest["quality"] == "1080p"
    assert newest["added"] == "just now"
    assert newest["has_poster"] is False
    assert newest["deeplink"] == "https://t.me/MyMovieBot?start=movie_hmm-2024"
    assert before[0] == 200 and before[1].startswith("image/svg+xml")
    assert b"MINATOVERSE" in before[2] and b"DEMO POSTER" not in before[2]

    # 3. a moment later: poster + backdrop are there, still first
    assert newest_after["id"] == "hmm-2024"
    assert newest_after["has_poster"] is True
    assert newest_after["poster"] != newest["poster"]  # cache-busting version changed
    assert after[0] == 200 and b"DEMO POSTER" in after[1]
    assert backdrop[0] == 200 and backdrop[1].startswith("image/svg+xml")
    assert b"demo backdrop" in backdrop[2]


def test_second_quality_of_the_same_movie_merges_instead_of_duplicating():
    async def scenario():
        async with await _client() as client:
            await client.post("/demo/upload", json={"file_name": "Hmm 2024 1080p.mkv", "poster_delay": 0})
            response = await client.post(
                "/demo/upload", json={"file_name": "Hmm (2024) 720p HDRip x264.mkv", "poster_delay": 0}
            )
            second = await response.json()
            feed = await (await client.get("/api/movies/new")).json()
            return second, feed

    second, feed = run(scenario())
    assert second["ok"] is True
    assert second["merged"] is True
    assert second["movie"]["quality_label"] == "1080p, 720p"
    assert "merged" in second["steps"][2]["detail"]
    ids = [movie["id"] for movie in feed["movies"]]
    assert ids.count("hmm-2024") == 1
    assert len(ids) == len(set(ids))
    assert feed["count"] == len(tool.SAMPLE_MOVIES) + 1


@pytest.mark.parametrize(
    "file_name,error",
    [
        ("Money Heist S01E02 720p.mkv", "series_skipped"),
        ("Hmm.2024.poster.jpg", "not_a_video"),
    ],
)
def test_non_movies_are_reported_and_not_stored(file_name, error):
    async def scenario():
        async with await _client() as client:
            response = await client.post("/demo/upload", json={"file_name": file_name})
            payload = await response.json()
            feed = await (await client.get("/api/movies/new")).json()
            return payload, feed

    payload, feed = run(scenario())
    assert payload["ok"] is False
    assert payload["error"] == error
    assert payload["steps"][-1]["ok"] is False
    assert feed["count"] == len(tool.SAMPLE_MOVIES)


def test_upload_without_a_name_is_rejected_and_reset_restores_the_samples():
    async def scenario():
        async with await _client() as client:
            bad = await client.post("/demo/upload", json={})
            await client.post("/demo/upload", json={"file_name": "Hmm 2024 1080p.mkv", "poster_delay": 0})
            reset = await (await client.post("/demo/reset")).json()
            feed = await (await client.get("/api/movies/new")).json()
            return bad.status, reset, feed

    status, reset, feed = run(scenario())
    assert status == 400
    assert reset == {"ok": True, "count": len(tool.SAMPLE_MOVIES)}
    assert "hmm-2024" not in [movie["id"] for movie in feed["movies"]]


@pytest.mark.parametrize("path", ["/", "/watch/demo", "/download"])
def test_pages_render_the_real_templates_with_the_simulator_panel(path):
    async def scenario():
        async with await _client() as client:
            response = await client.get(path)
            return response.status, await response.text()

    status, html = run(scenario())
    assert status == 200
    # real section + spotlight container, demo poll interval, demo bot username
    assert 'id="newlyUploaded"' in html
    assert 'id="nuSpotlight"' in html
    assert 'data-poll="%d"' % tool.DEMO_POLL_SECONDS in html
    assert 'data-bot="MyMovieBot"' in html
    # the simulator is injected only by the tool (never part of the templates)
    assert 'id="uploadSimulator"' in html
    assert "/demo/upload" in html
    for template in ("req.html", "dl.html"):
        assert "uploadSimulator" not in (ROOT / "dreamxbotz" / "template" / template).read_text(encoding="utf-8")
    assert "{{" not in html and "{%" not in html


def test_memory_collection_sorts_like_mongo_newest_first():
    """The in-memory stand-in must honour the store's SORT spec (None last)."""
    from datetime import datetime, timezone

    collection = tool.MemoryCollection()
    base = datetime(2026, 9, 22, tzinfo=timezone.utc)

    async def scenario():
        await collection.update_one({"_id": "b"}, {"$set": {"title": "B", "last_upload_at": base}}, upsert=True)
        await collection.update_one({"_id": "a"}, {"$set": {"title": "A", "last_upload_at": base}}, upsert=True)
        await collection.update_one({"_id": "old"}, {"$set": {"title": "Old"}}, upsert=True)
        await collection.update_one(
            {"_id": "new"},
            {"$set": {"title": "New", "last_upload_at": base.replace(hour=5)}, "$addToSet": {"qualities": "720p"}},
            upsert=True,
        )
        await collection.update_one({"_id": "new"}, {"$addToSet": {"qualities": "720p"}, "$inc": {"file_total": 1}})
        rows = [doc async for doc in collection.find({}, {"file_ids": 0}).sort(tool.SORT).limit(10)]
        return rows

    rows = run(scenario())
    assert [row["_id"] for row in rows] == ["new", "a", "b", "old"]
    assert rows[0]["qualities"] == ["720p"]  # $addToSet stays unique
    assert rows[0]["file_total"] == 1
