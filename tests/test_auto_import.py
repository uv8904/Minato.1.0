"""Auto-Import userbot (docs/AUTO_IMPORT.md).

Covers the reliability fixes for "raat ko chal raha tha, abhi nahi chal raha":

* a slow/down Mongo at boot must NOT kill auto-import for the whole run
* a transient Telegram/network failure at boot must self-heal (no bot restart)
* ``start_userbot()`` must be idempotent (no duplicate clients / workers)
* FloodWait on copy retries instead of failing the file
* ``/grab`` must survive progress-edit failures
* ``/grab https://t.me/c/...`` must not be hijacked by the index-link handler
* watch-matching accepts both ``-100…`` and bare t.me/c/ ids

Run with the project dependencies installed::

    pytest tests/test_auto_import.py -q
"""
import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("pyrogram")

from pyrogram import enums  # noqa: E402

import plugins.auto_import as ai  # noqa: E402


# --------------------------------------------------------------------- fakes
class FakeMdb:
    def __init__(self, fail=False):
        self.fail = fail
        self.store = {}

    async def get_config(self, key, default=None):
        if self.fail:
            raise RuntimeError("ServerSelectionTimeoutError: mongo waking up")
        return self.store.get(key, default)

    async def set_config(self, key, value):
        if self.fail:
            raise RuntimeError("mongo down")
        self.store[key] = value


class FakeMessage:
    def __init__(self, chat_id=111, chat_username="movie_bot", msg_id=1,
                 from_id=None, from_username=None, media="video",
                 file_name="Movie.2024.1080p.mkv"):
        self.id = msg_id
        self.empty = False
        self.outgoing = False
        self.media = {
            "video": enums.MessageMediaType.VIDEO,
            "document": enums.MessageMediaType.DOCUMENT,
            "audio": enums.MessageMediaType.AUDIO,
            None: None,
        }[media]
        self.chat = SimpleNamespace(id=chat_id, username=chat_username)
        self.from_user = SimpleNamespace(
            id=from_id or chat_id, username=from_username or chat_username
        )
        self.video = SimpleNamespace(file_name=file_name) if media == "video" else None
        self.document = SimpleNamespace(file_name=file_name) if media == "document" else None
        self.audio = SimpleNamespace(file_name=file_name) if media == "audio" else None
        self.caption = None


class FakeStatus:
    def __init__(self, edit_fail=False):
        self.edit_fail = edit_fail
        self.edits = []

    async def edit(self, text, **kwargs):
        if self.edit_fail:
            raise RuntimeError("edit blocked / MESSAGE_NOT_MODIFIED")
        self.edits.append(text)


class FakeReplyTarget:
    """Stands in for the admin ``message`` that /grab replies to."""

    def __init__(self, edit_fail=False):
        self.edit_fail = edit_fail
        self.reply_calls = []
        self.status = None

    async def reply(self, text, **kwargs):
        self.reply_calls.append(text)
        self.status = FakeStatus(edit_fail=self.edit_fail)
        return self.status


class FakeClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.handlers = []
        self.copied = []
        self.sent = []
        self.stopped = False
        self.is_connected = False
        self.start_fail_times = 0
        self.copy_fail_times = 0
        FakeClient.instances.append(self)

    async def start(self):
        if self.start_fail_times > 0:
            self.start_fail_times -= 1
            raise ConnectionError("Telegram unreachable while container comes up")
        self.is_connected = True
        return self

    async def stop(self):
        self.is_connected = False
        self.stopped = True

    async def get_me(self):
        return SimpleNamespace(id=999, username="myuser", first_name="Me")

    def add_handler(self, handler, group=0):
        self.handlers.append((handler, group))

    async def copy_message(self, target, chat_id, message_id):
        if self.copy_fail_times > 0:
            self.copy_fail_times -= 1
            raise ai.FloodWait(0)
        self.copied.append((target, chat_id, message_id))

    async def send_message(self, chat, text, **kwargs):
        self.sent.append((chat, text))

    async def get_chat(self, c):
        return SimpleNamespace(id=c, type=enums.ChatType.SUPERGROUP)

    async def get_messages(self, chat, ids):
        return [FakeMessage(chat_id=chat, msg_id=i) for i in ids]


