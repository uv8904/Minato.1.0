"""Daily Index Report (docs/DAILY_INDEX_REPORT.md).

Everything indexed through ``database.ia_filterdb.save_file()`` is logged in
:mod:`database.index_log_db`.  This module turns one day-bucket into a neat
Telegram report and posts it to ``LOG_CHANNEL``:

* started at bot boot (``bot.py`` → :func:`start_daily_index_report`),
* fires once a day at ``DAILY_INDEX_REPORT_TIME`` (Asia/Kolkata, default 08:00),
* sends the **previous** day's list: every file with name, size, type, target
  DB, source (``/index`` vs channel) and index time,
* small days arrive as inline text, bigger days as a ``.txt`` document,
* startup catch-up: if the bot was offline at the scheduled time, the missed
  report is delivered right after the next boot (never delivered twice).
"""
import asyncio
import html
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from pyrogram.errors import FloodWait

from database.index_log_db import IST, IndexLogStore, index_store
from utils import get_size

logger = logging.getLogger(__name__)

#: Days with at most this many files are listed inline in the log channel;
#: longer lists go out as a ``.txt`` document instead (no message flooding).
INLINE_LIST_LIMIT = 30
#: Telegram messages may hold 4096 chars; keep slices comfortably below that.
TEXT_CHUNK_LIMIT = 3800

_TYPE_EMOJI = {"video": "🎬", "audio": "🎵", "document": "📄"}
_SRC_LABEL = {"manual": "manual", "channel": "channel"}


# --------------------------------------------------------------------------- #
# Configuration (read lazily so importing this module never needs live config)
# --------------------------------------------------------------------------- #
def _config(key: str, default):
    try:
        import info

        return getattr(info, key, default)
    except Exception:
        return default


def report_enabled() -> bool:
    return bool(_config("DAILY_INDEX_REPORT", True))


def log_channel() -> int:
    try:
        return int(_config("LOG_CHANNEL", 0))
    except Exception:
        return 0


def report_send_time() -> Tuple[int, int]:
    """``(hour, minute)`` parsed from ``DAILY_INDEX_REPORT_TIME`` ('08:00')."""
    raw = str(_config("DAILY_INDEX_REPORT_TIME", "08:00")).strip()
    try:
        hour_s, minute_s = raw.split(":")[:2]
        hour = min(max(int(hour_s), 0), 23)
        minute = min(max(int(minute_s), 0), 59)
        return hour, minute
    except Exception:
        logger.warning("Daily index report: bad DAILY_INDEX_REPORT_TIME '%s' – using 08:00", raw)
        return 8, 0


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def ist_now() -> datetime:
    return datetime.now(IST)


def previous_day_date(now: Optional[datetime] = None) -> str:
    return ((now or ist_now()) - timedelta(days=1)).strftime("%Y-%m-%d")


