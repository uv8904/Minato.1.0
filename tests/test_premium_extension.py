"""Renewals must EXTEND remaining premium, never overwrite paid days."""
import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from database.users_chats_db import Database  # noqa: E402


def _fresh_db():
    client = mongomock_motor.AsyncMongoMockClient()
    db = Database.__new__(Database)          # skip the real Mongo client
    db.users = client["test"]["uersz"]
    return db


def test_extend_premium_adds_on_top_of_remaining_time():
    db = _fresh_db()

    async def scenario():
        now = datetime.datetime.now()
        await db.update_user({"id": 1, "expiry_time": now + datetime.timedelta(days=20)})
        expiry = await db.extend_premium(1, 30 * 86400)
        # 20 remaining days + 30 new days, not just 30.
        assert expiry > now + datetime.timedelta(days=49)
        assert expiry < now + datetime.timedelta(days=51)

    asyncio.run(scenario())


def test_extend_premium_from_now_when_expired_or_new():
    db = _fresh_db()

    async def scenario():
        now = datetime.datetime.now()
        await db.update_user({"id": 2, "expiry_time": now - datetime.timedelta(days=5)})
        expiry = await db.extend_premium(2, 86400)
        assert now + datetime.timedelta(hours=23) < expiry < now + datetime.timedelta(hours=25)
        # Brand-new user (no record at all).
        expiry = await db.extend_premium(3, 86400)
        assert now + datetime.timedelta(hours=23) < expiry < now + datetime.timedelta(hours=25)

    asyncio.run(scenario())


def test_free_trial_does_not_wipe_active_premium():
    db = _fresh_db()

    async def scenario():
        now = datetime.datetime.now()
        await db.update_user({"id": 4, "expiry_time": now + datetime.timedelta(days=10)})
        await db.give_free_trial(4)
        user = await db.get_user(4)
        assert user["expiry_time"] > now + datetime.timedelta(days=10)
        assert user["has_free_trial"] is True

    asyncio.run(scenario())


def test_fampay_grant_premium_uses_extension(monkeypatch):
    import plugins.FamPay as fampay

    now = datetime.datetime.now()
    stored = {"id": 7, "expiry_time": now + datetime.timedelta(days=20)}

    async def extend_premium(user_id, seconds):
        base = max(now, stored["expiry_time"])
        stored["expiry_time"] = base + datetime.timedelta(seconds=seconds)
        return stored["expiry_time"]

    fake_db = SimpleNamespace(
        extend_premium=AsyncMock(side_effect=extend_premium),
        get_user=AsyncMock(side_effect=lambda uid: dict(stored)),
    )
    monkeypatch.setattr(fampay, "db", fake_db)

    async def scenario():
        expiry_str = await fampay.grant_premium(None, 7, "1 month")
        assert expiry_str                      # IST display string returned
        fake_db.extend_premium.assert_awaited_once()
        assert stored["expiry_time"] > now + datetime.timedelta(days=45)

    asyncio.run(scenario())
