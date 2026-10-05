"""Offline tests for the Daily Index Report feature (docs/DAILY_INDEX_REPORT.md)."""
import asyncio
import io
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from database.index_log_db import IST, META_ID, IndexLogStore, ist_date_str
from dreamxbotz.util import index_report as report


class FakeCursor:
    """Motor-style async cursor over an in-memory list."""

    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, field, direction):
        reverse = direction < 0
        self.rows.sort(key=lambda r: r.get(field), reverse=reverse)
        return self

    def limit(self, size):
        self.rows = self.rows[:size]
        return self

    def __aiter__(self):
        async def gen():
            for row in self.rows:
                yield row

        return gen()


class FakeCollection:
    """Just enough of a motor collection for IndexLogStore."""

    def __init__(self):
        self.docs = []

    async def create_index(self, *args, **kwargs):
        return "fake"

    async def insert_one(self, doc):
        self.docs.append(doc)

    async def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if doc.get("_id") == query.get("_id"):
                doc.update(update.get("$set", {}))
                return SimpleNamespace(matched_count=1)
        if upsert:
            self.docs.append({"_id": query.get("_id"), **update.get("$set", {})})

    async def delete_many(self, query):
        cutoff = query["d"]["$lt"]
        before = len(self.docs)
        self.docs = [doc for doc in self.docs if doc.get("_id") == META_ID or doc.get("d", "") >= cutoff]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    async def find_one(self, query):
        for doc in self.docs:
            if doc.get("_id") == query.get("_id"):
                return dict(doc)
        return None

    def find(self, query, projection=None):
        return FakeCursor([dict(doc) for doc in self.docs if doc.get("d") == query.get("d")])

    def aggregate(self, pipeline):
        match = pipeline[0]["$match"]
        rows = [doc for doc in self.docs if doc.get("d") == match.get("d")]
        summary = {
            "_id": None,
            "files": len(rows),
            "size": sum(doc.get("s", 0) for doc in rows),
            "manual": sum(1 for doc in rows if doc.get("src") == "manual"),
        }
        return FakeCursor([summary] if rows else [])


@pytest.fixture
def store():
    return IndexLogStore(collection=FakeCollection(), collection_name="index_log")


def run(coro):
    return asyncio.run(coro)


def make_doc(n, s=100, t="video", db="Primary", src="manual", hour=10, minute=30, date="2026-10-04"):
    ts = datetime(2026, 10, 4, hour, minute, tzinfo=timezone.utc)
    return {"d": date, "n": n, "s": s, "t": t, "db": db, "src": src, "ts": ts}


# --------------------------------------------------------------------------- #
# Store behaviour
# --------------------------------------------------------------------------- #
def test_record_list_and_stats_roundtrip(store):
    async def scenario():
        when = datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)  # 19:30 IST → 04 Oct
        await store.record(file_name="A.mkv", file_size=1024, file_type="video",
                           db_name="Primary", source="manual", when=when)
        await store.record(file_name="B.mkv", file_size=2048, file_type="document",
                           db_name="Secondary", source="channel", when=when)
        # one file that belongs to another day must not leak into the bucket
        # (13:00 UTC on Oct 3 = 18:30 IST on Oct 3 → day-bucket "2026-10-03")
        other = datetime(2026, 10, 3, 13, 0, tzinfo=timezone.utc)
        await store.record(file_name="C.mkv", file_size=1, file_type="audio",
                           db_name="Primary", source="channel", when=other)
        day_docs = await store.list_for_date("2026-10-04")
        assert [doc["n"] for doc in day_docs] == ["A.mkv", "B.mkv"]
        stats = await store.stats_for_date("2026-10-04")
        assert stats == {"files": 2, "size": 3072, "manual": 1, "channel": 1}
        prev_docs = await store.list_for_date("2026-10-03")
        assert [doc["n"] for doc in prev_docs] == ["C.mkv"]

    run(scenario())


