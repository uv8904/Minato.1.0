"""Start-flash media (the 🌿 leaf shown before the start photo) and the
/alive sticker: value parsing and send dispatch.

Admins configure them live with /setstartemoji & /setalivesticker; the
runtime value is stored in MongoDB (configuration collection) and cached
in utils.temp.FLASH.

Run with the project dependencies installed::

    pytest tests/test_start_flash.py -q
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pyrogram")
pytest.importorskip("imdb")

import utils  # noqa: E402
from utils import parse_flash_value, send_start_flash  # noqa: E402


class _FakeMDB:
    """In-memory stand-in for database.config_db.mdb (never touches network)."""

    def __init__(self):
        self.store = {}
        self.reads = 0

    async def get_config(self, key, default=None):
        self.reads += 1
        return self.store.get(key, default)

    async def set_config(self, key, value):
        self.store[key] = value


@pytest.fixture(autouse=True)
def fake_mdb(monkeypatch):
    """The lazy `from database.config_db import mdb` inside get_flash_value()
    must find this fake — otherwise info.py's default URI would be contacted."""
    import database.config_db as cfg
    fake = _FakeMDB()
    monkeypatch.setattr(cfg, "mdb", fake)
    monkeypatch.setattr(utils.temp, "FLASH", {})
    return fake


# ---------------------------------------------------------------- parsing
def test_plain_emoji_is_text():
    assert parse_flash_value("🌿") == ("text", "🌿")


def test_raw_text_is_text():
    assert parse_flash_value("Dattebayo") == ("text", "Dattebayo")


def test_off_values_disable():
    for v in ("off", "OFF", "false", "0", "disabled", "none"):
        assert parse_flash_value(v) == ("off", None)
    assert parse_flash_value("  off  ") == ("off", None)


def test_gif_url_is_animation():
    kind, payload = parse_flash_value("https://files.catbox.moe/abc.gif")
    assert (kind, payload) == ("anim", "https://files.catbox.moe/abc.gif")


def test_mp4_url_is_animation():
    assert parse_flash_value("https://x.com/a/b.mp4")[0] == "anim"


def test_image_url_is_photo():
    kind, payload = parse_flash_value("https://graph.org/file/img.jpg")
    assert kind == "photo"
    assert payload == "https://graph.org/file/img.jpg"


def test_prefixed_kinds():
    assert parse_flash_value("sticker:CAACAgIAAxkBAAI")[0] == "sticker"
    assert parse_flash_value("anim:BQACAgIAAxkBAAI")[0] == "anim"
    assert parse_flash_value("photo:AgACAgIAAxkBAAI")[0] == "photo"
    assert parse_flash_value("video:BQACAgIAAxkBAAI")[0] == "video"
    assert parse_flash_value("text:sticker:not a sticker") == ("text", "sticker:not a sticker")


def test_raw_file_id_detected():
    fid = "CAACAgIAAxkBAAEBVAlmCYqbLub_o5pVUOEwbqhV8kRytgACRBkAAgjh2UlSqev16oISqB4E"
    assert parse_flash_value(fid) == ("file", fid)


def test_empty_is_off():
    assert parse_flash_value("") == ("off", None)
    assert parse_flash_value(None) == ("off", None)


# ---------------------------------------------------------------- sending
class _Msg:
    """Fake incoming message that records which reply_* was used."""

    def __init__(self, fail=()):
        self.calls = []
        self.sent = []
        self._fail = set(fail)
        self.reply_sticker = self._mk("sticker")
        self.reply_animation = self._mk("animation")
        self.reply_photo = self._mk("photo")
        self.reply_video = self._mk("video")
        self.reply_text = self._mk("text")

    def _mk(self, name):
        async def _send(payload=None, **kw):
            self.calls.append(name)
            if name in self._fail:
                raise RuntimeError(f"{name} not supported")
            m = SimpleNamespace(kind=name, media=payload)
            self.sent.append(m)
            return m
        return _send


def test_send_text_flash(monkeypatch):
    monkeypatch.setattr(utils.temp, "FLASH", {})
    msg = _Msg()
    sent = asyncio.get_event_loop().run_until_complete(
        send_start_flash(msg, key="k", env_default="🌿")
    )
    assert msg.calls == ["text"]
    assert sent.media == "🌿"


def test_send_off_returns_none(monkeypatch):
    monkeypatch.setattr(utils.temp, "FLASH", {})
    msg = _Msg()
    sent = asyncio.get_event_loop().run_until_complete(
        send_start_flash(msg, key="k", env_default="off")
    )
    assert sent is None
    assert msg.calls == []


def test_send_sticker_via_prefix(monkeypatch):
    monkeypatch.setattr(utils.temp, "FLASH", {})
    msg = _Msg()
    sent = asyncio.get_event_loop().run_until_complete(
        send_start_flash(msg, key="k", env_default="sticker:CAAC123")
    )
    assert msg.calls == ["sticker"]
    assert sent.media == "CAAC123"


def test_unknown_file_id_probes_sticker_then_photo(monkeypatch):
    monkeypatch.setattr(utils.temp, "FLASH", {})
    msg = _Msg(fail=("sticker",))  # sticker fails -> animation also fails -> photo
    msg.reply_animation = None  # force AttributeError path to be caught

    async def _boom(payload=None, **kw):
        msg.calls.append("animation")
        raise RuntimeError("not an animation")

    msg.reply_animation = _boom
    sent = asyncio.get_event_loop().run_until_complete(
        send_start_flash(msg, key="k", env_default="BQACAgIAAxkBAAEBVAlmCYqbLub_o5pVUOEwbqhV8kRytgACRB")
    )
    assert msg.calls[0] == "sticker"
    assert msg.calls[-1] == "photo"
    assert sent.kind == "photo"


def test_send_failure_never_raises(monkeypatch):
    monkeypatch.setattr(utils.temp, "FLASH", {})
    msg = _Msg(fail=("sticker", "animation", "photo", "video", "text"))
    sent = asyncio.get_event_loop().run_until_complete(
        send_start_flash(msg, key="k", env_default="sticker:BAD")
    )
    assert sent is None  # decorative flash must not break /start


def test_db_override_wins_and_is_cached(fake_mdb):
    fake_mdb.store["start_flash"] = "🔥"

    async def run():
        msg = _Msg()
        first = await send_start_flash(msg, key="start_flash", env_default="🌿")
        msg2 = _Msg()
        second = await send_start_flash(msg2, key="start_flash", env_default="🌿")
        return first, second, msg, msg2

    first, second, msg, msg2 = asyncio.get_event_loop().run_until_complete(run())
    assert first.media == "🔥"
    assert fake_mdb.reads == 1  # second call served from cache
    assert msg2.calls == ["text"]

    # cache invalidation -> new value picked up
    fake_mdb.store["start_flash"] = "sticker:CAACxyz"
    utils.temp.FLASH.pop("start_flash")
    msg3 = _Msg()
    third = asyncio.get_event_loop().run_until_complete(
        send_start_flash(msg3, key="start_flash", env_default="🌿")
    )
    assert msg3.calls == ["sticker"]
    assert third.media == "CAACxyz"
