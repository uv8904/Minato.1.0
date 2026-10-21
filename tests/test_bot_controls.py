"""Bot controls changed from Telegram: Send All gate + global force-sub.

Everything here is a *runtime* setting stored in the ``bot_settings`` Mongo
collection, so admins no longer need to edit ``info.py``/``AUTH_CHANNELS`` or
the Send All premium checks and redeploy.

Covers:

* ``database/users_chats_db.py``      – runtime setting get/set/unset
* ``dreamxbotz/util/bot_controls.py`` – gate + force-sub helpers and parsing
* ``plugins/bot_controls.py``         – panels, commands and buttons
* ``plugins/pmfilter.py``             – Send All gate integration

Run with the project dependencies installed::

    pytest tests/test_bot_controls.py -q
"""
import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("pyrogram")
mongomock_motor = pytest.importorskip("mongomock_motor")

import info  # noqa: E402
from database.users_chats_db import db  # noqa: E402
from dreamxbotz.util import bot_controls as controls  # noqa: E402
from plugins import bot_controls as botctl  # noqa: E402
from utils import temp  # noqa: E402
from pyrogram import enums  # noqa: E402

ADMIN_ID = int(info.ADMINS[0])
NORMAL_ID = 999000111


# --------------------------------------------------------------------------- #
# Fixtures / fakes
# --------------------------------------------------------------------------- #
@pytest.fixture
def fresh_botcol():
    client = mongomock_motor.AsyncMongoMockClient()
    original = db.botcol
    db.botcol = client["test"]["bot_settings"]
    controls.invalidate_runtime_cache()
    botctl._PENDING_ADD.clear()
    botctl._TITLE_CACHE.clear()
    botctl._PANEL_MSG.clear()
    yield db.botcol
    db.botcol = original
    controls.invalidate_runtime_cache()
    botctl._PENDING_ADD.clear()
    botctl._TITLE_CACHE.clear()
    botctl._PANEL_MSG.clear()


class FakeSentMessage:
    _next_id = 1000

    def __init__(self, chat_id=ADMIN_ID):
        self.deleted = False
        self.chat = SimpleNamespace(id=chat_id)
        FakeSentMessage._next_id += 1
        self.id = FakeSentMessage._next_id
        self.edits = []

    async def delete(self):
        self.deleted = True

    async def edit_text(self, text="", **kwargs):
        self.edits.append({"text": text, "kwargs": kwargs})
        return self


class FakeMessage:
    def __init__(self, text="", command=None, user_id=ADMIN_ID):
        self.text = text
        self.command = command or []
        self.from_user = SimpleNamespace(
            id=user_id, mention="Admin", first_name="Admin"
        )
        self.chat = SimpleNamespace(
            id=user_id, type=enums.ChatType.PRIVATE, title="Private"
        )
        self.replies = []
        self.edits = []
        self.stopped = False

    async def reply_text(self, text="", **kwargs):
        self.replies.append({"text": text, "kwargs": kwargs})
        return FakeSentMessage(chat_id=self.chat.id)

    async def edit_text(self, text="", **kwargs):
        self.edits.append({"text": text, "kwargs": kwargs})
        return FakeSentMessage()

    async def delete(self):
        self.deleted = True

    def stop_propagation(self):
        self.stopped = True


class FakeClient:
    def __init__(self, titles=None):
        self.titles = titles or {}
        self.sent = []
        self.edited_messages = []

    async def edit_message_text(self, chat_id, message_id, text="", **kwargs):
        self.edited_messages.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text, "kwargs": kwargs}
        )
        return SimpleNamespace(id=message_id, chat=SimpleNamespace(id=chat_id))

    async def get_chat(self, chat_id):
        if chat_id in self.titles:
            value = self.titles[chat_id]
            if hasattr(value, "id"):            # already a chat-like object
                return value
            return SimpleNamespace(
                id=chat_id,
                type=enums.ChatType.CHANNEL,
                title=value,
            )
        raise RuntimeError("chat not found")

    async def get_me(self):
        return SimpleNamespace(id=1, username="TestBot")

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status=enums.ChatMemberStatus.ADMINISTRATOR)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text})
        return FakeSentMessage()


