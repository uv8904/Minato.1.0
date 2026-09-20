"""
Tests for the "Notify me when uploaded" feature.

Run with the project dependencies installed::

    pytest tests/test_title_notify.py -q

The database tests swap ``db.notify_req`` for a mongomock-motor collection
(a real, in-memory MongoDB implementation), so the code under test is the
production code path - only the server behind it is fake.
"""
import asyncio
from types import SimpleNamespace

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

import info
from database.users_chats_db import db
from dreamxbotz.util import title_notify
from dreamxbotz.util.title_notify import (
    check_new_file,
    match,
    normalize,
    notify_keyboard,
    notify_rows,
    register_notify_request,
)
from pyrogram.errors import UserIsBlocked


# --------------------------------------------------------------------------
# Title matching (pure functions)
# --------------------------------------------------------------------------
MATCH_CASES = [
    # (file name as stored by save_file, search text, expected)
    ("Pushpa 2 The Rule 2024 1080p WEB DL Hindi AAC x264 mkv", "pushpa 2", True),
    ("Pushpa 2 The Rule 2024 1080p WEB DL mkv", "pushpa 2 the rule 2024", True),
    ("Devara Part 1 2024 720p HDRip mkv", "pushpa 2", False),
    ("Loki S01 E04 720p WEB DL mkv", "Loki S01E04", True),
    ("Kanguva 2024 Tamil 1080p mkv", "kanguva 2024 1080p", True),
    ("Pushpa Two The Rule 2024 mkv", "pushpa 2 the rule", True),
    ("Devara Part One 2024 mkv", "devara part 1", True),
    ("Pushpa 2 The Rule 2024 mkv", "pushpa 2 the rule 2025", False),
    ("Pushpa 2 The Rule mkv", "pushpa 2 the rule 2024", True),
    ("Vettaiyan 2024 mkv", "arm", False),
    ("Sikandar 2025 1080p mkv", "sikandar", True),
    ("Sikandar 2025 1080p mkv", "sikandar 2024", False),
]


@pytest.mark.parametrize("file_name,query,expected", MATCH_CASES)
def test_match(file_name, query, expected):
    assert match(file_name, query) is expected


def test_match_handles_empty_input():
    assert match("", "pushpa") is False
    assert match("pushpa.mkv", "") is False
    assert match(None, None) is False


def test_normalize_drops_noise_and_canonicalizes():
    assert normalize("Pushpa 2_The Rule (2024) 1080p WEB-DL x264.mkv") == "pushpa 2 the rule 2024"
    assert normalize("Loki.S01E04.720p.mkv") == "loki s01 e04"
    assert normalize("") == ""
    assert normalize(None) == ""


# --------------------------------------------------------------------------
# Keyboard: colours, callback payload, Google link
# --------------------------------------------------------------------------
def test_notify_keyboard_layout_and_colours():
    markup = notify_keyboard("-100123-456", 777, "pushpa 2 the rule")
    rows = markup.inline_keyboard

    assert len(rows) == 3
    notify_btn, request_btn, google_btn, close_btn = rows[0][0], rows[1][0], rows[1][1], rows[2][0]

    # electrogram stores the colour as flags (bg_success/bg_primary/bg_danger),
    # not as the ButtonStyle enum that was passed in.
    assert notify_btn.style.bg_success is True              # green
    assert request_btn.style.bg_primary is True             # blue
    assert google_btn.style.bg_primary is True              # blue
    assert close_btn.style.bg_danger is True                # red

    assert notify_btn.callback_data == "notify#-100123-456#777"
    assert len(notify_btn.callback_data.encode()) <= 64     # Telegram hard limit
    assert request_btn.url == info.OWNER_LNK
    assert "google.com/search?q=pushpa+2+the+rule" in google_btn.url
    assert close_btn.callback_data == "close_data"


def test_notify_rows_has_no_close_row():
    rows = notify_rows("-100123-456", 777, "pushpa")
    assert len(rows) == 2
    assert all(btn.callback_data != "close_data" for row in rows for btn in row)


# --------------------------------------------------------------------------
# Database round trip + PM delivery
# --------------------------------------------------------------------------
class StubBot:
    """Just enough of a Client for check_new_file()."""

    def __init__(self, blocked=()):
        self.me = SimpleNamespace(username="minato_bot")
        self.sent = []
        self.blocked = set(blocked)

    async def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.blocked:
            raise UserIsBlocked("user blocked the bot")
        self.sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(id=1)


