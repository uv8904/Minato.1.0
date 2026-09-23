"""Shared, Telegram-safe search-result presentation (no network dependencies)."""
from html import escape
import re

DIVIDER = "━━━━━━━━━━━━━━━━━━"


def compact(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def result_header(search, total, requester, brand, elapsed=None):
    """requester is Telegram's HTML mention; all other inputs are plain text."""
    stats = f"📁 <b>{int(total):,}</b> files found"
    if elapsed is not None:
        stats += f"  ·  ⚡ {escape(str(elapsed))}s"
    return (
        f"🎬 <b>{escape(compact(search, 100))}</b>\n"
        f"{DIVIDER}\n{stats}\n"
        f"👤 {requester or 'Movie fan'}\n"
        f"<i>{escape(compact(brand or 'Minato', 60))}</i>"
    )


def result_files(rows, bot_username, chat_id, offset=0, button_mode=False):
    """rows contain (file_id, cleaned filename, formatted size).

    offset is the zero-based database offset, not the first visible number.
    Filenames are display-only: the original ID/deep-link payload is retained.
    """
    if button_mode:
        return f"\n{DIVIDER}\n<b>↓ Choose a file below</b>\n<i>Refine by quality, language or season.</i>"
    parts = [f"\n{DIVIDER}\n<b>↓ Choose your download</b>\n"]
    for number, (file_id, name, size) in enumerate(rows, start=offset + 1):
        quality = re.search(r"(?i)(?<!\w)(2160p|1080p|720p|480p|360p|4k)(?!\w)", name)
        details = escape(str(size))
        if quality:
            details += " · " + escape(quality.group().upper())
        url = f"https://telegram.me/{bot_username}?start=file_{chat_id}_{file_id}"
        parts.append(
            f"<b>{number:02d}.</b> <a href='{escape(url, quote=True)}'>{escape(compact(name, 130))}</a>\n"
            f"     <code>{details}</code>\n"
        )
    parts.append("<i>Tap a title to get the file.</i>")
    return "\n".join(parts)
