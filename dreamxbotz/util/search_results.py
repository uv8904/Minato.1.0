"""Shared, Telegram-safe cinema-hub search-result presentation (no network deps)."""
from html import escape

BOX_TOP = "╔══════════════════════════╗"
BOX_TITLE = "🎬 CINEMA HUB 🎬"
BOX_SUB = "◆ SEARCH RESULTS ◆"
BOX_BOTTOM = "╚══════════════════════════╝"
DIAMOND_DIVIDER = "◇◆◇◆◇◆◇◆◇◆◇◆◇"
DEFAULT_BRAND = "Minato"


def compact(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def result_header(search, total, requester=None, elapsed=None):
    """Boxed CINEMA HUB header with title/files/time/requested-by metadata.

    requester is Telegram's HTML mention; all other inputs are plain text.
    """
    lines = [
        BOX_TOP,
        BOX_TITLE,
        BOX_SUB,
        BOX_BOTTOM,
        "",
        f"🎬 Title : <b>{escape(compact(search, 100))}</b>",
        f"📂 Total Files : <b>{int(total):,}</b>",
    ]
    if elapsed is not None:
        lines.append(f"⚡ Time Taken : <code>{escape(str(elapsed))}s</code>")
    lines += [
        f"👤 Requested By : {requester or 'Movie fan'}",
        "",
        DIAMOND_DIVIDER,
    ]
    return "\n".join(lines)


def result_footer(brand=None):
    """Minimal brand line closing the results."""
    name = escape(compact(brand or DEFAULT_BRAND, 40))
    return f"{DIAMOND_DIVIDER}\n<i>◆ Powered by {name} ◆</i>"


def result_files(rows, bot_username, chat_id, offset=0, button_mode=False, brand=None):
    """rows contain (file_id, cleaned filename, formatted size).

    offset is the zero-based database offset. Filenames are display-only: the
    original ID/deep-link payload is retained, so downloads keep working.
    """
    if button_mode:
        return (
            "\n<b>↓ Choose a file below</b>\n"
            "<i>Refine by quality, language or season.</i>\n\n"
            + result_footer(brand)
        )
    parts = ["\n<b>↓ Choose your download</b>\n"]
    for file_id, name, size in rows:
        url = f"https://telegram.me/{bot_username}?start=file_{chat_id}_{file_id}"
        parts.append(
            f"🔹 <b>[{escape(str(size))}]</b> "
            f"<a href='{escape(url, quote=True)}'>{escape(compact(name, 130))}</a>"
        )
    parts.append("")
    parts.append("<i>Tap a title to get the file.</i>")
    parts.append(result_footer(brand))
    return "\n".join(parts)
