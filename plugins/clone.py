"""Private, owner-controlled /clone flow for BotFather-created bot tokens."""
from __future__ import annotations

import html
import logging
import re
import time

from pyrogram import Client, StopPropagationError, enums, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from info import CLONE_CHILD_MODE
from dreamxbotz.util.clone_service import get_clone_manager
from dreamxbotz.util.buttons import green

logger = logging.getLogger(__name__)
_PENDING_TTL_SECONDS = 10 * 60
_ATTEMPT_COOLDOWN_SECONDS = 30
_MAX_TOKEN_ATTEMPTS = 3
_MAX_PENDING_PROMPTS = 1000
_TOKEN_LIKE_RE = re.compile(r"(?<![A-Za-z0-9_-])\d{5,15}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")
_pending: dict[int, dict[str, float | int]] = {}
_last_attempt: dict[int, float] = {}


_TOKEN_HELP = (
    "<b>🤖 Create your own bot clone</b>\n\n"
    "1. Open <a href=\"https://t.me/BotFather\">@BotFather</a> and send <code>/newbot</code>.\n"
    "2. Finish BotFather's setup and copy the new bot's API token.\n"
    "3. Paste that token here as your next private message (not in a group).\n\n"
    "<b>Security:</b> the token controls your bot. This host uses it to run the clone, "
    "stores it encrypted, and deletes the token message after receipt. Only continue "
    "if you trust this service. You can stop the clone with <code>/deleteclone</code> "
    "and revoke its token with @BotFather. Send <code>/cancelclone</code> to cancel. "
    "Never send your Telegram login code or password."
)


def _manager(client):
    me = getattr(client, "me", None)
    return get_clone_manager(getattr(me, "id", None))


def _private(message) -> bool:
    return bool(message.chat and message.chat.type == enums.ChatType.PRIVATE)


async def _delete_sensitive_message(message) -> bool:
    try:
        return (await message.delete()) is not False
    except Exception:
        return False


async def _private_notice_and_stop(client, user_id: int, text: str, **kwargs) -> None:
    """Keep token-bearing updates out of every later handler/search/logger."""
    try:
        await client.send_message(user_id, text, **kwargs)
    except Exception:
        logger.warning("Could not send a clone-token safety notice to owner %s", user_id)
    raise StopPropagationError


async def _group_token_notice_and_stop(client, message, user_id, removed: bool) -> None:
    """Warn without repeating a token that someone may have posted in a group."""
    dm_sent = False
    if user_id is not None:
        try:
            notice = (
                "I removed that token-like message and did not use it."
                if removed
                else "I could not remove that message and did not use it. Revoke any exposed token with @BotFather immediately."
            )
            await client.send_message(user_id, notice)
            dm_sent = True
        except Exception:
            pass
    if not removed or not dm_sent:
        warning = (
            "I couldn't remove that message. Revoke any exposed bot token with @BotFather immediately."
            if not removed
            else "I couldn't DM you. Never post bot tokens in chats; use my private chat and revoke any token you exposed."
        )
        try:
            await message.reply_text(warning)
        except Exception:
            pass
    raise StopPropagationError


def _prune_expired_state(now: float) -> None:
    """Keep anonymous public /clone attempts from growing process-local maps."""
    for user_id, state in list(_pending.items()):
        if now >= float(state.get("expires", 0)):
            _pending.pop(user_id, None)
    for user_id, started in list(_last_attempt.items()):
        if now - started >= _ATTEMPT_COOLDOWN_SECONDS:
            _last_attempt.pop(user_id, None)


async def _preflight_reply(client, message, user_id: int) -> bool:
    if CLONE_CHILD_MODE:
        await message.reply_text("This is already a clone. Start cloning from the main bot instead.")
        return False
    manager = _manager(client)
    try:
        result = await manager.preflight(user_id)
    except Exception:
        logger.warning("Clone preflight failed", exc_info=True)
        await message.reply_text("Clone service is temporarily unavailable. Please try again later.")
        return False

    status = result["status"]
    if status == "disabled":
        await message.reply_text(
            "Clone creation is not configured on this server yet. No token was requested."
        )
        return False
    if status == "already_exists":
        clone = result.get("clone", {})
        name = clone.get("username")
        link = f"https://t.me/{name}" if name else ""
        text = (
            f"You already have a clone: <a href=\"{link}\">@{html.escape(name)}</a>.\n"
            "Use <code>/myclone</code> for its status, <code>/restartclone</code> to retry, "
            "or <code>/deleteclone</code> to remove it."
            if name
            else "You already have a clone record. Use <code>/myclone</code> or <code>/deleteclone</code>."
        )
        await message.reply_text(text, disable_web_page_preview=True)
        return False
    if status == "capacity":
        await message.reply_text("All clone slots are currently in use. Please try again later.")
        return False
    if status == "record_limit":
        await message.reply_text("The server has reached its saved-clone limit. Please contact support.")
        return False
    if status == "memory":
        await message.reply_text("The server is low on free RAM, so a clone cannot start safely right now.")
        return False
    if status == "session_error":
        await message.reply_text("Clone service is unavailable because its private session storage is not ready.")
        return False
    return True


