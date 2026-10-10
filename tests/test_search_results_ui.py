"""Unit tests for the search results UI, filters, sorting and Send All gate.

The keyboard keeps files as individual buttons, offers discoverable search
controls, and asks for confirmation before premium bulk-send actions.
"""
import asyncio
import inspect
from types import SimpleNamespace
import pytest
from pyrogram import enums

from plugins.pmfilter import build_search_buttons, search_file_label
from database.users_chats_db import db
from utils import get_size, temp


class FakeFile:
    def __init__(self, file_id, file_name, file_size):
        self.file_id = file_id
        self.file_name = file_name
        self.file_size = file_size


class FakeSentMessage:
    def __init__(self):
        self.deleted = False
        self.edit_texts = []

    async def delete(self):
        self.deleted = True

    async def edit_text(self, text, **kwargs):
        self.edit_texts.append(text)
        return self


class FakeChatMessage:
    """The bot's search-result message inside a group."""

    def __init__(self, chat_id=-1001, replies_to=999):
        self.chat = SimpleNamespace(id=chat_id, type=enums.ChatType.SUPERGROUP)
        self.id = 456
        self.reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=replies_to), text="jawan")
        self.replies = []

    async def reply_text(self, text="", **kwargs):
        self.replies.append({"text": text, "kwargs": kwargs})
        return FakeSentMessage()


class FakeCallbackQuery:
    def __init__(self, data, user_id=999, message=None):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, first_name="Akas", mention="Akas")
        self.message = message if message is not None else FakeChatMessage()
        self.answers = []
        self.keyboards = []
        self.edited_texts = []

    async def answer(self, text="", show_alert=False, url=None):
        self.answers.append({"text": text, "show_alert": show_alert, "url": url})

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        self.keyboards.append(reply_markup)

    async def edit_message_text(self, *args, **kwargs):
        self.edited_texts.append((args, kwargs))


@pytest.fixture(autouse=True)
def setup_temp(monkeypatch):
    monkeypatch.setattr(temp, "U_NAME", "TestBot", raising=False)
    monkeypatch.setattr(temp, "GETALL", {})
    monkeypatch.setattr(temp, "SHORT", {})
    monkeypatch.setattr(temp, "IMDB_CAP", {})


# --------------------------------------------------------------------------- #
# 1 + 2: buttons instead of links, top row always complete
# --------------------------------------------------------------------------- #

def test_top_row_always_has_check_bot_pm_and_send_all(monkeypatch):
    """Both shortcuts are always on the top row - no premium check needed."""
    async def boom(user_id):
        raise AssertionError("build_search_buttons must not query premium status")

    monkeypatch.setattr(db, "has_premium_access", boom)

    files = [FakeFile("f1", "Flex x Cop S01E03 1080p WEB-DL.mkv", 1950000000)]

    btn = asyncio.run(
        build_search_buttons(
            key="123-456",
            files=files,
            offset=0,
            next_offset="",
            total_results=1,
            req_user_id=999,
            settings={"button": True, "max_btn": True},
        )
    )

    top_row = btn[0]
    assert [b.text for b in top_row] == ["⚡ Check Bot PM ⚡", "Sᴇɴᴅ Aʟʟ"]
    assert top_row[0].url == "https://t.me/TestBot"
    assert top_row[1].callback_data == "confirmfiles#123-456"


