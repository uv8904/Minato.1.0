"""Unit tests for the new search results UI and Send All premium restriction."""
import asyncio
from types import SimpleNamespace
from collections import defaultdict
from unittest.mock import AsyncMock
import pytest
from pyrogram import enums

from plugins.pmfilter import build_search_buttons, search_file_label
from database.users_chats_db import db
from utils import temp, get_size


class FakeFile:
    def __init__(self, file_id, file_name, file_size):
        self.file_id = file_id
        self.file_name = file_name
        self.file_size = file_size


@pytest.fixture(autouse=True)
def setup_temp(monkeypatch):
    monkeypatch.setattr(temp, "U_NAME", "TestBot", raising=False)
    monkeypatch.setattr(temp, "GETALL", {})
    monkeypatch.setattr(temp, "SHORT", {})
    monkeypatch.setattr(temp, "IMDB_CAP", {})


@pytest.mark.parametrize("button_mode", [True, False, None])
def test_search_buttons_non_premium_user(monkeypatch, button_mode):
    """Both actions and one button per file, even with legacy text settings."""
    async def mock_has_premium(user_id):
        return False

    monkeypatch.setattr(db, "has_premium_access", mock_has_premium)

    files = [
        FakeFile("f1", "@AKDRAMAHUB Flex x Cop DSNP WEB DL AAC", 1950000000),
        FakeFile("f2", "Flex x Cop DSNP x264 mkv", 728710000),
    ]

    btn = asyncio.run(
        build_search_buttons(
            key="123-456",
            files=files,
            offset=0,
            next_offset="",
            total_results=2,
            req_user_id=999,
            settings={"button": button_mode, "max_btn": True},
        )
    )

    # Row 0: Top row
    assert len(btn[0]) == 2
    assert btn[0][1].text == "Sᴇɴᴅ Aʟʟ"
    assert btn[0][1].callback_data == "sendfiles#123-456"
    assert btn[0][0].text == "⚡ Check Bot PM ⚡"
    assert btn[0][0].url == "https://t.me/TestBot"

    # Row 1: Filters
    assert len(btn[1]) == 3
    assert btn[1][0].text == "Quality"
    assert btn[1][1].text == "Language"
    assert btn[1][2].text == "Season"

    # Row 2 & 3: File buttons
    assert len(btn) == 4
    assert all(len(row) == 1 for row in btn[2:])
    assert [row[0].callback_data for row in btn[2:]] == ["file#f1", "file#f2"]
    assert "•" in btn[2][0].text
    assert "@AKDRAMAHUB Flex x Cop DSNP WEB DL AAC" in btn[2][0].text
    assert "•" in btn[3][0].text
    assert "Flex x Cop DSNP x264 mkv" in btn[3][0].text


def test_search_buttons_premium_user(monkeypatch):
    """Premium user: row 1 has both '⚡ Check Bot PM ⚡' and 'Send All'."""
    async def mock_has_premium(user_id):
        return True

    monkeypatch.setattr(db, "has_premium_access", mock_has_premium)

    files = [FakeFile("f1", "Flex x Cop E01", 500000000)]

    btn = asyncio.run(
        build_search_buttons(
            key="123-456",
            files=files,
            offset=0,
            next_offset="",
            total_results=1,
            req_user_id=777,
            settings={"button": True, "max_btn": True},
        )
    )

    # Row 0: Top row has both buttons
    assert len(btn[0]) == 2
    assert btn[0][0].text == "⚡ Check Bot PM ⚡"
    assert btn[0][1].text == "Sᴇɴᴅ Aʟʟ"
    assert btn[0][1].callback_data == "sendfiles#123-456"


