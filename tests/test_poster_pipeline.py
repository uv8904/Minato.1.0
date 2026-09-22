"""“The poster is not showing – what do I need?”  Tests for the poster pipeline.

* ``dreamxbotz/util/tmdb_direct.py``   – official TMDB search used as a fallback
* ``dreamxbotz/util/new_uploaded.py``  – lookup order helper → TMDB direct → IMDb,
                                          manual posters are never overwritten
* ``database/recent_movies_db.py``     – missing posters, title lookup, retry reset
* ``dreamxbotz/util/poster_admin.py``  – ``/posters`` report, ``/setposter`` helpers
* ``dreamxbotz/server/movie_api.py``   – ``tg://file/…`` posters served through the
                                          bot, versioned cache keys, eviction
* ``plugins/poster_admin.py``          – the admin command handlers

Run with the project dependencies installed::

    pytest tests/test_poster_pipeline.py -q
"""
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from database.recent_movies_db import RecentMoviesStore  # noqa: E402
from dreamxbotz.server import movie_api  # noqa: E402
from dreamxbotz.util import new_uploaded, poster_admin, tmdb_direct  # noqa: E402
from dreamxbotz.util.movie_titles import utcnow  # noqa: E402
from preview_section import MemoryCollection  # noqa: E402  (motor-like in-memory collection)


def run(coro):
    return asyncio.run(coro)


def make_store():
    return RecentMoviesStore(collection=MemoryCollection(), collection_name="recent_movies")


async def seed(store, *, with_posters=True):
    now = utcnow()
    await store.register_upload(title="Jawan", year=2023, quality="1080p", file_name="Jawan.2023.mkv", now=now)
    await store.register_upload(
        title="Hmm", year=2024, quality="1080p", file_name="Hmm.2024.mkv", now=now - timedelta(minutes=5)
    )
    await store.register_upload(title="Leo", year=2023, quality="720p", file_name="Leo.mkv", now=now - timedelta(days=2))
    if with_posters:
        await store.set_poster("jawan-2023", "https://image.tmdb.org/t/p/w780/jawan.jpg", "tmdb")
    return store


# --------------------------------------------------------------------------- #
# tmdb_direct
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "query,expected",
    [
        ("Jawan 2023", ("Jawan", 2023)),
        ("Pushpa 2 The Rule 2024", ("Pushpa 2 The Rule", 2024)),
        ("Hmm", ("Hmm", None)),
        ("2012 2009", ("2012", 2009)),  # last year-like token is the year
        ("", ("", None)),
    ],
)
def test_split_title_year(query, expected):
    assert tmdb_direct.split_title_year(query) == expected


def test_auth_supports_v3_keys_and_v4_tokens():
    assert tmdb_direct.auth_for("") == ({}, {})
    params, headers = tmdb_direct.auth_for("0123456789abcdef0123456789abcdef")
    assert params == {"api_key": "0123456789abcdef0123456789abcdef"} and headers == {}
    token = "eyJhbGciOiJIUzI1NiJ9." + "a" * 60 + "." + "b" * 40
    params, headers = tmdb_direct.auth_for(f'"{token}"')  # pasted with quotes
    assert params == {} and headers == {"Authorization": f"Bearer {token}"}
    assert tmdb_direct.looks_like_key("0123456789abcdef0123456789abcdef")
    assert tmdb_direct.looks_like_key(token)
    assert not tmdb_direct.looks_like_key("")
    assert not tmdb_direct.looks_like_key("my key")


