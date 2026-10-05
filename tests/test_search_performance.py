"""Offline regressions for search speed without changing matching or UI features."""
import asyncio
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dreamxbotz.util.async_cache import AsyncTTLCache


class Cursor:
    def __init__(self, model, query):
        self.model, self.query = model, query
        self.offset, self.size = 0, 10

    def sort(self, field, direction):
        assert (field, direction) == ("$natural", -1)
        return self

    def skip(self, offset):
        self.offset = offset
        return self

    def limit(self, size):
        assert size > 0  # Mongo limit(0) would mean unlimited!
        self.size = size
        return self

    async def to_list(self, length):
        self.model.reads += 1
        if self.model.read_started:
            self.model.read_started.set()
            await self.model.count_started.wait()
        return self.model.matches(self.query)[self.offset:self.offset + self.size]


class Model:
    def __init__(self, names):
        self.rows = [SimpleNamespace(file_name=n, file_type="video", caption="", file_id=n) for n in names]
        self.counts = self.reads = 0
        self.count_started = self.read_started = None

    def matches(self, query):
        def match(row, clause):
            for key, value in clause.items():
                if key == "$or":
                    if not any(match(row, c) for c in value):
                        return False
                elif hasattr(value, "search"):
                    if not value.search(getattr(row, key) or ""):
                        return False
                elif getattr(row, key) != value:
                    return False
            return True
        return [row for row in self.rows if match(row, query)]

    async def count_documents(self, query):
        self.counts += 1
        if self.count_started:
            self.count_started.set()
            await self.read_started.wait()
        return len(self.matches(query))

    def find(self, query):
        return Cursor(self, query)


@pytest.fixture
def store(monkeypatch):
    from database import ia_filterdb as mod
    primary = Model([f"Movie {i}" for i in range(5)])
    secondary = Model([f"Movie {i}" for i in range(5, 9)])
    monkeypatch.setattr(mod, "Media", primary)
    monkeypatch.setattr(mod, "Media2", secondary)
    monkeypatch.setattr(mod, "MULTIPLE_DB", False)
    monkeypatch.setattr(mod, "USE_CAPTION_FILTER", False)
    monkeypatch.setattr(mod, "_search_pages", AsyncTTLCache(128, 30))
    monkeypatch.setattr(mod, "_search_counts", AsyncTTLCache(128, 30))
    return mod, primary, secondary


def test_count_and_fetch_overlap_and_repeated_page_needs_no_db(store):
    mod, primary, secondary = store

    async def scenario():
        primary.count_started, primary.read_started = asyncio.Event(), asyncio.Event()
        result = await asyncio.wait_for(mod.get_search_results(None, "Movie", max_results=3), 1)
        assert (result[1], result[2]) == (3, 5)
        # The caller may modify the list without damaging cached results.
        result[0].clear()
        again = await mod.get_search_results(None, "Movie", max_results=3)
        assert len(again[0]) == 3
        page2 = await mod.get_search_results(None, "Movie", max_results=3, offset=3)
        assert (len(page2[0]), page2[1], page2[2]) == (2, "", 5)
        assert (primary.counts, primary.reads) == (1, 2)
        assert (secondary.counts, secondary.reads) == (0, 0)
    asyncio.run(scenario())


def test_identical_concurrent_queries_share_work(store):
    mod, primary, _ = store

    async def scenario():
        results = await asyncio.gather(*(mod.get_search_results(None, "Movie") for _ in range(20)))
        assert all(r[2] == 5 for r in results)
        assert (primary.counts, primary.reads) == (1, 1)
    asyncio.run(scenario())


@pytest.mark.parametrize("primary_size", [0, 2, 3, 5, 6, 9])
def test_dual_database_pagination_does_not_skip_or_duplicate(store, monkeypatch, primary_size):
    mod, primary, secondary = store
    monkeypatch.setattr(mod, "MULTIPLE_DB", True)
    primary.rows = Model([f"Movie {i}" for i in range(primary_size)]).rows
    secondary.rows = Model([f"Movie {i}" for i in range(primary_size, 12)]).rows

    async def scenario():
        offset, names = 0, []
        while offset != "":
            files, offset, total = await mod.get_search_results(None, "Movie", max_results=3, offset=offset)
            assert total == 12
            names.extend(f.file_name for f in files)
        assert names == [f"Movie {i}" for i in range(12)]
        assert primary.counts == secondary.counts == 1
        # Full primary pages do not query the secondary cursor at all.
        assert secondary.reads == (12 - primary_size + 2) // 3
    asyncio.run(scenario())