def test_report_meta_roundtrip_and_prune(store):
    async def scenario():
        assert await store.get_last_sent() is None
        await store.set_last_sent("2026-10-04")
        assert await store.get_last_sent() == "2026-10-04"
        await store.set_last_sent("2026-10-05")
        assert await store.get_last_sent() == "2026-10-05"
        await store.record(file_name="old.mkv", when=datetime(2026, 9, 20, tzinfo=timezone.utc))
        await store.record(file_name="new.mkv", when=datetime(2026, 10, 5, tzinfo=timezone.utc))
        removed = await store.prune_before("2026-10-01")
        assert removed == 1
        names = [doc.get("n") for doc in store.col.docs if doc.get("n")]
        assert names == ["new.mkv"] and await store.get_last_sent() == "2026-10-05"

    run(scenario())


def test_ist_date_str_buckets_by_kolkata_midnight():
    # 19:00 UTC = 00:30 IST next day
    assert ist_date_str(datetime(2026, 10, 4, 19, 0, tzinfo=timezone.utc)) == "2026-10-05"
    assert ist_date_str(datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)) == "2026-10-04"


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def test_format_summary_empty_and_filled():
    empty = report.format_summary("2026-10-04", [])
    assert "koi file index nahi hui" in empty and "04-10-2026" in empty
    docs = [make_doc("A", 1024, "video", src="manual"), make_doc("B", 2048, "document", db="Secondary", src="channel")]
    text = report.format_summary("2026-10-04", docs)
    assert "Total Files Indexed:</b> <code>2</code>" in text
    assert "Manual (/index):</b> <code>1</code>" in text
    assert "Channel:</b> <code>1</code>" in text
    assert "04-10-2026" in text


def test_list_lines_and_escaping():
    docs = [make_doc("<Movie> & Friends.mkv", 1536 * 1024 * 1024)]
    chunks = report.build_inline_chunks("2026-10-04", docs)
    assert len(chunks) == 1
    assert "&lt;Movie&gt; &amp; Friends.mkv" in chunks[0]
    assert "1. " in chunks[0] and "Primary" in chunks[0] and "manual" in chunks[0]


def test_inline_chunks_stay_under_telegram_limit():
    docs = [make_doc(f"File-{i}-" + "x" * 90) for i in range(200)]
    for chunk in report.build_inline_chunks("2026-10-04", docs):
        assert len(chunk) < 4096
    text = report.build_full_document("2026-10-04", docs).decode()
    assert "Total files: 200" in text and "200. File-199-" in text


def test_seconds_until_handles_rollover_and_past_times():
    now = datetime(2026, 10, 4, 7, 0, tzinfo=report.IST)
    assert report._seconds_until(8, 30, now) == 5400.0
    now = datetime(2026, 10, 4, 23, 0, tzinfo=report.IST)
    assert report._seconds_until(8, 30, now) == (9 * 3600 + 30 * 60)
    assert report._seconds_until(23, 0, now) == 86400.0  # already past → tomorrow


def test_report_send_time_parsing(monkeypatch):
    fake_info = SimpleNamespace(DAILY_INDEX_REPORT_TIME="06:45")
    monkeypatch.setitem(__import__("sys").modules, "info", fake_info)
    assert report.report_send_time() == (6, 45)
    fake_info.DAILY_INDEX_REPORT_TIME = "junk"
    assert report.report_send_time() == (8, 0)  # safe fallback


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
class FakeClient:
    def __init__(self, fail_channels=()):
        self.messages, self.documents = [], []
        self.fail_channels = set(fail_channels)

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.fail_channels:
            return None
        self.messages.append((chat_id, text))
        return SimpleNamespace(id=1)

    async def send_document(self, chat_id, document, caption=None, file_name=None, **kwargs):
        if chat_id in self.fail_channels:
            return None
        self.documents.append((chat_id, document, caption, file_name))
        return SimpleNamespace(id=2)


def test_send_report_small_day_is_inline(store):
    async def scenario():
        for i in range(3):
            await store.record(file_name=f"movie_{i}.mkv", file_size=i + 1,
                               file_type="video", db_name="Primary", source="manual",
                               when=datetime(2026, 10, 4, tzinfo=timezone.utc))
        client = FakeClient()
        ok = await report.send_report(client, "2026-10-04", chat_id=123, store=store)
        assert ok and not client.documents
        summary, listing = client.messages[0][1], client.messages[1][1]
        assert "Total Files Indexed:</b> <code>3</code>" in summary
        assert "movie_0.mkv" in listing and "movie_2.mkv" in listing

    run(scenario())


