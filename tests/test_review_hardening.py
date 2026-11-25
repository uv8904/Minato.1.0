"""Regression tests for the repo-review fixes (misc small bugs)."""
import asyncio
import hashlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


# --- pmfilter "del" callback NameError + malformed URL ---------------------- #
def test_del_callback_answers_with_a_valid_deeplink(monkeypatch):
    import plugins.pmfilter as pmf

    files = SimpleNamespace(file_name="Movie.mkv", file_size=1024, caption=None, file_id="FILEID")
    monkeypatch.setattr(pmf, "get_file_details", AsyncMock(return_value=[files]))
    monkeypatch.setattr(pmf, "get_settings", AsyncMock(return_value={}))
    monkeypatch.setattr(pmf, "CUSTOM_FILE_CAPTION", "")
    monkeypatch.setattr(pmf.temp, "U_NAME", "TestBot")
    query = SimpleNamespace(
        data="del#abc",
        from_user=SimpleNamespace(id=1),
        message=SimpleNamespace(chat=SimpleNamespace(id=123)),
        answer=AsyncMock(),
    )
    client = SimpleNamespace(create_chat_invite_link=AsyncMock())

    asyncio.run(pmf.cb_handler(client, query))

    url = query.answer.await_args_list[0].kwargs["url"]
    assert url == "https://telegram.me/TestBot?start=file_123_FILEID"
    # No invite link may be created for an ordinary button press.
    client.create_chat_invite_link.assert_not_awaited()


# --- invite link is cached, not one per callback ----------------------------- #
def test_request_channel_invite_is_cached(monkeypatch):
    import plugins.pmfilter as pmf

    monkeypatch.setattr(pmf, "REQST_CHANNEL", -100123)
    monkeypatch.setattr(pmf, "_REQST_INVITE", {"url": "", "ts": 0.0})
    client = SimpleNamespace(
        create_chat_invite_link=AsyncMock(return_value=SimpleNamespace(invite_link="https://t.me/+x"))
    )

    async def scenario():
        first = await pmf._request_channel_invite(client)
        second = await pmf._request_channel_invite(client)
        assert first == second == "https://t.me/+x"
        client.create_chat_invite_link.assert_awaited_once()

    asyncio.run(scenario())


# --- voice search must not call Groq without a key --------------------------- #
def test_transcription_is_skipped_without_a_key(monkeypatch):
    import plugins.voice_search as vs

    monkeypatch.setattr(vs, "GROQ_API_KEY", "")

    def _no_network(*a, **k):
        raise AssertionError("Groq must not be called without a configured key")

    monkeypatch.setattr(vs.requests, "post", _no_network)
    assert asyncio.run(vs.transcribe_with_groq("/nonexistent.ogg")) == ""
    assert vs.is_enabled() is False


# --- ott_catalog.__all__ must only name real symbols ------------------------- #
def test_ott_catalog_all_is_importable():
    import dreamxbotz.util.ott_catalog as catalog

    for name in catalog.__all__:
        assert hasattr(catalog, name), f"__all__ exports missing symbol {name}"


# --- analytics: spoofable XFF + salted hash ---------------------------------- #
def test_client_ip_uses_the_rightmost_forwarded_entry():
    from dreamxbotz.server import analytics_api

    request = SimpleNamespace(
        headers={"X-Forwarded-For": "9.9.9.9, 8.8.8.8, 1.2.3.4"}, remote="10.0.0.1"
    )
    assert analytics_api._client_ip(request) == "1.2.3.4"
    assert analytics_api._client_ip(SimpleNamespace(headers={}, remote="10.0.0.1")) == "10.0.0.1"


def test_recommendation_scores_are_process_independent():
    from dreamxbotz.server import analytics_api

    expected = (int(hashlib.sha1(b"Interstellar").hexdigest()[:8], 16) % 100) / 100
    assert analytics_api._rec_score([], "Interstellar") == expected


# --- top-messages array is capped -------------------------------------------- #
def test_top_messages_array_is_capped():
    mongomock_motor = pytest.importorskip("mongomock_motor")
    from database.config_db import mdb, MAX_TRACKED_MESSAGES

    async def scenario():
        client = mongomock_motor.AsyncMongoMockClient()
        original = mdb.col
        mdb.col = client["test"]["user"]
        try:
            for i in range(MAX_TRACKED_MESSAGES + 50):
                await mdb.update_top_messages(1, f"query {i}")
            doc = await mdb.col.find_one({"user_id": 1})
            assert len(doc["messages"]) == MAX_TRACKED_MESSAGES
            assert doc["messages"][-1]["text"] == f"query {MAX_TRACKED_MESSAGES + 49}"
        finally:
            mdb.col = original

    asyncio.run(scenario())


# --- schedule_later: non-blocking scheduled cleanup --------------------------- #
def test_schedule_later_runs_async_cleanup_without_blocking():
    from dreamxbotz.util.message_cleanup import schedule_later

    async def scenario():
        done = asyncio.Event()
        calls = []

        async def cleanup(tag):
            calls.append(tag)
            done.set()

        timer = schedule_later(0.01, cleanup, "files")
        assert not done.is_set()               # handler returns immediately
        await asyncio.wait_for(done.wait(), 1)
        assert calls == ["files"]
        timer.cancel()

        async def boom():
            raise RuntimeError("cleanup failed")

        schedule_later(0.01, boom)             # must not raise into the loop
        await asyncio.sleep(0.05)

    asyncio.run(scenario())


def test_hot_paths_no_longer_sleep_inside_handlers():
    for rel in ("plugins/commands.py", "plugins/pmfilter.py",
                "plugins/Premium.py", "plugins/p_ttishow.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "asyncio.sleep(DELETE_TIME)" not in src, rel
        assert "asyncio.sleep(600)" not in src, rel


def test_log_file_appends_instead_of_truncating():
    conf = (ROOT / "logging.conf").read_text(encoding="utf-8")
    assert "('DreamXlogs.txt', 'a')" in conf


def test_watch_page_does_not_fetch_its_own_url():
    import dreamxbotz.util.render_template as rt

    src = inspect.getsource(rt.render_page)
    assert "ClientSession" not in src
    assert "u.headers.get" not in src
