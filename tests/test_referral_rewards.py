"""The referral reward must actually reach the referrer.

The shipped code called ``message.reply_text(user_id, "...")`` (an int as
text) which raised AFTER the points were reset — the free month was never
granted. Points are now an atomic $inc and the reward is granted before any
messaging is attempted.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import plugins.commands as commands


def _fake_start_env(monkeypatch, points_after_inc):
    referdb = SimpleNamespace(
        is_user_in_list=AsyncMock(return_value=False),
        add_user=AsyncMock(),
        increment_refer_points=AsyncMock(side_effect=[points_after_inc, points_after_inc - 100]),
        get_refer_points=AsyncMock(return_value=points_after_inc),
    )
    db = SimpleNamespace(
        is_user_exist=AsyncMock(return_value=False),
        extend_premium=AsyncMock(),
    )
    uss = SimpleNamespace(id=555, mention="@referrer")
    client = SimpleNamespace(
        get_users=AsyncMock(return_value=uss),
        send_message=AsyncMock(),
    )
    message = SimpleNamespace(
        command=["start", "reff_555"],
        from_user=SimpleNamespace(id=999, mention="@newuser"),
        chat=SimpleNamespace(id=999, type=commands.enums.ChatType.PRIVATE),
        reply_text=AsyncMock(),
        react=AsyncMock(),
    )
    monkeypatch.setattr(commands, "referdb", referdb)
    monkeypatch.setattr(commands, "db", db)
    monkeypatch.setattr(commands, "EMOJI_MODE", False)
    return client, message, db, referdb


def test_reaching_100_points_grants_the_free_month(monkeypatch):
    client, message, db, referdb = _fake_start_env(monkeypatch, 100)

    asyncio.run(commands.start(client, message))

    db.extend_premium.assert_awaited_once_with(555, 2592000)
    # Points were decremented atomically, not blind-reset before the reward.
    assert referdb.increment_refer_points.await_args_list[1].args == (555, -100)
    # The referrer is notified via send_message (the old reply_text(user_id,…)
    # misuse crashed here and ate the reward).
    sent_to = [c.kwargs.get("chat_id", c.args[0] if c.args else None)
               for c in client.send_message.await_args_list]
    assert 555 in sent_to


def test_reward_survives_a_failed_notification(monkeypatch):
    client, message, db, _ = _fake_start_env(monkeypatch, 110)
    client.send_message = AsyncMock(side_effect=RuntimeError("user blocked bot"))

    asyncio.run(commands.start(client, message))

    db.extend_premium.assert_awaited_once_with(555, 2592000)


def test_below_100_points_keeps_accumulating(monkeypatch):
    client, message, db, referdb = _fake_start_env(monkeypatch, 40)

    asyncio.run(commands.start(client, message))

    db.extend_premium.assert_not_awaited()
    # Only the +10 increment happened; no reset.
    assert referdb.increment_refer_points.await_count == 1
    assert referdb.increment_refer_points.await_args.args == (555, 10)


def test_increment_is_atomic_and_returns_the_new_total():
    mongomock_motor = pytest.importorskip("mongomock_motor")
    from database.refer import UserTracker

    async def scenario():
        client = mongomock_motor.AsyncMongoMockClient()
        tracker = UserTracker(database=client["test"])
        assert await tracker.increment_refer_points(1, 10) == 10
        results = await asyncio.gather(*[tracker.increment_refer_points(1, 10) for _ in range(9)])
        assert sorted(results) == list(range(20, 110, 10))
        assert await tracker.get_refer_points(1) == 100

    asyncio.run(scenario())