def test_key_problem_explains_copy_paste_mistakes():
    good = "0123456789abcdef0123456789abcdef"
    assert tmdb_direct.key_problem(good) == ""
    # Markdown / quotes / the variable name pasted along with the key are harmless.
    for wrapped in (f"**{good}**", f'"{good}"', f"`{good}`", f"TMDB_API_KEY={good}", f" {good}\n"):
        assert tmdb_direct.clean_key(wrapped) == good, wrapped
        assert tmdb_direct.looks_like_key(wrapped), wrapped
        assert tmdb_direct.auth_for(wrapped) == ({"api_key": good}, {})
    # A garbled key (a real support case: 24 characters with an "r" in it) is rejected with a reason.
    problem = tmdb_direct.key_problem("ed52bfafe0bba9cbf8radab1")
    assert "'r'" in problem and "0-9" in problem
    assert not tmdb_direct.looks_like_key("ed52bfafe0bba9cbf8radab1")
    assert "30 characters instead of 32" in tmdb_direct.key_problem(good[:30])
    assert "spaces" in tmdb_direct.key_problem(good[:16] + " " + good[16:])
    assert "cut off" in tmdb_direct.key_problem("eyJhbGciOiJIUzI1NiJ9.short")
    assert tmdb_direct.key_problem("") == "missing"
    assert tmdb_direct.key_problem("eyJhbGciOiJIUzI1NiJ9." + "a" * 60 + "." + "b" * 40) == ""


def test_validate_key_tells_wrong_key_apart_from_no_internet():
    good = "0123456789abcdef0123456789abcdef"
    session = FakeSession([(200, {"success": True, "status_code": 1, "status_message": "Success."})])
    assert run(tmdb_direct.validate_key(good, session=session)) == (True, "accepted")
    url, params, headers = session.calls[0]
    assert url == tmdb_direct.AUTH_URL and params == {"api_key": good}

    rejected = FakeSession([(401, {"success": False, "status_code": 7, "status_message": "Invalid API key: You must be granted a valid key."})])
    ok, reason = run(tmdb_direct.validate_key(good, session=rejected))
    assert ok is False and reason.startswith("Invalid API key")

    class Offline:
        def get(self, url, params=None, headers=None):
            raise OSError("Network is unreachable")

    ok, reason = run(tmdb_direct.validate_key(good, session=Offline()))
    assert ok is None and "could not reach" in reason
    ok, reason = run(tmdb_direct.validate_key(good, session=FakeSession([(503, {})])))
    assert ok is None and reason == "HTTP 503"
    assert run(tmdb_direct.validate_key("", session=FakeSession([])))[0] is False


def test_info_accepts_common_spellings_of_the_key_variable():
    import info

    good = "ed52bfafe0bba9cbf8904dd783d7dab1"
    assert info.tmdb_key_from_env({"TMDB_API_KEY": good}) == good
    assert info.tmdb_key_from_env({"TMDB_API_KEY": f' **"{good}"** '}) == good
    assert info.tmdb_key_from_env({"TMDB_API_KEY": f"TMDB_API_KEY={good}"}) == good
    for name in ("TMDB_KEY", "TMDB_TOKEN", "tmdb api key", "Tmdb-Api-Key", "tmdb_api_key"):
        assert info.tmdb_key_from_env({name: good}) == good, name
    # The documented name wins over an alias; generic names are never used.
    assert info.tmdb_key_from_env({"TMDB_KEY": "x" * 32, "TMDB_API_KEY": good}) == good
    assert info.tmdb_key_from_env({"API": good, "API_KEY": good, "KEY": good}) == ""
    assert info.tmdb_key_from_env({}) == ""


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Records TMDB calls and answers from a scripted queue."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        status, payload = self.answers.pop(0) if self.answers else (200, {"results": []})
        return FakeResponse(status, payload)


def test_direct_search_returns_poster_and_backdrop_and_prefers_exact_title():
    session = FakeSession(
        [
            (
                200,
                {
                    "results": [
                        {"id": 1, "title": "Jawan Returns", "poster_path": "/x.jpg", "release_date": "2025-01-01"},
                        {"id": 2, "title": "Jawan", "poster_path": "/p.jpg", "backdrop_path": "/b.jpg", "release_date": "2023-09-07"},
                    ]
                },
            )
        ]
    )
    art = run(tmdb_direct.search_movie("Jawan 2023", api_key="0123456789abcdef0123456789abcdef", session=session))
    assert art["poster"] == "https://image.tmdb.org/t/p/w780/p.jpg"
    assert art["backdrop"] == "https://image.tmdb.org/t/p/w1280/b.jpg"
    assert art["year"] == 2023 and art["tmdb_id"] == 2 and art["source"] == "tmdb"
    url, params, headers = session.calls[0]
    assert url == tmdb_direct.SEARCH_URL
    assert params["query"] == "Jawan" and params["year"] == "2023" and params["api_key"]
    assert params["include_adult"] == "false"


