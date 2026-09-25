"""Maintenance mode (docs/MAINTENANCE_MODE.md).

While an admin has sent ``/maint on`` every normal user must get the
"under maintenance" notice instead of search results / files, admins keep
full access, and the flag survives restarts because it lives in Mongo.

The database tests swap ``db.botcol`` for a mongomock-motor collection
(a real, in-memory MongoDB implementation), so the code under test is the
production code path - only the server behind it is fake.

Run with the project dependencies installed::

    pytest tests/test_maintenance_mode.py -q
"""
import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("pyrogram")
mongomock_motor = pytest.importorskip("mongomock_motor")

import info  # noqa: E402
from database.users_chats_db import db  # noqa: E402
from plugins import maintenance as maint  # noqa: E402
from Script import script  # noqa: E402
from utils import temp  # noqa: E402

ADMIN_ID = info.ADMINS[0]
NORMAL_ID = 999000111


@pytest.fixture
def fresh_botcol():
    """Give the Database an isolated, in-memory bot_settings collection."""
    client = mongomock_motor.AsyncMongoMockClient()
    original = db.botcol
    db.botcol = client["test"]["bot_settings"]
    yield db.botcol
    db.botcol = original


# --------------------------------------------------------------------------
# Database: persistence of the flag
# --------------------------------------------------------------------------
def test_db_flag_defaults_off_and_roundtrips(fresh_botcol):
    async def run():
        assert await db.get_maintenance_mode() is False   # no doc yet -> OFF
        await db.set_maintenance_mode(True)
        assert await db.get_maintenance_mode() is True
        await db.set_maintenance_mode(False)
        assert await db.get_maintenance_mode() is False
        await db.set_maintenance_mode(True)               # back ON
        assert await db.get_maintenance_mode() is True

    asyncio.run(run())


# --------------------------------------------------------------------------
# Admin detection (id based + username based + missing from_user)
# --------------------------------------------------------------------------
def test_is_admin_by_id(monkeypatch):
    monkeypatch.setattr(maint, "ADMINS", [42, "SomeAdmin"])
    assert maint.is_admin_update(SimpleNamespace(from_user=SimpleNamespace(id=42, username=None))) is True
    assert maint.is_admin_update(SimpleNamespace(from_user=SimpleNamespace(id=43, username="SomeAdmin"))) is True
    assert maint.is_admin_update(SimpleNamespace(from_user=SimpleNamespace(id=43, username="someADMIN"))) is True
    assert maint.is_admin_update(SimpleNamespace(from_user=SimpleNamespace(id=43, username="other"))) is False
    assert maint.is_admin_update(SimpleNamespace(from_user=None)) is False


# --------------------------------------------------------------------------
# The runtime filter simply mirrors temp.MAINTENANCE
# --------------------------------------------------------------------------
def test_filter_follows_temp_flag(monkeypatch):
    monkeypatch.setattr(temp, "MAINTENANCE", True)
    assert asyncio.run(maint.maintenance_is_on(None, None, None)) is True
    monkeypatch.setattr(temp, "MAINTENANCE", False)
    assert asyncio.run(maint.maintenance_is_on(None, None, None)) is False


# --------------------------------------------------------------------------
# Fakes that behave like pyrogram Message / CallbackQuery objects
# --------------------------------------------------------------------------
class FakeMessage:
    def __init__(self, user_id, chat_type="private", text=None, username=None):
        self.from_user = SimpleNamespace(id=user_id, username=username, mention=f"[user](tg://user?id={user_id})")
        self.chat = SimpleNamespace(type=chat_type)
        self.text = text
        self.caption = None
        self.replies = []
        self.stopped = False

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)

    def stop_propagation(self):
        self.stopped = True


class FakeQuery:
    def __init__(self, user_id):
        self.from_user = SimpleNamespace(id=user_id, username=None)
        self.answers = []
        self.stopped = False

    async def answer(self, text, **kwargs):
        self.answers.append(text)

    def stop_propagation(self):
        self.stopped = True


# --------------------------------------------------------------------------
# The message blocker
# --------------------------------------------------------------------------
def test_private_normal_user_gets_notice_and_is_blocked(monkeypatch):
    monkeypatch.setattr(temp, "MAINTENANCE", True)
    msg = FakeMessage(NORMAL_ID, chat_type="private")
    asyncio.run(maint.maintenance_block(None, msg))
    assert msg.stopped is True
    assert len(msg.replies) == 1
    assert "Mᴀɪɴᴛᴇɴᴀɴᴄᴇ" in msg.replies[0]