def display_date(date_str: str) -> str:
    """``2026-10-04`` → ``04-10-2026`` for humans."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")
    except Exception:
        return date_str


def _seconds_until(hour: int, minute: int, now: Optional[datetime] = None) -> float:
    """Seconds from ``now`` until the next daily ``hour:minute`` occurrence."""
    now = now or ist_now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _count_by(docs: List[Dict[str, Any]], field: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for doc in docs:
        key = str(doc.get(field) or "?")
        counts[key] = counts.get(key, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# Report formatting
# --------------------------------------------------------------------------- #
def format_summary(date_str: str, docs: List[Dict[str, Any]]) -> str:
    """The headline card of the report (HTML)."""
    pretty = display_date(date_str)
    if not docs:
        return (
            f"📊 <b>Daily Index Report — {pretty}</b>\n\n"
            "ℹ️ Kal koi file index nahi hui.\n"
            "<i>Index karte hi list yahan aani shuru ho jayegi. ✅</i>"
        )
    total_size = sum(int(doc.get("s") or 0) for doc in docs)
    by_source = _count_by(docs, "src")
    by_type = _count_by(docs, "t")
    by_db = _count_by(docs, "db")
    type_bits = "  |  ".join(
        f"{_TYPE_EMOJI.get(t, '📦')} <b>{t.title()}:</b> <code>{n}</code>"
        for t, n in sorted(by_type.items(), key=lambda kv: -kv[1])
    )
    db_bits = "  |  ".join(
        f"<b>{db} DB:</b> <code>{n}</code>"
        for db, n in sorted(by_db.items(), key=lambda kv: -kv[1])
    )
    db_bits = f"🗃 {db_bits}"
    return (
        f"📊 <b>Daily Index Report — {pretty}</b>\n\n"
        f"🗂 <b>Total Files Indexed:</b> <code>{len(docs)}</code>\n"
        f"💾 <b>Total Size:</b> <code>{get_size(total_size)}</code>\n"
        f"🛠 <b>Manual (/index):</b> <code>{by_source.get('manual', 0)}</code>  |  "
        f"📡 <b>Channel:</b> <code>{by_source.get('channel', 0)}</code>\n"
        f"{type_bits}\n"
        f"{db_bits}"
    )


def _list_line(i: int, doc: Dict[str, Any]) -> str:
    """One detailed line, e.g. ``7. Name.mkv │ 1.45 GB │ video │ Primary │ manual │ 14:32``."""
    name = str(doc.get("n") or "?")
    size = get_size(int(doc.get("s") or 0))
    ftype = str(doc.get("t") or "?")
    db = str(doc.get("db") or "?")
    src = _SRC_LABEL.get(str(doc.get("src") or ""), str(doc.get("src") or "?"))
    ts = doc.get("ts")
    if isinstance(ts, datetime):
        aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        clock = aware.astimezone(IST).strftime("%H:%M")
    else:
        clock = "--:--"
    return f"{i}. {name} │ {size} │ {ftype} │ {db} │ {src} │ {clock}"


def build_inline_chunks(date_str: str, docs: List[Dict[str, Any]]) -> List[str]:
    """HTML message chunks with the detailed list (each < 4096 chars)."""
    lines = [html.escape(_list_line(i, doc)) for i, doc in enumerate(docs, 1)]
    chunks: List[str] = []
    current = ""
    for line in lines:
        addition = line if not current else "\n" + line
        if current and len(current) + len(addition) > TEXT_CHUNK_LIMIT:
            chunks.append(current)
            current = line
        else:
            current += addition
    if current:
        chunks.append(current)
    total = len(chunks)
    pretty = display_date(date_str)
    return [
        f"📋 <b>Indexed Files — {pretty}</b> ({i}/{total})\n\n{chunk}"
        for i, chunk in enumerate(chunks, 1)
    ]


def build_full_document(date_str: str, docs: List[Dict[str, Any]]) -> bytes:
    """Plain-text ``.txt`` with the complete detailed list (any size)."""
    pretty = display_date(date_str)
    total_size = get_size(sum(int(doc.get("s") or 0) for doc in docs))
    parts = [
        f"DAILY INDEX REPORT  —  {pretty}",
        f"Total files: {len(docs)}   |   Total size: {total_size}",
        "=" * 72,
        "",
    ]
    parts.extend(_list_line(i, doc) for i, doc in enumerate(docs, 1))
    return "\n".join(parts).encode("utf-8")


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
async def _send(callable_, *args, **kwargs):
    """Send with FloodWait back-off; returns the message (or None on failure)."""
    for _ in range(3):
        try:
            return await callable_(*args, **kwargs)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception as e:
            logger.warning("Daily index report: send failed: %s", e)
            return None
    return None


async def send_report(
    client,
    date_str: str,
    chat_id: Optional[int] = None,
    store: Optional[IndexLogStore] = None,
) -> bool:
    """Build the report for ``date_str`` and post it to the log channel.

    Returns True when the summary message was delivered.
    """
    store = store or index_store
    chat_id = chat_id or log_channel()
    docs = await store.list_for_date(date_str)
    summary = format_summary(date_str, docs)
    if not docs:
        return await _send(client.send_message, chat_id, summary) is not None
    if len(docs) <= INLINE_LIST_LIMIT:
        pages = build_inline_chunks(date_str, docs)
        summary += "\n\n📋 Full list below ⬇️"
        head = await _send(client.send_message, chat_id, summary)
        if head is None:
            return False
        ok = True
        for page in pages:
            if await _send(client.send_message, chat_id, page, disable_web_page_preview=True) is None:
                ok = False
        return ok
    summary += "\n\n📋 Full detailed list attached ⬇️"
    document = io.BytesIO(build_full_document(date_str, docs))
    message = await _send(
        client.send_document,
        chat_id,
        document,
        caption=summary,
        file_name=f"index_report_{date_str}.txt",
    )
    return message is not None


async def deliver_previous_day(client, store: Optional[IndexLogStore] = None) -> bool:
    """Send yesterday's report once – skip when already delivered."""
    store = store or index_store
    yesterday = previous_day_date()
    if await store.get_last_sent() == yesterday:
        logger.info("Daily index report: %s already delivered, skipping.", yesterday)
        return False
    logger.info("Daily index report: delivering %s …", yesterday)
    sent = await send_report(client, yesterday, store=store)
    if sent:
        await store.set_last_sent(yesterday)
        await store.prune_before((ist_now() - timedelta(days=10)).strftime("%Y-%m-%d"))
        logger.info("Daily index report: %s delivered ✅", yesterday)
    return sent


# --------------------------------------------------------------------------- #
# Scheduler loop
# --------------------------------------------------------------------------- #
async def daily_report_loop(client, store: Optional[IndexLogStore] = None) -> None:
    """Fire the previous day's report every day at the configured IST time."""
    store = store or index_store
    hour, minute = report_send_time()
    logger.info(
        "Daily index report: scheduled for %02d:%02d IST → LOG_CHANNEL %s",
        hour, minute, log_channel(),
    )
    try:
        await store.ensure_indexes()
    except Exception:  # pragma: no cover - defensive
        logger.debug("Daily index report: index creation failed", exc_info=True)
    # Startup catch-up: the bot may have been offline when the report was due.
    try:
        if await deliver_previous_day(client, store):
            logger.info("Daily index report: delivered a missed report on startup ✅")
    except Exception:
        logger.exception("Daily index report: startup catch-up failed")
    while True:
        await asyncio.sleep(_seconds_until(hour, minute))
        try:
            await deliver_previous_day(client, store)
        except Exception:
            logger.exception("Daily index report: scheduled delivery failed")
            await asyncio.sleep(60)  # avoid a tight error loop


def start_daily_index_report(client, store: Optional[IndexLogStore] = None):
    """Register the background report task on the client's event loop."""
    if not report_enabled():
        logger.info("Daily index report disabled via DAILY_INDEX_REPORT=False")
        return None
    loop = getattr(client, "loop", None) or asyncio.get_event_loop()
    return loop.create_task(daily_report_loop(client, store))