def test_direct_search_retries_without_the_year_and_gives_up_cleanly():
    session = FakeSession([(200, {"results": []}), (200, {"results": [{"id": 9, "title": "Hmm", "poster_path": "/h.jpg"}]})])
    art = run(tmdb_direct.search_movie("Hmm 2024", api_key="0123456789abcdef0123456789abcdef", session=session))
    assert art["poster"].endswith("/h.jpg")
    assert [call[1].get("year") for call in session.calls] == ["2024", None]

    # no key → no request at all
    assert run(tmdb_direct.search_movie("Hmm 2024", api_key="", session=FakeSession([]))) == {}
    # wrong key → stop after the 401, no second attempt
    session = FakeSession([(401, {"status_message": "Invalid API key"})])
    assert run(tmdb_direct.search_movie("Hmm 2024", api_key="0123456789abcdef0123456789abcdef", session=session)) == {}
    assert len(session.calls) == 1
    # nothing found anywhere
    session = FakeSession([(200, {"results": []}), (200, {"results": []})])
    assert run(tmdb_direct.search_movie("Hmm 2024", api_key="0123456789abcdef0123456789abcdef", session=session)) == {}


# --------------------------------------------------------------------------- #
# lookup order + manual posters (new_uploaded)
# --------------------------------------------------------------------------- #
def test_lookup_art_falls_back_to_direct_tmdb_when_the_helper_has_nothing(monkeypatch):
    import types

    helper = types.ModuleType("plugins.Dreamxfutures.Imdbposter")

    async def get_movie_detailsx(query, id=False, file=None):
        return {"error": "wrapper down"}

    async def get_movie_details(query, id=False, file=None):
        raise AssertionError("IMDb must not be asked when TMDB direct answered")

    helper.get_movie_detailsx = get_movie_detailsx
    helper.get_movie_details = get_movie_details
    monkeypatch.setitem(sys.modules, "plugins.Dreamxfutures.Imdbposter", helper)

    calls = []

    async def fake_direct(query, timeout):
        calls.append(query)
        return {"poster": "https://image.tmdb.org/t/p/w780/p.jpg", "backdrop": "https://image.tmdb.org/t/p/w1280/b.jpg", "tmdb_id": 5}

    monkeypatch.setattr(new_uploaded, "_tmdb_direct", fake_direct)
    art = run(new_uploaded.lookup_art("Hmm 2024", "Hmm", timeout=3))
    assert calls == ["Hmm 2024"]
    assert art == {
        "poster": "https://image.tmdb.org/t/p/w780/p.jpg",
        "backdrop": "https://image.tmdb.org/t/p/w1280/b.jpg",
        "source": "tmdb",
    }


def test_lookup_art_uses_imdb_when_tmdb_has_no_key_and_helper_fails(monkeypatch):
    import types

    helper = types.ModuleType("plugins.Dreamxfutures.Imdbposter")

    async def get_movie_detailsx(query, id=False, file=None):
        return None

    async def get_movie_details(query, id=False, file=None):
        return {"poster_url": "https://m.media-amazon.com/images/M/x.jpg"}

    helper.get_movie_detailsx = get_movie_detailsx
    helper.get_movie_details = get_movie_details
    monkeypatch.setitem(sys.modules, "plugins.Dreamxfutures.Imdbposter", helper)
    monkeypatch.setattr(new_uploaded, "tmdb_api_key", lambda: "")
    art = run(new_uploaded.lookup_art("Hmm 2024", "Hmm", timeout=3))
    assert art["poster"] == "https://m.media-amazon.com/images/M/x.jpg"
    assert art["source"] == "imdb" and art["backdrop"] is None