class FakeCallbackQuery:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, mention="Admin", first_name="Admin")
        self.message = FakeMessage(user_id=user_id)
        self.answers = []
        self.stopped = False

    async def answer(self, text="", show_alert=False, **kwargs):
        self.answers.append({"text": text, "show_alert": show_alert})

    def stop_propagation(self):
        self.stopped = True


# --------------------------------------------------------------------------- #
# Mongo persistence
# --------------------------------------------------------------------------- #
def test_runtime_setting_roundtrip_and_unset(fresh_botcol):
    async def run():
        assert await db.get_runtime_setting("demo", "fallback") == "fallback"
        await db.set_runtime_setting("demo", 123)
        assert await db.get_runtime_setting("demo") == 123
        await db.unset_runtime_setting("demo")
        assert await db.get_runtime_setting("demo", "fallback") == "fallback"

    asyncio.run(run())


def test_send_all_gate_defaults_on_and_persists(fresh_botcol):
    async def run():
        # Historical default: premium only.
        assert await controls.send_all_requires_premium() is True

        assert await controls.set_send_all_requires_premium(False) is False
        assert await controls.send_all_requires_premium() is False

        controls.invalidate_runtime_cache()          # simulate another process
        assert await controls.send_all_requires_premium() is False

        assert await controls.set_send_all_requires_premium(True) is True
        controls.invalidate_runtime_cache()
        assert await controls.send_all_requires_premium() is True

    asyncio.run(run())


def test_send_all_allowed_follows_gate_and_premium(monkeypatch, fresh_botcol):
    async def no_premium(user_id):
        return False

    async def yes_premium(user_id):
        return True

    async def run():
        monkeypatch.setattr(db, "has_premium_access", no_premium)
        # Gate ON -> non-premium cannot use Send All.
        assert await controls.send_all_allowed(NORMAL_ID) is False

        # Gate OFF -> everyone can.
        await controls.set_send_all_requires_premium(False)
        assert await controls.send_all_allowed(NORMAL_ID) is True

        # Gate ON + premium -> allowed.
        await controls.set_send_all_requires_premium(True)
        monkeypatch.setattr(db, "has_premium_access", yes_premium)
        assert await controls.send_all_allowed(NORMAL_ID) is True

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Force-sub helpers
# --------------------------------------------------------------------------- #
def test_fsub_defaults_to_env_then_bot_managed_then_reset(fresh_botcol):
    async def run():
        assert await controls.get_custom_fsub_channels() is None

        fallback = await controls.effective_fsub_channels(settings={"fsub": [-100111]})
        assert -100111 in fallback
        assert all(channel in fallback for channel in info.AUTH_CHANNELS)

        await controls.save_custom_fsub_channels([-100222])
        assert await controls.get_custom_fsub_channels() == [-100222]
        # The bot-managed list replaces env + group settings entirely.
        assert await controls.effective_fsub_channels(settings={"fsub": [-100111]}) == [-100222]

        await controls.reset_custom_fsub_channels()
        assert await controls.get_custom_fsub_channels() is None
        assert -100111 in await controls.effective_fsub_channels(settings={"fsub": [-100111]})

    asyncio.run(run())


def test_first_edit_seeds_env_list_and_last_remove_disables_fsub(fresh_botcol):
    async def run():
        seed = await controls.effective_fsub_channels(settings={})
        assert seed == list(dict.fromkeys(info.AUTH_CHANNELS))
        assert seed, "test expects at least one default AUTH_CHANNELS entry"

        saved = await controls.remove_custom_fsub_channel(seed[0], seed_from=seed)
        assert saved == seed[1:]

        # Removing the remaining channels one by one ends with an explicit []:
        # force-sub is disabled, not silently reset to env.
        for channel_id in list(saved):
            saved = await controls.remove_custom_fsub_channel(channel_id, seed_from=seed)
        assert saved == []
        assert await controls.get_custom_fsub_channels() == []
        assert await controls.effective_fsub_channels(settings={}) == []

    asyncio.run(run())


def test_add_channels_deduplicates_and_seeds(fresh_botcol):
    async def run():
        seed = await controls.effective_fsub_channels(settings={})
        saved = await controls.add_custom_fsub_channels([-100999, -100999], seed_from=seed)
        assert saved.count(-100999) == 1
        assert all(channel in saved for channel in seed)

    asyncio.run(run())


