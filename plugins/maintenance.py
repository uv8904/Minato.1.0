"""Maintenance mode (docs/MAINTENANCE_MODE.md).

Lets an admin shut the bot down for normal users with a single command:

    /maint on      -> maintenance mode ON  (persisted in Mongo)
    /maint off     -> maintenance mode OFF (bot is instantly back for everyone)
    /maint status  -> show the current state

While maintenance is ON:
  * only ADMINS can use the bot (messages, buttons, inline buttons included),
  * every normal user gets the "under maintenance" notice instead,
  * the state survives restarts (it is restored from Mongo in bot.py).

The blocker handlers run in group ``MAINT_GROUP`` (-200), i.e. before every
other handler in the repo (the lowest group used elsewhere is -10), so no
search/file/other plugin ever sees a non-admin update during maintenance.
When maintenance is OFF the filters below match nothing and the plugin is a
zero-cost no-op.
"""
import logging

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.users_chats_db import db
from info import ADMINS, LOG_CHANNEL, SUPPORT_CHAT
from Script import script
from utils import temp

logger = logging.getLogger(__name__)

# Runs before every other message/callback handler in the project.
MAINT_GROUP = -200

MAINT_ON_ARGS = {"on", "enable", "start", "true", "1"}
MAINT_OFF_ARGS = {"off", "disable", "stop", "false", "0"}


def is_admin_update(update) -> bool:
    """True when the update comes from a bot admin (id *or* username based)."""
    user = getattr(update, "from_user", None)
    if user is None:
        return False
    if user.id in ADMINS:
        return True
    username = getattr(user, "username", None)
    if not username:
        return False
    return username.lower() in {str(a).lower() for a in ADMINS if isinstance(a, str)}


async def maintenance_is_on(_, __, ___) -> bool:
    return bool(temp.MAINTENANCE)


maintenance_active = filters.create(maintenance_is_on)


def _group_message_wants_reply(message: Message) -> bool:
    """In groups only answer commands/mentions so the chat is not spammed.

    The message is blocked either way (stop_propagation below); this only
    decides whether the notice is visibly sent.
    """
    text = (message.text or message.caption or "")
    if text.startswith("/"):
        return True
    bot_username = temp.U_NAME
    return bool(bot_username) and f"@{bot_username}".lower() in text.lower()


@Client.on_message(filters.incoming & maintenance_active, group=MAINT_GROUP)
async def maintenance_block(bot, message: Message):
    """Block every non-admin update while maintenance mode is ON."""
    if is_admin_update(message):
        return  # admins keep full access – let the update fall through

    try:
        if message.chat.type == "private":
            buttons = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Sᴜᴘᴘᴏʀᴛ", url=SUPPORT_CHAT)]])
            mention = getattr(message.from_user, "mention", "ᴛʜᴇʀᴇ") if message.from_user else "ᴛʜᴇʀᴇ"
            await message.reply_text(
                script.MAINTENANCE_TXT.format(mention),
                reply_markup=buttons,
                disable_web_page_preview=True,
            )
        elif _group_message_wants_reply(message):
            await message.reply_text(script.MAINTENANCE_ALERT_TXT)
    except Exception as exc:
        logger.warning("Couldn't send the maintenance notice: %s", exc)

    # Swallow the update: search, files, every other handler stays silent.
    message.stop_propagation()


@Client.on_callback_query(maintenance_active, group=MAINT_GROUP)
async def maintenance_callback(bot, query: CallbackQuery):
    """Block inline-button presses from non-admins during maintenance."""
    if is_admin_update(query):
        return
    try:
        await query.answer(script.MAINTENANCE_ALERT_TXT, show_alert=True)
    except Exception:
        pass
    query.stop_propagation()


async def _notify_log_channel(bot, text: str):
    """Best-effort heads-up in LOG_CHANNEL; never breaks the toggle."""
    try:
        await bot.send_message(chat_id=LOG_CHANNEL, text=text)
    except Exception as exc:
        logger.warning("Couldn't post the maintenance notice to LOG_CHANNEL %s: %s", LOG_CHANNEL, exc)


@Client.on_message(filters.command(["maint", "maintenance"]) & filters.user(ADMINS))
async def maintenance_toggle(bot, message: Message):
    """Admin-only switch: /maint on | /maint off | /maint status."""
    arg = message.command[1].lower() if len(message.command) > 1 else ""

    if arg == "status":
        state = "🚧 ON – normal users are blocked" if temp.MAINTENANCE else "✅ OFF – everyone can use the bot"
        await message.reply_text(f"<b>Maintenance mode:</b> <code>{state}</code>")
        return

    if arg in MAINT_ON_ARGS:
        if temp.MAINTENANCE:
            await message.reply_text("Maintenance mode is already ON. Send <code>/maint off</code> to end it.")
            return
        temp.MAINTENANCE = True
        try:
            await db.set_maintenance_mode(True)
        except Exception as exc:
            logger.exception("Couldn't persist maintenance mode – it will still work for this run: %s", exc)
        await message.reply_text(script.MAINTENANCE_ON_TXT)
        await _notify_log_channel(bot, "🚧 <b>Maintenance mode turned ON</b> by an admin. Normal users are blocked until <code>/maint off</code>.")
        return

    if arg in MAINT_OFF_ARGS:
        if not temp.MAINTENANCE:
            await message.reply_text("Maintenance mode is already OFF.")
            return
        temp.MAINTENANCE = False
        try:
            await db.set_maintenance_mode(False)
        except Exception as exc:
            logger.exception("Couldn't persist maintenance mode – it will still work for this run: %s", exc)
        await message.reply_text(script.MAINTENANCE_OFF_TXT)
        await _notify_log_channel(bot, "✅ <b>Maintenance mode turned OFF</b> – the bot is back online for everyone.")
        return

    await message.reply_text(
        "<b>Usage:</b>\n"
        "<code>/maint on</code> – block normal users (maintenance notice)\n"
        "<code>/maint off</code> – bring the bot back for everyone\n"
        "<code>/maint status</code> – show current state"
    )