def test_manual_poster_survives_the_movie_update_flow(monkeypatch):
    store = run(seed(make_store()))
    import database.recent_movies_db as rm

    monkeypatch.setattr(rm, "recent_movies", store)
    run(store.set_poster("hmm-2024", "tg://file/ABC", new_uploaded.MANUAL_SOURCE))

    run(
        new_uploaded._persist(
            {"title": "Hmm", "year": 2024, "quality": "720p", "poster_url": "https://image.tmdb.org/t/p/w780/auto.jpg", "poster_source": "tmdb", "file_names": ["Hmm.2024.720p.mkv"]}
        )
    )
    doc = run(store.get("hmm-2024"))
    assert doc["poster_url"] == "tg://file/ABC"  # admin's choice kept
    assert doc["poster_source"] == "manual"
    assert "720p" in doc["qualities"]  # the upload itself was still merged

    # a movie without a manual poster does take the movie-update artwork
    run(new_uploaded._persist({"title": "Leo", "year": 2023, "poster_url": "https://image.tmdb.org/t/p/w780/leo.jpg", "file_names": ["Leo.mkv"]}))
    assert run(store.get("leo-2023"))["poster_url"].endswith("/leo.jpg")


# --------------------------------------------------------------------------- #
# store helpers
# --------------------------------------------------------------------------- #
def test_store_lists_missing_posters_and_resets_their_checks():
    store = run(seed(make_store()))
    missing = run(store.missing_posters())
    assert [doc["_id"] for doc in missing] == ["hmm-2024", "leo-2023"]  # newest first, Jawan has one
    # a failed lookup was recorded → reset makes it due again
    run(store.set_poster("hmm-2024", None, checked=True))
    assert run(store.get("hmm-2024"))["poster_checked_at"] is not None
    assert run(store.reset_poster_checks()) == 2
    assert run(store.get("hmm-2024"))["poster_checked_at"] is None
    assert new_uploaded._poster_check_expired(None)


def test_store_finds_a_movie_by_id_or_title():
    store = run(seed(make_store()))
    assert [d["_id"] for d in run(store.find_by_title("hmm-2024"))] == ["hmm-2024"]
    assert [d["_id"] for d in run(store.find_by_title("Hmm 2024"))] == ["hmm-2024"]
    assert [d["_id"] for d in run(store.find_by_title("hmm"))] == ["hmm-2024"]  # year-less title → regex
    assert [d["_id"] for d in run(store.find_by_title("JAWAN (2023)"))] == ["jawan-2023"]
    assert run(store.find_by_title("Pathaan")) == []
    assert run(store.find_by_title("")) == []


def test_clear_poster_brings_the_placeholder_back_and_allows_a_retry():
    store = run(seed(make_store()))
    assert run(store.clear_poster("jawan-2023")) is True
    doc = run(store.get("jawan-2023"))
    assert doc["poster_url"] is None and doc["poster_source"] is None and doc["poster_checked_at"] is None
    assert run(store.clear_poster("nope")) is False


# --------------------------------------------------------------------------- #
# poster_admin helpers + report
# --------------------------------------------------------------------------- #
def test_setposter_argument_parsing():
    assert poster_admin.split_setposter_args("/setposter hmm-2024 https://image.tmdb.org/t/p/w500/a.jpg") == (
        "hmm-2024",
        "https://image.tmdb.org/t/p/w500/a.jpg",
    )
    assert poster_admin.split_setposter_args("/setposter Hmm 2024") == ("Hmm 2024", "")
    assert poster_admin.split_setposter_args("/setposter@MyBot hmm") == ("hmm", "")
    assert poster_admin.split_setposter_args("/setposter") == ("", "")


def test_poster_url_validation_mirrors_the_proxy_allow_list():
    assert poster_admin.validate_poster_url("https://image.tmdb.org/t/p/w500/a.jpg") == (True, "")
    ok, reason = poster_admin.validate_poster_url("http://image.tmdb.org/a.jpg")
    assert not ok and "https" in reason
    ok, reason = poster_admin.validate_poster_url("https://evil.example/a.jpg")
    assert not ok and "not allow-listed" in reason and "reply to the poster photo" in reason
    assert poster_admin.validate_poster_url("")[0] is False


