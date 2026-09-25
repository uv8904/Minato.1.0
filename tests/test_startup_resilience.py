"""Startup resilience (docs/STARTUP_RESILIENCE.md).

The bot must reach ``idle()`` even when the *optional* parts of the start-up
sequence fail (Mongo slow, wrong LOG_CHANNEL, web port taken, one broken
plugin, one bad MULTI_TOKEN ...), and hard failures must be retried with a
capped back-off instead of killing the process.

Run with the project dependencies installed::

    pytest tests/test_startup_resilience.py -q
"""
import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pyrogram")
pytest.importorskip("imdb")

import bot  # noqa: E402  (imports the real start-up module – no network at import time)
import plugins  # noqa: E402
import dreamxbotz.Bot.clients as clients_mod  # noqa: E402


# ------------------------------------------------------------------ back-off
def test_retry_delay_doubles_and_is_capped():
    assert [bot.startup_retry_delay(n) for n in range(1, 8)] == [5, 10, 20, 40, 80, 160, 300]
    assert bot.startup_retry_delay(50) == bot.START_RETRY_MAX_DELAY
    assert bot.startup_retry_delay(0) == bot.START_RETRY_BASE_DELAY   # never 0 / negative


# ------------------------------------------------------- broken plugin files
def test_quarantine_disables_only_the_plugins_that_do_not_compile(tmp_path, monkeypatch):
    root = tmp_path / "fakeplugins"
    (root / "sub").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "ok.py").write_text("X = 1\n")
    (root / "bad.py").write_text("def broken(:\n")                 # SyntaxError
    (root / "sub" / "__init__.py").write_text("")
    (root / "sub" / "worse.py").write_text("if True\n    pass\n")   # SyntaxError
    for name in ("fakeplugins.bad", "fakeplugins.sub.worse", "fakeplugins.ok"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    broken = bot.quarantine_broken_plugins(str(root))

    assert broken == ["fakeplugins.bad", "fakeplugins.sub.worse"]
    # the broken ones are shadowed by empty modules -> pyrogram finds no handlers, no crash
    assert isinstance(sys.modules["fakeplugins.bad"], types.ModuleType)
    assert isinstance(sys.modules["fakeplugins.sub.worse"], types.ModuleType)
    assert not [n for n in vars(sys.modules["fakeplugins.bad"]) if not n.startswith("__")]
    # healthy plugins (and packages) are left alone
    assert "fakeplugins.ok" not in sys.modules
    assert "fakeplugins" not in sys.modules
    for name in broken:
        monkeypatch.delitem(sys.modules, name, raising=False)


def test_quarantine_is_a_noop_for_the_real_plugins_folder():
    """Guard for the repo itself: every shipped plugin must compile."""
    assert bot.quarantine_broken_plugins("plugins") == []


def test_preload_plugins_quarantines_import_errors_and_executes_once(tmp_path, monkeypatch):
    root = tmp_path / "fakeplugins"
    root.mkdir()
    (root / "__init__.py").write_text("")
    (root / "good.py").write_text(
        "LOADS = globals().get('LOADS', 0) + 1\nLOADED = True\n"
    )
    (root / "bad.py").write_text("raise RuntimeError('boom at import time')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ("fakeplugins", "fakeplugins.good", "fakeplugins.bad"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    loaded, failed = bot.preload_plugins(str(root))
    # A second preload must get the same cached module, not execute its body
    # again like the old exec_module loop did.
    bot.preload_plugins(str(root))

    assert loaded == ["fakeplugins.good"]
    assert failed == ["fakeplugins.bad"]
    assert sys.modules["fakeplugins.good"].LOADED is True
    assert sys.modules["fakeplugins.good"].LOADS == 1
    assert isinstance(sys.modules["fakeplugins.bad"], types.ModuleType)
    assert not [n for n in vars(sys.modules["fakeplugins.bad"]) if not n.startswith("__")]


# ------------------------------------------------------ dreamxbotz_start()
class _FakeClient:
    """Just enough of a pyrogram Client for dreamxbotz_start()."""

    def __init__(self, fail_chats=()):
        self.fail_chats = set(fail_chats)
        self.sent = []
        self.started = False
        self.is_connected = False
        self.username = None

    @property
    def loop(self):
        return asyncio.get_running_loop()

    async def start(self):
        self.started = True
        self.is_connected = True

    async def stop(self):
        self.is_connected = False

    async def get_me(self):
        return SimpleNamespace(id=42, username="TestBot", first_name="Test", mention="@TestBot")

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(chat_id)
        if chat_id in self.fail_chats:
            raise RuntimeError(f"PEER_ID_INVALID for {chat_id}")


class _FakeDB:
    def __init__(self, fail=False):
        self.fail = fail

    async def get_banned(self):
        if self.fail:
            raise ConnectionError("mongo not reachable")
        return [1, 2], [3]


class _FakeMedia:
    calls = 0

    def __init__(self, fail=False):
        self.fail = fail

    async def ensure_indexes(self):
        _FakeMedia.calls += 1
        if self.fail:
            raise TimeoutError("index build timed out")


def _stub_module(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


async def _noop(*args, **kwargs):
    return None


def _wire(monkeypatch, *, client, db, media, media2, web_server, reached):
    """Point every collaborator of dreamxbotz_start() at an in-memory fake."""
    async def fake_idle():
        reached["idle"] = True

    async def failing_clients():
        raise RuntimeError("MULTI_TOKEN_2 is garbage")

    monkeypatch.setattr(bot, "dreamxbotz", client)
    monkeypatch.setattr(bot, "db", db)
    monkeypatch.setattr(bot, "Media", media)
    monkeypatch.setattr(bot, "Media2", media2)
    monkeypatch.setattr(bot, "web_server", web_server)
    monkeypatch.setattr(bot, "idle", fake_idle)
    monkeypatch.setattr(bot, "initialize_clients", failing_clients)
    monkeypatch.setattr(
        bot,
        "preload_plugins",
        lambda root="plugins": reached.__setitem__("preloaded", True) or ([], []),
    )
    monkeypatch.setattr(bot, "check_expired_premium", _noop)
    monkeypatch.setattr(bot, "keep_alive", _noop)
    monkeypatch.setattr(bot, "ON_HEROKU", False)
    monkeypatch.setattr(bot, "MULTIPLE_DB", True)
    monkeypatch.setattr(bot, "LOG_CHANNEL", -100123)
    monkeypatch.setattr(bot, "ADMINS", [777, 888])
    monkeypatch.setattr(bot, "PORT", "0")
    monkeypatch.setattr(bot, "temp", SimpleNamespace())
    # lazy imports inside dreamxbotz_start() – never touch IMAP / Mongo in tests
    _stub_module(monkeypatch, "plugins.FamPay", start_fampay_workers=lambda: reached.__setitem__("fampay", True))
    _stub_module(monkeypatch, "dreamxbotz.util.new_uploaded", start_worker=_noop)


def test_start_reaches_idle_when_every_optional_step_fails(monkeypatch):
    reached = {}
    client = _FakeClient(fail_chats={-100123, 777})          # LOG_CHANNEL + one admin unreachable

    async def broken_web_server():
        raise OSError(98, "address already in use")

    _wire(monkeypatch, client=client, db=_FakeDB(fail=True), media=_FakeMedia(fail=True),
          media2=_FakeMedia(fail=True), web_server=broken_web_server, reached=reached)

    asyncio.run(bot.dreamxbotz_start())

    assert reached["idle"] is True                           # <- the whole point
    assert client.started
    assert client.username == "@TestBot"
    assert bot.temp.BANNED_USERS == [] and bot.temp.BANNED_CHATS == []   # DB down -> empty lists
    assert bot.temp.ME == 42 and bot.temp.U_NAME == "TestBot"
    assert client.sent == [-100123, 777, 888]                # kept going after each failure
    assert reached["preloaded"] is True                       # handlers prepared exactly once
    assert reached["fampay"] is True


def test_start_happy_path_still_does_everything(monkeypatch):
    reached = {}
    client = _FakeClient()
    built = {}

    async def ok_web_server():
        from aiohttp import web
        built["app"] = web.Application()
        return built["app"]

    _FakeMedia.calls = 0
    _wire(monkeypatch, client=client, db=_FakeDB(), media=_FakeMedia(), media2=_FakeMedia(),
          web_server=ok_web_server, reached=reached)

    asyncio.run(bot.dreamxbotz_start())

    assert reached["idle"] is True
    assert bot.temp.BANNED_USERS == [1, 2] and bot.temp.BANNED_CHATS == [3]
    assert _FakeMedia.calls == 2                             # Media + Media2 (MULTIPLE_DB)
    assert client.sent == [-100123, 777, 888]
    assert "app" in built                                    # web server was built and started on PORT=0


def test_a_broken_telegram_connection_still_propagates(monkeypatch):
    """start() failing is *not* optional – it must bubble up to the retry loop."""
    reached = {}
    client = _FakeClient()

    async def cannot_connect():
        raise ConnectionError("Telegram unreachable")

    _wire(monkeypatch, client=client, db=_FakeDB(), media=_FakeMedia(), media2=_FakeMedia(),
          web_server=_noop, reached=reached)
    monkeypatch.setattr(client, "start", cannot_connect)

    with pytest.raises(ConnectionError):
        asyncio.run(bot.dreamxbotz_start())
    assert "idle" not in reached


def test_stop_client_quietly_never_raises(monkeypatch):
    class Boom(_FakeClient):
        async def stop(self):
            raise RuntimeError("already disconnected")

    boom = Boom()
    boom.is_connected = True
    monkeypatch.setattr(bot, "dreamxbotz", boom)
    asyncio.run(bot.stop_client_quietly())                   # swallowed

    calm = _FakeClient()
    calm.is_connected = False
    stopped = []
    calm.stop = lambda: stopped.append(True) or _noop()
    monkeypatch.setattr(bot, "dreamxbotz", calm)
    asyncio.run(bot.stop_client_quietly())
    assert stopped == []                                     # nothing to stop -> stop() not called


# ------------------------------------------------------- extra MULTI_TOKENs
def test_one_bad_multi_token_does_not_crash_initialize_clients(monkeypatch):
    class FakeExtraClient:
        def __init__(self, name, bot_token, **kwargs):
            self.name, self.bot_token = name, bot_token

        async def start(self):
            if self.bot_token == "bad":
                raise ValueError("The bot token is invalid")
            return self

    monkeypatch.setattr(clients_mod.TokenParser, "parse_from_env", lambda self: {1: "good", 2: "bad"})
    monkeypatch.setattr(clients_mod, "Client", FakeExtraClient)
    monkeypatch.setattr(clients_mod.asyncio, "sleep", _noop)
    monkeypatch.setattr(clients_mod, "multi_clients", {})
    monkeypatch.setattr(clients_mod, "work_loads", {})

    asyncio.run(clients_mod.initialize_clients())            # used to raise TypeError from dict([None])

    assert sorted(clients_mod.multi_clients) == [0, 1]
    assert clients_mod.multi_clients[1].bot_token == "good"
    assert 2 not in clients_mod.multi_clients


# ------------------------------------------------ premium-expiry background task
def test_premium_expiry_worker_survives_a_db_error():
    class _Stop(Exception):
        pass

    calls, naps = [], []

    class FlakyDB:
        async def get_expired(self, now):
            calls.append(now)
            if len(calls) == 1:
                raise ConnectionError("mongo blip")
            return []

    async def fake_sleep(seconds):
        naps.append(seconds)
        if len(naps) == 2:
            raise _Stop()

    async def run(monkeypatch):
        monkeypatch.setattr(plugins, "db", FlakyDB())
        monkeypatch.setattr(plugins, "sleep", fake_sleep)
        await plugins.check_expired_premium(client=None)

    mp = pytest.MonkeyPatch()
    try:
        with pytest.raises(_Stop):
            asyncio.run(run(mp))
    finally:
        mp.undo()

    assert len(calls) == 2                                   # loop went on after the failure
    assert naps == [30, 1]                                   # back-off after error, normal tick after
