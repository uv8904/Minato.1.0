"""
Premium Exclusive / Early Access content — admin controls + user notify flow.

Owner/Admin marks a movie/series keyword as premium-only with
``/add_premium_content <keyword>``.  While a title is locked:

* **Premium users** search it and get the files instantly (results carry the
  💎 Early Access badge).
* **Normal users** see a lock screen explaining that the title is available
  for Premium users right now and for everyone later — "owner jab khele tab" —
  with TWO unlock options: (1) 🆓 **free shortlink verification** (the same
  shortener configured in ``info.py`` / group settings) which unlocks this
  title instantly, and (2) 💎 **Buy Premium** for instant access to everything
  (plus a free-trial button and a 🔔 notify-me button).
* ``/release_premium <keyword>`` (or a scheduled ``/schedule_release``) makes
  it public for everybody and PMs everyone who tapped the 🔔 button.

Commands (admin only):
  /add_premium_content <keyword> [display title]  — lock a title
  /del_premium_content <keyword>                  — remove the lock entirely
  /release_premium <keyword>                      — release to all users now
  /schedule_release <keyword> <minutes>           — auto-release after N min
  /premium_content                                — list all locked/released
  /notify_premium_release <keyword>               — PM waiting users now

Note: this plugin is deliberately named with a capital P so it is imported
before ``plugins/pmfilter.py``'s catch-all callback handler (pyrogram runs the
first matching handler per group).
"""
import asyncio
import base64
import datetime
import html
import logging
import random
import re
import string

from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message

from info import ADMINS, PREMIUM_CONTENT_ENABLED, TUTORIAL
from Script import script
from database.premium_content_db import premium_content_store
from database.users_chats_db import db
from dreamxbotz.util.buttons import blue, green, red
from utils import get_shortlink, temp

logger = logging.getLogger(__name__)

# keyword -> asyncio.Task, so a scheduled release survives "normal" flow
_SCHEDULES = {}


