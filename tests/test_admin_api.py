"""The HTTP dashboards must not be readable by anonymous visitors."""
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import info
from dreamxbotz.server import admin_api, analytics_api, auth


class _Request:
    def __init__(self, query=None, headers=None):
        self.query = query or {}
        self.headers = headers or {}


@pytest.fixture(autouse=True)
def _locked_down(monkeypatch):
    monkeypatch.setattr(info, "ADMINS", [12345])
    monkeypatch.setattr(info, "ADMIN_DASHBOARD_TOKEN", "s3cret-dashboard-token")
    monkeypatch.setattr(info, "ADMIN_DASHBOARD_PUBLIC", False)
    monkeypatch.setattr(info, "ADMIN_DASHBOARD_ALLOW_ID", False)


def test_public_telegram_id_is_not_a_credential_by_default():
    # Telegram ids are public information — knowing an admin's id must NOT
    # unlock the dashboards unless the operator explicitly opts in.
    assert not auth.is_admin_request(_Request(query={"admin_id": "12345"}))
    assert not auth.is_admin_request(_Request(headers={"X-Admin-Id": "12345"}))


def test_admin_id_legacy_shortcut_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(info, "ADMIN_DASHBOARD_ALLOW_ID", True)
    assert auth.is_admin_request(_Request(query={"admin_id": "12345"}))
    assert auth.is_admin_request(_Request(headers={"X-Admin-Id": "12345"}))
    assert not auth.is_admin_request(_Request(query={"admin_id": "99999"}))


def test_configured_token_is_accepted_in_constant_time():
    assert auth.is_admin_request(_Request(headers={"X-Admin-Token": "s3cret-dashboard-token"}))
    assert auth.is_admin_request(_Request(query={"token": "s3cret-dashboard-token"}))


def test_arbitrary_tokens_host_headers_and_empty_admins_are_rejected(monkeypatch):
    assert not auth.is_admin_request(_Request())
    assert not auth.is_admin_request(_Request(query={"token": "x" * 40}))
    assert not auth.is_admin_request(_Request(headers={"Host": "localhost", "X-Forwarded-For": "127.0.0.1"}))
    monkeypatch.setattr(info, "ADMINS", [])
    assert not auth.is_admin_request(_Request(query={"admin_id": "12345"}))


def _make_app():
    app = web.Application()
    app.add_routes(admin_api.routes)
    app.add_routes(analytics_api.routes)
    return app


def test_protected_routes_reject_anonymous_requests():
    async def scenario():
        async with TestClient(TestServer(_make_app())) as client:
            for path in ("/api/admin/stats", "/admin", "/api/analytics/stats", "/analytics"):
                resp = await client.get(path, headers={"Host": "e2b.app"})
                assert resp.status == 401, path

    asyncio.run(scenario())


def test_admin_token_unlocks_the_dashboard_html():
    async def scenario():
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/analytics", headers={"X-Admin-Token": "s3cret-dashboard-token"})
            assert resp.status == 200
            assert "MinatoVerse" in await resp.text()

    asyncio.run(scenario())


def test_preview_opt_in_is_explicit(monkeypatch):
    monkeypatch.setattr(info, "ADMIN_DASHBOARD_PUBLIC", True)
    assert auth.is_admin_request(_Request())


def test_track_endpoint_has_a_per_ip_budget(monkeypatch):
    monkeypatch.setattr(analytics_api, "_track_hits", {"1.2.3.4": [0.0] * analytics_api._TRACK_BUDGET})

    async def scenario():
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/analytics/track",
                data=json.dumps({"title": "Jawan"}),
                headers={"X-Forwarded-For": "1.2.3.4"},
            )
            assert resp.status == 429

    asyncio.run(scenario())