def test_file_buttons_use_size_episode_filename_format(monkeypatch):
    """{size} • {SxxExx/Exx} • {filename} - one button per file."""
    monkeypatch.setattr(db, "has_premium_access", lambda *a, **k: _true())

    files = [
        FakeFile("f1", "Flex x Cop S01E03 1080p WEB-DL AAC.mkv", 1950000000),
        FakeFile("f2", "Show.E05.720p.mkv", 728710000),
        FakeFile("f3", "Jawan (2023) 1080p WEB-DL.mkv", 2147483648),
    ]

    btn = asyncio.run(
        build_search_buttons(
            key="123-456",
            files=files,
            offset=0,
            next_offset="",
            total_results=3,
            req_user_id=999,
            settings={"button": True, "max_btn": True},
        )
    )

    # Row 0: shortcuts, row 1: existing filters, row 2: client help tools.
    assert len(btn) == 6
    assert [b.text for b in btn[1]] == ["Quality", "Language", "Season"]
    assert [b.text for b in btn[2]] == ["🎧 Audio/Subs", "↕ Sort: Latest", "📄 Details"]

    first, second, third = (row[0] for row in btn[3:])
    assert first.text == f"{get_size(1950000000)} • S01E03 • Flex x Cop S01E03 1080p WEB-DL AAC.mkv"
    assert second.text == f"{get_size(728710000)} • E05 • Show.E05.720p.mkv"
    # Movies have no season/episode part at all
    assert third.text == f"{get_size(2147483648)} • Jawan (2023) 1080p WEB-DL.mkv"

    assert [b.callback_data for b in (first, second, third)] == ["file#f1", "file#f2", "file#f3"]


def test_file_buttons_are_built_even_for_legacy_text_mode(monkeypatch):
    """Groups with the old 'text' result-page setting still get buttons."""
    monkeypatch.setattr(db, "has_premium_access", lambda *a, **k: _true())

    files = [FakeFile("f1", "Movie 2023 S01E01.mkv", 1024)]

    btn = asyncio.run(
        build_search_buttons(
            key="123-456",
            files=files,
            offset=0,
            next_offset="",
            total_results=1,
            req_user_id=0,
            settings={"button": False, "max_btn": True},
        )
    )

    assert [b.text for b in btn[0]] == ["⚡ Check Bot PM ⚡", "Sᴇɴᴅ Aʟʟ"]
    assert btn[3][0].callback_data == "file#f1"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Flex x Cop S01E03 1080p.mkv", "1.00 GB • S01E03 • Flex x Cop S01E03 1080p.mkv"),
        ("Show E05 720p.mkv", "1.00 GB • E05 • Show E05 720p.mkv"),
        ("Show S01 Complete.mkv", "1.00 GB • S01 • Show S01 Complete.mkv"),
        ("Jawan (2023).mkv", "1.00 GB • Jawan (2023).mkv"),
    ],
)
def test_search_file_label(name, expected):
    """``search_file_label`` stays the public per-file label helper."""
    assert search_file_label(FakeFile("f1", name, 1073741824)) == expected


def test_file_button_label_never_exceeds_telegram_limit(monkeypatch):
    """A 64 character label cap keeps Telegram from rejecting the keyboard."""
    monkeypatch.setattr(db, "has_premium_access", lambda *a, **k: _true())

    long_name = "Flex x Cop DSNP WEB DL AAC x264 DDP5 1 ESub [@Team] - E07 1080p.mkv"
    files = [FakeFile("f1", long_name, 1950000000)]

    btn = asyncio.run(
        build_search_buttons(
            key="123-456",
            files=files,
            offset=0,
            next_offset="",
            total_results=1,
            req_user_id=999,
            settings={"button": True, "max_btn": True},
        )
    )

    label = btn[3][0].text
    assert len(label) <= 64
    assert label.startswith(f"{get_size(1950000000)} • E07 • ")
    assert label.endswith("…")


def test_search_buttons_pagination_middle_page(monkeypatch):
    """Page 2 of 9: « BACK | 🗓 2/9 | NEXT »."""
    monkeypatch.setattr(db, "has_premium_access", lambda *a, **k: _true())

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
    monkeypatch.setattr(db, "has_premium_access", lambda *a, **k: _true())

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


async def _true(*args, **kwargs):
    return True


# --------------------------------------------------------------------------- #
# 1: the message text itself never carries file links
# --------------------------------------------------------------------------- #