@pytest.fixture
def notify_col(monkeypatch):
    client = mongomock_motor.AsyncMongoMockClient()
    col = client["dreamx_test"]["notify_requests"]
    monkeypatch.setattr(db, "notify_req", col)
    monkeypatch.setattr(title_notify, "TITLE_NOTIFY", True)
    return col


def test_register_and_notify_on_index(notify_col):
    async def scenario():
        saved, reason = await register_notify_request(
            user_id=777, search_text="pushpa 2", chat_id=-100123, message_id=456, name="Yuvi"
        )
        assert (saved, reason) == (True, "saved")
        assert await notify_col.count_documents({}) == 1

        # Same user + same title is not stored twice.
        assert await register_notify_request(777, "pushpa 2") == (False, "duplicate")
        assert await notify_col.count_documents({}) == 1

        bot = StubBot()
        # Unrelated upload: nobody is PM'd, the request stays pending.
        assert await check_new_file(bot, "Devara Part 1 2024 720p HDRip mkv") == 0
        assert bot.sent == []
        assert await notify_col.count_documents({}) == 1

        # Matching upload: the user is PM'd and the request is consumed.
        assert await check_new_file(bot, "Pushpa 2 The Rule 2024 1080p WEB DL mkv") == 1
        assert len(bot.sent) == 1
        chat_id, text, reply_markup = bot.sent[0]
        assert chat_id == 777
        assert "Pushpa 2 The Rule 2024 1080p WEB DL mkv" in text
        assert reply_markup.inline_keyboard[0][0].url == "https://t.me/minato_bot"
        assert await notify_col.count_documents({}) == 0

    asyncio.run(scenario())


def test_unreachable_user_request_is_dropped(notify_col):
    async def scenario():
        await register_notify_request(777, "kanguva")
        bot = StubBot(blocked={777})
        assert await check_new_file(bot, "Kanguva 2024 Tamil 1080p mkv") == 0
        assert bot.sent == []
        assert await notify_col.count_documents({}) == 0

    asyncio.run(scenario())


def test_requests_are_capped_per_user(notify_col):
    async def scenario():
        for i in range(info.TITLE_NOTIFY_MAX_PER_USER + 2):
            await register_notify_request(777, f"title number {i}")
        assert await notify_col.count_documents({}) == info.TITLE_NOTIFY_MAX_PER_USER

    asyncio.run(scenario())


def test_ttl_and_purge(notify_col):
    async def scenario():
        await register_notify_request(777, "old movie")
        fresh = await db.get_notify_requests()
        assert len(fresh) == 1

        # Nothing is returned once the cutoff is in the future.
        from datetime import datetime, timedelta

        assert await db.get_notify_requests(cutoff=datetime.utcnow() + timedelta(days=1)) == []
        assert await db.purge_old_notify_requests(days=-1) == 1
        assert await notify_col.count_documents({}) == 0

    asyncio.run(scenario())


def test_notify_disabled_by_config(notify_col, monkeypatch):
    async def scenario():
        monkeypatch.setattr(title_notify, "TITLE_NOTIFY", False)
        assert await register_notify_request(777, "pushpa 2") == (False, "disabled")
        assert await notify_col.count_documents({}) == 0
        assert await check_new_file(StubBot(), "Pushpa 2 The Rule 2024 mkv") == 0

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------
def test_notify_handler_is_registered_before_the_catch_all():
    """plugins are loaded in module order, so ^notify# must come first."""
    import plugins.pmfilter as pmf

    order = [n for n, f in vars(pmf).items() if callable(f) and getattr(f, "handlers", None)]
    assert "notify_me_cb" in order and "cb_handler" in order
    assert order.index("notify_me_cb") < order.index("cb_handler")


def test_channel_indexes_trigger_the_notify_check():
    import inspect

    import plugins.channel as ch

    assert ch.check_new_file is check_new_file
    assert "check_new_file" in inspect.getsource(ch.media_handler)


def test_secrets_are_not_emptied():
    """Koyeb crashes on empty credentials - keep the defaults filled in."""
    assert info.API_ID and info.API_ID > 0
    assert info.API_HASH
    assert info.DATABASE_URI.startswith("mongodb")
    assert info.DATABASE_URI2.startswith("mongodb")
    assert info.SHORTENER_API
    assert info.SHORTENER_API2
    assert info.SHORTENER_API3
