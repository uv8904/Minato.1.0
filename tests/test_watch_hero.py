"""Tests for the watch-page movie hero (strip above the player).

Covers, without Telegram or a real MongoDB:

* ``dreamxbotz/util/watch_hero.py``    – title/year/quality/deep-link context
* ``dreamxbotz/server/movie_api.py``   – ``GET /api/movies/art/<id>`` and the
                                        16:9 ``/api/movies/backdrop/<id>`` proxy
* ``database/movie_art_db.py``         – artwork cache writes/reads
* ``dreamxbotz/template/req.html``     – markup, assets, deep links, states

Run with the project dependencies installed::

    pytest tests/test_watch_hero.py -q
"""
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402

from dreamxbotz.server import movie_api, static_assets  # noqa: E402
from dreamxbotz.util import watch_hero  # noqa: E402

# Reuse the existing harness (fakes, request helpers, page renderer).
from test_newly_uploaded import (  # noqa: E402
    NOW,
    _scenario,
    FakeArtStore,
    FakeResponse,
    FakeSession,
    FakeStore,
    make_client,
    movie_doc,
    run,
    _get,
    _get_binary,
)

BOT = "MinatoMovieBot"
MARCO = "[CK] - Marco (2024) Malayalam HQ 1080p BR-Rip - x264 - (AA.mkv"
POSTER_URL = "https://image.tmdb.org/t/p/original/marco.jpg"
BACKDROP_URL = "https://image.tmdb.org/t/p/original/marco-backdrop.jpg"


@pytest.fixture(autouse=True)
def _clean_caches():
    """Keep the in-process poster cache and config overrides from leaking."""
    movie_api._POSTER_CACHE.clear()
    movie_api._POSTER_INFLIGHT.clear()
    movie_api._POSTER_CACHE_BYTES = 0
    yield
    movie_api._POSTER_CACHE.clear()
    movie_api._POSTER_INFLIGHT.clear()
    movie_api._POSTER_CACHE_BYTES = 0


def _art_response(poster=POSTER_URL, backdrop=BACKDROP_URL, source="tmdb"):
    """A stub for ``new_uploaded.lookup_art``."""
    calls = []

    async def fake_lookup(query, title="", timeout=None):
        calls.append((query, title))
        return {"poster": poster, "backdrop": backdrop, "source": source}

    fake_lookup.calls = calls
    return fake_lookup


def _patch_lookup(monkeypatch, **kwargs):
    """Patch the on-demand lookup used by ``resolve_art`` (no network)."""
    fake = _art_response(**kwargs)

    async def wrapper(query, title="", timeout=None):
        return await fake(query, title, timeout)

    import dreamxbotz.util.new_uploaded as nu

    monkeypatch.setattr(nu, "lookup_art", wrapper)
    return fake


# --------------------------------------------------------------------------- #
# watch_hero.build_context  (pure)
# --------------------------------------------------------------------------- #
def test_context_carries_title_year_quality_and_deeplink():
    ctx = watch_hero.build_context(MARCO, BOT, None)
    assert ctx["watch_hero_enabled"] is True
    assert ctx["watch_hero_title"] == "Marco"
    assert ctx["watch_hero_year"] == 2024
    assert ctx["watch_hero_quality"] == "1080p"
    assert ctx["watch_hero_movie_id"] == "marco-2024"
    assert ctx["watch_hero_deeplink"] == f"https://t.me/{BOT}?start=movie_marco-2024"
    assert ctx["watch_hero_ready"] is True
    assert ctx["watch_hero_art_url"] == "/api/movies/art"
    # chips: year, quality and the release tags, without duplicates
    assert ctx["watch_hero_chips"] == ["2024", "1080p", "Malayalam", "BRRip", "x264"]


def test_context_marks_the_shared_release_group_junk_as_no_title():
    ctx = watch_hero.build_context("[YTS] Jawan 2023 720p WEB-DL.mkv", BOT)
    assert ctx["watch_hero_title"] == "Jawan"
    assert ctx["watch_hero_movie_id"] == "jawan-2023"


def test_context_without_bot_username_has_no_link():
    ctx = watch_hero.build_context(MARCO, "", None)
    assert ctx["watch_hero_deeplink"] == ""
    assert ctx["watch_hero_ready"] is False
    assert ctx["watch_hero_movie_id"] == "marco-2024"  # still labelled


def test_context_upload_date_is_relative():
    ctx = watch_hero.build_context(
        "Marco 2024 1080p.mkv", BOT, NOW - timedelta(hours=5), now=NOW
    )
    assert ctx["watch_hero_added"] == "5 h ago"
    assert ctx["watch_hero_kind"] == "Movie"