def test_auto_filter_caption_has_no_file_links(monkeypatch):
    """The result message shows buttons only - no <a href> file list."""
    import plugins.pmfilter as pmf

    files = [
        FakeFile("f1", "Jawan 2023 S01E01 1080p.mkv", 1950000000),
        FakeFile("f2", "Jawan 2023 S01E02 1080p.mkv", 1950000000),
    ]

    async def fake_search(chat_id, query, offset=0, filter=True):
        return files, "", len(files)

    async def fake_settings(chat_id):
        # `button: False` is the legacy "text mode" - files must still be buttons
        return {
            "button": False,
            "imdb": False,
            "auto_delete": False,
            "spell_check": False,
            "max_btn": True,
            "template": None,
        }

    monkeypatch.setattr(pmf, "get_search_results", fake_search)
    monkeypatch.setattr(pmf, "get_settings", fake_settings)

    message = SimpleNamespace(
        id=456,
        text="jawan",
        chat=SimpleNamespace(id=-1001, type=enums.ChatType.SUPERGROUP),
        from_user=SimpleNamespace(id=999, first_name="Akas"),
        replies=[],
    )

    async def reply_text(text="", **kwargs):
        message.replies.append({"text": text, "kwargs": kwargs})
        return FakeSentMessage()

    message.reply_text = reply_text

    asyncio.run(pmf.auto_filter(None, message))

    caption = message.replies[-1]["text"]
    assert "start=file_" not in caption
    assert "<a href" not in caption
    assert "telegram.me" not in caption

    markup = message.replies[-1]["kwargs"]["reply_markup"].inline_keyboard
    assert [b.text for b in markup[0]] == ["⚡ Check Bot PM ⚡", "Sᴇɴᴅ Aʟʟ"]
    assert [row[0].callback_data for row in markup[3:]] == ["file#f1", "file#f2"]


def test_next_page_only_swaps_the_keyboard(monkeypatch):
    """Paging edits the keyboard, it never rewrites the caption as text."""
    import plugins.pmfilter as pmf

    key = "123-456"
    pmf.FRESH[key] = "jawan"
    files = [FakeFile("f1", "Jawan S01E01.mkv", 1024)]

    async def fake_search(chat_id, query, offset=0, filter=True, sort_by="newest"):
        assert sort_by == "newest"
        return files, 10, 20

    async def fake_settings(chat_id):
        return {"button": False, "max_btn": True}

    monkeypatch.setattr(pmf, "get_search_results", fake_search)
    monkeypatch.setattr(pmf, "get_settings", fake_settings)

    query = FakeCallbackQuery(data=f"next_999_{key}_10")
    asyncio.run(pmf.next_page(None, query))

    assert query.edited_texts == []
    assert len(query.keyboards) == 1
    assert [b.text for b in query.keyboards[0].inline_keyboard[0]] == ["⚡ Check Bot PM ⚡", "Sᴇɴᴅ Aʟʟ"]


def test_audio_subtitle_filter_menu_shows_useful_presets(monkeypatch):
    import plugins.pmfilter as pmf

    key = "123-456"
    pmf.FRESH[key] = "take charge of my heart"
    query = FakeCallbackQuery(data=f"audiosubs#{key}")
    asyncio.run(pmf.audiosubs_cb_handler(None, query))

    labels = [row[0].text for row in query.keyboards[0].inline_keyboard]
    assert "🌐 Dual/Multi Audio" in labels
    assert "🇮🇳 Hindi Dub" in labels
    assert "📝 English Subs" in labels
    assert "📂 All audio/subtitle types" in labels


def test_audio_subtitle_filter_applies_filename_tag_variants(monkeypatch):
    import plugins.pmfilter as pmf

    key = "123-456"
    pmf.FRESH[key] = "take charge of my heart"
    pmf.SORTS[key] = "newest"
    files = [FakeFile("f1", "Take Charge of My Heart Dual Audio ESub.mkv", 500)]
    seen = {}

    async def fake_search(chat_id, query, offset=0, filter=True, sort_by="newest"):
        seen["query"] = query
        seen["sort_by"] = sort_by
        return files, "", 1

    async def fake_settings(chat_id):
        return {"max_btn": True}

    monkeypatch.setattr(pmf, "get_search_results", fake_search)
    monkeypatch.setattr(pmf, "get_settings", fake_settings)
    query = FakeCallbackQuery(data=f"faudio#dual#{key}")
    asyncio.run(pmf.filter_audiosubs_cb_handler(None, query))

    assert seen["query"] == [
        "take charge of my heart dual audio",
        "take charge of my heart multi audio",
    ]
    assert seen["sort_by"] == "newest"
    assert pmf.BUTTONS[key] == seen["query"]