def test_search_buttons_pagination_middle_page(monkeypatch):
    """Page 2 of 9: « BACK | 🗓 2/9 | NEXT »."""
    async def mock_has_premium(user_id):
        return False

    monkeypatch.setattr(db, "has_premium_access", mock_has_premium)

    files = [FakeFile(f"f{i}", f"Flex x Cop E{i:02d}", 500000000) for i in range(1, 11)]

    btn = asyncio.run(
        build_search_buttons(
            key="123-456",
            files=files,
            offset=10,        # Page 2 (offset 10 for 10 per page)
            next_offset=20,   # Next page offset
            total_results=85, # Total 85 -> 9 pages
            req_user_id=999,
            settings={"button": True, "max_btn": True},
        )
    )

    pagination_row = btn[-1]
    assert len(pagination_row) == 3
    assert pagination_row[0].text == "« BACK"
    assert pagination_row[0].callback_data == "next_999_123-456_0"

    assert pagination_row[1].text == "🗓 2/9"
    assert pagination_row[1].callback_data == "pages"

    assert pagination_row[2].text == "NEXT »"
    assert pagination_row[2].callback_data == "next_999_123-456_20"


def test_search_buttons_pagination_first_page(monkeypatch):
    """Page 1 of 9: « BACK (pages_first) | 🗓 1/9 | NEXT » (next offset)."""
    async def mock_has_premium(user_id):
        return False

    monkeypatch.setattr(db, "has_premium_access", mock_has_premium)

    files = [FakeFile(f"f{i}", f"Movie E{i:02d}", 500000000) for i in range(1, 11)]

    btn = asyncio.run(
        build_search_buttons(
            key="123-456",
            files=files,
            offset=0,
            next_offset=10,
            total_results=85,
            req_user_id=999,
            settings={"button": True, "max_btn": True},
        )
    )

    pagination_row = btn[-1]
    assert len(pagination_row) == 3
    assert pagination_row[0].text == "« BACK"
    assert pagination_row[0].callback_data == "pages_first"
    assert pagination_row[1].text == "🗓 1/9"
    assert pagination_row[2].text == "NEXT »"
    assert pagination_row[2].callback_data == "next_999_123-456_10"


def test_sendfiles_callback_non_premium_shows_alert(monkeypatch):
    """Clicking Send All when non-premium shows an alert."""
    from plugins.pmfilter import cb_handler

    async def mock_has_premium(user_id):
        return False

    monkeypatch.setattr(db, "has_premium_access", mock_has_premium)

    answered = []

    class FakeQuery:
        data = "sendfiles#123-456"
        from_user = SimpleNamespace(id=999, first_name="Akas")
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-1001, type="supergroup"),
            id=456,
            reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=999)),
        )

        async def answer(self, text="", show_alert=False, url=None):
            answered.append({"text": text, "show_alert": show_alert, "url": url})

    query = FakeQuery()
    asyncio.run(cb_handler(None, query))

    assert len(answered) == 1
    assert "only for Premium users" in answered[0]["text"]
    assert answered[0]["show_alert"] is True
    assert answered[0]["url"] is None


def test_sendfiles_callback_premium_allowed(monkeypatch):
    """Clicking Send All when premium generates deep link URL."""
    from plugins.pmfilter import cb_handler

    async def mock_has_premium(user_id):
        return True

    async def mock_get_settings(chat_id):
        return {}

    monkeypatch.setattr(db, "has_premium_access", mock_has_premium)
    from utils import get_settings
    monkeypatch.setattr("plugins.pmfilter.get_settings", mock_get_settings)

    answered = []

    class FakeQuery:
        data = "sendfiles#123-456"
        from_user = SimpleNamespace(id=777, first_name="VIP")
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-1001, type="supergroup"),
            id=456,
            reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=777)),
        )

        async def answer(self, text="", show_alert=False, url=None):
            answered.append({"text": text, "show_alert": show_alert, "url": url})

    query = FakeQuery()
    asyncio.run(cb_handler(None, query))
    assert answered == [{
        "text": "", "show_alert": False,
        "url": "https://telegram.me/TestBot?start=allfiles_-1001_123-456",
    }]

