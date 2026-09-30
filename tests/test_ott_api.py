"""``/api/ott/*`` + page endpoints of the JioHotstar storefront (Option A).

Everything runs against an in-memory aiohttp app with the catalog stubbed out,
so the tests pin down the HTTP contract (clamping, cache headers, CORS, error
shapes, registration rules) without a database or network::

    pytest tests/test_ott_api.py -q
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import info  # noqa: E402
import plugins.route as route_module  # noqa: E402
from dreamxbotz.server import ott_api, static_assets  # noqa: E402
from dreamxbotz.util import ott_catalog, ott_pages  # noqa: E402

CARD = {
    "id": "jawan-2023",
    "title": "Jawan",
    "year": 2023,
    "quality": "1080p",
    "poster": "/api/movies/poster/jawan-2023?v=abc",
    "deeplink": "https://t.me/MyMovieBot?start=movie_jawan-2023",
}


def run(coro):
    return asyncio.run(coro)


def app_for_tests(*, catalog=True):
    app = web.Application()
    ott_api.build_routes(app, with_pages=True)
    return app


def stub_catalog(monkeypatch, *, home=None, search=None, suggest=None, movie=None, fail=False, spy=None):
    """Replace the catalog entry points and record what the handlers ask for."""
    calls = spy if spy is not None else {}

    async def fake_home(**kwargs):
        calls["home"] = kwargs
        if fail:
            raise RuntimeError("mongo down")
        return home if home is not None else {"ok": True, "hero": [CARD], "rails": [], "genres": [], "counts": {}}

    async def fake_search(query, **kwargs):
        calls["search"] = {"query": query, **kwargs}
        if fail:
            raise RuntimeError("mongo down")
        return search if search is not None else {"ok": True, "results": [CARD], "total": 1, "facets": {}}

    async def fake_suggest(query, limit):
        calls["suggest"] = {"query": query, "limit": limit}
        return suggest if suggest is not None else {"ok": True, "items": [{"title": "Jawan", "query": "jawan"}]}

    async def fake_movie(movie_id):
        calls["movie"] = movie_id
        return movie

    monkeypatch.setattr(ott_catalog, "cached_home_payload", fake_home)
    monkeypatch.setattr(ott_catalog, "cached_search", fake_search)
    monkeypatch.setattr(ott_catalog, "cached_suggestions", fake_suggest)
    monkeypatch.setattr(ott_catalog, "cached_movie", fake_movie)
    monkeypatch.setattr(ott_catalog, "kick_enrichment", lambda *_a, **_k: calls.setdefault("enriched", True) or False)
    return calls


def _status_and_body(make_app, path):
    async def scenario():
        async with TestClient(TestServer(make_app())) as client:
            response = await client.get(path)
            return response.status, await response.text()

    return run(scenario())


def get(make_app, path, **kwargs):
    """One request on a fresh app (aiohttp binds an app to its first loop)."""
    app = make_app() if callable(make_app) else make_app

    async def scenario():
        async with TestClient(TestServer(app)) as client:
            response = await client.get(path, **kwargs)
            return response.status, dict(response.headers), await response.text()

    return run(scenario())


# --------------------------------------------------------------------------- #
# Health / registration
# --------------------------------------------------------------------------- #
def test_health_probe_matches_the_old_root_json():
    status, headers, body = get(app_for_tests, "/health")

    assert status == 200
    assert '"service": "dreamxbotz"' in body
    assert '"ok": true' in body
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Robots-Tag"] == "noindex, nofollow"


def test_register_adds_the_storefront_before_the_catch_all(monkeypatch):
    app = web.Application()
    ott_api.register(app)
    paths = {route.resource.canonical for route in app.router.routes()}

    assert {"/home", "/search", "/api/ott/home", "/api/ott/search", "/api/ott/genres", "/api/ott/suggest", "/health"} <= paths
    assert "/" in paths  # OTT_HOME_AT_ROOT default
    assert "/api/ott/movie/{movie_id}" in paths


def test_register_is_a_no_op_when_the_homepage_is_disabled(monkeypatch):
    monkeypatch.setattr(ott_api, "_enabled", lambda: False)
    app = web.Application()
    ott_api.register(app)

    assert len(app.router.routes()) == 0


def test_pages_report_an_empty_storefront_when_disabled(monkeypatch):
    monkeypatch.setattr(ott_api, "_enabled", lambda: False)

    status, headers, body = get(app_for_tests, "/api/ott/home")
    assert status == 200 and '"error": "disabled"' in body
    assert headers["Cache-Control"] == "no-store"

    status, _headers, body = get(app_for_tests, "/api/ott/search?q=x")
    assert status == 200 and '"error": "disabled"' in body


# --------------------------------------------------------------------------- #
# JSON APIs
# --------------------------------------------------------------------------- #
def test_home_api_clamps_its_parameters(monkeypatch):
    calls = stub_catalog(monkeypatch)
    monkeypatch.setattr(ott_api, "_public_username", lambda _request=None: "MyMovieBot")

    status, _headers, body = get(app_for_tests, "/api/ott/home?rail=999&rails=0&hero=0&light=1")

    assert status == 200
    assert calls["home"] == {
        "rail_limit": 40,     # OTT_RAIL_LIMIT clamp
        "max_rails": 1,       # OTT_RAILS_MAX clamp
        "hero_limit": 1,      # OTT_HERO_LIMIT clamp
        "light": True,
    }
    assert calls["enriched"] is True
    assert '"bot_username": "MyMovieBot"' in body


def test_home_api_degrades_to_an_empty_payload(monkeypatch):
    stub_catalog(monkeypatch, fail=True)

    status, headers, body = get(app_for_tests, "/api/ott/home")

    assert status == 200
    assert '"ok": false' in body and '"error": "unavailable"' in body
    assert headers["Cache-Control"] == "no-store"


def test_search_api_clamps_pagination_and_only_caches_page_one(monkeypatch):
    calls = stub_catalog(monkeypatch)

    status, headers, _body = get(
        app_for_tests,
        "/api/ott/search?q=jawan&page=3&limit=999&sort=rating&genre=Action&quality=4K&year=2024",
    )

    assert status == 200
    assert calls["search"]["limit"] == info.OTT_SEARCH_MAX_LIMIT
    assert calls["search"]["offset"] == 2 * info.OTT_SEARCH_MAX_LIMIT
    assert calls["search"]["genre"] == "Action"
    assert calls["search"]["quality"] == "4K"
    assert calls["search"]["year"] == "2024"
    assert headers["Cache-Control"] == "no-store"   # deep pages must not be cached

    _status, headers, _body = get(app_for_tests, "/api/ott/search?q=jawan")
    assert headers["Cache-Control"].startswith("public, max-age=")


def test_search_api_never_fails_the_page(monkeypatch):
    stub_catalog(monkeypatch, fail=True)

    status, _headers, body = get(app_for_tests, "/api/ott/search?q=jawan")

    assert status == 200 and '"results": []' in body and '"error": "unavailable"' in body


def test_suggest_api_clamps_and_short_caches(monkeypatch):
    calls = stub_catalog(monkeypatch)

    status, headers, body = get(app_for_tests, "/api/ott/suggest?q=jaw&limit=99")

    assert status == 200
    assert calls["suggest"] == {"query": "jaw", "limit": 20}
    assert headers["Cache-Control"] == "public, max-age=30"
    assert '"items"' in body


def test_movie_api_returns_one_card(monkeypatch):
    calls = stub_catalog(monkeypatch, movie=dict(CARD))

    status, _headers, body = get(app_for_tests, "/api/ott/movie/jawan-2023")

    assert status == 200 and calls["movie"] == "jawan-2023"
    assert '"movie"' in body

    stub_catalog(monkeypatch, movie=None)
    status, headers, body = get(app_for_tests, "/api/ott/movie/nope-2020")
    assert status == 404 and '"error": "not_found"' in body
    assert headers["Cache-Control"] == "public, max-age=10"


def test_movie_api_rejects_payloads_that_are_not_ids(monkeypatch):
    stub_catalog(monkeypatch, movie=dict(CARD))

    status, _headers, body = get(app_for_tests, "/api/ott/movie/%40%40%40")

    assert status == 400 and '"error": "invalid_id"' in body


def test_genres_api_echoes_the_catalog_counts(monkeypatch):
    stub_catalog(
        monkeypatch,
        home={"ok": True, "hero": [], "rails": [], "genres": [{"name": "Action", "count": 4}], "counts": {"movies": 4}},
    )

    status, _headers, body = get(app_for_tests, "/api/ott/genres")

    assert status == 200
    assert '"genres": [{"name": "Action", "count": 4}]' in body
    assert '"movies": 4' in body


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
def test_pages_are_private_and_short_lived(monkeypatch):
    async def fake_home(_context=None):
        return "<html><body>storefront</body></html>"

    async def fake_search(_context=None):
        return "<html><body>search</body></html>"

    monkeypatch.setattr(ott_pages, "render_home", fake_home)
    monkeypatch.setattr(ott_pages, "render_search", fake_search)

    status, headers, body = get(app_for_tests, "/home")
    assert status == 200 and "storefront" in body
    assert headers["Content-Type"].startswith("text/html")
    assert headers["X-Robots-Tag"] == "noindex, nofollow"
    assert headers["Cache-Control"].startswith("public, max-age=")  # refreshed by the browser

    status, headers, body = get(app_for_tests, "/search?q=jawan")
    assert status == 200 and "search" in body
    assert headers["Cache-Control"] == "no-store"


def test_home_page_still_renders_when_the_catalog_is_down(monkeypatch):
    """The real page + a failing catalog: HTTP 200, empty storefront, hint."""
    stub_catalog(monkeypatch, fail=True)

    async def scenario():
        async with TestClient(TestServer(app_for_tests())) as client:
            response = await client.get("/home")
            return response, await response.text()

    response, body = run(scenario())

    assert response.status == 200
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert 'data-empty="1"' in body          # hero shows its empty state
    assert "ot-alert" in body                # …and the visitor gets a hint
    assert "Traceback" not in body


# --------------------------------------------------------------------------- #
# CORS
# --------------------------------------------------------------------------- #
def test_cors_is_only_sent_for_an_allowed_origin(monkeypatch):
    stub_catalog(monkeypatch)
    monkeypatch.setattr(info, "NEW_UPLOADED_CORS_ORIGIN", "https://site.example")

    _status, headers, _body = get(app_for_tests, "/api/ott/home", headers={"Origin": "https://site.example"})
    assert headers.get("Access-Control-Allow-Origin") == "https://site.example"

    _status, headers, _body = get(app_for_tests, "/api/ott/home", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in headers


def test_no_cors_headers_by_default(monkeypatch):
    stub_catalog(monkeypatch)
    monkeypatch.setattr(info, "NEW_UPLOADED_CORS_ORIGIN", "")

    _status, headers, _body = get(app_for_tests, "/api/ott/home", headers={"Origin": "https://site.example"})

    assert "Access-Control-Allow-Origin" not in headers


# --------------------------------------------------------------------------- #
# Routing precedence (the stream catch-all is greedy)
# --------------------------------------------------------------------------- #
def test_storefront_wins_over_the_stream_catch_all(monkeypatch):
    """``/{path:\S+}`` in ``plugins/route.py`` must never swallow the storefront."""
    stub_catalog(monkeypatch)

    async def fake_home(_context=None):
        return "<html><body>storefront</body></html>"

    async def fake_search(_context=None):
        return "<html><body>search page</body></html>"

    monkeypatch.setattr(ott_pages, "render_home", fake_home)
    monkeypatch.setattr(ott_pages, "render_search", fake_search)

    def make_app():
        app = web.Application()
        app.add_routes(static_assets.routes)
        ott_api.build_routes(app, with_pages=True)
        app.add_routes(route_module.routes)  # the catch-all
        return app

    for path, needle in (("/home", "storefront"), ("/search", "search page"), ("/api/ott/home", "\"ok\"")):
        status, body = _status_and_body(make_app, path)
        assert status == 200, path
        assert needle in body, path