def test_telegram_reply_becomes_a_tg_reference():
    photo = SimpleNamespace(photo=SimpleNamespace(file_id="PHOTO123"), document=None)
    assert poster_admin.telegram_poster_ref(photo) == ("tg://file/PHOTO123", "")
    image_doc = SimpleNamespace(photo=None, document=SimpleNamespace(file_id="DOC9", mime_type="image/png", file_size=1024))
    assert poster_admin.telegram_poster_ref(image_doc) == ("tg://file/DOC9", "")
    video = SimpleNamespace(photo=None, document=SimpleNamespace(file_id="V", mime_type="video/mp4", file_size=10))
    assert poster_admin.telegram_poster_ref(video)[0] == ""
    assert poster_admin.telegram_poster_ref(None) == ("", "")
    assert poster_admin.telegram_poster_ref(SimpleNamespace(photo=None, document=None))[0] == ""


async def _live_accepted(timeout=6.0):
    return True, "accepted"


async def _live_rejected(timeout=6.0):
    return False, "Invalid API key: You must be granted a valid key."


async def _live_offline(timeout=6.0):
    return None, "could not reach api.themoviedb.org (ClientConnectorError)"


def test_status_report_explains_what_is_missing(monkeypatch):
    store = run(seed(make_store()))
    monkeypatch.setattr(poster_admin, "_store", lambda: store)
    monkeypatch.setattr(poster_admin, "live_key_check", _live_accepted)
    settings = {"TMDB_API_KEY": "", "NEW_UPLOADED_POSTER_FETCH": True, "TMDB_POSTER": True, "URL": "https://bot.example/"}
    monkeypatch.setattr(poster_admin, "_cfg", lambda name, default: settings.get(name, default))

    status = run(poster_admin.collect_status())
    assert status["tmdb_key_ok"] is False and status["tmdb_key"] == "missing"
    assert status["with_poster"] == 1 and [d["_id"] for d in status["missing"]] == ["hmm-2024", "leo-2023"]

    report = poster_admin.format_report(status)
    assert "TMDB_API_KEY: ❌ missing" in report
    assert "<code>hmm-2024</code>" in report and "not looked up yet" in report
    assert "Set <code>TMDB_API_KEY</code>" in report  # first fix when the key is missing
    assert "/setposter MOVIE_ID" in report and "/posters retry" in report

    settings["TMDB_API_KEY"] = "0123456789abcdef0123456789abcdef"
    report = poster_admin.format_report(run(poster_admin.collect_status()))
    assert "TMDB_API_KEY: ✅ set (v3 key, …cdef)" in report
    assert "TMDB says: ✅ key accepted" in report
    assert "Set <code>TMDB_API_KEY</code>" not in report
    assert poster_admin.poster_preview_url("hmm-2024") == "https://bot.example/api/movies/poster/hmm-2024"

    # The live question is skipped when asked for (or when the key already fails the format check).
    status = run(poster_admin.collect_status(live=False))
    assert status["tmdb_live"] == "skipped" and "TMDB says:" not in poster_admin.format_report(status)


def test_status_report_shows_tmdbs_own_verdict_on_the_key(monkeypatch):
    store = run(seed(make_store()))
    monkeypatch.setattr(poster_admin, "_store", lambda: store)
    settings = {"TMDB_API_KEY": "ed52bfafe0bba9cbf8924dd783d7dab1", "NEW_UPLOADED_POSTER_FETCH": True, "TMDB_POSTER": True}
    monkeypatch.setattr(poster_admin, "_cfg", lambda name, default: settings.get(name, default))

    # Looks fine, but TMDB says no (one wrong digit) → first fix is "copy the key again".
    monkeypatch.setattr(poster_admin, "live_key_check", _live_rejected)
    report = poster_admin.format_report(run(poster_admin.collect_status()))
    assert "TMDB says: ❌ key REJECTED – Invalid API key" in report
    assert report.index("1. TMDB rejected this key") < report.index("Use clean release names")

    # No internet → say so instead of blaming the key.
    monkeypatch.setattr(poster_admin, "live_key_check", _live_offline)
    report = poster_admin.format_report(run(poster_admin.collect_status()))
    assert "TMDB says: ⚠️ could not check right now" in report and "rejected this key" not in report

    # A key that fails the format check is never sent to TMDB.
    settings["TMDB_API_KEY"] = "ed52bfafe0bba9cbf8radab1"
    called = []

    async def spy(timeout=6.0):
        called.append(1)
        return True, "accepted"

    monkeypatch.setattr(poster_admin, "live_key_check", spy)
    status = run(poster_admin.collect_status())
    assert called == [] and status["tmdb_live"] == "skipped"