async def _begin_clone(client, message, user_id: int) -> None:
    now = time.monotonic()
    _prune_expired_state(now)
    if user_id not in _last_attempt and len(_last_attempt) >= _MAX_PENDING_PROMPTS:
        await message.reply_text("Too many clone setups are in progress. Please try again shortly.")
        return
    last = _last_attempt.get(user_id, 0.0)
    if last and now - last < _ATTEMPT_COOLDOWN_SECONDS:
        remaining = int(_ATTEMPT_COOLDOWN_SECONDS - (now - last)) + 1
        await message.reply_text(f"Please wait {remaining}s before trying /clone again.")
        return
    if user_id not in _pending and len(_pending) >= _MAX_PENDING_PROMPTS:
        await message.reply_text("Too many clone setups are in progress. Please try again shortly.")
        return
    _last_attempt[user_id] = now
    if not await _preflight_reply(client, message, user_id):
        return
    _pending[user_id] = {"expires": now + _PENDING_TTL_SECONDS, "attempts": 0}
    await message.reply_text(
        _TOKEN_HELP,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🤖 Open @BotFather", url="https://t.me/BotFather")]]
        ),
        disable_web_page_preview=True,
    )


@Client.on_message(filters.command("clone") & filters.incoming, group=1)
async def clone_command(client, message):
    """Start a clone flow only in PM; never accept a token in command arguments."""
    sender = getattr(message, "from_user", None)
    owner_id = int(sender.id) if sender else None
    if len(getattr(message, "command", []) or []) > 1:
        # Be defensive even in groups: a token pasted as `/clone <token>` must
        # never be validated, and we try to remove the entire sensitive message.
        removed = False
        try:
            deletion_result = await message.delete()
            removed = deletion_result is not False
        except Exception:
            pass
        if _private(message):
            if not removed:
                return await message.reply_text(
                    "I couldn't remove that command, so I did not use the token. "
                    "If it was real, revoke it with @BotFather now. Then send <code>/clone</code> "
                    "and follow the private token prompt."
                )
            return await message.reply_text(
                "I deleted that command. Please use <code>/clone</code> first, read the warning, "
                "then send the token in a separate private message."
            )
        try:
            await client.send_message(
                owner_id,
                "I did not use or store that command's text. "
                + ("I removed it from the chat. " if removed else "I could not remove it; revoke any exposed bot token with @BotFather now. ")
                + "Use <code>/clone</code> in my private chat and follow the warning.",
            )
        except Exception:
            # The user may not have started the bot in PM. Never repeat the
            # sensitive command; a generic group warning is the safe fallback.
            return await message.reply_text(
                "I couldn't DM you. Never post bot tokens in chats; revoke any token you exposed with @BotFather."
            )
        if not removed:
            return await message.reply_text(
                "I couldn't remove that message. Revoke any exposed bot token with @BotFather immediately."
            )
        return
    if owner_id is None:
        return
    if not _private(message):
        return await message.reply_text("For your safety, use <code>/clone</code> in my private chat.")
    await _begin_clone(client, message, owner_id)


@Client.on_message(filters.private & filters.command("cancelclone"), group=1)
async def cancel_clone_prompt(_client, message):
    if not message.from_user:
        return
    if _pending.pop(int(message.from_user.id), None):
        return await message.reply_text("Clone request cancelled. No token was saved.")
    await message.reply_text("There is no pending clone request.")


@Client.on_callback_query(filters.regex(r"^clone_start$"))
async def clone_start_button(client, query):
    if not query.from_user:
        return await query.answer()
    if not query.message or not _private(query.message):
        return await query.answer("Open the bot in a private chat to clone it.", show_alert=True)
    await query.answer()
    await _begin_clone(client, query.message, int(query.from_user.id))


