"""Tests for the Stream Mode "Coming Soon" rail (upcoming releases + countdown).

Covers everything that can be verified without Telegram, TMDB or a real
MongoDB:

* ``dreamxbotz/util/coming_soon.py`` – date parsing, the countdown maths, the
  TMDB payload → rows mapping, the ``/comingsoon`` list text
* ``database/upcoming_db.py``       – upsert de-duplication, staleness,
  the notify counter, housekeeping (fake collection)
* ``dreamxbotz/server/movie_api.py``– ``/api/movies/upcoming`` (+ its poster
  proxy): the whitelisted JSON shape, disabled/503 paths, ETag
* ``dreamxbotz/server/static_assets.py`` – the two new assets are whitelisted
* both Stream Mode templates        – the section and its assets are wired up

Run with the project dependencies installed::

    pytest tests/test_coming_soon.py -q
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from database.upcoming_db import UpcomingMoviesStore  # noqa: E402
from dreamxbotz.server import movie_api, static_assets  # noqa: E402
from dreamxbotz.util import coming_soon as cs  # noqa: E402

#: A fixed "now" so every countdown assertion is deterministic.
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Pure date / countdown helpers
# --------------------------------------------------------------------------- #
def test_parse_release_date_accepts_upstream_format():
    parsed = cs.parse_release_date("2026-12-18")
    assert parsed == datetime(2026, 12, 18, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw", ["", None, "0000-00-00", "2026-13-40", "not-a-date", "2026-12"])
def test_parse_release_date_rejects_unusable_values(raw):
    """A blank or broken upstream date must not crash the rail."""
    assert cs.parse_release_date(raw) is None


def test_days_until_counts_calendar_days_not_24h_blocks():
    """"Releases tomorrow" at 23:00 is 1 day away, not 0."""
    release = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 9, 24, 23, 0, tzinfo=timezone.utc)
    assert cs.days_until(release, now) == 1


def test_days_until_is_zero_today_and_negative_in_the_past():
    today = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
    assert cs.days_until(today, NOW) == 0
    assert cs.days_until(today - timedelta(days=2), NOW) == -2
    assert cs.days_until(None, NOW) is None


def test_countdown_parts_matches_the_javascript_breakdown():
    parts = cs.countdown_parts(90061)  # 1d 1h 1m 1s
    assert parts == {"days": 1, "hours": 1, "minutes": 1, "seconds": 1}


def test_countdown_parts_never_goes_negative():
    assert cs.countdown_parts(-500) == {"days": 0, "hours": 0, "minutes": 0, "seconds": 0}
    assert cs.countdown_parts("nonsense") == {"days": 0, "hours": 0, "minutes": 0, "seconds": 0}


def test_seconds_until_floors_at_zero_once_released():
    assert cs.seconds_until(NOW - timedelta(days=1), NOW) == 0
    assert cs.seconds_until(NOW + timedelta(hours=1), NOW) == 3600


@pytest.mark.parametrize(
    "release, expected",
    [
        (datetime(2026, 9, 24, tzinfo=timezone.utc), "releases today"),
        (datetime(2026, 9, 25, tzinfo=timezone.utc), "releases tomorrow"),
        (datetime(2026, 10, 6, tzinfo=timezone.utc), "in 12 days"),
        (datetime(2026, 9, 23, tzinfo=timezone.utc), "released yesterday"),
        (datetime(2026, 9, 19, tzinfo=timezone.utc), "released 5 days ago"),
    ],
)
def test_countdown_label_wording(release, expected):
    assert cs.countdown_label(release, NOW) == expected


@pytest.mark.parametrize(
    "release, expected",
    [
        (datetime(2026, 9, 23, tzinfo=timezone.utc), "released"),  # inside the grace window
        (datetime(2026, 9, 24, tzinfo=timezone.utc), "soon"),      # today counts as soon
        (datetime(2026, 9, 29, tzinfo=timezone.utc), "soon"),      # 5 days out
        (datetime(2026, 12, 18, tzinfo=timezone.utc), "upcoming"),
        (datetime(2026, 8, 1, tzinfo=timezone.utc), ""),           # long gone
        (None, ""),
    ],
)
def test_urgency_picks_the_chip(release, expected):
    assert cs.urgency(release, NOW, soon=7, grace=3) == expected


def test_release_date_label_format():
    assert cs.release_date_label("2026-12-18") == "18 Dec 2026"
    assert cs.release_date_label(None) == ""


def test_release_cutoff_honours_the_grace_window():
    cutoff = cs.release_cutoff(NOW, grace=3)
    assert cutoff == datetime(2026, 9, 21, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# TMDB payload -> rows
# --------------------------------------------------------------------------- #
def tmdb_item(**overrides):
    item = {
        "id": 91316,
        "title": "Avatar 3",
        "original_title": "Avatar: Fire and Ash",
        "overview": "The Sully family returns.",
        "poster_path": "/avatar3.jpg",
        "release_date": "2026-12-18",
        "popularity": 187.4,
    }
    item.update(overrides)
    return item


def test_upcoming_from_payload_builds_rows():
    rows = cs.upcoming_from_payload({"results": [tmdb_item()]}, now=NOW)
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Avatar 3"
    assert row["year"] == 2026
    assert row["release_date_raw"] == "2026-12-18"
    assert row["tmdb_id"] == 91316
    assert row["popularity"] == 187.4
    # The poster stays an upstream URL here; the API layer proxies the bytes.
    assert row["poster_url"].startswith("https://image.tmdb.org/t/p/")
    assert row["poster_source"] == "tmdb"


def test_upcoming_from_payload_drops_rows_without_a_date_or_title():
    payload = {
        "results": [
            tmdb_item(id=1, title="No Date", release_date=""),
            tmdb_item(id=2, title="", original_title="", release_date="2026-12-18"),
            tmdb_item(id=3, title="Kept", release_date="2026-12-18"),
            "not-a-dict",
        ]
    }
    rows = cs.upcoming_from_payload(payload, now=NOW)
    assert [row["title"] for row in rows] == ["Kept"]


def test_upcoming_from_payload_drops_releases_beyond_the_grace_window():
    payload = {
        "results": [
            tmdb_item(id=1, title="Ancient", release_date="2020-01-01"),
            tmdb_item(id=2, title="Just Out", release_date="2026-09-23"),
            tmdb_item(id=3, title="Future", release_date="2026-12-18"),
        ]
    }
    rows = cs.upcoming_from_payload(payload, now=NOW)
    assert [row["title"] for row in rows] == ["Just Out", "Future"]


def test_upcoming_from_payload_de_duplicates_by_movie_id():
    payload = {"results": [tmdb_item(), tmdb_item(id=999)]}
    rows = cs.upcoming_from_payload(payload, now=NOW)
    assert len(rows) == 1


def test_upcoming_from_payload_handles_a_broken_body():
    assert cs.upcoming_from_payload({}, now=NOW) == []
    assert cs.upcoming_from_payload(None, now=NOW) == []
    assert cs.upcoming_from_payload({"results": "nope"}, now=NOW) == []


# --------------------------------------------------------------------------- #
# /comingsoon list text
# --------------------------------------------------------------------------- #
def upcoming_doc(**overrides):
    doc = {
        "_id": "avatar-3-2026",
        "title": "Avatar 3",
        "year": 2026,
        "release_date": datetime(2026, 12, 18, tzinfo=timezone.utc),
        "notify_total": 37,
        "poster_url": "https://image.tmdb.org/t/p/w500/avatar3.jpg",
    }
    doc.update(overrides)
    return doc


def test_upcoming_list_text_shows_title_date_countdown_and_waiters():
    text = cs.upcoming_list_text([upcoming_doc()], now=NOW)
    assert "Avatar 3" in text
    assert "(2026)" in text
    assert "18 Dec 2026" in text
    assert "in 85 days" in text
    assert "🔥 37 waiting" in text
    assert "1️⃣" in text


def test_upcoming_list_text_is_empty_when_there_is_nothing_to_show():
    assert cs.upcoming_list_text([], now=NOW) == ""
    assert cs.upcoming_list_text([{"_id": "", "title": ""}], now=NOW) == ""


def test_notify_key_is_stable_short_and_hash_free():
    key = cs.notify_key_for("avatar-3-2026")
    assert key == cs.notify_key_for("avatar-3-2026")
    assert key != cs.notify_key_for("jawan-2023")
    assert key.startswith("cs-")
    assert "#" not in key
    # callback_data is capped at 64 bytes: notify#<key>#<user id> must fit.
    assert len(f"notify#{key}#{9876543210}") <= 64


def test_remember_upcoming_maps_the_key_back_to_the_card():
    key = cs.remember_upcoming("avatar-3-2026", "Avatar 3")
    assert cs.upcoming_movie_id_for(key) == "avatar-3-2026"
    assert cs.upcoming_movie_id_for("cs-does-not-exist") == ""
    cs.NOTIFY_MOVIE_IDS.pop(key, None)


# --------------------------------------------------------------------------- #
# Public JSON shape
# --------------------------------------------------------------------------- #
def test_public_movie_whitelists_fields_and_never_leaks_the_poster_url():
    movie = cs.public_upcoming_movie(upcoming_doc(), "TestBot", NOW)
    assert set(movie) == {
        "id", "title", "year", "release_date", "release_label", "days_left",
        "seconds_left", "countdown", "state", "has_poster", "poster",
        "waiting", "deeplink",
    }
    assert movie["id"] == "avatar-3-2026"
    assert movie["release_date"] == "2026-12-18T00:00:00Z"
    assert movie["days_left"] == 85
    assert movie["state"] == "upcoming"
    assert movie["waiting"] == 37
    # The upstream URL is replaced by our own origin.
    assert movie["poster"] == "/api/movies/upcoming/poster/avatar-3-2026"
    assert "image.tmdb.org" not in json.dumps(movie)
    assert movie["deeplink"] == "https://t.me/TestBot?start=movie_avatar-3-2026"


def test_public_movies_drops_documents_without_a_usable_id():
    movies = cs.public_upcoming_movies(
        [upcoming_doc(), {"_id": "", "title": "Ghost"}], "TestBot", NOW
    )
    assert [movie["id"] for movie in movies] == ["avatar-3-2026"]


# --------------------------------------------------------------------------- #
# Store (fake collection)
# --------------------------------------------------------------------------- #
class FakeCursor:
    def __init__(self, docs, projection=None):
        self.docs = [_project(doc, projection) for doc in docs]

    def sort(self, spec):
        for field, direction in reversed(list(spec)):
            self.docs.sort(key=lambda doc: doc.get(field) or 0, reverse=direction < 0)
        return self

    def limit(self, count):
        self.docs = self.docs[: max(0, int(count))]
        return self

    def __aiter__(self):
        async def generator():
            for doc in self.docs:
                yield doc

        return generator()


def _project(doc, projection):
    """Apply a Mongo-style projection so the fake behaves like motor.

    Without this the store's ``INTERNAL_FIELDS`` would be ignored and a test
    could never notice that a field the API depends on was stripped.
    """
    if not projection:
        return dict(doc)
    excluded = {key for key, flag in projection.items() if not flag}
    return {key: value for key, value in doc.items() if key not in excluded}


class FakeCollection:
    """Records the update operators the store uses."""

    def __init__(self):
        self.docs = {}
        self.updates = []

    def find(self, filter=None, projection=None):
        docs = list(self.docs.values())
        condition = (filter or {}).get("release_date")
        if isinstance(condition, dict) and "$gte" in condition:
            docs = [d for d in docs if (d.get("release_date") or 0) >= condition["$gte"]]
        return FakeCursor(docs, projection)

    async def find_one(self, filter, projection=None):
        return self.docs.get(filter.get("_id"))

    async def count_documents(self, filter=None):
        return len(self.docs)

    async def delete_many(self, filter=None):
        condition = (filter or {}).get("release_date", {})
        before = condition.get("$lt")
        doomed = [
            key for key, doc in self.docs.items()
            if before is not None and (doc.get("release_date") or before) < before
        ]
        for key in doomed:
            self.docs.pop(key)
        return SimpleNamespace(deleted_count=len(doomed))

    async def update_one(self, filter, update, upsert=False):
        key = filter["_id"]
        self.updates.append((key, update))
        doc = self.docs.get(key, {"_id": key})
        for field, value in (update.get("$set") or {}).items():
            doc[field] = value
        for field, value in (update.get("$setOnInsert") or {}).items():
            doc.setdefault(field, value)
        for field, value in (update.get("$inc") or {}).items():
            doc[field] = int(doc.get(field) or 0) + int(value)
        self.docs[key] = doc
        return SimpleNamespace(matched_count=1, modified_count=1)

    async def create_index(self, *args, **kwargs):
        return "index"


def make_store(docs=None, meta=None):
    collection = FakeCollection()
    for doc in docs or []:
        collection.docs[doc["_id"]] = dict(doc)
    meta_collection = FakeCollection()
    for doc in meta or []:
        meta_collection.docs[doc["_id"]] = dict(doc)
    store = UpcomingMoviesStore(
        collection=collection,
        meta_collection=meta_collection,
        collection_name="upcoming_movies",
        meta_collection_name="upcoming_meta",
    )
    return store, collection, meta_collection


def test_save_movie_derives_the_same_id_as_the_uploaded_rail():
    store, collection, _ = make_store()
    movie_id = run(store.save_movie(
        title="Avatar 3",
        year=2026,
        release_date=datetime(2026, 12, 18, tzinfo=timezone.utc),
        release_date_raw="2026-12-18",
        now=NOW,
    ))
    from dreamxbotz.util.movie_titles import movie_id_for

    assert movie_id == movie_id_for("Avatar 3", 2026)
    assert collection.docs[movie_id]["release_date_raw"] == "2026-12-18"
    # The counter starts at zero and is only ever $inc-ed.
    assert collection.docs[movie_id]["notify_total"] == 0


def test_save_movie_needs_a_release_date():
    store, collection, _ = make_store()
    assert run(store.save_movie(title="Avatar 3", year=2026)) is None
    assert collection.docs == {}


def test_add_notify_raises_the_waiting_counter():
    store, collection, _ = make_store([upcoming_doc()])
    assert run(store.add_notify("avatar-3-2026")) == 38
    assert run(store.add_notify("avatar-3-2026")) == 39
    assert collection.docs["avatar-3-2026"]["notify_total"] == 39
    assert run(store.add_notify("")) == 0


def test_is_stale_is_true_before_the_first_refresh():
    store, _, _ = make_store()
    assert run(store.is_stale(6 * 3600, now=NOW)) is True


def test_is_stale_flips_back_once_refreshed():
    store, _, meta = make_store(
        meta=[{"_id": "refresh", "fetched_at": NOW - timedelta(hours=1)}]
    )
    assert run(store.is_stale(6 * 3600, now=NOW)) is False
    assert run(store.is_stale(1800, now=NOW)) is True


def test_mark_fetched_stores_a_single_meta_document():
    store, _, meta = make_store()
    run(store.mark_fetched(12, now=NOW))
    assert meta.docs["refresh"]["movies"] == 12
    assert meta.docs["refresh"]["fetched_at"] == NOW


def test_list_upcoming_sorts_soonest_first_and_applies_the_cutoff():
    store, _, _ = make_store([
        upcoming_doc(_id="far-2027", title="Far", release_date=datetime(2027, 5, 1, tzinfo=timezone.utc)),
        upcoming_doc(_id="near-2026", title="Near", release_date=datetime(2026, 10, 1, tzinfo=timezone.utc)),
        upcoming_doc(_id="old-2026", title="Old", release_date=datetime(2026, 9, 1, tzinfo=timezone.utc)),
    ])
    rows = run(store.list_upcoming(10, cutoff=datetime(2026, 9, 21, tzinfo=timezone.utc)))
    assert [row["_id"] for row in rows] == ["near-2026", "far-2027"]


def test_list_projection_keeps_poster_url_so_has_poster_is_real():
    """Regression: stripping poster_url in the query made every card poster-less.

    The upstream URL is kept out of the *browser* by ``public_upcoming_movie``'s
    whitelist, not by the projection – the projection must still carry it, or
    ``has_poster`` silently reports False for every movie that has artwork.
    """
    from database.upcoming_db import INTERNAL_FIELDS

    assert "poster_url" not in INTERNAL_FIELDS

    store, _, _ = make_store([upcoming_doc()])
    rows = run(store.list_upcoming(10))
    assert rows and rows[0].get("poster_url")
    assert cs.public_upcoming_movie(rows[0], "TestBot", NOW)["has_poster"] is True


def test_purge_old_only_drops_expired_releases():
    store, collection, _ = make_store([
        upcoming_doc(_id="old-2026", release_date=datetime(2026, 9, 1, tzinfo=timezone.utc)),
        upcoming_doc(_id="kept-2026", release_date=datetime(2026, 12, 18, tzinfo=timezone.utc)),
    ])
    removed = run(store.purge_old(datetime(2026, 9, 21, tzinfo=timezone.utc)))
    assert removed == 1
    assert list(collection.docs) == ["kept-2026"]
    assert run(store.purge_old(None)) == 0


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
class FakeUpcomingStore:
    """In-memory stand-in for ``UpcomingMoviesStore`` (no Mongo, no network)."""

    def __init__(self, docs=None, error=None, stale=False):
        self.docs = list(docs or [])
        self.error = error
        self.stale = stale
        self.calls = []

    async def list_upcoming(self, limit=12, cutoff=None, projection=None,
                            raise_on_error=False):
        self.calls.append(("list_upcoming", limit, cutoff))
        if self.error:
            raise self.error
        return self.docs[:limit]

    async def is_stale(self, ttl_seconds, now=None):
        self.calls.append(("is_stale", ttl_seconds))
        return self.stale

    async def get(self, movie_id):
        self.calls.append(("get", movie_id))
        for doc in self.docs:
            if doc["_id"] == movie_id:
                return doc
        return None


def make_client(docs=None, error=None, stale=False, bot_username="TestBot"):
    """Build an aiohttp app around the coming-soon handlers with a fake store."""
    fake = FakeUpcomingStore(docs or [], error=error, stale=stale)
    app = web.Application()
    app.add_routes(movie_api.routes)
    app["nu_bot_username"] = bot_username
    movie_api._UPCOMING_STORE = fake
    movie_api._upcoming_store = lambda: fake
    return app, fake


async def _get(app, path, **kwargs):
    async with TestClient(TestServer(app)) as client:
        response = await client.get(path, **kwargs)
        return response, await response.text()


@pytest.fixture(autouse=True)
def _clean_movie_api_state():
    """Keep the in-process poster cache + refresh flag from leaking."""
    movie_api._POSTER_CACHE.clear()
    movie_api._POSTER_CACHE_BYTES = 0
    movie_api._UPCOMING_REFRESH_RUNNING = False
    yield
    movie_api._UPCOMING_REFRESH_RUNNING = False


def test_api_returns_the_whitelisted_shape():
    app, _ = make_client([upcoming_doc()])
    response, body = run(_get(app, "/api/movies/upcoming"))
    assert response.status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["bot_username"] == "TestBot"
    movie = payload["movies"][0]
    assert set(movie) == {
        "id", "title", "year", "release_date", "release_label", "days_left",
        "seconds_left", "countdown", "state", "has_poster", "poster",
        "waiting", "deeplink",
    }
    assert movie["deeplink"] == "https://t.me/TestBot?start=movie_avatar-3-2026"
    # No upstream URL, no file id, no token anywhere in the body.
    assert "image.tmdb.org" not in body
    assert "poster_url" not in body


def test_api_honours_the_limit_argument_and_caps_it():
    docs = [upcoming_doc(_id=f"movie-{n}", title=f"Movie {n}") for n in range(30)]
    app, store = make_client(docs)
    _, body = run(_get(app, "/api/movies/upcoming?limit=5"))
    assert json.loads(body)["count"] == 5
    list_calls = [call for call in store.calls if call[0] == "list_upcoming"]
    assert list_calls and list_calls[0][1] == 5

    app, store = make_client(docs)
    _, body = run(_get(app, "/api/movies/upcoming?limit=9999"))
    assert json.loads(body)["limit"] == movie_api.UPCOMING_MAX_LIMIT


def test_api_reports_disabled_instead_of_an_empty_rail():
    app, _ = make_client([upcoming_doc()])
    original = cs.is_enabled
    cs.is_enabled = lambda: False
    try:
        response, body = run(_get(app, "/api/movies/upcoming"))
    finally:
        cs.is_enabled = original
    assert response.status == 200
    payload = json.loads(body)
    assert payload == {"ok": True, "count": 0, "limit": 12, "movies": [], "disabled": True}


def test_api_answers_503_when_the_database_is_down():
    app, _ = make_client(error=RuntimeError("mongo is down"))
    response, body = run(_get(app, "/api/movies/upcoming"))
    assert response.status == 503
    assert json.loads(body)["error"] == "database_unavailable"


def test_api_sends_an_etag_and_answers_304():
    app, _ = make_client([upcoming_doc()])

    async def scenario():
        results = []
        async with TestClient(TestServer(app)) as client:
            first = await client.get("/api/movies/upcoming")
            etag = first.headers.get("ETag")
            results.append((first.status, etag))
            second = await client.get(
                "/api/movies/upcoming", headers={"If-None-Match": etag}
            )
            results.append((second.status, None))
        return results

    (first_status, etag), (second_status, _) = run(scenario())
    assert etag
    assert first_status == 200
    assert second_status == 304


def test_poster_route_404s_for_an_unknown_movie():
    app, _ = make_client([])
    response, body = run(_get(app, "/api/movies/upcoming/poster/nope-not-here"))
    assert response.status == 404


def test_poster_route_rejects_an_unsafe_id():
    app, _ = make_client([upcoming_doc()])
    response, _ = run(_get(app, "/api/movies/upcoming/poster/../../etc/passwd"))
    assert response.status in (400, 404)


def test_stale_cache_kicks_a_background_refresh_but_still_answers_200():
    app, store = make_client([upcoming_doc()], stale=True)
    response, body = run(_get(app, "/api/movies/upcoming"))
    assert response.status == 200
    assert json.loads(body)["count"] == 1
    assert ("is_stale", 6 * 3600) in store.calls


# --------------------------------------------------------------------------- #
# Static assets + templates
# --------------------------------------------------------------------------- #
def test_the_two_new_assets_are_whitelisted():
    assert "coming_soon.css" in static_assets.ASSETS
    assert "coming_soon.js" in static_assets.ASSETS
    assert (static_assets.STATIC_DIR / "coming_soon.css").is_file()
    assert (static_assets.STATIC_DIR / "coming_soon.js").is_file()


def test_assets_are_served_with_the_right_content_type():
    app = web.Application()
    app.add_routes(static_assets.routes)

    async def scenario():
        results = []
        async with TestClient(TestServer(app)) as client:
            for name in ("coming_soon.css", "coming_soon.js"):
                response = await client.get(f"/static/{name}")
                results.append((response.status, response.headers.get("Content-Type")))
        return results

    results = run(scenario())
    assert results[0][0] == 200 and results[0][1].startswith("text/css")
    assert results[1][0] == 200 and results[1][1].startswith("text/javascript")


@pytest.mark.parametrize("template", ["dl.html", "req.html"])
def test_both_stream_pages_wire_up_the_section(template):
    html = (ROOT / "dreamxbotz/template" / template).read_text(encoding="utf-8")
    assert '/static/coming_soon.css' in html
    assert '/static/coming_soon.js' in html
    assert 'id="comingSoon"' in html
    assert "{% if coming_soon_enabled %}" in html
    assert 'data-api="{{ coming_soon_api | e }}"' in html
    assert 'data-cs-grid' in html


def test_render_template_exposes_the_coming_soon_context():
    from dreamxbotz.util import render_template

    assert render_template.coming_soon_api_url() == "/api/movies/upcoming"
    # The renderer must always provide the four variables the templates use.
    source = (ROOT / "dreamxbotz/util/render_template.py").read_text(encoding="utf-8")
    for name in ("coming_soon_enabled=", "coming_soon_api=",
                 "coming_soon_limit=", "coming_soon_poll="):
        assert name in source


def test_coming_soon_section_is_rendered_only_when_enabled():
    """The Jinja block is guarded, so COMING_SOON=False removes the rail."""
    import jinja2

    html = (ROOT / "dreamxbotz/template/dl.html").read_text(encoding="utf-8")
    start = html.index('{% if coming_soon_enabled %}')
    end = html.index('{% endif %}', start) + len('{% endif %}')
    block = html[start:end]
    rendered_on = jinja2.Template(block).render(
        coming_soon_enabled=True, coming_soon_api="/api/movies/upcoming",
        coming_soon_limit=12, coming_soon_poll=0, bot_username="TestBot",
    )
    assert 'id="comingSoon"' in rendered_on
    assert jinja2.Template(block).render(coming_soon_enabled=False) == ""


# --------------------------------------------------------------------------- #
# Config surface
# --------------------------------------------------------------------------- #
def test_info_exposes_every_coming_soon_setting():
    import info

    for name in (
        "COMING_SOON", "COMING_SOON_LIMIT", "COMING_SOON_REFRESH_HOURS",
        "COMING_SOON_GRACE_DAYS", "COMING_SOON_SOON_DAYS", "COMING_SOON_CACHE_TTL",
        "COMING_SOON_POLL", "COMING_SOON_COLLECTION", "COMING_SOON_META_COLLECTION",
        "COMING_SOON_API_PATH",
    ):
        assert hasattr(info, name), name


def test_config_readers_stay_inside_their_guards(monkeypatch):
    """Clamped so a typo in an env var cannot produce an absurd query."""
    monkeypatch.setattr(cs, "_cfg", lambda name, default: 9999)
    assert cs.limit() == 24
    assert cs.refresh_ttl() == 168 * 3600
    assert cs.grace_days() == 30
    assert cs.soon_days() == 60
    assert cs.cache_ttl() == 9999  # inside the guard, passes through unchanged

    monkeypatch.setattr(cs, "_cfg", lambda name, default: 99999)
    assert cs.cache_ttl() == 86400  # and clamped down when it is not

    monkeypatch.setattr(cs, "_cfg", lambda name, default: "not-a-number")
    assert cs.limit() == 12
    assert cs.refresh_ttl() == 6 * 3600
    assert cs.grace_days() == 3
    assert cs.is_enabled() is True