@pytest.mark.parametrize("raw, expected", [
    ("-1001234567890", -1001234567890),
    ("-1001234567890\n", -1001234567890),
    (12345, 12345),
    ("@MyChannel", "@MyChannel"),
    ("MyChannel", "@MyChannel"),
    ("t.me/MyChannel", "@MyChannel"),
    ("https://t.me/MyChannel", "@MyChannel"),
    ("https://telegram.me/MyChannel/", "@MyChannel"),
    ("https://t.me/+PrivateInvite", None),
    ("not a channel", None),
    ("", None),
    (None, None),
])
def test_parse_channel_reference(raw, expected):
    assert controls.parse_channel_reference(raw) == expected


# --------------------------------------------------------------------------- #
# Panel text
# --------------------------------------------------------------------------- #
def test_sendall_panel_text_explains_both_states():
    on = botctl.sendall_panel_text(True)
    off = botctl.sendall_panel_text(False)
    assert "ON" in on and "premium" in on.lower()
    assert "OFF" in off and "every user" in off.lower()
    assert "MongoDB" in on


def test_fsub_panel_text_shows_mode_and_escapes_titles():
    repo = botctl.fsub_panel_text(None, [-100111], {-100111: "Repo <Channel>"})
    assert "repo/env default" in repo
    assert "Repo &lt;Channel&gt;" in repo and "<Channel>" not in repo

    empty = botctl.fsub_panel_text([], [], {})
    assert "Force-sub is OFF" in empty
    assert "bot-managed" in empty


def test_main_panel_text_summarises_both_controls():
    text = botctl.main_panel_text(True, None, len(info.AUTH_CHANNELS))
    assert "premium only" in text
    assert "repo/env default" in text
    assert str(len(info.AUTH_CHANNELS)) in text


# --------------------------------------------------------------------------- #
# Commands and buttons
# --------------------------------------------------------------------------- #
def test_sendall_command_off_persists_and_replies(fresh_botcol):
    client = FakeClient()
    message = FakeMessage(command=["sendall", "off"])

    asyncio.run(botctl.send_all_gate_cmd(client, message))

    async def check():
        return await controls.send_all_requires_premium()

    assert asyncio.run(check()) is False
    assert "OFF" in message.replies[0]["text"]
    assert client.sent and "gate OFF" in client.sent[0]["text"]


def test_sendall_panel_button_toggles_without_repo_edit(fresh_botcol):
    client = FakeClient()
    query = FakeCallbackQuery("botctl#sendall#off")

    asyncio.run(botctl.bot_control_callback(client, query))

    async def check():
        return await controls.send_all_requires_premium()

    assert asyncio.run(check()) is False
    assert query.message.edits, "panel was not edited"
    assert "OFF" in query.message.edits[-1]["text"]
    assert query.stopped is True
    assert client.sent and "gate OFF" in client.sent[0]["text"]


def test_fsub_command_panel_lists_channels_and_actions(fresh_botcol):
    titles = {channel: f"Channel {index}" for index, channel in enumerate(info.AUTH_CHANNELS)}
    client = FakeClient(titles)
    message = FakeMessage(command=["fsub"])

    asyncio.run(botctl.fsub_cmd(client, message))

    panel = message.replies[0]
    assert "repo/env default" in panel["text"]
    assert all(f"<code>{channel}</code>" in panel["text"] for channel in info.AUTH_CHANNELS)

    labels = [button.text for row in panel["kwargs"]["reply_markup"].inline_keyboard for button in row]
    assert any("Add channel" in label for label in labels)
    assert not any("Reset to env" in label for label in labels)


def test_fsub_add_command_saves_channel_and_shows_managed_mode(fresh_botcol):
    client = FakeClient({-100999: "New Force Channel"})
    message = FakeMessage(command=["fsub", "add", "-100999"])

    asyncio.run(botctl.fsub_cmd(client, message))

    async def saved():
        return await controls.get_custom_fsub_channels()

    custom = asyncio.run(saved())
    assert -100999 in custom
    assert all(channel in custom for channel in info.AUTH_CHANNELS)
    assert "added" in message.replies[0]["text"].lower()

    panel = message.replies[-1]
    assert "bot-managed" in panel["text"]
    assert "New Force Channel" in panel["text"]
    labels = [button.text for row in panel["kwargs"]["reply_markup"].inline_keyboard for button in row]
    assert any("Reset to env" in label for label in labels)