@Client.on_message(filters.text & filters.incoming, group=-9)
async def receive_clone_token(client, message):
    """Consume clone tokens early so no later handler can search or log them."""
    sender = getattr(message, "from_user", None)
    owner_id = int(sender.id) if sender else None
    text = (message.text or "").strip()
    token_like = bool(_TOKEN_LIKE_RE.search(text))

    if not _private(message):
        if not token_like:
            return
        if owner_id is not None:
            _pending.pop(owner_id, None)
        removed = await _delete_sensitive_message(message)
        await _group_token_notice_and_stop(client, message, owner_id, removed)
    if owner_id is None:
        if not token_like:
            return
        removed = await _delete_sensitive_message(message)
        await _group_token_notice_and_stop(client, message, None, removed)

    pending = None if CLONE_CHILD_MODE else _pending.get(owner_id)

    # Catch accidental tokens even without a prompt and in command arguments.
    # A normal command such as /cancelclone still reaches its own handler.
    if text.startswith("/"):
        if not token_like:
            return
        _pending.pop(owner_id, None)
        removed = await _delete_sensitive_message(message)
        notice = (
            "I removed that command and did not use its text. Send <code>/clone</code> "
            "and follow the private token prompt."
            if removed
            else "I couldn't delete that command, so I did not use or store its text. "
            "Revoke any exposed bot token with @BotFather now."
        )
        await _private_notice_and_stop(client, owner_id, notice)

    if not pending:
        if not token_like:
            return
        removed = await _delete_sensitive_message(message)
        if removed and CLONE_CHILD_MODE:
            notice = "I removed that token-like message and did not use or store it. Start the clone flow in the main bot."
        elif removed:
            notice = "I removed that token-like message and did not use or store it. Start with <code>/clone</code> if you want to create a bot."
        else:
            notice = "I couldn't delete that message, so I did not use or store it. Revoke any exposed token with @BotFather."
        await _private_notice_and_stop(client, owner_id, notice)

    now = time.monotonic()
    if now > float(pending.get("expires", 0)):
        _pending.pop(owner_id, None)
        removed = await _delete_sensitive_message(message)
        notice = (
            "Clone request expired. I removed that message; send <code>/clone</code> to start again."
            if removed
            else "Clone request expired and I couldn't delete that message. I did not use it; revoke any exposed token with @BotFather."
        )
        await _private_notice_and_stop(client, owner_id, notice)

    token = text
    if not await _delete_sensitive_message(message):
        _pending.pop(owner_id, None)
        await _private_notice_and_stop(
            client,
            owner_id,
            "I couldn't delete that message, so I did not use or store the token. "
            "If it was real, revoke it with @BotFather before trying again.",
        )

    manager = _manager(client)
    if not manager.valid_token_format(token):
        pending["attempts"] = int(pending.get("attempts", 0)) + 1
        if pending["attempts"] >= _MAX_TOKEN_ATTEMPTS:
            _pending.pop(owner_id, None)
            notice = "That doesn't look like a BotFather token. I removed the message and cancelled this request; use <code>/clone</code> to try again."
        else:
            notice = "That doesn't look like a BotFather token. I removed the message; please check it and try again."
        token = ""
        await _private_notice_and_stop(client, owner_id, notice)

    # create_clone re-checks the quota and RAM under its lock, immediately
    # before persisting anything; another user may have filled the last slot.
    try:
        bot_info = await manager.validate_token(token)
    except Exception:
        bot_info = None
    if not bot_info:
        pending["attempts"] = int(pending.get("attempts", 0)) + 1
        if pending["attempts"] >= _MAX_TOKEN_ATTEMPTS:
            _pending.pop(owner_id, None)
            notice = "Telegram rejected that token. I removed it and cancelled the request."
        else:
            notice = "Telegram could not verify that token. I removed it; check BotFather and try again."
        token = ""
        await _private_notice_and_stop(client, owner_id, notice)

    _pending.pop(owner_id, None)
    try:
        result = await manager.create_clone(owner_id=owner_id, bot=bot_info, token=token)
    except Exception:
        # Never log token, Bot API URL, request object, or the raw exception.
        logger.warning("Clone creation failed for owner %s", owner_id, exc_info=False)
        token = ""
        await _private_notice_and_stop(client, owner_id, "Clone creation failed safely. No token was shown; please try later.")
    finally:
        token = ""

    status = result.get("status")
    if status == "started":
        clone = result.get("clone", {})
        username = html.escape(str(clone.get("username", "")))
        await _private_notice_and_stop(
            client,
            owner_id,
            f"✅ <b>Your clone is running:</b> <a href=\"https://t.me/{username}\">@{username}</a>\n\n"
            "Open it and press <code>/start</code>. Its users/files database is separate. "
            "To add files, add your clone as an admin to your own file channel and send <code>/index</code> there. "
            "Website streaming is off on clones to keep host load low.\n\n"
            "Manage it with <code>/myclone</code>, <code>/restartclone</code>, or <code>/deleteclone</code>.",
            reply_markup=InlineKeyboardMarkup(
                [[green("🚀 Open your clone", url=f"https://t.me/{username}")]]
            ),
            disable_web_page_preview=True,
        )
    if status == "session_error":
        await _private_notice_and_stop(client, owner_id, "Clone service storage is unavailable. No token was stored; contact support.")
    if status == "capacity":
        await _private_notice_and_stop(client, owner_id, "All clone slots filled while you were setting this up. Please try again later.")
    if status == "memory":
        await _private_notice_and_stop(client, owner_id, "Free RAM dropped below the safety limit. No clone was started; try later.")
    if status == "record_limit":
        await _private_notice_and_stop(client, owner_id, "The server has reached its saved-clone limit. No token was stored; contact support.")
    if status == "already_exists":
        await _private_notice_and_stop(client, owner_id, "You already have a clone. Use <code>/myclone</code> or <code>/deleteclone</code>.")
    if status == "main_bot":
        await _private_notice_and_stop(client, owner_id, "That token belongs to this main bot, so it cannot be cloned here.")
    if status == "bot_already_registered":
        await _private_notice_and_stop(client, owner_id, "That bot token is already registered as a clone.")
    if status == "start_failed":
        await _private_notice_and_stop(
            client,
            owner_id,
            "Telegram accepted the token, but the clone could not finish starting. "
            "Use <code>/myclone</code> and <code>/restartclone</code>, or remove it with <code>/deleteclone</code>.",
        )
    await _private_notice_and_stop(client, owner_id, "Clone service is unavailable. No token was sent back to you.")

