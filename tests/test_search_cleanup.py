"""Handlers return immediately while deletion and IMDb features remain available."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dreamxbotz.util.message_cleanup import delete_later


def test_deletion_timer_is_nonblocking_and_attempts_each_message():
    async def scenario():
        deleted = asyncio.Event()
        missing = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("already gone")))
        request = SimpleNamespace(delete=AsyncMock(side_effect=deleted.set))
        timer = delete_later(0.01, missing, request)
        assert not timer.cancelled()
        missing.delete.assert_not_awaited()
        await asyncio.wait_for(deleted.wait(), 1)
        missing.delete.assert_awaited_once()
        request.delete.assert_awaited_once()
        timer = delete_later(300, request)
        timer.cancel()
    asyncio.run(scenario())


@pytest.mark.parametrize("poster_mode", ["off", "photo", "bad_photo", "fallback"])
@pytest.mark.parametrize("auto_delete", [True, False, None])
def test_search_returns_without_waiting_for_delete_timer(monkeypatch, poster_mode, auto_delete):
    import plugins.pmfilter as pmf

    settings = {"imdb": poster_mode != "off", "spell_check": False, "template": "{title}"}
    if auto_delete is not None:
        settings["auto_delete"] = auto_delete
    progress = SimpleNamespace(delete=AsyncMock())
    result = SimpleNamespace(delete=AsyncMock())
    message = SimpleNamespace(
        id=5, text="movie", chat=SimpleNamespace(id=123),
        from_user=SimpleNamespace(id=8), delete=AsyncMock(),
        reply_text=AsyncMock(side_effect=[progress, result]),
        reply_photo=AsyncMock(return_value=result),
    )
    if poster_mode == "bad_photo":
        message.reply_photo.side_effect = [pmf.PhotoInvalidDimensions(), result]
    if poster_mode == "fallback":
        message.reply_photo.side_effect = RuntimeError("photo unavailable")
    fields = ("title votes aka seasons box_office localized_title kind imdb_id cast runtime "
              "countries certificates languages director writer producer composer cinematographer "
              "music_team distributors release_date year genres poster plot rating url").split()
    poster = dict.fromkeys(fields, "test")
    poster["poster"] = "https://example.org/poster.jpg"
    scheduled = []
    monkeypatch.setattr(pmf, "get_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(pmf, "save_group_settings", AsyncMock())
    monkeypatch.setattr(pmf, "get_search_results", AsyncMock(return_value=([SimpleNamespace(file_name="Movie")], "", 1)))
    monkeypatch.setattr(pmf, "build_search_buttons", AsyncMock(return_value=[]))
    monkeypatch.setattr(pmf, "get_poster", AsyncMock(return_value=poster))
    monkeypatch.setattr(pmf, "delete_later", lambda *args: scheduled.append(args))
    monkeypatch.setattr(pmf.temp, "GETALL", {})
    monkeypatch.setattr(pmf.temp, "SHORT", {})
    monkeypatch.setattr(pmf.temp, "IMDB_CAP", {})
    monkeypatch.setattr(pmf, "FRESH", {})

    asyncio.run(asyncio.wait_for(pmf.auto_filter(None, message), 1))
    progress.delete.assert_awaited_once()
    result.delete.assert_not_awaited()
    message.delete.assert_not_awaited()
    assert scheduled == ([(pmf.DELETE_TIME, result, message)] if auto_delete is not False else [])
    if auto_delete is None:
        pmf.save_group_settings.assert_awaited_once_with(123, "auto_delete", True)
    if poster_mode == "off":
        pmf.get_poster.assert_not_awaited()
    else:
        message.reply_photo.assert_awaited()


def test_poster_and_spell_imdb_calls_run_off_loop_and_are_cached(monkeypatch):
    import utils
    from dreamxbotz.util.async_cache import AsyncTTLCache

    main_thread = threading.get_ident()
    calls = []

    def blocking_poster(*args):
        assert threading.get_ident() != main_thread
        calls.append(args)
        return {"title": "Movie", "poster": "cover.jpg"}

    def blocking_search(query):
        assert threading.get_ident() != main_thread
        calls.append(query)
        return [{"title": "Movie"}]

    monkeypatch.setattr(utils, "_poster_cache", AsyncTTLCache(64, 300))
    monkeypatch.setattr(utils, "_get_poster_sync", blocking_poster)
    monkeypatch.setattr(utils, "imdb", SimpleNamespace(search_movie=blocking_search))

    async def scenario():
        posters = await asyncio.gather(*(utils.get_poster("movie") for _ in range(10)))
        assert all(p["poster"] == "cover.jpg" for p in posters)
        assert len(calls) == 1
        assert await utils.search_imdb_titles("movie") == [{"title": "Movie"}]
        assert await utils.search_imdb_titles("movie") == [{"title": "Movie"}]
        assert len(calls) == 2
    asyncio.run(scenario())
