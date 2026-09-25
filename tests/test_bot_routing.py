"""Regression tests for the PM routing outage reported on 2026-09-25.

Electrogram/Pyrogram executes only the first matching handler in each group.
A broad FamPay proof handler loaded before commands/search and silently consumed
almost every PM update, so the bot looked online but answered nothing.
"""
import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("pyrogram")

from pyrogram import enums
from pyrogram.types import Chat, Message, User

import plugins.FamPay as fampay
import plugins.commands as commands
import plugins.p_ttishow as status_plugin
import plugins.pmfilter as pmfilter


CLIENT = SimpleNamespace(me=SimpleNamespace(username="WandaBot"))


def private_message(text: str, *, user_id: int = 123) -> Message:
    return Message(
        id=1,
        text=text,
        chat=Chat(id=user_id, type=enums.ChatType.PRIVATE),
        from_user=User(id=user_id, first_name="Tester"),
        outgoing=False,
    )


def matches(handler_function, text: str, *, user_id: int = 123) -> bool:
    handler, _group = handler_function.handlers[0]
    return asyncio.run(handler.check(CLIENT, private_message(text, user_id=user_id)))


@pytest.mark.parametrize(
    "text",
    [
        "marco",
        "rrr",
        "pushpa 2",
        "/start",
        "/settings",
        "/stats",
        "/alive",
        "/plan",
        "/unknown 420987654321",
    ],
)
def test_fampay_plain_utr_handler_does_not_steal_searches_or_commands(text):
    assert not matches(fampay.fampay_plain_utr_message, text)


@pytest.mark.parametrize(
    "text",
    [
        "420987654321",
        "UTR: 420987654321",
        "my utr is 420987654321 thanks",
        "RRN=123456789012",
    ],
)
def test_fampay_plain_utr_handler_still_accepts_no_command_proof(text):
    assert matches(fampay.fampay_plain_utr_message, text)


def test_updates_reach_the_handlers_the_user_expected():
    assert matches(pmfilter.pm_text, "marco")
    assert matches(commands.start, "/start")
    assert matches(commands.settings, "/settings")

    admin_id = int(commands.ADMINS[0])
    assert matches(status_plugin.get_stats, "/stats", user_id=admin_id)


def test_payment_screenshot_probe_runs_before_but_does_not_replace_media_handlers():
    _handler, group = fampay.fampay_screenshot_message.handlers[0]
    assert group == -1


def test_start_replies_before_optional_database_registration(monkeypatch):
    replies = []

    async def no_flash(*args, **kwargs):
        return None

    async def database_down(user_id):
        raise ConnectionError("mongo unavailable")

    class FakeMessage:
        command = ["start"]
        text = "/start"
        chat = SimpleNamespace(id=123, type=enums.ChatType.PRIVATE)
        from_user = SimpleNamespace(
            id=123,
            first_name="Tester",
            mention="<a href='tg://user?id=123'>Tester</a>",
        )

        async def reply_photo(self, *args, **kwargs):
            raise RuntimeError("expired start photo URL")

        async def reply_text(self, text, **kwargs):
            if kwargs.get("reply_markup"):
                raise RuntimeError("button styles rejected")
            replies.append((text, kwargs))
            return SimpleNamespace(delete=lambda: None)

        async def react(self, **kwargs):
            raise RuntimeError("reactions disabled")

    monkeypatch.setattr(commands, "EMOJI_MODE", True)
    monkeypatch.setattr(commands, "send_start_flash", no_flash)
    monkeypatch.setattr(commands.db, "is_user_exist", database_down)
    monkeypatch.setattr(commands.temp, "U_NAME", "WandaBot")
    monkeypatch.setattr(commands.temp, "B_NAME", "Wanda")

    asyncio.run(commands.start(SimpleNamespace(), FakeMessage()))

    assert len(replies) == 1
    assert "Tester" in replies[0][0]
    assert "reply_markup" not in replies[0][1]