def test_matching_flags_types_lists_and_page_sizes_are_isolated(store, monkeypatch):
    mod, primary, _ = store
    primary.rows[0].caption = "Special"
    primary.rows[1].file_type = "audio"

    async def scenario():
        assert (await mod.get_search_results(None, "Special"))[2] == 0
        monkeypatch.setattr(mod, "USE_CAPTION_FILTER", True)
        assert (await mod.get_search_results(None, "Special"))[2] == 1
        assert (await mod.get_search_results(None, "Movie", file_type="audio"))[2] == 1
        assert (await mod.get_search_results(None, ["Movie 0", "Movie 2"]))[2] == 2
        assert await mod.get_search_results(None, [" "]) == ([], "", 0)
        assert await mod.get_search_results(None, "[") == ([], "", 0)
        settings = {1: {"max_btn": True}, 2: {"max_btn": False}}
        monkeypatch.setattr(mod, "get_settings", AsyncMock(side_effect=lambda chat: settings[chat]))
        monkeypatch.setattr(mod, "MAX_B_TN", "2")
        assert len((await mod.get_search_results(1, "Movie"))[0]) == 5
        assert len((await mod.get_search_results(2, "Movie"))[0]) == 2
    asyncio.run(scenario())


def test_invalidation_refreshes_empty_and_populated_results(store):
    mod, primary, _ = store

    async def scenario():
        assert (await mod.get_search_results(None, "New"))[2] == 0
        primary.rows.extend(Model(["New"]).rows)
        mod.invalidate_search_cache()
        assert (await mod.get_search_results(None, "New"))[2] == 1
        primary.rows.clear()
        mod.invalidate_search_cache()
        assert (await mod.get_search_results(None, "New"))[2] == 0
    asyncio.run(scenario())


def test_ttl_lru_disabled_cache_and_exceptions(monkeypatch):
    import dreamxbotz.util.async_cache as module
    now = [0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0])

    async def scenario():
        load = AsyncMock(return_value="value")
        cache = AsyncTTLCache(2, 10)
        for key in ["a", "b", "a", "c"]:
            await cache.get(key, load)
        assert list(cache._values) == ["a", "c"]
        assert load.await_count == 3
        now[0] = 11
        await cache.get("a", load)
        assert load.await_count == 4
        disabled = AsyncTTLCache(0, 10)
        await disabled.get("x", load)
        await disabled.get("x", load)
        assert load.await_count == 6
        fail = AsyncMock(side_effect=RuntimeError("offline"))
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cache.get("bad", fail)
        assert fail.await_count == 2
        assert not cache._pending
    asyncio.run(scenario())


def test_cancelled_waiter_does_not_cancel_shared_load():
    async def scenario():
        cache = AsyncTTLCache()
        started, finish = asyncio.Event(), asyncio.Event()

        async def load():
            started.set()
            await finish.wait()
            return 42

        first = asyncio.create_task(cache.get("x", load))
        await started.wait()
        second = asyncio.create_task(cache.get("x", load))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        finish.set()
        assert await second == 42
        assert await cache.get("x", AsyncMock(side_effect=AssertionError)) == 42
    asyncio.run(scenario())


def test_clear_during_inflight_read_cannot_repopulate_stale_cache():
    async def scenario():
        cache = AsyncTTLCache()
        started, finish = asyncio.Event(), asyncio.Event()

        async def old_load():
            started.set()
            await finish.wait()
            return "old"

        old = asyncio.create_task(cache.get("x", old_load))
        await started.wait()
        cache.clear()
        assert await cache.get("x", AsyncMock(return_value="new")) == "new"
        finish.set()
        assert await old == "old"
        assert await cache.get("x", AsyncMock(side_effect=AssertionError)) == "new"
    asyncio.run(scenario())


def test_all_raw_index_deletes_invalidate_before_next_await():
    root = Path(__file__).resolve().parents[1]
    for name in ["plugins/commands.py", "plugins/files_delete.py", "plugins/pmfilter.py"]:
        tree = ast.parse((root / name).read_text())
        for parent in ast.walk(tree):
            for _, body in ast.iter_fields(parent):
                if not isinstance(body, list):
                    continue
                for index, stmt in enumerate(body):
                    if not isinstance(stmt, (ast.Assign, ast.Expr)):
                        continue
                    value = stmt.value
                    if not isinstance(value, ast.Await) or not isinstance(value.value, ast.Call):
                        continue
                    func = value.value.func
                    if (isinstance(func, ast.Attribute) and func.attr in {"delete_one", "delete_many", "drop"}
                            and ast.unparse(func.value) in {"Media.collection", "Media2.collection"}):
                        assert ast.unparse(body[index + 1]) == "invalidate_search_cache()"


def test_save_file_invalidates_after_successful_commit(store, monkeypatch):
    mod, _, _ = store
    record = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(mod, "Media", lambda **kwargs: record)
    monkeypatch.setattr(mod, "unpack_new_file_id", lambda _: ("id", "ref"))
    calls = []
    monkeypatch.setattr(mod, "invalidate_search_cache", lambda: calls.append(True))
    monkeypatch.setattr(mod, "notify_new_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "record_indexed_file", AsyncMock())
    media = SimpleNamespace(file_id="id", file_name="Movie", file_size=100, file_type="video", mime_type="video/mp4", caption=None)
    assert asyncio.run(mod.save_file(media)) == (True, 1)
    assert calls == [True]
    record.commit.side_effect = RuntimeError("database unavailable")
    assert asyncio.run(mod.save_file(media)) == (False, 3)
    assert calls == [True]
