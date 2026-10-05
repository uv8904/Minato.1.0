import logging
import re
from datetime import datetime, timedelta

from pyrogram import Client, filters

from database.index_log_db import IST, index_store
from dreamxbotz.util.index_report import display_date, send_report
from info import ADMINS

logger = logging.getLogger(__name__)


@Client.on_message(filters.command("indexreport") & filters.user(ADMINS))
async def index_report_cmd(bot, message):
    """Daily Index Report preview (docs/DAILY_INDEX_REPORT.md).

    /indexreport              → aaj (aaj tak jitna index hua)
    /indexreport yesterday    → kal ki report
    /indexreport 2026-10-04   → us date ki report

    The report is delivered to the chat where the command is used, so admins
    can test the feature any time without waiting for the morning schedule.
    """
    parts = (message.text or "").split()
    arg = parts[1].strip().lower() if len(parts) > 1 else "today"
    now = datetime.now(IST)
    if arg in ("today", "aaj", "now"):
        date_str = now.strftime("%Y-%m-%d")
    elif arg in ("yesterday", "kal"):
        date_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg):
        date_str = arg
    else:
        return await message.reply(
            "<b>Usage:</b>\n"
            "/indexreport – aaj ki report\n"
            "/indexreport yesterday – kal ki report\n"
            "/indexreport 2026-10-04 – kisi specific date ki report"
        )
    status = await message.reply(
        f"⏳ Generating index report for <code>{display_date(date_str)}</code> …"
    )
    stats = await index_store.stats_for_date(date_str)
    if not stats.get("files"):
        return await status.edit(
            f"ℹ️ <b>{display_date(date_str)}</b> ko koi file index nahi hui."
        )
    sent = await send_report(bot, date_str, chat_id=message.chat.id, store=index_store)
    if sent:
        await status.delete()
    else:
        await status.edit("⚠️ Report generate hui par Telegram par bhejte waqt error aya – logs check karein.")