class _StubMsg:
    """A real (uninitialized) pyrogram Message with just the attrs filters use."""

    def __init__(self, text=None, chat_type=enums.ChatType.PRIVATE, outgoing=False,
                 forward_date=None):
        from pyrogram.types import Message as PyroMessage

        m = PyroMessage.__new__(PyroMessage)
        m.text = text
        m.caption = None
        m.outgoing = outgoing
        m.forward_date = forward_date
        m.chat = SimpleNamespace(id=1, type=chat_type, username=None)
        m.from_user = SimpleNamespace(id=42, username="admin")
        self._msg = m

    @property
    def msg(self):
        return self._msg


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def env(monkeypatch):
    """Fresh, isolated auto-import runtime for every test."""
    FakeClient.instances = []
    monkeypatch.setattr(ai, "Client", FakeClient)
    monkeypatch.setattr(ai, "USER_SESSION", "FakeSessionString")
    monkeypatch.setattr(ai, "AUTO_IMPORT_DELAY", 1)
    monkeypatch.setattr(ai, "AUTO_IMPORT_RETRY_DELAY", 1)
    monkeypatch.setattr(ai, "BOOT_RETRY_BASE_DELAY", 0)
    monkeypatch.setattr(ai, "GRAB_COPY_DELAY", 0)
    monkeypatch.setattr(ai, "mdb", FakeMdb())
    monkeypatch.setattr(ai, "ubot", None)
    monkeypatch.setattr(ai, "worker_task", None)
    monkeypatch.setattr(ai, "flusher_task", None)
    monkeypatch.setattr(ai, "supervisor_task", None)
    monkeypatch.setattr(ai, "settings_loaded", False)
    monkeypatch.setattr(ai, "queue", asyncio.Queue())
    ai.settings.update({"enabled": False, "watch": [], "target": -1002086319581})
    ai.health.update({"state": "off", "last_error": "", "restarts": 0})
    ai.pending.update({"copied": 0, "failed": 0, "last_error": "", "names": []})
    return monkeypatch


# --------------------------------------------------------------- watch logic
def test_norm_entry_variants():
    assert ai.norm_entry("@Movie_Bot") == "@movie_bot"
    assert ai.norm_entry("movie_bot") == "@movie_bot"
    assert ai.norm_entry("-1002086319581") == "-1002086319581"
    assert ai.norm_entry("2086319581") == "2086319581"
    assert ai.norm_entry("") is None
    assert ai.norm_entry("@") is None
    assert ai.norm_entry(None) is None


def test_watch_matching_accepts_both_channel_id_forms():
    """/watch with the bare t.me/c/ id must match the -100… supergroup id and
    the other way round — this silently never matched before."""
    msg = FakeMessage(chat_id=-1002086319581, chat_username=None,
                     from_id=123, from_username=None)
    for entry in ("-1002086319581", "2086319581"):
        ai.settings["watch"] = [entry]
        assert ai.is_watched(msg), f"entry {entry} should match supergroup message"


def test_watch_matching_by_username_and_pm_bot():
    bot_msg = FakeMessage(chat_id=555, chat_username="Movie_Bot", from_id=555)
    ai.settings["watch"] = ["@movie_bot"]
    assert ai.is_watched(bot_msg)
    ai.settings["watch"] = ["@other_bot"]
    assert not ai.is_watched(bot_msg)


# ------------------------------------------------------- settings resilience
def test_load_settings_failure_is_nonfatal_and_retryable(env):
    async def run():
        env.setattr(ai, "mdb", FakeMdb(fail=True))
        await ai.load_settings()                      # must not raise
        assert ai.settings_loaded is False
        assert "settings load" in ai.health["last_error"]

        good = FakeMdb()
        good.store = {"autoimport_enabled": True, "autoimport_watch": ["@movie_bot"],
                      "autoimport_target": -1001234}
        env.setattr(ai, "mdb", good)
        await ai.load_settings()
        assert ai.settings_loaded is True
        assert ai.settings["enabled"] is True
        assert ai.settings["watch"] == ["@movie_bot"]
        assert ai.settings["target"] == -1001234

    asyncio.run(run())


