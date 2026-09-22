"""Backend tests for the Stream Mode "Newly Uploaded Movies" section.

Covers the parts that can be verified without Telegram or a real MongoDB:

* ``dreamxbotz/util/movie_titles.py``  – ids, de-duplication keys, parsing
* ``dreamxbotz/server/movie_api.py``   – JSON API, sanitizing, poster proxy
* ``database/recent_movies_db.py``    – upsert de-duplication (fake collection)
* ``plugins/commands.py``              – ``/start movie_<MOVIE_ID>`` deep link

Run with the project dependencies installed::

    pytest tests/test_newly_uploaded.py -q
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from dreamxbotz.server import movie_api  # noqa: E402
from dreamxbotz.util import movie_titles as mt  # noqa: E402

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class FakeStore:
    """Minimal stand-in for ``RecentMoviesStore``."""

    def __init__(self, docs=None, error=None):
        self.docs = list(docs or [])
        self.error = error
        self.calls = []

    async def list_recent(self, limit=20, *, raise_on_error=False):
        self.calls.append(("list_recent", limit))
        if self.error:
            raise self.error
        return self.docs[:limit]

    async def get(self, movie_id):
        self.calls.append(("get", movie_id))
        if self.error:
            raise self.error
        for doc in self.docs:
            if doc.get("_id") == movie_id:
                return doc
        return None


class FakeCursor:
    """Just enough motor cursor for ``RecentMoviesStore.list_recent``."""

    def __init__(self, docs, projection=None):
        self._docs = list(docs)
        self._hidden = tuple(
            key for key, value in (projection or {}).items() if value == 0
        )

    def _strip(self, doc):
        return {k: v for k, v in doc.items() if k not in self._hidden}

    def sort(self, *args, **kwargs):  # newest-first is asserted through the data
        return self

    def limit(self, count):
        self._docs = self._docs[:count]
        return self

    def skip(self, count):
        self._docs = self._docs[count:]
        return self

    async def to_list(self, length=None):
        docs = [self._strip(doc) for doc in self._docs]
        return docs[:length] if length else docs

    def __aiter__(self):
        self._iterator = iter([self._strip(doc) for doc in self._docs])
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as stop:  # pragma: no cover - defensive
            raise StopAsyncIteration from stop


class FakeCollection:
    """Records the update operators ``register_upload`` uses."""

    def __init__(self):
        self.docs = {}
        self.updates = []

    def find(self, filter=None, projection=None):
        docs = list(self.docs.values())
        order = (filter or {}).get("_id", {}).get("$in") if filter else None
        if order:
            docs = [doc for doc in docs if doc["_id"] in order]
        docs.sort(key=lambda doc: doc.get("last_upload_at") or 0, reverse=True)
        return FakeCursor(docs, projection)

    async def count_documents(self, filter=None):
        return len(self.docs)

    async def delete_many(self, filter=None):
        removed = len(self.docs)
        self.docs.clear()
        return SimpleNamespace(deleted_count=removed)

    async def find_one(self, filter, projection=None):
        return self.docs.get(filter.get("_id"))

    async def update_one(self, filter, update, upsert=False):
        movie_id = filter["_id"]
        self.updates.append((movie_id, update))
        doc = self.docs.get(movie_id, {"_id": movie_id})
        for key, value in (update.get("$set") or {}).items():
            doc[key] = value
        for key, value in (update.get("$setOnInsert") or {}).items():
            doc.setdefault(key, value)
        for key, value in (update.get("$addToSet") or {}).items():
            current = list(doc.get(key) or [])
            if value not in current:
                current.append(value)
            doc[key] = current
        for key, value in (update.get("$inc") or {}).items():
            doc[key] = int(doc.get(key) or 0) + int(value)
        self.docs[movie_id] = doc
        return SimpleNamespace(matched_count=1, modified_count=1, upserted_id=None)

    async def create_index(self, *args, **kwargs):
        return "index"


class FakeArtStore:
    """In-memory stand-in for ``MovieArtStore`` (no MongoDB, no network)."""

    def __init__(self, docs=None):
        self.docs = {doc["_id"]: dict(doc) for doc in (docs or [])}
        self.calls = []

    async def get(self, movie_id):
        self.calls.append(("get", movie_id))
        return self.docs.get(movie_id)

    async def set_art(
        self,
        movie_id,
        *,
        title=None,
        year=None,
        poster_url=None,
        backdrop_url=None,
        source=None,
        now=None,
    ):
        self.calls.append(("set", movie_id))
        self.docs[movie_id] = {
            "_id": movie_id,
            "title": title,
            "year": year,
            "poster_url": poster_url,
            "backdrop_url": backdrop_url,
            "poster_source": source,
            # like the real store: "when was this lookup attempted"
            "checked_at": now or datetime.now(timezone.utc),
        }
        return True

    async def count(self):
        return len(self.docs)


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self.content = SimpleNamespace(read=self._read)
        self._body = body

    async def _read(self, size=-1):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.requested = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.response


def make_client(app_docs=None, store=None, bot_username="TestBot", error=None, art=None):
    """Build an aiohttp app around the poster/list handlers with a fake store."""
    fake = store or FakeStore(app_docs or [], error=error)
    art_store = art or FakeArtStore()
    app = web.Application()
    app.add_routes(movie_api.routes)
    app["nu_bot_username"] = bot_username
    movie_api._store = lambda: fake
    movie_api._art_store = lambda: art_store
    return app, fake


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_movie_api_caches():
    """Keep the in-process poster cache from leaking between tests."""
    movie_api._POSTER_CACHE.clear()
    movie_api._POSTER_INFLIGHT.clear()
    movie_api._POSTER_CACHE_BYTES = 0
    yield


# --------------------------------------------------------------------------- #
# movie_titles helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "title,year,expected",
    [
        ("Jawan", 2023, "jawan-2023"),
        ("Jawan (2023)", 2023, "jawan-2023"),
        ("  JAWAN  ", None, "jawan"),
        ("Pushpa 2 The Rule", 2024, "pushpa-2-the-rule-2024"),
        ("Spider-Man: No Way Home", 2021, "spider-man-no-way-home-2021"),
    ],
)
def test_movie_id_is_deterministic(title, year, expected):
    assert mt.movie_id_for(title, year) == expected
    # Same movie, different spelling -> same id (this is the de-duplication).
    assert mt.movie_id_for(title.replace(":", ""), year) == expected


def test_movie_id_is_deeplink_safe_and_bounded():
    movie_id = mt.movie_id_for("A" * 90, 2024)
    assert len(movie_id) <= mt.MAX_MOVIE_ID_LENGTH
    assert mt.MOVIE_ID_RE.match(movie_id)
    # A 64-char Telegram start payload still fits: "movie_" + id
    assert len("movie_" + movie_id) <= 64


def test_movie_id_for_non_ascii_title_uses_hash():
    movie_id = mt.movie_id_for("கபாலி", 2016)
    assert movie_id.startswith("m-")
    assert mt.MOVIE_ID_RE.match(movie_id)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("jawan-2023", "jawan-2023"),
        ("movie_jawan-2023", "jawan-2023"),
        ("  jawan-2023  ", "jawan-2023"),
        ("../../etc/passwd", ""),  # path traversal is rejected outright
        ("movie/../jawan", ""),
        ("'; DROP TABLE--", ""),
        ("", ""),
        ("x" * 80, ""),
    ],
)
def test_sanitize_movie_id(value, expected):
    assert mt.sanitize_movie_id(value) == expected


def test_parse_release_name_cleans_release_junk():
    parsed = mt.parse_release_name("[HdHub] Jawan.2023.1080p.WEB-DL.Hindi.x264.mkv")
    assert parsed["title"] == "Jawan"
    assert parsed["year"] == 2023
    assert parsed["quality"] == "1080p"
    assert parsed["is_series"] is False


def test_parse_release_name_detects_series():
    parsed = mt.parse_release_name("The.Family.Man.S02E05.1080p.WEB-DL.mkv")
    assert parsed["is_series"] is True
    assert parsed["title"] == "The Family Man"


def test_quality_badge_helpers():
    assert mt.primary_quality(["480p", "1080P", "720p"]) == "1080p"
    assert mt.quality_label(["480p", "1080p", "720p"]) == "1080p, 720p, 480p"
    assert mt.canonical_quality("4K") == "4K"
    assert mt.canonical_quality("n/a") == ""
    assert mt.quality_label([]) == ""


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, ""),
        (NOW, "just now"),
        (NOW - timedelta(minutes=5), "5 min ago"),
        (NOW - timedelta(hours=3), "3 h ago"),
        (NOW - timedelta(days=1), "yesterday"),
        (NOW - timedelta(days=3), "3 days ago"),
    ],
)
def test_relative_time_label(value, expected):
    assert mt.relative_time_label(value, now=NOW) == expected


def test_build_deeplink_requires_valid_username():
    assert mt.build_deeplink("@MyMovieBot", "jawan-2023") == (
        "https://t.me/MyMovieBot?start=movie_jawan-2023"
    )
    assert mt.build_deeplink("", "jawan-2023") == ""
    assert mt.build_deeplink("bad name", "jawan-2023") == ""
    assert mt.build_deeplink("MyMovieBot", "../../x") == ""


def test_clean_title_text_strips_markup():
    assert mt.clean_title_text("<script>alert(1)</script>Jawan") == "scriptalert(1)/scriptJawan"
    assert "<" not in mt.clean_title_text("A <b>B</b>")
    assert len(mt.clean_title_text("x" * 500)) <= mt.MAX_TITLE_LENGTH
    assert mt.clean_title_text(None) == ""


# --------------------------------------------------------------------------- #
# recent_movies store
# --------------------------------------------------------------------------- #
def test_register_upload_dedupes_and_keeps_newest_first():
    from database.recent_movies_db import RecentMoviesStore

    collection = FakeCollection()
    store = RecentMoviesStore(collection=collection)

    run(
        store.register_upload(
            title="Jawan", year=2023, quality="720p", file_id="file-a", now=NOW
        )
    )
    run(
        store.register_upload(
            title="Jawan (2023)",  # same movie, different spelling
            quality="1080p",
            file_id="file-b",
            file_name="Jawan.2023.1080p.mkv",
            now=NOW + timedelta(hours=2),
        )
    )

    assert list(collection.docs) == ["jawan-2023"]  # one document only
    doc = collection.docs["jawan-2023"]
    assert doc["file_ids"] == ["file-a", "file-b"]
    assert sorted(doc["qualities"]) == ["1080p", "720p"]
    assert doc["uploaded_at"] == NOW  # first seen …
    assert doc["last_upload_at"] == NOW + timedelta(hours=2)  # … newest file
    assert doc["search_query"] == "Jawan 2023"


def test_register_upload_rejects_empty_titles():
    from database.recent_movies_db import RecentMoviesStore

    collection = FakeCollection()
    store = RecentMoviesStore(collection=collection)
    assert run(store.register_upload(title="   ")) is None
    assert collection.docs == {}


def test_store_rejects_pathological_movie_ids():
    from database.recent_movies_db import RecentMoviesStore

    store = RecentMoviesStore(collection=FakeCollection())
    assert run(store.get("../../etc/passwd")) is None


# --------------------------------------------------------------------------- #
# JSON API
# --------------------------------------------------------------------------- #
def movie_doc(**overrides):
    doc = {
        "_id": "jawan-2023",
        "title": "Jawan",
        "year": 2023,
        "qualities": ["1080p", "720p"],
        "poster_url": "https://image.tmdb.org/t/p/w500/jawan.jpg",
        "search_query": "Jawan 2023",
        "uploaded_at": NOW,
        "last_upload_at": NOW,
        "updated_at": NOW,
        # internal fields that must never be exposed:
        "file_ids": ["secret-file-id"],
        "file_names": ["Jawan.2023.1080p.mkv"],
    }
    doc.update(overrides)
    return doc


async def _get(app, path, **kwargs):
    async with TestClient(TestServer(app)) as client:
        response = await client.get(path, **kwargs)
        body = await response.text()
        return response, body


async def _get_binary(app, path, **kwargs):
    async with TestClient(TestServer(app)) as client:
        response = await client.get(path, **kwargs)
        body = await response.read()
        return response, body


def _scenario(app, paths):
    """Run several requests against one client (one event loop, like a browser)."""
    async def run_scenario():
        results = []
        async with TestClient(TestServer(app)) as client:
            for path, kwargs in paths:
                response = await client.get(path, **kwargs)
                body = await response.read()
                results.append((response, body))
        return results

    return run(run_scenario())


def test_api_returns_sanitized_payload_newest_first():
    docs = [
        movie_doc(),
        movie_doc(_id="pathaan-2023", title="Pathaan", uploaded_at=NOW - timedelta(days=1)),
    ]
    app, store = make_client(docs)
    response, body = run(_get(app, "/api/movies/new"))

    assert response.status == 200
    assert response.headers["Cache-Control"].startswith("public")
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["count"] == 2
    assert [m["id"] for m in payload["movies"]] == ["jawan-2023", "pathaan-2023"]
    assert payload["bot_username"] == "TestBot"

    first = payload["movies"][0]
    assert set(first) == {
        "id",
        "title",
        "year",
        "quality",
        "quality_label",
        "poster",
        "backdrop",
        "has_poster",
        "uploaded_at",
        "added",
        "deeplink",
    }
    assert first["quality"] == "1080p"
    assert first["quality_label"] == "1080p, 720p"
    assert first["poster"] == "/api/movies/poster/jawan-2023?v=%s" % movie_api._poster_version(
        docs[0]
    )
    assert first["backdrop"] == "/api/movies/backdrop/jawan-2023?v=%s" % movie_api._poster_version(
        docs[0]
    )
    assert first["deeplink"] == "https://t.me/TestBot?start=movie_jawan-2023"
    assert first["uploaded_at"].endswith("Z")
    assert payload["updated_at"].endswith("Z")  # newest upload, not "now"

    # no internals / download links / token anywhere in the response
    for leak in ("file_ids", "file_names", "secret-file-id", "Jawan.2023.1080p.mkv", "hash="):
        assert leak not in body


def test_api_never_exposes_download_or_file_links():
    doc = movie_doc(
        file_url="https://files.example/abcdef123456/42?hash=secret",
        download_url="https://files.example/42",
        file_ref="ref",
    )
    app, _ = make_client([doc])
    _, body = run(_get(app, "/api/movies/new"))
    assert "files.example" not in body
    assert "download_url" not in body


@pytest.mark.parametrize(
    "query,expected_limit,expected_call",
    [
        ("?limit=5", 5, 5),
        ("?limit=100", 20, 20),  # hard cap
        ("?limit=0", 1, 1),  # never negative/empty
        ("?limit=abc", 20, 20),
        ("", 20, 20),
    ],
)
def test_api_clamps_limit(query, expected_limit, expected_call):
    app, store = make_client([movie_doc()])
    _, body = run(_get(app, "/api/movies/new" + query))
    assert json.loads(body)["limit"] == expected_limit
    assert store.calls[0] == ("list_recent", expected_call)


def test_api_returns_503_when_database_is_unavailable():
    app, _ = make_client([], error=RuntimeError("mongo down"))
    response, body = run(_get(app, "/api/movies/new"))
    assert response.status == 503
    payload = json.loads(body)
    assert payload["ok"] is False
    assert payload["error"] == "database_unavailable"


def test_api_returns_empty_list_for_brand_new_bot():
    app, _ = make_client([])
    response, body = run(_get(app, "/api/movies/new"))
    assert response.status == 200
    assert json.loads(body)["movies"] == []


def test_api_skips_documents_with_an_invalid_id():
    app, _ = make_client([movie_doc(_id="../evil"), movie_doc()])
    _, body = run(_get(app, "/api/movies/new"))
    movies = json.loads(body)["movies"]
    assert [m["id"] for m in movies] == ["jawan-2023"]


def test_api_supports_etag_revalidation():
    app, _ = make_client([movie_doc()])

    async def scenario():
        async with TestClient(TestServer(app)) as client:
            initial = await client.get("/api/movies/new")
            await initial.read()
            etag = initial.headers["ETag"]
            revalidated = await client.get(
                "/api/movies/new", headers={"If-None-Match": etag}
            )
            await revalidated.read()
            stale = await client.get(
                "/api/movies/new", headers={"If-None-Match": '"nope"'}
            )
            await stale.read()
            return etag, revalidated.status, stale.status

    etag, revalidated_status, stale_status = run(scenario())
    assert etag
    assert revalidated_status == 304  # unchanged feed -> no body
    assert stale_status == 200


# --------------------------------------------------------------------------- #
# Poster proxy
# --------------------------------------------------------------------------- #
def test_poster_without_url_serves_branded_placeholder():
    app, _ = make_client([movie_doc(poster_url=None)])
    response, body = run(_get(app, "/api/movies/poster/jawan-2023"))
    assert response.status == 200
    assert response.headers["Content-Type"] == "image/svg+xml"
    assert "Jawan" in body
    assert "MINATOVERSE" in body


def test_poster_placeholder_escapes_titles():
    svg = movie_api.placeholder_svg("<script>x</script>Jawan").decode("utf-8")
    assert "<script" not in svg
    assert "&lt;" not in svg  # angle brackets are stripped before escaping
    assert "scriptx/scriptJawan" in svg


def test_poster_for_unknown_id_is_404():
    app, _ = make_client([])
    response, _ = run(_get(app, "/api/movies/poster/does-not-exist"))
    assert response.status == 404


def test_poster_rejects_invalid_ids_without_touching_the_database():
    app, store = make_client([movie_doc()])
    response, _ = run(_get(app, "/api/movies/poster/..%2f..%2fetc%2fpasswd"))
    assert response.status == 404
    assert not [call for call in store.calls if call[0] == "get"]


def test_poster_from_disallowed_host_falls_back_to_placeholder():
    app, _ = make_client([movie_doc(poster_url="https://evil.example/x.jpg")])
    response, body = run(_get(app, "/api/movies/poster/jawan-2023"))
    assert response.status == 200
    assert response.headers["Content-Type"] == "image/svg+xml"
    assert "MINATOVERSE" in body


def test_poster_is_proxied_and_served_from_our_origin():
    app, _ = make_client([movie_doc()])
    session = FakeSession(
        FakeResponse(
            body=b"\xff\xd8\xff\xe0fake-jpeg",
            headers={"Content-Type": "image/jpeg", "Content-Length": "18"},
        )
    )
    original_session = movie_api._session
    movie_api._session = lambda request=None: session
    try:
        response, body = run(_get_binary(app, "/api/movies/poster/jawan-2023?w=320"))
    finally:
        movie_api._session = original_session

    assert response.status == 200
    assert response.headers["Content-Type"].startswith("image/")
    assert response.headers["Cache-Control"].startswith("public")
    assert body  # image bytes
    assert session.requested == ["https://image.tmdb.org/t/p/w500/jawan.jpg"]



def test_poster_host_allowlist():
    assert movie_api.poster_host_allowed("https://image.tmdb.org/t/p/w500/x.jpg")
    assert movie_api.poster_host_allowed("https://m.media-amazon.com/images/M/x.jpg")
    assert not movie_api.poster_host_allowed("http://image.tmdb.org/x.jpg")  # not https
    assert not movie_api.poster_host_allowed("https://127.0.0.1/x.jpg")
    assert not movie_api.poster_host_allowed("https://user:pass@image.tmdb.org/x.jpg")
    assert not movie_api.poster_host_allowed("file:///etc/passwd")


# --------------------------------------------------------------------------- #
# Bot deep link:  /start movie_<MOVIE_ID>
# --------------------------------------------------------------------------- #
pytest.importorskip("pyrogram")


def _deeplink_modules():
    os.chdir(ROOT)
    import plugins.commands as cmds  # noqa: E402
    from dreamxbotz.util import movie_deeplink  # noqa: E402

    return cmds, movie_deeplink


def make_start_message(payload):
    replies = []

    async def reply_text(text, **kwargs):
        replies.append(SimpleNamespace(text=text, kwargs=kwargs))
        return None

    async def react(**kwargs):
        return None

    message = SimpleNamespace(
        command=["start", payload],
        text="/start " + payload,
        react=react,
        reply_text=reply_text,
        from_user=SimpleNamespace(id=123, first_name="Tester"),
        chat=SimpleNamespace(id=456, type="private", title="pm"),
    )
    return message, replies


def test_start_movie_payload_searches_the_exact_movie(monkeypatch):
    cmds, movie_deeplink = _deeplink_modules()

    async def fake_lookup(movie_id):
        assert movie_id == "jawan-2023"
        return {"_id": "jawan-2023", "title": "Jawan", "search_query": "Jawan 2023"}

    monkeypatch.setattr(movie_deeplink, "lookup_movie", fake_lookup)
    movie_deeplink.clear_cache()

    searches = []
    messages = []

    async def fake_auto_filter(client, msg, spoll=False):
        searches.append(msg.text)
        messages.append(msg)
        return True

    monkeypatch.setattr(cmds, "auto_filter", fake_auto_filter)

    message, replies = make_start_message("movie_jawan-2023")
    run(cmds.start(object(), message))

    assert searches == ["Jawan 2023"]  # exact stored movie, no typing needed
    assert replies == []
    assert message.text == "Jawan 2023"


def test_start_movie_payload_falls_back_to_the_slug(monkeypatch):
    cmds, movie_deeplink = _deeplink_modules()

    async def fake_lookup(movie_id):  # movie no longer in recent_movies
        return None

    monkeypatch.setattr(movie_deeplink, "lookup_movie", fake_lookup)
    movie_deeplink.clear_cache()

    searches = []

    async def fake_auto_filter(client, msg, spoll=False):
        searches.append(msg.text)
        return True

    monkeypatch.setattr(cmds, "auto_filter", fake_auto_filter)

    message, _ = make_start_message("movie_tiger-3-2023")
    run(cmds.start(object(), message))

    assert searches == ["Tiger 3 2023"]


@pytest.mark.parametrize("payload", ["movie_", "movie_/etc/passwd", "movie_" + "x" * 80])
def test_start_movie_payload_rejects_broken_links(monkeypatch, payload):
    cmds, movie_deeplink = _deeplink_modules()
    movie_deeplink.clear_cache()

    searches = []

    async def fake_auto_filter(client, msg, spoll=False):
        searches.append(msg.text)
        return True

    monkeypatch.setattr(cmds, "auto_filter", fake_auto_filter)

    message, replies = make_start_message(payload)
    run(cmds.start(object(), message))

    assert searches == []
    assert len(replies) == 1
    assert "ᴠᴀʟɪᴅ ᴀɴʏᴍᴏʀᴇ" in replies[0].text  # "not valid anymore"


def test_resolve_movie_deeplink_statuses(monkeypatch):
    _, movie_deeplink = _deeplink_modules()

    async def fake_lookup(movie_id):
        if movie_id == "jawan-2023":
            return {"_id": "jawan-2023", "title": "Jawan", "search_query": "Jawan 2023"}
        return None

    monkeypatch.setattr(movie_deeplink, "lookup_movie", fake_lookup)
    movie_deeplink.clear_cache()

    found = run(movie_deeplink.resolve_movie_deeplink("movie_jawan-2023"))
    assert found["status"] == "ok"
    assert found["query"] == "Jawan 2023"

    fallback = run(movie_deeplink.resolve_movie_deeplink("movie_leo-2023"))
    assert fallback["status"] == "fallback"
    assert fallback["query"] == "Leo 2023"

    invalid = run(movie_deeplink.resolve_movie_deeplink("movie_!!!"))
    assert invalid["status"] == "invalid"
    assert invalid["query"] == ""


# --------------------------------------------------------------------------- #
# Route wiring: the API must win over the catch-all stream route
# --------------------------------------------------------------------------- #
def test_api_and_static_routes_are_registered_before_the_stream_catch_all():
    import plugins as plugins_package  # noqa: E402 — imported for its web_server()

    modules = {}
    for name in ("dreamxbotz.server.static_assets", "dreamxbotz.server.movie_api", "plugins.route"):
        __import__(name)
        modules[name] = sys.modules[name]

    app = web.Application()
    app.add_routes(modules["dreamxbotz.server.static_assets"].routes)
    app.add_routes(modules["dreamxbotz.server.movie_api"].routes)
    app.add_routes(modules["plugins.route"].routes)
    app["nu_bot_username"] = "TestBot"

    fake = FakeStore([movie_doc()])
    movie_api._store = lambda: fake

    (api_response, api_body), (css, css_body), (missing, _) = _scenario(
        app,
        [
            ("/api/movies/new", {}),
            ("/static/newly_uploaded.css", {}),
            ("/static/secret.txt", {}),
        ],
    )

    assert api_response.status == 200  # not the stream catch-all
    assert json.loads(api_body)["movies"][0]["id"] == "jawan-2023"

    assert css.status == 200
    assert b"nu-card" in css_body
    assert css.headers["Cache-Control"].startswith("public")

    assert missing.status == 404  # only whitelisted assets are served
    assert plugins_package.web_server is not None


# --------------------------------------------------------------------------- #
# End-to-end: indexing hook → database → API → Telegram deep link
# --------------------------------------------------------------------------- #
def test_upload_travels_from_the_index_hook_to_the_bot_deep_link(monkeypatch):
    """The full happy path with fakes: no Telegram, no real MongoDB."""
    cmds, movie_deeplink = _deeplink_modules()
    from database.recent_movies_db import recent_movies
    from dreamxbotz.util import new_uploaded

    collection = FakeCollection()
    monkeypatch.setattr(recent_movies, "_collection", collection)
    monkeypatch.setattr(recent_movies, "_indexes_ready", True)
    movie_deeplink.clear_cache()

    # 1. a channel upload is saved by database.ia_filterdb.save_file() …
    filename = "Jawan.2023.1080p.WEB-DL.Hindi.x264.mkv"
    assert new_uploaded.notify_new_file(filename, "file-1", mime_type="video/x-matroska")

    # … and the background worker records the movie (drained synchronously here)
    run(new_uploaded._persist({"file_name": filename, "file_id": "file-1"}))
    assert list(collection.docs) == ["jawan-2023"]
    assert collection.docs["jawan-2023"]["title"] == "Jawan"
    assert collection.docs["jawan-2023"]["qualities"] == ["1080p"]

    # a series file must not pollute the movies rail
    assert not new_uploaded.notify_new_file("The.Family.Man.S02E05.1080p.mkv", "file-2")
    # non-video files are ignored as well
    assert not new_uploaded.notify_new_file("subtitles.zip", "file-3")

    # 2. the website API serves it (newest first, sanitized)
    app, _ = make_client([])  # builds the app …
    movie_api._store = lambda: recent_movies  # … and the real store backs it
    response, body = run(_get_binary(app, "/api/movies/new"))
    payload = json.loads(body)
    assert response.status == 200
    movie = payload["movies"][0]
    assert movie["id"] == "jawan-2023"
    assert movie["title"] == "Jawan"
    assert movie["quality"] == "1080p"
    assert movie["deeplink"] == "https://t.me/TestBot?start=movie_jawan-2023"
    assert "file-1" not in body.decode()
    assert filename not in body.decode()

    # 3. tapping the poster → /start movie_jawan-2023 → the exact movie
    searches = []

    async def fake_auto_filter(client, msg, spoll=False):
        searches.append(msg.text)
        return True

    monkeypatch.setattr(cmds, "auto_filter", fake_auto_filter)
    message, replies = make_start_message("movie_" + movie["id"])
    run(cmds.start(object(), message))

    assert searches == ["Jawan 2023"]
    assert replies == []


def test_series_and_archives_are_skipped_by_the_hook(monkeypatch):
    from dreamxbotz.util import new_uploaded

    assert not new_uploaded.notify_new_file("Show.S01E02.1080p.mkv", "id")
    assert not new_uploaded.notify_new_file("poster.jpg", "id")
    assert not new_uploaded.notify_new_file("", "id")


def test_movie_update_enrichment_skips_series(monkeypatch):
    from dreamxbotz.util import new_uploaded

    assert new_uploaded.register_movie_update("The Family Man", file_names=["The.Family.Man.S02E05.mkv"]) is False
    assert new_uploaded.register_movie_update("Jawan", year=2023, quality="1080p", file_names=["Jawan.2023.1080p.mkv"]) is True


def test_feature_flag_disables_the_hook(monkeypatch):
    from dreamxbotz.util import new_uploaded

    monkeypatch.setattr(new_uploaded, "is_enabled", lambda: False)
    assert new_uploaded.notify_new_file("Jawan.2023.1080p.mkv", "id") is False