def test_fsub_remove_button_can_drop_env_channels(fresh_botcol):
    target = info.AUTH_CHANNELS[0]
    client = FakeClient()
    query = FakeCallbackQuery(f"botctl#fsub#rm#{target}")

    asyncio.run(botctl.bot_control_callback(client, query))

    async def saved():
        return await controls.get_custom_fsub_channels()

    custom = asyncio.run(saved())
    assert target not in custom
    assert custom == [channel for channel in info.AUTH_CHANNELS if channel != target]
    assert query.stopped is True


def test_fsub_reset_button_restores_env_mode(monkeypatch, fresh_botcol):
    async def run():
        await controls.save_custom_fsub_channels([-100555])
        client = FakeClient()
        query = FakeCallbackQuery("botctl#fsub#reset")
        await botctl.bot_control_callback(client, query)
        assert await controls.get_custom_fsub_channels() is None

    asyncio.run(run())


def test_pending_channel_input_adds_forwarded_channel(fresh_botcol):
    client = FakeClient({-100777: "Forwarded Channel"})
    botctl._set_pending(ADMIN_ID)

    message = FakeMessage(text="")
    message.forward_from_chat = SimpleNamespace(
        id=-100777, type=enums.ChatType.CHANNEL, title="Forwarded Channel"
    )

    asyncio.run(botctl.fsub_channel_input(client, message))

    async def saved():
        return await controls.get_custom_fsub_channels()

    assert -100777 in asyncio.run(saved())
    assert message.stopped is True
    assert botctl._pending_active(ADMIN_ID) is False


def test_cancel_clears_pending_channel_input():
    botctl._set_pending(ADMIN_ID)
    message = FakeMessage(command=["fsub", "cancel"])
    asyncio.run(botctl.fsub_cmd(FakeClient(), message))
    assert botctl._pending_active(ADMIN_ID) is False
    assert "cancelled" in message.replies[0]["text"].lower()


# --------------------------------------------------------------------------- #
# Send All gate integration (pmfilter)
# --------------------------------------------------------------------------- #
class _SendAllMessage:
    def __init__(self):
        self.chat = SimpleNamespace(id=-1001, type=enums.ChatType.SUPERGROUP)
        self.id = 456
        self.reply_to_message = SimpleNamespace(
            from_user=SimpleNamespace(id=NORMAL_ID), text="jawan"
        )
        self.replies = []

    async def reply_text(self, text="", **kwargs):
        self.replies.append({"text": text, "kwargs": kwargs})
        return FakeSentMessage(chat_id=self.chat.id)


class _SendAllQuery:
    def __init__(self, data, user_id=NORMAL_ID):
        self.data = data
        self.from_user = SimpleNamespace(
            id=user_id, first_name="User", mention="User"
        )
        self.message = _SendAllMessage()
        self.answers = []

    async def answer(self, text="", show_alert=False, url=None):
        self.answers.append({"text": text, "show_alert": show_alert, "url": url})


def test_send_all_gate_off_skips_premium_prompt(monkeypatch, fresh_botcol):
    import plugins.pmfilter as pmf

    async def no_premium(user_id):
        return False

    monkeypatch.setattr(db, "has_premium_access", no_premium)
    monkeypatch.setattr(temp, "U_NAME", "TestBot", raising=False)

    async def run():
        await controls.set_send_all_requires_premium(False)
        query = _SendAllQuery("sendfiles#123-456")
        await pmf.cb_handler(None, query)
        # Non-premium user goes straight to the allfiles deep link.
        assert query.answers[-1]["url"] == (
            "https://telegram.me/TestBot?start=allfiles_-1001_123-456"
        )
        assert query.message.replies == []

    asyncio.run(run())


def test_send_all_gate_on_still_prompts_premium(monkeypatch, fresh_botcol):
    import plugins.pmfilter as pmf

    async def no_premium(user_id):
        return False

    monkeypatch.setattr(db, "has_premium_access", no_premium)
    monkeypatch.setattr(temp, "U_NAME", "TestBot", raising=False)

    async def run():
        await controls.set_send_all_requires_premium(True)
        query = _SendAllQuery("sendfiles#123-456")
        await pmf.cb_handler(None, query)
        assert query.answers[-1]["show_alert"] is True
        assert query.answers[-1]["url"] is None
        assert query.message.replies, "premium offer should be sent"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Button-first UX: direct add + no toast popups