@Client.on_message(filters.private & filters.command("myclone"), group=1)
async def my_clone(client, message):
    if not message.from_user:
        return
    if CLONE_CHILD_MODE:
        return await message.reply_text("This bot is a clone; manage it from the main bot.")
    try:
        clone = await _manager(client).get_owner_clone(message.from_user.id)
    except Exception:
        logger.warning("Could not read clone status for owner %s", message.from_user.id, exc_info=True)
        return await message.reply_text("Clone status is temporarily unavailable.")
    if not clone:
        return await message.reply_text("You don't have a clone yet. Tap <code>Clone Bot</code> or send <code>/clone</code>.")
    username = str(clone.get("username", ""))
    status = html.escape(str(clone.get("status", "unknown")))
    if username:
        link = f"\n🤖 <a href=\"https://t.me/{html.escape(username)}\">@{html.escape(username)}</a>"
    else:
        link = ""
    await message.reply_text(
        f"<b>Clone status:</b> <code>{status}</code>{link}\n\n"
        "Commands: <code>/restartclone</code> · <code>/deleteclone</code>",
        disable_web_page_preview=True,
    )


@Client.on_message(filters.private & filters.command("restartclone"), group=1)
async def restart_clone(client, message):
    if not message.from_user:
        return
    if CLONE_CHILD_MODE:
        return await message.reply_text("This bot is a clone; manage it from the main bot.")
    try:
        result = await _manager(client).restart_clone(message.from_user.id)
    except Exception:
        logger.warning("Could not restart clone for owner %s", message.from_user.id, exc_info=True)
        return await message.reply_text("Clone restart is temporarily unavailable.")
    status = result.get("status")
    if status == "started":
        username = html.escape(str(result.get("clone", {}).get("username", "")))
        return await message.reply_text(
            f"✅ Clone is running: <a href=\"https://t.me/{username}\">@{username}</a>",
            disable_web_page_preview=True,
        )
    messages = {
        "disabled": "Clone service is currently disabled.",
        "session_error": "Clone service session storage is unavailable; contact support.",
        "not_found": "No saved clone found. Use <code>/clone</code> to create one.",
        "already_active": "Your clone is already running.",
        "capacity": "All clone slots are in use; try again later.",
        "memory": "Free RAM is below the safety reserve; try again later.",
        "start_failed": "The clone still could not start. Check its BotFather token, then use <code>/deleteclone</code> to remove it.",
    }
    await message.reply_text(messages.get(status, "Clone could not be restarted."))


@Client.on_message(filters.private & filters.command("deleteclone"), group=1)
async def delete_clone(client, message):
    if not message.from_user:
        return
    if CLONE_CHILD_MODE:
        return await message.reply_text("This bot is a clone; manage it from the main bot.")
    try:
        deleted = await _manager(client).delete_clone(message.from_user.id)
    except Exception:
        logger.warning("Could not delete clone for owner %s", message.from_user.id, exc_info=True)
        return await message.reply_text("Clone removal is temporarily unavailable.")
    if not deleted:
        return await message.reply_text("You don't have a saved clone.")
    _pending.pop(int(message.from_user.id), None)
    await message.reply_text(
        "🛑 Your clone has been stopped and its encrypted token removed from this database. "
        "For safety, also revoke the token in <a href=\"https://t.me/BotFather\">@BotFather</a>.",
        disable_web_page_preview=True,
    )