# --------------------------------------------------------- boot / self-heal
def test_boot_survives_mongo_outage_and_settings_heal_later(env):
    """Boot par Mongo slow/down ho to userbot phir bhi start hona chahiye
    (pehle load_settings ki wajah se poora feature mar jata tha), aur settings
    supervisor ke next tick par khud load ho jate hain."""
    async def run():
        env.setattr(ai, "mdb", FakeMdb(fail=True))    # Mongo waking up at boot
        await ai.start_userbot()                      # must not raise
        assert ai.ubot is not None                    # userbot still came up
        assert ai.health["state"] == "online"
        assert ai.settings_loaded is False
        assert len(ai.ubot.handlers) == 1             # _on_incoming registered

        # Mongo wapas aa gaya — supervisor tick settings heal karta hai
        good = FakeMdb()
        good.store = {"autoimport_enabled": True, "autoimport_watch": ["@movie_bot"]}
        env.setattr(ai, "mdb", good)
        await ai._supervisor_tick()
        assert ai.settings_loaded is True
        assert ai.settings["enabled"] is True
        assert ai.settings["watch"] == ["@movie_bot"]

    asyncio.run(run())


def test_boot_retries_transient_telegram_failures(env):
    """client.start() ka network error — boot attempts ke andar recover, aur
    poora outage ho to supervisor dobara start karta hai."""
    async def run():
        class FlakyClient(FakeClient):
            remaining_failures = 0                    # class-level: naye instances pe bhi

            async def start(self):
                if FlakyClient.remaining_failures > 0:
                    FlakyClient.remaining_failures -= 1
                    raise ConnectionError("Telegram unreachable")
                self.is_connected = True
                return self

        # (a) do attempts fail, teesra isi boot me chal gaya
        FlakyClient.remaining_failures = 2
        env.setattr(ai, "Client", FlakyClient)
        await ai.start_userbot()
        assert ai.ubot is not None
        assert ai.health["state"] == "online"
        assert len(FlakyClient.instances) == 3        # 2 failed + 1 live

        # (b) poora outage — teeno attempts fail, phir self-heal
        await ai.stop_userbot()
        FlakyClient.remaining_failures = 99           # isi tick par bhi fail
        await ai.start_userbot()
        assert ai.ubot is None
        assert ai.health["state"] == "retrying"

        FlakyClient.remaining_failures = 0            # network wapas aa gaya
        await ai._supervisor_tick()
        assert ai.ubot is not None
        assert ai.health["state"] == "online"

    asyncio.run(run())


def test_start_userbot_is_idempotent(env):
    async def run():
        await ai.start_userbot()
        first = ai.ubot
        assert first is not None
        worker_after_first = ai.worker_task

        await ai.start_userbot()                      # e.g. startup retry loop
        second = ai.ubot
        assert second is not None and second is not first
        assert first.stopped is True                  # old client released
        await asyncio.sleep(0)                        # cancelled task runs
        assert worker_after_first.done()              # old worker dead
        assert ai.worker_task is not worker_after_first
        # only the live client holds the single _on_incoming handler
        assert len(second.handlers) == 1

    asyncio.run(run())


def test_supervisor_restarts_dead_client(env):
    async def run():
        await ai.start_userbot()
        ub = ai.ubot
        ub.is_connected = False                       # connection died overnight
        await ai._supervisor_tick()
        assert ub.stopped is True
        assert ai.ubot is not None and ai.ubot is not ub
        assert ai.health["state"] == "online"

    asyncio.run(run())


def test_start_without_session_is_silent_noop(env):
    async def run():
        env.setattr(ai, "USER_SESSION", "")
        await ai.start_userbot()
        assert ai.ubot is None
        assert ai.health["state"] == "disabled"
        assert FakeClient.instances == []

    asyncio.run(run())


# --------------------------------------------------------- copy queue/worker
def test_on_incoming_gating(env):
    async def run():
        await ai.start_userbot()
        ub = ai.ubot
        ai.settings["enabled"] = False
        await ai._on_incoming(ub, FakeMessage())
        assert ai.queue.qsize() == 0                  # feature off -> ignored

        ai.settings["enabled"] = True
        ai.settings["watch"] = ["@movie_bot"]
        await ai._on_incoming(ub, FakeMessage(media=None))
        assert ai.queue.qsize() == 0                  # non-media -> ignored

        unwatched = FakeMessage(chat_id=2, chat_username="other_bot")
        await ai._on_incoming(ub, unwatched)
        assert ai.queue.qsize() == 0                  # not watched -> ignored

        watched = FakeMessage(chat_id=1, chat_username="movie_bot", msg_id=9)
        await ai._on_incoming(ub, watched)
        assert ai.queue.qsize() == 1

    asyncio.run(run())