def test_context_detects_series():
    ctx = watch_hero.build_context("The.Family.Man.S02E05.1080p.mkv", BOT)
    assert ctx["watch_hero_kind"] == "Series"


def test_context_survives_a_hostile_file_name():
    ctx = watch_hero.build_context("<script>alert(1)</script>Marco 2024 1080p.mkv", BOT)
    assert "<" not in ctx["watch_hero_title"]
    assert "<" not in "".join(ctx["watch_hero_chips"])
    assert ctx["watch_hero_deeplink"].startswith("https://t.me/")


def test_context_of_an_empty_name_falls_back_to_a_label():
    ctx = watch_hero.build_context("", BOT)
    assert ctx["watch_hero_title"] == "Movie"
    assert ctx["watch_hero_movie_id"]


def test_art_endpoint_follows_api_url(monkeypatch):
    monkeypatch.setattr(watch_hero, "_cfg", lambda name, default: "https://api.example.com/" if name == "API_URL" else default)
    assert watch_hero.hero_api_url() == "https://api.example.com/api/movies/art"


def test_hero_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(watch_hero, "_cfg", lambda name, default: False if name == "WATCH_HERO" else default)
    assert watch_hero.hero_enabled() is False
    assert watch_hero.build_context(MARCO, BOT)["watch_hero_enabled"] is False


# --------------------------------------------------------------------------- #
# GET /api/movies/art/<MOVIE_ID>
# --------------------------------------------------------------------------- #
def test_art_uses_the_rail_poster_without_any_lookup(monkeypatch):
    fake = _patch_lookup(monkeypatch)
    app, store = make_client([movie_doc(_id="marco-2024", title="Marco", year=2024)])
    response, body = run(_get(app, "/api/movies/art/marco-2024?q=Marco&y=2024"))
    payload = json.loads(body)

    assert response.status == 200
    assert payload["ok"] is True
    assert payload["id"] == "marco-2024"
    assert payload["has_poster"] is True
    assert payload["poster"].startswith("/api/movies/poster/marco-2024?v=")
    assert payload["backdrop"].startswith("/api/movies/backdrop/marco-2024?v=")
    # the poster is known but the 16:9 backdrop is not -> one lookup is allowed
    assert len(fake.calls) == 1
    assert fake.calls[0][0] == "Marco 2024"


def test_art_resolves_and_caches_the_full_artwork(monkeypatch):
    fake = _patch_lookup(monkeypatch)
    art = FakeArtStore()
    app, _ = make_client([], art=art)
    url = "/api/movies/art/marco-2024?q=Marco&y=2024"

    # two page views, one event loop: the first resolves, the second is cached
    (response, body), (again, again_body) = _scenario(app, [(url, {}), (url, {})])
    payload = json.loads(body)
    assert response.status == 200
    assert payload["has_poster"] is True and payload["has_backdrop"] is True
    assert payload["source"] == "tmdb"
    assert len(fake.calls) == 1
    # upstream URLs never reach the browser – only our own proxy paths do
    assert "image.tmdb.org" not in body.decode()

    stored = art.docs["marco-2024"]
    assert stored["poster_url"] == POSTER_URL
    assert stored["backdrop_url"] == BACKDROP_URL

    assert again.status == 200 and json.loads(again_body)["has_poster"] is True
    assert len(fake.calls) == 1


def test_art_remembers_a_miss_instead_of_hammering_the_apis(monkeypatch):
    fake = _patch_lookup(monkeypatch, poster=None, backdrop=None, source=None)
    art = FakeArtStore()
    app, _ = make_client([], art=art)
    url = "/api/movies/art/no-such-movie-2024?q=No Such Movie&y=2024"

    (response, body), (again, _) = _scenario(app, [(url, {}), (url, {})])
    payload = json.loads(body)
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["has_poster"] is False and payload["has_backdrop"] is False
    assert len(fake.calls) == 1
    assert "no-such-movie-2024" in art.docs  # the attempt is recorded

    assert again.status == 200
    assert len(fake.calls) == 1  # no retry within WATCH_HERO_ART_RETRY_HOURS


def test_art_lookup_can_be_switched_off(monkeypatch):
    fake = _patch_lookup(monkeypatch)
    monkeypatch.setattr(movie_api, "_art_fetch_enabled", lambda: False)
    app, _ = make_client([])
    _, body = run(_get(app, "/api/movies/art/marco-2024?q=Marco&y=2024"))
    assert json.loads(body)["has_poster"] is False
    assert fake.calls == []