def test_sort_choice_is_applied_and_shown_on_results(monkeypatch):
    import plugins.pmfilter as pmf

    key = "123-456"
    pmf.FRESH[key] = "jawan"
    pmf.BUTTONS[key] = "jawan 1080p"
    pmf.SORTS[key] = "newest"
    files = [FakeFile("f1", "Jawan 1080p.mkv", 500)]
    seen = {}

    async def fake_search(chat_id, query, offset=0, filter=True, sort_by="newest"):
        seen.update(query=query, sort_by=sort_by)
        return files, "", 1

    async def fake_settings(chat_id):
        return {"max_btn": True}

    monkeypatch.setattr(pmf, "get_search_results", fake_search)
    monkeypatch.setattr(pmf, "get_settings", fake_settings)
    query = FakeCallbackQuery(data=f"sortby#smallest#{key}")
    asyncio.run(pmf.sort_results_cb_handler(None, query))

    assert seen == {"query": "jawan 1080p", "sort_by": "smallest"}
    assert pmf.SORTS[key] == "smallest"
    assert query.keyboards[0].inline_keyboard[2][1].text == "↕ Sort: Smallest"


def test_search_details_summarize_page_and_explain_file_buttons(monkeypatch):
    import plugins.pmfilter as pmf

    key = "123-456"
    temp.GETALL[key] = [FakeFile("f1", "Show S01E03 1080p.mkv", 1024)]
    pmf.RESULT_META[key] = {
        "total": 12, "page": 2, "pages": 2, "showing": 1, "sort_by": "smallest"
    }
    query = FakeCallbackQuery(data=f"searchdetails#{key}")
    asyncio.run(pmf.search_details_cb_handler(None, query))

    details = query.answers[0]["text"]
    assert query.answers[0]["show_alert"] is True
    assert "12 result(s)" in details
    assert "page 2/2" in details
    assert "1080P" in details
    assert "Tap a file button" in details


# --------------------------------------------------------------------------- #
# 3: Send All - confirmation first, premium only may complete it
# --------------------------------------------------------------------------- #

def test_confirmfiles_prompts_before_bulk_send(monkeypatch):
    from plugins.pmfilter import cb_handler

    async def mock_has_premium(user_id):
        return True

    monkeypatch.setattr(db, "has_premium_access", mock_has_premium)
    query = FakeCallbackQuery(data="confirmfiles#123-456", user_id=999)
    asyncio.run(cb_handler(None, query))

    assert len(query.message.replies) == 1
    prompt = query.message.replies[0]
    assert "This may send several files" in prompt["text"]
    buttons = prompt["kwargs"]["reply_markup"].inline_keyboard
    assert buttons[0][0].callback_data == "sendfiles#123-456#999"
    assert buttons[1][0].callback_data == "cancelsendall#999"


def test_send_all_confirmation_cannot_be_approved_by_another_user(monkeypatch):
    from plugins.pmfilter import cb_handler

    async def should_not_check_premium(user_id):
        raise AssertionError("a different user must not approve this confirmation")

    monkeypatch.setattr(db, "has_premium_access", should_not_check_premium)
    query = FakeCallbackQuery(data="sendfiles#123-456#999", user_id=777)
    asyncio.run(cb_handler(None, query))

    assert query.answers[0]["show_alert"] is True
    assert query.message.replies == []