def _is_admin(user) -> bool:
    if not user:
        return False
    uid = user.id
    for admin in ADMINS:
        try:
            if int(admin) == int(uid):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _norm(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").strip().lower())


# ------------------------------------------------------------------ #
# Free shortlink-verification unlock (option 2 on the lock screen)
# ------------------------------------------------------------------ #
# Deep-link payload:  /start pcv_<b64url(user_id_verify_id_keyword)>
# The verify_id is created with db.create_verify_id() when the user taps
# "✅ verify now" and consumed by the /start handler below — same
# verification system the file-verify flow already uses.
VERIFY_PAYLOAD_PREFIX = "pcv_"


def build_verify_payload(user_id: int, verify_id: str, keyword: str) -> str:
    """Pure helper: pack (user_id, verify_id, keyword) into a /start payload."""
    raw = f"{int(user_id)}_{verify_id}_{keyword}".encode("utf-8")
    return VERIFY_PAYLOAD_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_verify_payload(payload: str):
    """Inverse of :func:`build_verify_payload`. Returns (user_id, verify_id,
    keyword) or None for anything malformed / tampered."""
    if not isinstance(payload, str) or not payload.startswith(VERIFY_PAYLOAD_PREFIX):
        return None
    body = payload[len(VERIFY_PAYLOAD_PREFIX):]
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", body):
        return None
    body += "=" * (-len(body) % 4)
    try:
        raw = base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8")
        uid_s, verify_id, keyword = raw.split("_", 2)
        user_id = int(uid_s)
    except Exception:
        return None
    # verify_id is always 7 chars of [A-Z0-9] (see premium_unlock_callback);
    # keyword must be plain printable text — anything else is tampering.
    if user_id <= 0 or not re.fullmatch(r"[A-Z0-9]{7}", verify_id):
        return None
    if not keyword or not keyword.isprintable():
        return None
    return user_id, verify_id, keyword


async def _resolve_locked_doc(keyword: str):
    """Exact lookup first, then prefix match (buttons truncate to 30/50 chars).

    Returns (doc_or_None, full_keyword)."""
    doc = await premium_content_store.get_content(keyword)
    if doc:
        return doc, keyword
    try:
        all_locked = await premium_content_store.list_contents(include_released=False, limit=200)
    except Exception:
        all_locked = []
    for item in all_locked or []:
        kw = item.get("keyword") or item.get("_id") or ""
        if kw.startswith(keyword):
            return item, kw
    return None, keyword


async def _send_release_notice(client, doc, keyword_norm):
    """PM everyone who asked for a notification when this title goes public."""
    title = doc.get("display_title") or keyword_norm
    try:
        subs = await premium_content_store.get_subscribers(keyword_norm)
    except Exception:
        subs = []
    text = script.PREMIUM_RELEASE_TXT.format(
        title=title,
        keyword=keyword_norm,
    )
    buttons = InlineKeyboardMarkup([[
        green("🔍 sᴇᴀʀᴄʜ ɴᴏᴡ", switch_inline_query_current_chat=keyword_norm)
    ]])
    sent = 0
    for uid in subs or []:
        try:
            await client.send_message(
                chat_id=int(uid),
                text=text,
                reply_markup=buttons,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
            sent += 1
        except Exception as exc:
            logger.debug("release notify to %s failed: %s", uid, exc)
        await asyncio.sleep(0.1)  # flood-safe
    try:
        await premium_content_store.clear_subscribers(keyword_norm)
    except Exception:
        pass
    return sent, len(subs or [])


async def _schedule_release_task(client, keyword_norm: str, delay_seconds: float):
    await asyncio.sleep(max(0, delay_seconds))
    try:
        doc = await premium_content_store.get_content(keyword_norm)
        if doc and not doc.get("is_released"):
            doc = await premium_content_store.release_content(keyword_norm)
            if doc:
                sent, total = await _send_release_notice(client, doc, keyword_norm)
                for admin in ADMINS:
                    try:
                        await client.send_message(
                            chat_id=int(admin),
                            text=(
                                f"<b>⏰ Auto-Release Done</b>\n\n"
                                f"🎬 <b>{doc.get('display_title') or keyword_norm}</b>\n"
                                f"👥 Notified: {sent}/{total} waiting user(s)"
                            ),
                            parse_mode=enums.ParseMode.HTML,
                        )
                    except Exception:
                        pass
    except Exception as exc:
        logger.warning("scheduled release failed for %s: %s", keyword_norm, exc)
    finally:
        _SCHEDULES.pop(keyword_norm, None)


# ------------------------------------------------------------------ #
# Admin commands
# ------------------------------------------------------------------ #
@Client.on_message(filters.command("add_premium_content") & filters.user(ADMINS))
async def add_premium_content(client, message):
    """Lock a movie/series keyword behind premium (early access)."""
    if not PREMIUM_CONTENT_ENABLED:
        return await message.reply_text("⚠️ Premium exclusive feature is disabled (PREMIUM_CONTENT_ENABLED=False).")
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        return await message.reply_text(
            "<b>Usage:</b>\n"
            "<code>/add_premium_content jawan</code>\n"
            "<code>/add_premium_content kalki 2898 ad Kalki 2898 AD (2024)</code>\n\n"
            "1st arg = search keyword to lock · rest = display title (optional)",
            parse_mode=enums.ParseMode.HTML,
        )
    keyword = parts[1].strip()
    display = parts[2].strip() if len(parts) > 2 else keyword
    try:
        doc = await premium_content_store.add_content(
            keyword=keyword,
            display_title=display,
            added_by=message.from_user.id,
            content_type="movie",
        )
    except ValueError as exc:
        return await message.reply_text(f"❌ {exc}")
    except Exception as exc:
        logger.exception("add_premium_content failed")
        return await message.reply_text(f"❌ Failed: <code>{exc}</code>", parse_mode=enums.ParseMode.HTML)
    await message.reply_text(
        script.PREMIUM_CONTENT_ADDED_TXT.format(
            title=doc.get("display_title") or keyword,
            keyword=doc.get("keyword") or keyword,
            hits=doc.get("hit_count", 0),
        ),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [green("🔓 ʀᴇʟᴇᴀsᴇ ɴᴏᴡ", callback_data=f"pcadm_rel#{(doc.get('keyword') or keyword)[:20]}")],
            [red("🗑 ʀᴇᴍᴏᴠᴇ", callback_data=f"pcadm_del#{(doc.get('keyword') or keyword)[:20]}")],
        ]),
    )
    try:
        from info import PREMIUM_LOGS
        await client.send_message(
            PREMIUM_LOGS,
            f"#PremiumExclusive\n\n🎬 <b>{doc.get('display_title') or keyword}</b>\n"
            f"🔑 <code>{doc.get('keyword') or keyword}</code>\n"
            f"By: {message.from_user.mention}",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        pass


@Client.on_message(filters.command("del_premium_content") & filters.user(ADMINS))
async def del_premium_content(client, message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply_text("Usage: <code>/del_premium_content keyword</code>", parse_mode=enums.ParseMode.HTML)
    keyword = parts[1].strip()
    task = _SCHEDULES.pop(_norm(keyword), None)
    if task:
        task.cancel()
    if await premium_content_store.remove_content(keyword):
        await message.reply_text(f"✅ Removed lock for <code>{keyword}</code> — ab normal users ko bhi mil jayega.", parse_mode=enums.ParseMode.HTML)
    else:
        await message.reply_text("❌ Not found.", parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command("release_premium") & filters.user(ADMINS))
async def release_premium(client, message):
    """Owner: release premium-exclusive title to everyone now."""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply_text("Usage: <code>/release_premium keyword</code>", parse_mode=enums.ParseMode.HTML)
    keyword = parts[1].strip()
    norm = _norm(keyword)
    task = _SCHEDULES.pop(norm, None)
    if task:
        task.cancel()
    doc = await premium_content_store.release_content(keyword)
    if not doc:
        return await message.reply_text("❌ Not found.", parse_mode=enums.ParseMode.HTML)
    sent, total = await _send_release_notice(client, doc, norm)
    await message.reply_text(
        f"🔓 <b>Released!</b> <b>{doc.get('display_title') or norm}</b> ab sab users ke liye available hai ✅\n"
        f"🔔 Waiting users notified: {sent}/{total}",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            green("🔍 sᴇᴀʀᴄʜ ɴᴏᴡ", switch_inline_query_current_chat=norm)
        ]]),
    )