@pytest.mark.parametrize("movie_id", ["..%2f..%2fetc%2fpasswd", "<script>", "x" * 80])
def test_art_rejects_unusable_ids(movie_id):
    app, _ = make_client([])
    response, body = run(_get(app, f"/api/movies/art/{movie_id}"))
    assert response.status == 400
    assert json.loads(body)["ok"] is False


def test_art_title_hint_from_a_slug_is_used_for_the_lookup(monkeypatch):
    fake = _patch_lookup(monkeypatch)
    app, _ = make_client([])
    run(_get(app, "/api/movies/art/oppenheimer-2023"))
    assert fake.calls and fake.calls[0][0] == "oppenheimer 2023"


def test_art_skips_lookups_for_hash_only_ids(monkeypatch):
    fake = _patch_lookup(monkeypatch)
    app, _ = make_client([])
    run(_get(app, "/api/movies/art/m-fa16673c9e6b"))
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# GET /api/movies/backdrop/<MOVIE_ID>  (16:9)
# --------------------------------------------------------------------------- #
def test_backdrop_is_proxied_and_resized(monkeypatch):
    art = FakeArtStore([
        {"_id": "marco-2024", "title": "Marco", "year": 2024, "backdrop_url": BACKDROP_URL,
         "poster_url": POSTER_URL, "poster_source": "tmdb", "checked_at": NOW},
    ])
    app, _ = make_client([], art=art)
    session = FakeSession(
        FakeResponse(
            body=b"\xff\xd8\xff\xe0fake-wide-jpeg",
            headers={"Content-Type": "image/jpeg", "Content-Length": "20"},
        )
    )
    original = movie_api._session
    movie_api._session = lambda request=None: session
    try:
        response, body = run(_get_binary(app, "/api/movies/backdrop/marco-2024?w=1280"))
    finally:
        movie_api._session = original

    assert response.status == 200
    assert response.headers["Content-Type"] == "image/jpeg"
    assert body.startswith(b"\xff\xd8\xff")
    assert session.requested == [BACKDROP_URL]


def test_backdrop_falls_back_to_the_poster_when_there_is_no_wide_art(monkeypatch):
    art = FakeArtStore([
        {"_id": "marco-2024", "title": "Marco", "poster_url": POSTER_URL, "checked_at": NOW},
    ])
    app, _ = make_client([], art=art)
    session = FakeSession(FakeResponse(body=b"jpeg", headers={"Content-Type": "image/jpeg"}))
    original = movie_api._session
    movie_api._session = lambda request=None: session
    try:
        response, _ = run(_get_binary(app, "/api/movies/backdrop/marco-2024?w=960"))
    finally:
        movie_api._session = original
    assert response.status == 200
    assert session.requested == [POSTER_URL]


def test_backdrop_without_artwork_serves_the_16_9_placeholder():
    app, _ = make_client([movie_doc(poster_url=None)])
    response, body = run(_get(app, "/api/movies/backdrop/jawan-2023"))
    assert response.status == 200
    assert response.headers["Content-Type"] == "image/svg+xml"
    assert 'width="1280" height="720"' in body
    assert "Jawan" in body
    assert "MINATOVERSE" in body


def test_backdrop_of_an_unknown_movie_is_404():
    app, _ = make_client([])
    response, body = run(_get(app, "/api/movies/backdrop/does-not-exist"))
    assert response.status == 404
    assert "MINATOVERSE" in body


def test_backdrop_placeholder_escapes_titles():
    svg = movie_api.placeholder_backdrop_svg("<script>x</script>Marco").decode("utf-8")
    assert "<script" not in svg
    assert "scriptx/scriptMarco" in svg


def test_backdrop_width_is_snapped_to_the_allow_list():
    assert movie_api._safe_width("100", movie_api.ALLOWED_BACKDROP_WIDTHS) == 480
    assert movie_api._safe_width("1000", movie_api.ALLOWED_BACKDROP_WIDTHS) == 1280
    assert movie_api._safe_width("99999", movie_api.ALLOWED_BACKDROP_WIDTHS) == 1920
    assert movie_api._safe_width(None) == 320  # poster default is unchanged


def test_poster_proxy_serves_hero_only_movies(monkeypatch):
    """A movie that never reached the rail still gets its poster."""
    art = FakeArtStore([
        {"_id": "marco-2024", "title": "Marco", "poster_url": POSTER_URL, "checked_at": NOW},
    ])
    app, _ = make_client([], art=art)
    session = FakeSession(FakeResponse(body=b"jpeg-bytes", headers={"Content-Type": "image/jpeg"}))
    original = movie_api._session
    movie_api._session = lambda request=None: session
    try:
        response, body = run(_get_binary(app, "/api/movies/poster/marco-2024?w=480"))
    finally:
        movie_api._session = original
    assert response.status == 200
    assert body == b"jpeg-bytes"


