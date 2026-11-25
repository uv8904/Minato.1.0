"""Search must not hand out random files or loop forever on spell-fixes.

* A stop-word-only message ("bro") used to reduce to "" whose regex "."
  matches EVERY indexed file.
* A corrected title containing a stripped stop-word ("The New Mutants" ->
  "the mutants") bounced between correction and search without any depth cap.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import plugins.pmfilter as pmf
from database.ia_filterdb import get_search_results


def test_empty_query_returns_nothing_instead_of_every_file():
    async def scenario():
        files, offset, total = await get_search_results(None, "")
        assert (files, offset, total) == ([], "", 0)
        files, offset, total = await get_search_results(None, "   ")
        assert (files, offset, total) == ([], "", 0)

    asyncio.run(scenario())


def test_stopword_only_message_is_not_a_search(monkeypatch):
    search = AsyncMock()
    monkeypatch.setattr(pmf, "get_search_results", search)
    message = SimpleNamespace(text="bro", reply_text=AsyncMock())

    asyncio.run(pmf.auto_filter(None, message))

    search.assert_not_awaited()
    message.reply_text.assert_not_awaited()


def _spell_env(monkeypatch, corrected="The New Mutants"):
    ai = AsyncMock(return_value=corrected)
    chok = AsyncMock()
    monkeypatch.setattr(pmf, "ai_spell_check", ai)
    monkeypatch.setattr(pmf, "advantage_spell_chok", chok)
    monkeypatch.setattr(pmf, "is_premium_locked_query", AsyncMock(return_value=None))
    monkeypatch.setattr(pmf, "get_search_results", AsyncMock(return_value=([], "", 0)))
    monkeypatch.setattr(pmf, "get_settings", AsyncMock(return_value={"spell_check": True}))
    ai_sts = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
    progress = SimpleNamespace(delete=AsyncMock(), edit=AsyncMock(return_value=ai_sts))
    message = SimpleNamespace(
        id=5, text="the mutants", chat=SimpleNamespace(id=1),
        from_user=SimpleNamespace(id=2), reply_text=AsyncMock(return_value=progress),
    )
    return message, ai, chok


def test_spellfix_recursion_is_capped(monkeypatch):
    message, ai, chok = _spell_env(monkeypatch)

    asyncio.run(pmf.auto_filter(None, message))

    # depth 0 and 1 may correct; depth 2 must fall through to the no-results
    # screen instead of looping (the old code had no cap at all).
    assert ai.await_count == 2
    chok.assert_awaited_once()
    assert message.reply_text.await_count == 3


def test_depth_cap_skips_correction_entirely(monkeypatch):
    message, ai, chok = _spell_env(monkeypatch)

    asyncio.run(pmf.auto_filter(None, message, _spell_depth=2))

    ai.assert_not_awaited()
    chok.assert_awaited_once()


def test_anonymous_group_message_does_not_crash_the_filter(monkeypatch):
    update = AsyncMock()
    monkeypatch.setattr(pmf.mdb, "update_top_messages", update)
    message = SimpleNamespace(from_user=None, text="some movie", chat=SimpleNamespace(id=1))

    asyncio.run(pmf.give_filter(None, message))

    update.assert_not_awaited()