def test_allfiles_direct_start_non_premium_blocked(monkeypatch):
    """Directly opening /start allfiles_... blocks non-premium users."""
    import plugins.commands as cmds

    async def mock_has_premium(user_id):
        return False

    async def mock_is_user_exist(user_id):
        return True

    async def mock_is_subscribed(client, message):
        return True

    async def mock_is_req_subscribed(client, message):
        return True

    monkeypatch.setattr(cmds.db, "has_premium_access", mock_has_premium)
    monkeypatch.setattr(cmds.db, "is_user_exist", mock_is_user_exist)
    monkeypatch.setattr(cmds, "is_subscribed", mock_is_subscribed)
    monkeypatch.setattr(cmds, "is_req_subscribed", mock_is_req_subscribed)

    replies = []

    class FakeMsg:
        command = ["start", "allfiles_-1001_123-456"]
        from_user = SimpleNamespace(id=999, first_name="Akas", mention="Akas")
        chat = SimpleNamespace(id=999, type=enums.ChatType.PRIVATE)

        async def reply_text(self, text="", **kwargs):
            replies.append({"text": text, "kwargs": kwargs})

        async def react(self, **kwargs):
            return None

    msg = FakeMsg()
    asyncio.run(cmds.start(None, msg))

    assert len(replies) == 1
    assert "only for premium" in replies[0]["text"].lower() or "ᴘʀᴇᴍɪᴜᴍ" in replies[0]["text"]



@pytest.mark.parametrize("filename,tag,normalized", [
    ("Show.S01E02.1080p.mkv", "S01E02", "Show.S01E02.1080p.mkv"),
    ("Show_s2_e3.mkv", "S02E03", "Show_s2_e3.mkv"),
    ("Show s01 e12", "S01E12", "Show s01 e12"),
    ("Show E07.mkv", "E07", "Show E07.mkv"),
    ("Show Ep 8", "E08", "Show Ep 8"),
    ("Show Episode 123", "E123", "Show Episode 123"),
    ("  Movie  2026\n1080p.mkv ", "", "Movie 2026 1080p.mkv"),
    ("Movie HE1080p", "", "Movie HE1080p"),
    (None, "", "File"),
    ("   ", "", "File"),
])
def test_file_button_label(filename, tag, normalized):
    file = FakeFile("f1", filename, 500000000)
    expected = " • ".join(part for part in (get_size(file.file_size), tag, normalized) if part)
    assert search_file_label(file) == expected


def test_keyboard_does_not_lookup_premium(monkeypatch):
    premium = AsyncMock(side_effect=AssertionError("No DB lookup to render buttons"))
    monkeypatch.setattr(db, "has_premium_access", premium)
    buttons = asyncio.run(build_search_buttons("key", [], 0, "", 0, 0, {}))
    assert len(buttons[0]) == 2
    premium.assert_not_awaited()


@pytest.mark.parametrize("handler,data", [
    ("next_page", "next_777_123-456_10"),
    ("filter_qualities_cb_handler", "fq#1080p#123-456"),
    ("filter_languages_cb_handler", "fl#english#123-456"),
    ("filter_seasons_cb_handler", "fs#s01#123-456"),
])
def test_pagination_and_filters_keep_file_buttons(monkeypatch, handler, data):
    import plugins.pmfilter as pm

    files = [FakeFile("f1", "Show S01E02.mkv", 500000000)]
    monkeypatch.setattr(pm, "FRESH", {"123-456": "show"})
    monkeypatch.setattr(pm, "BUTTONS", {})
    monkeypatch.setattr(pm, "get_search_results", AsyncMock(return_value=(files, 20, 30)))
    monkeypatch.setattr(pm, "get_settings", AsyncMock(return_value={"button": False}))
    query = SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=777),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=-1001),
            reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=777)),
            edit_text=AsyncMock(side_effect=AssertionError("Text links must not return")),
        ),
        edit_message_reply_markup=AsyncMock(), answer=AsyncMock(),
    )
    asyncio.run(getattr(pm, handler)(None, query))
    rows = query.edit_message_reply_markup.call_args.kwargs["reply_markup"].inline_keyboard
    assert [button.text for button in rows[0]] == ["⚡ Check Bot PM ⚡", "Sᴇɴᴅ Aʟʟ"]
    assert rows[2][0].callback_data == "file#f1"
    assert " • S01E02 • " in rows[2][0].text
    query.message.edit_text.assert_not_awaited()
    assert temp.GETALL["123-456"] == files