def test_poster_proxy_never_starts_a_lookup(monkeypatch):
    """The rail's poster path must stay cache-only (no upstream requests)."""
    fake = _patch_lookup(monkeypatch)
    app, _ = make_client([movie_doc(poster_url=None)])
    run(_get(app, "/api/movies/poster/jawan-2023"))
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# movie_art store
# --------------------------------------------------------------------------- #
def test_movie_art_store_upserts_and_reads(monkeypatch):
    from database.movie_art_db import MovieArtStore

    class Col:
        def __init__(self):
            self.docs = {}

        async def update_one(self, filter, update, upsert=False):
            self.docs[filter["_id"]] = {**self.docs.get(filter["_id"], {}), **update["$set"]}

        async def find_one(self, filter):
            return self.docs.get(filter["_id"])

        async def count_documents(self, filter):
            return len(self.docs)

        async def create_index(self, *args, **kwargs):
            return "index"

    store = MovieArtStore(collection=Col())

    async def scenario():
        saved = await store.set_art(
            "marco-2024", title="Marco", year=2024, poster_url=POSTER_URL,
            backdrop_url=BACKDROP_URL, source="tmdb",
        )
        doc = await store.get("marco-2024")
        rejected = await store.set_art("../../etc", title="x")
        unknown = await store.get("../../etc")
        count = await store.count()
        return saved, doc, rejected, unknown, count

    saved, doc, rejected, unknown, count = run(scenario())
    assert saved is True
    assert doc["poster_url"] == POSTER_URL
    assert doc["backdrop_url"] == BACKDROP_URL
    assert doc["checked_at"] is not None
    # unusable ids are rejected before touching the database
    assert rejected is False
    assert unknown is None
    assert count == 1


def test_movie_art_collection_name_comes_from_info(monkeypatch):
    from database import movie_art_db

    store = movie_art_db.MovieArtStore(collection_name=None)
    assert store.collection_name in ("movie_art",)


# --------------------------------------------------------------------------- #
# Template wiring (req.html)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def stream_page():
    from test_newly_uploaded_ui import render_page

    return render_page(mime_type="video/mp4")


@pytest.fixture(scope="module")
def download_page():
    from test_newly_uploaded_ui import render_page

    return render_page(mime_type="application/zip")


def _parse(html):
    from test_newly_uploaded_ui import parse

    return parse(html)


def test_hero_is_rendered_above_the_player(stream_page):
    _, html = stream_page
    parsed = _parse(html)

    assert "movieHero" in parsed.ids
    tag, attrs = parsed.ids["movieHero"]
    assert tag == "section"
    assert attrs["data-art"] == "/api/movies/art"
    assert attrs["data-movie-id"] == "example-movie-2023"
    assert attrs["data-title"] == "Example movie"
    assert attrs["data-year"] == "2023"
    assert attrs["data-deeplink"] == f"https://t.me/{BOT}?start=movie_example-movie-2023"
    assert attrs["data-mh-state"] == "loading"  # artwork starts as a skeleton
    assert attrs["aria-labelledby"] == "mhTitle"
    # direct child of <main>, like the rail/trending sections
    assert [t for t, _ in parsed.ancestors["movieHero"]][-1] == "main"
    assert "movieHero" in html.split('id="player"')[0]


def test_hero_has_a_poster_card_that_opens_the_movie(stream_page):
    _, html = stream_page
    parsed = _parse(html)
    _, card = parsed.ids["mhPosterCard"]
    assert card["href"] == f"https://t.me/{BOT}?start=movie_example-movie-2023"
    assert card["target"] == "_blank"
    assert card["rel"] == "noopener noreferrer"
    assert "mh-poster-fallback" in html  # placeholder before/without artwork
    assert 'aspect-ratio: 2 / 3' in Path(ROOT / "dreamxbotz/static/watch_hero.css").read_text()
    # the visual button points at the very same link
    assert parsed.ids["mhOpen"][1]["href"] == card["href"]


def test_hero_buttons_and_states_exist(stream_page):
    _, html = stream_page
    parsed = _parse(html)
    assert parsed.ids["mhCopy"][0] == "button"
    assert parsed.ids["mhPlay"][1]["href"] == "#player"
    assert parsed.ids["mhPoster"][0] == "img"
    assert parsed.ids["mhBackdrop"][0] == "img"
    # loading / error / placeholder states are handled by the runtime
    assert parsed.ids["mhStatus"][1]["role"] == "status"
    assert "hidden" in parsed.ids["mhStatus"][1]


