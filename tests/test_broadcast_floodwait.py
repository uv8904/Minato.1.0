"""Broadcast helpers must survive FloodWait and never delete live groups.

Regression tests for three shipped bugs:
* ``e.x`` does not exist on electrogram's FloodWait (``.value`` does), so the
  retry path itself raised AttributeError;
* the retry dropped the required ``is_pin`` argument (TypeError);
* ``groups_broadcast``/``junk_group`` deleted the group from the DB on ANY
  exception — one transient network error unregistered a live group.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pyrogram.errors import FloodWait, PeerIdInvalid

import utils


def _msg(copy_side_effect):
    return SimpleNamespace(copy=AsyncMock(side_effect=copy_side_effect))


def test_users_broadcast_retries_floodwait_and_keeps_is_pin(monkeypatch):
    monkeypatch.setattr(utils, "db", SimpleNamespace(delete_user=AsyncMock()))
    pinned = SimpleNamespace(pin=AsyncMock())

    async def scenario():
        message = _msg([FloodWait(0), pinned])
        ok, result = await utils.users_broadcast(111, message, True)
        assert (ok, result) == (True, "Success")
        assert message.copy.await_count == 2      # retried after FloodWait
        pinned.pin.assert_awaited_once()          # is_pin survived the retry

    asyncio.run(scenario())


def test_groups_broadcast_retries_floodwait(monkeypatch):
    monkeypatch.setattr(utils, "db", SimpleNamespace(delete_chat=AsyncMock()))
    sent = SimpleNamespace(pin=AsyncMock())

    async def scenario():
        message = _msg([FloodWait(0), sent])
        assert await utils.groups_broadcast(-100, message, False) == "Success"
        assert message.copy.await_count == 2
        utils.db.delete_chat.assert_not_awaited()

    asyncio.run(scenario())


def test_transient_group_error_keeps_the_group_in_the_database(monkeypatch):
    monkeypatch.setattr(utils, "db", SimpleNamespace(delete_chat=AsyncMock()))

    async def scenario():
        message = _msg(RuntimeError("network hiccup"))
        assert await utils.groups_broadcast(-100, message, False) == "Error"
        utils.db.delete_chat.assert_not_awaited()

    asyncio.run(scenario())


def test_dead_group_is_removed_from_the_database(monkeypatch):
    monkeypatch.setattr(utils, "db", SimpleNamespace(delete_chat=AsyncMock()))

    async def scenario():
        message = _msg(PeerIdInvalid())
        assert await utils.groups_broadcast(-100, message, False) == "Error"
        utils.db.delete_chat.assert_awaited_once_with(-100)

    asyncio.run(scenario())


def test_junk_group_transient_error_does_not_unregister(monkeypatch):
    monkeypatch.setattr(utils, "db", SimpleNamespace(delete_chat=AsyncMock()))

    async def scenario():
        message = _msg(RuntimeError("timeout"))
        ok, status, _ = await utils.junk_group(-100, message)
        assert ok is False
        assert status != "deleted"               # must not trigger leave_chat
        utils.db.delete_chat.assert_not_awaited()

    asyncio.run(scenario())