def test_copy_retries_after_floodwait(env):
    async def run():
        await ai.start_userbot()
        ub = ai.ubot
        ub.copy_fail_times = 1                        # first attempt flood-limited
        ai.settings["enabled"] = True
        msg = FakeMessage()
        await ai._copy_one(msg)
        assert ai.pending["copied"] == 1
        assert ai.pending["failed"] == 0
        assert ub.copied == [(-1002086319581, msg.chat.id, msg.id)]

    asyncio.run(run())


def test_copy_failure_on_dead_target_reports_to_saved_messages(env):
    class DeadTargetClient(FakeClient):
        async def copy_message(self, target, chat_id, message_id):
            raise ai.ChannelInvalid("CHANNEL_INVALID")

    async def run():
        env.setattr(ai, "Client", DeadTargetClient)
        await ai.start_userbot()
        ub = ai.ubot
        ai.settings["enabled"] = True
        await ai._copy_one(FakeMessage())
        assert ai.pending["failed"] == 1
        assert any(chat == "me" and "target channel" in text for chat, text in ub.sent)

    asyncio.run(run())


# ------------------------------------------------------------------- /grab
def test_grab_survives_progress_edit_failures(env):
    """Progress edit (ya status message delete) se bulk copy cancel nahi honi
    chahiye — /grab aksar isi wajah se beech me mara tha."""
    async def run():
        await ai.start_userbot()
        ub = ai.ubot

        message = FakeReplyTarget(edit_fail=True)     # har edit fail
        await ai._run_grab(message, message, chat=-1002086319581, start=1, end=5)

        assert len(ub.copied) == 5                    # saari files copy ho gayi
        # result message.reply fallback se gaya (edit to fail hi the)
        assert any("Grab khatam" in t for t in message.reply_calls)

    asyncio.run(run())


def test_grab_reports_unreachable_channel_cleanly(env):
    class NoAccessClient(FakeClient):
        async def get_chat(self, c):
            raise ai.ChannelInvalid("CHANNEL_INVALID")

    async def run():
        env.setattr(ai, "Client", NoAccessClient)
        await ai.start_userbot()

        message = FakeReplyTarget(edit_fail=False)
        await ai._run_grab(message, message, chat=-1001, start=1, end=2)
        assert message.status is not None
        assert any("accessible nahi hai" in t for t in message.status.edits)

    asyncio.run(run())


def test_abs_chat_id_treats_tme_c_and_supergroup_ids_as_same():
    assert ai.abs_chat_id("-1002086319581") == ai.abs_chat_id("2086319581")
    assert ai.abs_chat_id(-1002086319581) == ai.abs_chat_id(2086319581)
    assert ai.abs_chat_id("@name") == "@name"


def test_parse_grab_args_link_and_ranges():
    assert ai.parse_grab_args("/grab https://t.me/c/1234567/890") == (-1001234567, 1, 890, None)
    assert ai.parse_grab_args("/grab -100123 5000") == (-100123, 1, 5000, None)
    assert ai.parse_grab_args("/grab @ch 100 200") == ("ch", 100, 200, None)
    assert ai.parse_grab_args("/grab")[3] is not None


# ------------------------------------------------------- index handler clash
def test_index_link_handler_does_not_steal_slash_commands():
    """'/grab https://t.me/c/…/890' ko index handler 'Invalid link' bol ke
    chaba jata tha — ab sirf bare links/forwards uske paas jate hain."""
    from plugins.index import send_for_index

    handler = send_for_index.handlers[0][0]
    grab_msg = _StubMsg(text="/grab https://t.me/c/1234567/890")
    assert asyncio.run(handler.check(None, grab_msg.msg)) is False

    bare_link = _StubMsg(text="https://t.me/c/1234567/890")
    assert asyncio.run(handler.check(None, bare_link.msg)) is True


# ------------------------------------------------------------- status texts
def test_problem_texts_distinguish_missing_session_vs_offline(env):
    env.setattr(ai, "USER_SESSION", "")
    assert "USER_SESSION" in ai.userbot_problem_text()
    assert "setup adhura" in ai.userbot_problem_text()

    env.setattr(ai, "USER_SESSION", "FakeSessionString")
    ai.health["last_error"] = "AuthKeyUnregistered: session revoked"
    text = ai.userbot_problem_text()
    assert "offline" in text
    assert "AuthKeyUnregistered" in text