@pytest.mark.parametrize("imdb_enabled", [False, True])
@pytest.mark.parametrize("button_mode", [False, True])
def test_initial_search_has_no_file_text_links(monkeypatch, imdb_enabled, button_mode):
    import plugins.pmfilter as pm

    files = [FakeFile("f1", "Show S01E02.mkv", 500000000)]
    settings = {"button": button_mode, "imdb": imdb_enabled, "template": "{title}", "auto_delete": False}
    monkeypatch.setattr(pm, "FRESH", {})
    monkeypatch.setattr(pm, "get_search_results", AsyncMock(return_value=(files, "", 1)))
    monkeypatch.setattr(pm, "get_settings", AsyncMock(return_value=settings))
    poster = defaultdict(str, title="Show", poster="https://example.com/poster.jpg")
    monkeypatch.setattr(pm, "get_poster", AsyncMock(return_value=poster))
    message = SimpleNamespace(
        text="Show", id=456, chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=777),
        reply_text=AsyncMock(), reply_photo=AsyncMock(),
    )
    asyncio.run(pm.auto_filter(None, message))
    result = message.reply_photo.call_args if imdb_enabled else message.reply_text.call_args
    caption = result.kwargs["caption" if imdb_enabled else "text"]
    assert "start=file_" not in caption
    assert "<a href" not in caption
    assert files[0].file_name not in caption
    rows = result.kwargs["reply_markup"].inline_keyboard
    assert len(rows[0]) == 2
    assert rows[2][0].callback_data == "file#f1"


def test_allfiles_premium_sends_every_cached_file_to_clicker_pm(monkeypatch):
    import plugins.commands as cmds

    files = [FakeFile(f"f{i}", f"Show S01E{i:02d}.mkv", 500000000) for i in range(1, 4)]
    for file in files:
        file.caption = None
    temp.GETALL["123-456"] = files
    monkeypatch.setattr(cmds.db, "has_premium_access", AsyncMock(return_value=True))
    monkeypatch.setattr(cmds.db, "is_user_exist", AsyncMock(return_value=True))
    monkeypatch.setattr(cmds, "get_settings", AsyncMock(return_value={"file_secure": True}))
    monkeypatch.setattr(cmds, "get_file_details", AsyncMock(side_effect=[[file] for file in files]))
    monkeypatch.setattr(cmds.asyncio, "sleep", AsyncMock())
    message = SimpleNamespace(
        command=["start", "allfiles_-1001_123-456"],
        from_user=SimpleNamespace(id=777), chat=SimpleNamespace(id=777, type=enums.ChatType.PRIVATE),
        react=AsyncMock(), reply_text=AsyncMock(),
    )
    client = SimpleNamespace(send_cached_media=AsyncMock(), send_message=AsyncMock())
    asyncio.run(cmds.start(client, message))
    sent = client.send_cached_media.call_args_list
    assert len(sent) == len(files)
    assert [call.kwargs["file_id"] for call in sent] == [file.file_id for file in files]
    assert all(call.kwargs["chat_id"] == 777 for call in sent)
    assert all(call.kwargs["protect_content"] is True for call in sent)
    message.reply_text.assert_not_awaited()


def test_button_mode_defaults_true(monkeypatch):
    import runpy

    monkeypatch.delenv("BUTTON_MODE", raising=False)
    # Do not let a developer's local .env affect this default test.
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    assert runpy.run_path("info.py")["BUTTON_MODE"] is True