def test_hero_shows_year_quality_and_release_chips(stream_page):
    _, html = stream_page
    assert "mh-chip" in html
    assert ">2023<" in html.replace("\n", "").replace(" ", "") or "2023" in html
    assert "1080p" in html
    assert "mh-badge" in html


def test_hero_loads_only_its_own_assets_with_a_cache_buster(stream_page):
    _, html = stream_page
    assert "/static/watch_hero.css?v=" in html
    assert '/static/watch_hero.js?v=' in html
    assert html.count("/static/watch_hero.css?v=") == 1
    assert html.count("/static/watch_hero.js?v=") == 1
    # the hero assets are served by the bot (whitelisted), not by a CDN
    assert "watch_hero.css" in static_assets.ASSETS
    assert "watch_hero.js" in static_assets.ASSETS
    token = static_assets.version_token()
    assert "watch_hero.css" in static_assets.ASSETS and token


def test_page_has_a_single_h1_and_the_sidebar_keeps_its_style(stream_page):
    _, html = stream_page
    assert html.count("<h1") == 1
    assert '<h1 class="mh-title"' in html
    assert '<h2 class="side-title">' in html
    # styling of the sidebar title is unchanged (class based)
    assert ".side-title {" in html


def test_watch_page_never_leaks_secrets(stream_page):
    _, html = stream_page
    for secret in ("BOT_TOKEN", "DATABASE_URI", "DATABASE_CONNECTION", "api_hash", "file_id",
                   "secret-file-id", "image.tmdb.org"):
        assert secret not in html


def test_hero_can_be_disabled(monkeypatch):
    from test_newly_uploaded_ui import render_page

    monkeypatch.setattr(
        watch_hero, "_cfg", lambda name, default: False if name == "WATCH_HERO" else default
    )
    _, html = render_page(mime_type="video/mp4")
    assert "movieHero" not in html
    assert "/static/watch_hero.css" not in html
    # the page keeps a heading, so nothing about the layout breaks
    assert '<h1 class="side-title">' in html


def test_audio_streams_do_not_get_a_movie_hero():
    """req.html also renders songs – the poster strip must stay away there."""
    from test_newly_uploaded_ui import render_page

    _, html = render_page(mime_type="audio/mpeg")
    assert "movieHero" not in html
    assert "watch_hero.css" not in html
    assert '<h1 class="side-title">' in html  # player page keeps its heading


def test_download_page_is_untouched(download_page):
    _, html = download_page
    assert "movieHero" not in html
    assert "watch_hero.css" not in html
    assert "newlyUploaded" in html


# --------------------------------------------------------------------------- #
# Assets
# --------------------------------------------------------------------------- #
def test_hero_javascript_is_defensive():
    source = (ROOT / "dreamxbotz/static/watch_hero.js").read_text(encoding="utf-8")
    assert "innerHTML" not in source
    assert "document.write" not in source
    assert "eval(" not in source
    # deep links and ids are validated in the browser as well
    assert "SAFE_DEEPLINK" in source and "SAFE_ID" in source
    # artwork may only come from the API's own /api/movies/ endpoints
    assert 'indexOf("/api/movies/")' in source
    assert "encodeURIComponent" in source
    # explicit loading / ready / error states + placeholder fallback
    for state in ('"loading"', '"ready"', '"error"'):
        assert state in source
    assert "data-mh-state" in source


def test_hero_css_covers_theme_ratio_and_responsiveness():
    css = (ROOT / "dreamxbotz/static/watch_hero.css").read_text(encoding="utf-8")
    assert "aspect-ratio: 2 / 3" in css        # correct poster ratio
    assert "object-fit: cover" in css          # no squashed artwork
    assert "translateY(-5px) scale(1.03)" in css  # hover zoom
    assert "0 0 34px rgba(245, 197, 24, 0.3)" in css  # gold glow
    assert "@media (max-width: 640px)" in css
    assert "@media (max-width: 420px)" in css
    assert "prefers-reduced-motion" in css
    assert "url(http" not in css and "@import" not in css  # no external assets


def test_static_assets_serve_the_hero_files():
    app = web.Application()
    app.add_routes(static_assets.routes)

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            css = await client.get("/static/watch_hero.css")
            js = await client.get("/static/watch_hero.js")
            missing = await client.get("/static/watch_hero.txt")
            return css, js, missing

    css, js, missing = run(scenario())
    assert css.status == 200 and css.headers["Content-Type"].startswith("text/css")
    assert js.status == 200 and js.headers["Content-Type"].startswith("text/javascript")
    assert missing.status == 404  # only whitelisted files are served