def test_admin_bypasses_the_blocker(monkeypatch):
    monkeypatch.setattr(temp, "MAINTENANCE", True)
    msg = FakeMessage(ADMIN_ID, chat_type="private")
    asyncio.run(maint.maintenance_block(None, msg))
    assert msg.stopped is False        # update falls through to real handlers
    assert msg.replies == []


def test_group_silent_block_for_plain_text(monkeypatch):
    """Blocked in groups too, but no visible reply on every message (no spam)."""
    monkeypatch.setattr(temp, "MAINTENANCE", True)
    msg = FakeMessage(NORMAL_ID, chat_type="supergroup", text="war 2 2025")
    asyncio.run(maint.maintenance_block(None, msg))
    assert msg.stopped is True
    assert msg.replies == []


def test_group_commands_and_mentions_get_the_notice(monkeypatch):
    monkeypatch.setattr(temp, "MAINTENANCE", True)
    monkeypatch.setattr(temp, "U_NAME", "MinatoTestBot")
    for text in ("/start", "/maint on", "@MinatoTestBot war 2"):
        msg = FakeMessage(NORMAL_ID, chat_type="supergroup", text=text)
        asyncio.run(maint.maintenance_block(None, msg))
        assert msg.stopped is True
        assert len(msg.replies) == 1, f"no notice for {text!r}"


# --------------------------------------------------------------------------
# The callback blocker (inline buttons)
# --------------------------------------------------------------------------
def test_callback_blocked_for_normal_user_but_not_admin(monkeypatch):
    monkeypatch.setattr(temp, "MAINTENANCE", True)
    q = FakeQuery(NORMAL_ID)
    asyncio.run(maint.maintenance_callback(None, q))
    assert q.stopped is True and q.answers == [script.MAINTENANCE_ALERT_TXT]

    q_admin = FakeQuery(ADMIN_ID)
    asyncio.run(maint.maintenance_callback(None, q_admin))
    assert q_admin.stopped is False and q_admin.answers == []


# --------------------------------------------------------------------------
# The /maint admin command
# --------------------------------------------------------------------------
class FakeToggleMessage:
    def __init__(self, command):
        self.command = command
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


def test_toggle_on_then_off(monkeypatch, fresh_botcol):
    monkeypatch.setattr(temp, "MAINTENANCE", False)
    bot = FakeBot()

    async def run():
        on_msg = FakeToggleMessage(["maint", "on"])
        await maint.maintenance_toggle(bot, on_msg)
        assert temp.MAINTENANCE is True
        assert await db.get_maintenance_mode() is True
        assert any("ON" in text for _, text in bot.sent)      # LOG_CHANNEL heads-up

        off_msg = FakeToggleMessage(["maint", "off"])
        await maint.maintenance_toggle(bot, off_msg)
        assert temp.MAINTENANCE is False
        assert await db.get_maintenance_mode() is False
        assert off_msg.replies[-1] == script.MAINTENANCE_OFF_TXT

    asyncio.run(run())


def test_toggle_status_and_usage(monkeypatch, fresh_botcol):
    monkeypatch.setattr(temp, "MAINTENANCE", False)
    bot = FakeBot()

    async def run():
        status_msg = FakeToggleMessage(["maint", "status"])
        await maint.maintenance_toggle(bot, status_msg)
        assert "OFF" in status_msg.replies[-1]

        usage_msg = FakeToggleMessage(["maint"])
        await maint.maintenance_toggle(bot, usage_msg)
        assert "Usage" in usage_msg.replies[-1]
        assert temp.MAINTENANCE is False                       # unchanged

    asyncio.run(run())


def test_toggle_on_is_idempotent(monkeypatch, fresh_botcol):
    monkeypatch.setattr(temp, "MAINTENANCE", True)
    bot = FakeBot()

    async def run():
        msg = FakeToggleMessage(["maint", "on"])
        await maint.maintenance_toggle(bot, msg)
        assert temp.MAINTENANCE is True
        assert "already ON" in msg.replies[-1]
        assert bot.sent == []                                  # no duplicate log spam

    asyncio.run(run())
