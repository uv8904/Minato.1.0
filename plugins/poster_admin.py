"""Admin commands for the Stream Mode posters ("Newly Uploaded Movies").

``/posters``            – why a poster is (not) showing: TMDB key, switches,
                          queue, the newest movies without artwork, how to fix
``/posters retry``      – forget failed lookups and queue them again
``/setposter <movie>``  – reply to a poster photo → that photo becomes the poster
``/setposter <movie> <https://…jpg>`` – poster from an allow-listed image host
``/delposter <movie>``  – remove a poster (placeholder again, worker may retry)

``<movie>`` is the ``MOVIE_ID`` shown by ``/posters`` (e.g. ``hmm-2024``) or
the title (``Hmm 2024``).  Admins only, private chat.  The logic lives in
``dreamxbotz/util/poster_admin.py``; see docs/NEWLY_UPLOADED_MOVIES.md.
"""
import logging

from pyrogram import Client, enums, filters

from info import ADMINS
from dreamxbotz.util import poster_admin

logger = logging.getLogger(__name__)

HTML = enums.ParseMode.HTML
USAGE_SETPOSTER = (
    "<b>Usage</b>\n"
    "• reply to a poster photo with <code>/setposter MOVIE_ID</code>\n"
    "• or <code>/setposter MOVIE_ID https://image.tmdb.org/t/p/w500/….jpg</code>\n"
    "• <code>/delposter MOVIE_ID</code> removes it again\n\n"
    "MOVIE_ID (e.g. <code>hmm-2024</code>) or the title (<code>Hmm 2024</code>) – "
    "<code>/posters</code> lists the movies without a poster."
)


@Client.on_message(filters.private & filters.command("posters") & filters.user(ADMINS))
async def posters_status(client, message):
    """``/posters`` → status report, ``/posters retry`` → re-queue lookups."""
    argument = message.command[1].strip().lower() if len(message.command) > 1 else ""
    try:
        if argument in ("retry", "refresh", "refetch"):
            reset, queued = await poster_admin.retry_missing()
            await message.reply_text(
                f"<b>🔁 Poster retry</b>\n{reset} movie(s) reset, {queued} lookup(s) queued. "
                "The worker resolves about one per second – check <code>/posters</code> in a minute.",
                parse_mode=HTML,
            )
            return
        await message.reply_text(
            await poster_admin.status_report(), parse_mode=HTML, disable_web_page_preview=True
        )
    except Exception as exc:
        logger.error("/posters failed: %s", exc, exc_info=True)
        await message.reply_text(f"<b>❗ /posters failed:</b> <code>{poster_admin.esc(exc)}</code>", parse_mode=HTML)


async def _pick_movie(message, target: str):
    """Resolve the typed target; answer the admin when it is ambiguous/unknown."""
    movie, candidates = await poster_admin.resolve_target(target)
    if movie:
        return movie
    if candidates:
        listing = "\n".join("• " + poster_admin.describe(doc) for doc in candidates[:8])
        await message.reply_text(
            f"<b>Which one?</b> Use the MOVIE_ID:\n{listing}", parse_mode=HTML
        )
    else:
        await message.reply_text(
            f"<b>Not found:</b> “{poster_admin.esc(target)}” is not in the newly-uploaded movies. "
            "Only movies indexed while the section was on are listed – "
            "<code>/posters</code> shows them.",
            parse_mode=HTML,
        )
    return None


@Client.on_message(filters.private & filters.command("setposter") & filters.user(ADMINS))
async def set_poster(client, message):
    """Attach a poster by hand (replied photo or https image URL)."""
    target, url = poster_admin.split_setposter_args(message.text or "")
    if not target:
        await message.reply_text(USAGE_SETPOSTER, parse_mode=HTML)
        return
    try:
        if url:
            ok, reason = poster_admin.validate_poster_url(url)
            if not ok:
                await message.reply_text(f"<b>❌ Link rejected:</b> {reason}", parse_mode=HTML)
                return
            poster_ref = url
        else:
            poster_ref, reason = poster_admin.telegram_poster_ref(message.reply_to_message)
            if not poster_ref:
                await message.reply_text(
                    f"<b>❌ No poster given:</b> {reason or 'reply to a photo or add an https:// link'}.\n\n"
                    + USAGE_SETPOSTER,
                    parse_mode=HTML,
                )
                return

        movie = await _pick_movie(message, target)
        if not movie:
            return
        movie_id = movie["_id"]
        if not await poster_admin.set_manual_poster(movie_id, poster_ref):
            await message.reply_text("<b>❗ Could not store the poster</b> (database error).", parse_mode=HTML)
            return
        source = "Telegram photo" if poster_ref.startswith("tg://") else "link"
        await message.reply_text(
            f"<b>✅ Poster set</b> for {poster_admin.describe(movie)} ({source}).\n"
            f"Preview: {poster_admin.poster_preview_url(movie_id)}\n"
            "The website picks it up on its next refresh; automatic lookups will not replace it.",
            parse_mode=HTML,
            disable_web_page_preview=False,
        )
    except Exception as exc:
        logger.error("/setposter failed: %s", exc, exc_info=True)
        await message.reply_text(f"<b>❗ /setposter failed:</b> <code>{poster_admin.esc(exc)}</code>", parse_mode=HTML)


@Client.on_message(filters.private & filters.command("delposter") & filters.user(ADMINS))
async def del_poster(client, message):
    """Remove a poster – the placeholder shows again and the worker may retry."""
    target, _ = poster_admin.split_setposter_args(message.text or "")
    if not target:
        await message.reply_text(USAGE_SETPOSTER, parse_mode=HTML)
        return
    try:
        movie = await _pick_movie(message, target)
        if not movie:
            return
        if await poster_admin.clear_manual_poster(movie["_id"]):
            await message.reply_text(
                f"<b>🗑 Poster removed</b> for {poster_admin.describe(movie)}. "
                "<code>/posters retry</code> looks it up again.",
                parse_mode=HTML,
            )
        else:
            await message.reply_text("<b>❗ Nothing removed</b> (database error).", parse_mode=HTML)
    except Exception as exc:
        logger.error("/delposter failed: %s", exc, exc_info=True)
        await message.reply_text(f"<b>❗ /delposter failed:</b> <code>{poster_admin.esc(exc)}</code>", parse_mode=HTML)