# --------------------------------------------------------------------------- #
def test_add_button_is_silent_and_opens_a_reply_box(fresh_botcol):
    """➕ Add channel must not pop a toast; it opens a focused reply box."""
    from pyrogram.types import ForceReply

    client = FakeClient()
    query = FakeCallbackQuery("botctl#fsub#add")

    asyncio.run(botctl.bot_control_callback(client, query))

    assert query.answers == [{"text": "", "show_alert": False}]   # no popup text
    assert botctl._pending_active(ADMIN_ID) is True
    prompt = query.message.replies[0]
    assert "Add a force-sub channel" in prompt["text"]
    assert isinstance(prompt["kwargs"]["reply_markup"], ForceReply)


def test_pasting_a_channel_id_shows_a_one_tap_add_card(fresh_botcol):
    """No command needed: a bare -100… id in PM offers a one-tap add."""
    client = FakeClient({-1008880001111: "Pasted Channel"})
    message = FakeMessage(text="-1008880001111")

    asyncio.run(botctl.fsub_channel_input(client, message))

    assert message.stopped is True
    card = message.replies[0]
    assert "Add this channel to force-sub?" in card["text"]
    buttons = card["kwargs"]["reply_markup"].inline_keyboard
    assert buttons[0][0].callback_data == "botctl#fsub#ok#-1008880001111"
    assert buttons[1][0].callback_data == "botctl#fsub#no"

    async def saved():
        return await controls.get_custom_fsub_channels()

    assert asyncio.run(saved()) is None      # only after the button is tapped


def test_pasting_a_tme_link_also_offers_the_add_card(fresh_botcol):
    client = FakeClient({
        -1008880002222: "Linked Channel",
        "@LinkedChannel": SimpleNamespace(
            id=-1008880002222, type=enums.ChatType.CHANNEL, title="Linked Channel"
        ),
    })
    message = FakeMessage(text="https://t.me/LinkedChannel")

    asyncio.run(botctl.fsub_channel_input(client, message))

    assert message.stopped is True
    assert "Add this channel" in message.replies[0]["text"]


def test_confirm_button_adds_the_channel_without_a_toast(fresh_botcol):
    client = FakeClient({-1008880001111: "Pasted Channel"})
    query = FakeCallbackQuery("botctl#fsub#ok#-1008880001111")

    asyncio.run(botctl.bot_control_callback(client, query))

    async def saved():
        return await controls.get_custom_fsub_channels()

    custom = asyncio.run(saved())
    assert -1008880001111 in custom
    assert query.answers == [{"text": "", "show_alert": False}]
    assert "Added to force-sub" in query.message.edits[-1]["text"]


def test_confirm_card_cancel_just_deletes_the_card(fresh_botcol):
    client = FakeClient()
    query = FakeCallbackQuery("botctl#fsub#no")

    asyncio.run(botctl.bot_control_callback(client, query))

    assert query.answers == [{"text": "", "show_alert": False}]
    assert query.message.deleted is True


def test_non_channel_username_is_left_for_normal_search(fresh_botcol):
    """An @mention that is not a channel must not swallow the message."""
    client = FakeClient()                     # resolves nothing
    message = FakeMessage(text="@notachannel")

    asyncio.run(botctl.fsub_channel_input(client, message))

    assert message.stopped is False
    assert message.replies == []


def test_panel_buttons_never_show_a_toast(fresh_botcol):
    client = FakeClient()
    for data in (
        "botctl#panel",
        "botctl#sendall",
        "botctl#sendall#off",
        "botctl#sendall#on",
        "botctl#fsub",
        "botctl#fsub#rm#-1003140286992",
        "botctl#fsub#reset",
    ):
        query = FakeCallbackQuery(data)
        asyncio.run(botctl.bot_control_callback(client, query))
        assert query.answers, data
        assert all(a["text"] == "" for a in query.answers), (data, query.answers)
        assert all(a["show_alert"] is False for a in query.answers), (data, query.answers)