def test_send_report_big_day_sends_document(store):
    async def scenario():
        for i in range(report.INLINE_LIST_LIMIT + 1):
            await store.record(file_name=f"f{i}.mkv", when=datetime(2026, 10, 4, tzinfo=timezone.utc))
        client = FakeClient()
        ok = await report.send_report(client, "2026-10-04", chat_id=123, store=store)
        assert ok and len(client.documents) == 1
        chat_id, document, caption, file_name = client.documents[0]
        assert chat_id == 123 and file_name == "index_report_2026-10-04.txt"
        body = document.getvalue().decode()
        assert "Total files: 31" in body and "Full detailed list attached" in caption

    run(scenario())


def test_deliver_previous_day_sends_once_and_marks(store, monkeypatch):
    async def scenario():
        monkeypatch.setattr(report, "previous_day_date", lambda now=None: "2026-10-04")
        monkeypatch.setattr(report, "ist_now", lambda: datetime(2026, 10, 5, 8, 0, tzinfo=report.IST))
        await store.record(file_name="a.mkv", when=datetime(2026, 10, 4, tzinfo=timezone.utc))
        client = FakeClient()
        assert await report.deliver_previous_day(client, store=store) is True
        assert client.messages and await store.get_last_sent() == "2026-10-04"
        # second call must skip – the report for that day was already sent
        assert await report.deliver_previous_day(client, store=store) is False
        assert len(client.messages) + len(client.documents) <= 2

    run(scenario())


def test_deliver_failure_is_retried_later(store, monkeypatch):
    async def scenario():
        monkeypatch.setattr(report, "previous_day_date", lambda now=None: "2026-10-04")
        await store.record(file_name="a.mkv", when=datetime(2026, 10, 4, tzinfo=timezone.utc))
        client = FakeClient(fail_channels={999})
        assert await report.send_report(client, "2026-10-04", chat_id=999, store=store) is False
        # failed → not marked sent → next run may retry
        assert await store.get_last_sent() is None

    run(scenario())


# --------------------------------------------------------------------------- #
# save_file hook wiring
# --------------------------------------------------------------------------- #
def test_save_file_records_indexed_files(monkeypatch):
    from database import ia_filterdb as mod

    record = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(mod, "Media", lambda **kwargs: record)
    monkeypatch.setattr(mod, "unpack_new_file_id", lambda _: ("id", "ref"))
    monkeypatch.setattr(mod, "invalidate_search_cache", lambda: None)
    monkeypatch.setattr(mod, "notify_new_file", lambda *a, **k: None)
    log_calls = []
    monkeypatch.setattr(mod, "record_indexed_file",
                        lambda media, name, db, src: log_calls.append((name, db, src)) or asyncio.sleep(0))
    media = SimpleNamespace(file_id="id", file_name="Movie", file_size=100,
                            file_type="video", mime_type="video/mp4", caption=None)
    assert asyncio.run(mod.save_file(media, source="manual")) == (True, 1)
    assert log_calls == [("Movie", "Primary", "manual")]
    # failures during commit must not log anything
    record.commit.side_effect = RuntimeError("db down")
    assert asyncio.run(mod.save_file(media)) == (False, 3)
    assert log_calls == [("Movie", "Primary", "manual")]


def test_record_indexed_file_swallows_errors(monkeypatch):
    from database import ia_filterdb as mod

    monkeypatch.setattr(mod, "DAILY_INDEX_REPORT", True)

    async def boom(**kwargs):
        raise RuntimeError("mongo down")

    fake_store = SimpleNamespace(record=boom)
    import database.index_log_db as log_mod

    monkeypatch.setattr(log_mod, "index_store", fake_store)
    media = SimpleNamespace(file_name="X", file_size=1, file_type="video")
    # must not raise even when the store blows up
    asyncio.run(mod.record_indexed_file(media, "X", "Primary", "manual"))
    # and disabled flag short-circuits entirely
    monkeypatch.setattr(mod, "DAILY_INDEX_REPORT", False)
    calls = []
    fake_store.record = lambda **kwargs: calls.append(kwargs) or asyncio.sleep(0)
    asyncio.run(mod.record_indexed_file(media, "X", "Primary", "manual"))
    assert calls == []
