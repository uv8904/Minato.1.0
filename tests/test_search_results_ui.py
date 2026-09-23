"""Unit tests for the new search results UI and Send All premium restriction."""
import asyncio
from types import SimpleNamespace
import pytest
from pyrogram import enums

from plugins.pmfilter import build_search_buttons
from database.users_chats_db import db
from utils import temp


class FakeFile:
    def __init__(self, file_id, file_name, file_size):
        self.file_id = file_id
        self.file_name = file_name
        self.file_size = file_size


@pytest.fixture(autouse=True)
def setup_temp():
    temp.U_NAME = "TestBot"


def test_search_buttons_non_premium_user(monkeypatch):
    """Non-premium user: row 1 is only '⚡ Check Bot PM ⚡', no Send All."""
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
            settings={"button": True, "max_btn": True},
        )
    )

    # Row 0: Top row
    assert len(btn[0]) == 1
    assert btn[0][0].text == "⚡ Check Bot PM ⚡"
    assert btn[0][0].url == "https://t.me/TestBot"

    # Row 1: Filters
    assert len(btn[1]) == 3
    assert btn[1][0].text == "Quality"
    assert btn[1][1].text == "Language"
    assert btn[1][2].text == "Season"

    # Row 2 & 3: File buttons
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