def test_sendfiles_callback_non_premium_prompts_premium_purchase(monkeypatch):
    """Tapping Send All as a non-premium user: premium alert + buy button."""
    from plugins.pmfilter import cb_handler
    from Script import script

    async def mock_has_premium(user_id):
        return False

    monkeypatch.setattr(db, "has_premium_access", mock_has_premium)

    query = FakeCallbackQuery(data="sendfiles#123-456")
    asyncio.run(cb_handler(None, query))

    # 1) the alert
    assert len(query.answers) == 1
    assert query.answers[0]["show_alert"] is True
    assert query.answers[0]["url"] is None
    assert "Premium" in query.answers[0]["text"]
    assert "buy premium" in query.answers[0]["text"].lower()
    assert len(query.answers[0]["text"]) <= 200  # Telegram alert limit

    # 2) the tappable premium offer
    assert len(query.message.replies) == 1
    offer = query.message.replies[0]
    assert script.SEND_ALL_PREMIUM_TEXT.splitlines()[0] in offer["text"]
    buttons = offer["kwargs"]["reply_markup"].inline_keyboard
    assert buttons[0][0].url == "https://t.me/TestBot?start=premium"


def test_sendfiles_callback_premium_gets_all_files_deeplink(monkeypatch):
    """Premium users are redirected to the allfiles deep link."""
    from plugins.pmfilter import cb_handler

    async def mock_has_premium(user_id):
        return True

    async def mock_get_settings(chat_id):
        return {}

    monkeypatch.setattr(db, "has_premium_access", mock_has_premium)
    monkeypatch.setattr("plugins.pmfilter.get_settings", mock_get_settings)

    query = FakeCallbackQuery(data="sendfiles#123-456", user_id=777)
    asyncio.run(cb_handler(None, query))

    assert query.answers == [{
        "text": "", "show_alert": False,
        "url": "https://telegram.me/TestBot?start=allfiles_-1001_123-456",
    }]
    assert query.message.replies == []


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


def test_allfiles_start_premium_sends_every_file_to_pm(monkeypatch):
    """Premium user: all files of the search are delivered in PM."""
    import plugins.commands as cmds

    async def mock_has_premium(user_id):
        return True

    async def mock_is_user_exist(user_id):
        return True

    async def mock_get_settings(chat_id):
        return {"file_secure": False, "caption": None}

    async def mock_get_file_details(file_id):
        return [SimpleNamespace(file_id=file_id, file_name=f"{file_id}.mkv", file_size=1024, caption=None)]

    monkeypatch.setattr(cmds.db, "has_premium_access", mock_has_premium)
    monkeypatch.setattr(cmds.db, "is_user_exist", mock_is_user_exist)
    monkeypatch.setattr(cmds, "get_settings", mock_get_settings)
    monkeypatch.setattr(cmds, "get_file_details", mock_get_file_details)
    monkeypatch.setattr(cmds, "DELETE_TIME", 0)

    files = [SimpleNamespace(file_id="f1"), SimpleNamespace(file_id="f2")]
    monkeypatch.setitem(temp.GETALL, "123-456", files)

    class FakeClient:
        def __init__(self):
            self.media = []

        async def send_cached_media(self, chat_id, file_id, **kwargs):
            self.media.append({"chat_id": chat_id, "file_id": file_id})
            return FakeSentMessage()

        async def send_message(self, chat_id, text, **kwargs):
            return FakeSentMessage()

    class FakeMsg:
        command = ["start", "allfiles_-1001_123-456"]
        from_user = SimpleNamespace(id=777, first_name="VIP", mention="VIP")
        chat = SimpleNamespace(id=777, type=enums.ChatType.PRIVATE)

        async def reply_text(self, text="", **kwargs):
            return FakeSentMessage()

        async def react(self, **kwargs):
            return None

    client = FakeClient()
    asyncio.run(cmds.start(client, FakeMsg()))

    # Every file of the search, each in the user's PM (never in the group)
    assert [item["file_id"] for item in client.media] == ["f1", "f2"]
    assert all(item["chat_id"] == 777 for item in client.media)


# --------------------------------------------------------------------------- #
# 4: BUTTON_MODE default
# --------------------------------------------------------------------------- #

def test_button_mode_defaults_to_true():
    import info
    from database import users_chats_db

    assert info.BUTTON_MODE is True
    assert """environ.get('BUTTON_MODE', "True")""" in inspect.getsource(info)

    # New groups/chats inherit button mode as well
    settings_source = inspect.getsource(type(users_chats_db.db).get_settings)
    assert "'button': BUTTON_MODE" in settings_source