def test_status_report_calls_out_a_garbled_or_misnamed_key(monkeypatch):
    store = run(seed(make_store()))
    monkeypatch.setattr(poster_admin, "_store", lambda: store)
    monkeypatch.setattr(poster_admin, "live_key_check", _live_accepted)
    settings = {"TMDB_API_KEY": "ed52bfafe0bba9cbf8radab1", "NEW_UPLOADED_POSTER_FETCH": True, "TMDB_POSTER": True}
    monkeypatch.setattr(poster_admin, "_cfg", lambda name, default: settings.get(name, default))

    # Set, but mistyped → say exactly what is wrong instead of a bare "invalid".
    status = run(poster_admin.collect_status())
    assert status["tmdb_key_ok"] is False and status["tmdb_key_set"] is True
    report = poster_admin.format_report(status)
    assert "TMDB_API_KEY: ❌ set, but it does not look like a TMDB key: contains 'r'" in report
    assert "is set but wrong" in report and "32 characters" in report
    assert "Set <code>TMDB_API_KEY</code>" not in report

    # Missing, but a look-alike variable exists → point at the misspelt name.
    settings["TMDB_API_KEY"] = ""
    monkeypatch.setenv("TMDBAPI", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("TMDB_POSTER", "True")  # a toggle, not a key – must not be listed
    assert poster_admin.misnamed_key_variables() == ["TMDBAPI"]
    assert poster_admin.misnamed_key_variables({"tmdb api key": "x", "TMDB_API_KEY": ""}) == []
    report = poster_admin.format_report(run(poster_admin.collect_status()))
    assert "Found <code>TMDBAPI</code> – the variable must be called <code>TMDB_API_KEY</code>" in report


def test_manual_poster_is_stored_as_manual_and_evicts_the_cache(monkeypatch):
    store = run(seed(make_store()))
    monkeypatch.setattr(poster_admin, "_store", lambda: store)

    class ArtStore:
        def __init__(self):
            self.calls = []

        async def set_art(self, movie_id, **kwargs):
            self.calls.append((movie_id, kwargs))

    import database.movie_art_db as mad

    art = ArtStore()
    monkeypatch.setattr(mad, "movie_art", art)

    movie_api._cache_put("hmm-2024:320:old", b"old", "image/svg+xml", 300)
    movie_api._cache_put("backdrop:hmm-2024:1280:old", b"old", "image/svg+xml", 300)
    movie_api._cache_put("leo-2023:320:x", b"keep", "image/jpeg", 300)

    assert run(poster_admin.set_manual_poster("hmm-2024", "tg://file/PHOTO123")) is True
    doc = run(store.get("hmm-2024"))
    assert doc["poster_url"] == "tg://file/PHOTO123" and doc["poster_source"] == "manual"
    assert movie_api._cache_get("hmm-2024:320:old") is None
    assert movie_api._cache_get("backdrop:hmm-2024:1280:old") is None
    assert movie_api._cache_get("leo-2023:320:x") is not None
    assert art.calls and art.calls[0][0] == "hmm-2024" and art.calls[0][1]["source"] == "manual"

    movie, candidates = run(poster_admin.resolve_target("hmm 2024"))
    assert movie["_id"] == "hmm-2024"
    movie, candidates = run(poster_admin.resolve_target("nothing here"))
    assert movie is None and candidates == []

    assert run(poster_admin.clear_manual_poster("hmm-2024")) is True
    assert run(store.get("hmm-2024"))["poster_url"] is None


# --------------------------------------------------------------------------- #
# the poster proxy: tg:// artwork, versioned cache keys
# --------------------------------------------------------------------------- #
def _jpeg_bytes(size=(60, 90)):
    from io import BytesIO

    from PIL import Image

    out = BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(out, format="JPEG")
    return out.getvalue()


def make_api_app(store, monkeypatch):
    monkeypatch.setattr(movie_api, "_store", lambda: store)

    class NoArt:
        async def get(self, movie_id):
            return None

        async def set_art(self, *a, **k):
            return None

    monkeypatch.setattr(movie_api, "_art_store", lambda: NoArt())
    monkeypatch.setattr(movie_api, "_art_fetch_enabled", lambda: False)  # no TMDB/IMDb in tests
    app = web.Application()
    app["nu_bot_username"] = "TestBot"
    app.add_routes(movie_api.routes)
    return app


def test_telegram_posters_are_downloaded_through_the_bot_and_resized(monkeypatch):
    store = run(seed(make_store()))
    run(store.set_poster("hmm-2024", "tg://file/PHOTO123", "manual"))
    downloads = []

    async def fake_download(file_id):
        downloads.append(file_id)
        return _jpeg_bytes((600, 900))

    monkeypatch.setattr(movie_api, "download_telegram_file", fake_download)
    movie_api._POSTER_CACHE.clear()
    app = make_api_app(store, monkeypatch)

    async def scenario():
        async with TestClient(TestServer(app)) as client:
            feed = await (await client.get("/api/movies/new")).json()
            hmm = next(m for m in feed["movies"] if m["id"] == "hmm-2024")
            first = await client.get(hmm["poster"] + "&w=200")
            body = await first.read()
            second = await client.get(hmm["poster"] + "&w=200")  # served from the cache
            wide = await client.get(hmm["backdrop"] + "&w=720")
            return hmm, first.status, first.headers["Content-Type"], body, second.status, wide.status, wide.headers["Content-Type"]

    hmm, status, ctype, body, status2, wide_status, wide_type = run(scenario())
    assert hmm["has_poster"] is True
    assert "tg://" not in json.dumps(hmm)  # the reference never reaches the browser
    assert status == 200 and ctype == "image/jpeg"
    from io import BytesIO

    from PIL import Image

    assert Image.open(BytesIO(body)).size[0] == 200  # resized to the requested width
    assert status2 == 200 and wide_status == 200 and wide_type == "image/jpeg"
    assert downloads == ["PHOTO123"]  # poster, cached poster and the backdrop band: one download


def test_poster_cache_key_includes_the_version_so_a_new_poster_shows_at_once(monkeypatch):
    store = run(seed(make_store()))
    movie_api._POSTER_CACHE.clear()
    app = make_api_app(store, monkeypatch)

    async def fake_download(file_id):
        return _jpeg_bytes()

    monkeypatch.setattr(movie_api, "download_telegram_file", fake_download)

    async def scenario():
        async with TestClient(TestServer(app)) as client:
            before = await (await client.get("/api/movies/new")).json()
            hmm_before = next(m for m in before["movies"] if m["id"] == "hmm-2024")
            placeholder = await client.get(hmm_before["poster"] + "&w=320")
            placeholder_type = placeholder.headers["Content-Type"]

            await store.set_poster("hmm-2024", "tg://file/NEW", "manual")  # what /setposter does
            after = await (await client.get("/api/movies/new")).json()
            hmm_after = next(m for m in after["movies"] if m["id"] == "hmm-2024")
            fresh = await client.get(hmm_after["poster"] + "&w=320")
            return hmm_before["poster"], placeholder_type, hmm_after["poster"], fresh.headers["Content-Type"]

    url_before, placeholder_type, url_after, fresh_type = run(scenario())
    assert placeholder_type.startswith("image/svg+xml")  # no poster yet → placeholder
    assert url_before != url_after  # ?v= changed
    assert fresh_type == "image/jpeg"  # and the new artwork is served immediately


def test_evict_cached_only_touches_the_given_movie():
    movie_api._POSTER_CACHE.clear()
    movie_api._cache_put("a-1:320:v1", b"1", "image/jpeg", 100)
    movie_api._cache_put("backdrop:a-1:1280:v1", b"2", "image/jpeg", 100)
    movie_api._cache_put("a-10:320:v1", b"3", "image/jpeg", 100)
    assert movie_api.evict_cached("a-1") == 2
    assert movie_api._cache_get("a-10:320:v1") is not None
    assert movie_api.evict_cached("<bad>") == 0


# --------------------------------------------------------------------------- #
# the admin commands
# --------------------------------------------------------------------------- #
pytest.importorskip("pyrogram")


def make_message(text, reply=None):
    replies = []

    async def reply_text(message_text, **kwargs):
        replies.append(message_text)

    parts = text.split()
    message = SimpleNamespace(
        text=text,
        command=[parts[0].lstrip("/")] + parts[1:],
        reply_text=reply_text,
        reply_to_message=reply,
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=1, type="private"),
    )
    return message, replies