@Client.on_message(filters.command("schedule_release") & filters.user(ADMINS))
async def schedule_release(client, message):
    """Auto-release after N minutes: /schedule_release keyword 60"""
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        return await message.reply_text(
            "Usage: <code>/schedule_release keyword minutes</code>\nExample: <code>/schedule_release jawan 60</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    keyword = parts[1].strip()
    try:
        minutes = int(parts[2].strip())
    except ValueError:
        return await message.reply_text("❌ minutes must be a number.")
    if minutes < 1:
        return await message.reply_text("❌ minutes must be >= 1.")
    doc = await premium_content_store.get_content(keyword)
    if not doc:
        return await message.reply_text("❌ Pehle ye keyword add karo: /add_premium_content <code>keyword</code>", parse_mode=enums.ParseMode.HTML)
    when = datetime.datetime.utcnow() + datetime.timedelta(minutes=minutes)
    try:
        await premium_content_store.schedule_release(keyword, when)
    except Exception as exc:
        return await message.reply_text(f"❌ {exc}")
    norm = _norm(keyword)
    old = _SCHEDULES.pop(norm, None)
    if old:
        old.cancel()
    _SCHEDULES[norm] = asyncio.create_task(_schedule_release_task(client, norm, minutes * 60))
    await message.reply_text(
        f"⏰ <b>{doc.get('display_title') or norm}</b> ko <b>{minutes} minute(s)</b> baad auto-release + notify kar diya jayega ✅\n"
        f"Premium users tab tak instant dekh sakte hain.",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("premium_content") & filters.user(ADMINS))
async def premium_content_list(client, message):
    """List all premium-exclusive items (locked + released)."""
    items = await premium_content_store.list_contents(include_released=True, limit=30)
    if not items:
        return await message.reply_text(
            "<b>💎 Premium Exclusive list empty.</b>\n\n"
            "Add one: <code>/add_premium_content jawan</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    lines = []
    for doc in items:
        status = "🔓 ᴘᴜʙʟɪᴄ" if doc.get("is_released") else "🔒 ᴘʀᴇᴍɪᴜᴍ ᴏɴʟʏ"
        title = doc.get("display_title") or doc.get("keyword")
        lines.append(
            f"• <b>{title}</b> — {status}\n"
            f"  🔑 <code>{doc.get('keyword')}</code> · 👁 {doc.get('hit_count', 0)} hits · 🔔 {len(doc.get('subscribers') or [])} waiting"
        )
    text = script.PREMIUM_CONTENT_LIST_TXT.format(
        count=len(items),
        items="\n".join(lines),
    )
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True)


@Client.on_message(filters.command("notify_premium_release") & filters.user(ADMINS))
async def notify_premium_release(client, message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply_text("Usage: <code>/notify_premium_release keyword</code>", parse_mode=enums.ParseMode.HTML)
    keyword = parts[1].strip()
    norm = _norm(keyword)
    doc = await premium_content_store.get_content(norm)
    if not doc:
        return await message.reply_text("❌ Not found.")
    sent, total = await _send_release_notice(client, doc, norm)
    await message.reply_text(f"🔔 Notification sent: {sent}/{total} waiting user(s).", parse_mode=enums.ParseMode.HTML)


# ------------------------------------------------------------------ #
# Callbacks
# ------------------------------------------------------------------ #
@Client.on_callback_query(filters.regex(r"^premium_notify#"))
async def premium_notify_callback(client: Client, query: CallbackQuery):
    """User tapped '🔔 notify when public' on the lock screen."""
    try:
        keyword = query.data.split("#", 1)[1].strip()
    except (IndexError, ValueError):
        return await query.answer()
    if not keyword:
        return await query.answer("⚠️ Invalid request", show_alert=True)
    # keyword was truncated to 30 chars in the button; shared prefix resolver
    doc, keyword = await _resolve_locked_doc(keyword)
    if not doc:
        return await query.answer("⚠️ Ye content ab locked nahi hai — search again.", show_alert=True)
    if doc.get("is_released"):
        return await query.answer("✅ Ye content already public hai! Search karo.", show_alert=True)
    saved = await premium_content_store.add_subscriber(keyword, query.from_user.id)
    if saved:
        return await query.answer(
            f"✅ Done! Jab '{doc.get('display_title') or keyword}' public hogi to PM me bata dunga 🔔",
            show_alert=True,
        )
    return await query.answer("⚠️ Could not save, try again.", show_alert=True)


@Client.on_callback_query(filters.regex(r"^pcunlock#"))
async def premium_unlock_callback(client: Client, query: CallbackQuery):
    """Lock screen option 1: FREE unlock via one shortlink verification.

    Shows the verify screen with a shortlink button (shortener from the chat's
    settings / ``info.py``), a how-to link and the Buy Premium button."""
    if not PREMIUM_CONTENT_ENABLED:
        return await query.answer("⚠️ Feature disabled.", show_alert=True)
    try:
        keyword = query.data.split("#", 1)[1].strip()
    except (IndexError, ValueError):
        return await query.answer()
    if not keyword:
        return await query.answer("⚠️ Invalid request", show_alert=True)
    doc, keyword = await _resolve_locked_doc(keyword)
    if not doc or doc.get("is_released"):
        return await query.answer("✅ Ye content ab public hai — search karo.", show_alert=True)
    user = query.from_user
    if not user:
        return await query.answer()
    if await premium_content_store.is_verified_user(keyword, user.id):
        return await query.answer("✅ Aap already verified ho — dubara search karo!", show_alert=True)

    verify_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=7))
    await db.create_verify_id(user.id, verify_id)
    payload = build_verify_payload(user.id, verify_id, keyword)
    deep_link = f"https://telegram.me/{temp.U_NAME}?start={payload}"
    chat_id = query.message.chat.id if query.message and query.message.chat else 0
    try:
        short_url = await get_shortlink(deep_link, chat_id)
    except Exception as exc:
        logger.warning("premium verify shortlink failed: %s", exc)
        return await query.answer(
            "⚠️ Shortlink service thodi der ke liye down hai — thodi der baad try karo, ya Premium lo 💎",
            show_alert=True,
        )
    title = html.escape(doc.get("display_title") or keyword)
    text = script.PREMIUM_VERIFY_TXT.format(title=title)
    buttons = InlineKeyboardMarkup([
        [green("✅ ᴠᴇʀɪꜰʏ ɴᴏᴡ", url=short_url)],
        [blue("📖 ʜᴏᴡ ᴛᴏ ᴠᴇʀɪꜰʏ?", url=TUTORIAL), green("💎 ʙᴜʏ ᴘʀᴇᴍɪᴜᴍ", callback_data="premium_info")],
        [red("🚫 ᴄʟᴏsᴇ", callback_data="close_data")],
    ])
    try:
        await query.message.edit_text(
            text,
            reply_markup=buttons,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        await query.message.reply_text(
            text,
            reply_markup=buttons,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_to_message_id=query.message.id,
        )
    return await query.answer()


@Client.on_message(filters.command("start") & filters.private & filters.regex(r"^/start\s+pcv_"))
async def premium_verify_complete(client: Client, message: Message):
    """Shortlink round-trip: validate the verify-id, unlock the title and run
    the search for it right away (same pattern as the msrch_ deep links).

    Registered in this module (capital P → before pmfilter/commands) so the
    payload is claimed before commands.py's generic /start handler."""
    payload = (message.text or "").split(None, 1)
    if len(payload) < 2:
        return await message.reply_text("<b>⚠️ ʟɪɴᴋ ɪɴᴠᴀʟɪᴅ ʜᴀɪ.</b>")
    parsed = parse_verify_payload(payload[1].strip())
    if not parsed:
        return await message.reply_text(
            "<b>⚠️ ʟɪɴᴋ ɪɴᴠᴀʟɪᴅ ʏᴀ ᴇxᴘɪʀᴇᴅ ʜᴀɪ.</b>\n\nᴅᴏʙᴀʀᴀ sᴇᴀʀᴄʜ ᴋᴀʀᴏ ᴀᴜʀ ɴᴀʏᴀ ᴠᴇʀɪꜰʏ ʟɪɴᴋ ʟᴏ।"
        )
    user_id, verify_id, keyword = parsed
    me = message.from_user
    if not me or me.id != user_id:
        return await message.reply_text(
            "<b>⚠️ ʏᴇ ᴠᴇʀɪꜰʏ ʟɪɴᴋ ᴀᴀᴘᴋᴇ ʟɪʏᴇ ɴᴀʜɪ ʜᴀɪ.</b>\n\nᴋʜᴜᴅ sᴇᴀʀᴄʜ ᴋᴀʀᴏ ᴀᴜʀ ᴋʜᴜᴅ ᴠᴇʀɪꜰʏ ᴋᴀʀᴏ ✅"
        )
    info = await db.get_verify_id_info(user_id, verify_id)
    if not info or info.get("verified"):
        return await message.reply_text("<b>ʟɪɴᴋ ᴇxᴘɪʀᴇᴅ ᴛʀʏ ᴀɢᴀɪɴ...</b>")
    await db.update_verify_id_info(user_id, verify_id, {"verified": True})
    try:
        await premium_content_store.add_verified_user(keyword, user_id)
    except Exception as exc:
        logger.warning("add_verified_user failed: %s", exc)
    doc = await premium_content_store.get_content(keyword)
    title = html.escape((doc or {}).get("display_title") or keyword)
    await message.reply_text(
        script.PREMIUM_UNLOCKED_TXT.format(title=title),
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )
    # Show the results for the unlocked title straight away.
    message.text = keyword
    from plugins.pmfilter import auto_filter  # lazy: avoid import cycles at load
    return await auto_filter(client, message)


@Client.on_callback_query(filters.regex(r"^pcadm_(rel|del)#"))
async def premium_content_admin_buttons(client: Client, query: CallbackQuery):
    """Inline admin shortcuts on the 'added' confirmation (admin only)."""
    if not _is_admin(query.from_user):
        return await query.answer("🚫 Admin only.", show_alert=True)
    try:
        action, keyword = query.data.split("#", 1)
    except ValueError:
        return await query.answer()
    keyword = keyword.strip()
    if action.endswith("rel"):
        doc = await premium_content_store.release_content(keyword)
        if not doc:
            return await query.answer("❌ Not found.", show_alert=True)
        norm = _norm(keyword)
        sent, total = await _send_release_notice(client, doc, norm)
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await query.answer(f"🔓 Released! Notified {sent}/{total}.", show_alert=True)
    # del
    task = _SCHEDULES.pop(_norm(keyword), None)
    if task:
        task.cancel()
    removed = await premium_content_store.remove_content(keyword)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.answer("🗑 Removed." if removed else "❌ Not found.", show_alert=True)


# ------------------------------------------------------------------ #
# Restart-safe release watcher — any doc whose release_at has passed is
# released (and waiting users notified) even if the in-memory schedule task
# was lost on a restart. Started once from bot.py.
# ------------------------------------------------------------------ #
async def process_due_releases(client) -> int:
    """Release every title whose ``release_at`` has passed. Returns the count."""
    released_count = 0
    if not PREMIUM_CONTENT_ENABLED:
        return 0
    now = datetime.datetime.utcnow()
    docs = await premium_content_store.list_contents(include_released=False, limit=200)
    for doc in docs:
        release_at = doc.get("release_at")
        if not release_at or release_at > now:
            continue
        norm = doc.get("keyword") or doc.get("_id") or ""
        if not norm:
            continue
        task = _SCHEDULES.pop(norm, None)
        if task:
            task.cancel()
        released = await premium_content_store.release_content(norm)
        if not released:
            continue
        released_count += 1
        sent, total = await _send_release_notice(client, released, norm)
        for admin in ADMINS:
            try:
                await client.send_message(
                    chat_id=int(admin),
                    text=(
                        f"<b>⏰ Scheduled Release Done</b>\n\n"
                        f"🎬 <b>{released.get('display_title') or norm}</b>\n"
                        f"👥 Notified: {sent}/{total} waiting user(s)"
                    ),
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception:
                pass
    return released_count


async def premium_release_watcher(client, interval: int = 60):
    while True:
        try:
            await process_due_releases(client)
        except Exception as exc:  # a DB hiccup must never kill the watcher
            logger.debug("premium release watcher pass failed: %s", exc)
        await asyncio.sleep(max(10, int(interval)))


# ------------------------------------------------------------------ #
# Startup helper — restore scheduled releases after a restart
# ------------------------------------------------------------------ #
@Client.on_message(filters.command("premium_content_status") & filters.user(ADMINS))
async def premium_content_status(client, message):
    total_locked = await premium_content_store.total_locked_count()
    total_released = await premium_content_store.total_released_count()
    await message.reply_text(
        f"💎 Premium Exclusive Status\n\n"
        f"🔒 Locked (premium only): {total_locked}\n"
        f"🔓 Released (public): {total_released}\n"
        f"⏰ Schedules in memory: {len(_SCHEDULES)}\n"
        f"⚙️ Feature: {'ON' if PREMIUM_CONTENT_ENABLED else 'OFF'}",
    )