def _plugin(monkeypatch):
    """Import the pyrogram plugin (must happen before the first asyncio.run())."""
    import os

    os.chdir(ROOT)
    try:  # building the Client needs a current event loop
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    import plugins.poster_admin as plugin

    store = run(seed(make_store()))
    monkeypatch.setattr(poster_admin, "_store", lambda: store)
    settings = {"TMDB_API_KEY": "", "NEW_UPLOADED_POSTER_FETCH": True, "TMDB_POSTER": True, "URL": "https://bot.example"}
    monkeypatch.setattr(poster_admin, "_cfg", lambda name, default: settings.get(name, default))
    return plugin, store


def test_posters_command_reports_and_retries(monkeypatch):
    plugin, store = _plugin(monkeypatch)

    message, replies = make_message("/posters")
    run(plugin.posters_status(object(), message))
    assert len(replies) == 1
    assert "poster status" in replies[0] and "<code>hmm-2024</code>" in replies[0]

    queued = []

    async def fake_refresh(limit=None):
        queued.append(limit)
        return 2

    monkeypatch.setattr(new_uploaded, "refresh_posters", fake_refresh)
    message, replies = make_message("/posters retry")
    run(plugin.posters_status(object(), message))
    assert "2 movie(s) reset, 2 lookup(s) queued" in replies[0]
    assert queued == [poster_admin.REPORT_WINDOW]


def test_setposter_with_a_replied_photo_and_with_a_link(monkeypatch):
    plugin, store = _plugin(monkeypatch)

    import database.movie_art_db as mad

    class ArtStore:
        async def set_art(self, *a, **k):
            return None

    monkeypatch.setattr(mad, "movie_art", ArtStore())

    photo = SimpleNamespace(photo=SimpleNamespace(file_id="PHOTO123"), document=None)
    message, replies = make_message("/setposter hmm-2024", reply=photo)
    run(plugin.set_poster(object(), message))
    assert "✅ Poster set" in replies[0] and "Telegram photo" in replies[0]
    assert "https://bot.example/api/movies/poster/hmm-2024" in replies[0]
    assert run(store.get("hmm-2024"))["poster_url"] == "tg://file/PHOTO123"

    message, replies = make_message("/setposter Leo 2023 https://image.tmdb.org/t/p/w500/leo.jpg")
    run(plugin.set_poster(object(), message))
    assert "✅ Poster set" in replies[0] and "(link)" in replies[0]
    assert run(store.get("leo-2023"))["poster_url"] == "https://image.tmdb.org/t/p/w500/leo.jpg"

    message, replies = make_message("/setposter leo-2023 https://evil.example/x.jpg")
    run(plugin.set_poster(object(), message))
    assert "Link rejected" in replies[0]

    message, replies = make_message("/setposter unknown-movie", reply=photo)
    run(plugin.set_poster(object(), message))
    assert "Not found" in replies[0]

    message, replies = make_message("/setposter hmm-2024")  # neither photo nor link
    run(plugin.set_poster(object(), message))
    assert "No poster given" in replies[0]

    message, replies = make_message("/setposter")
    run(plugin.set_poster(object(), message))
    assert "Usage" in replies[0]

    message, replies = make_message("/delposter hmm-2024")
    run(plugin.del_poster(object(), message))
    assert "Poster removed" in replies[0]
    assert run(store.get("hmm-2024"))["poster_url"] is None
