"""Admin controls that run **from Telegram** (no repo edit, no redeploy).

The two values this plugin manages are stored in MongoDB through
``dreamxbotz.util.bot_controls``:

* **Sᴇɴᴅ Aʟʟ premium gate** – keep the bulk send feature premium-only (the
  historical behaviour) or open it to every user.
* **Force-subscribe channels** – a bot-managed channel list that replaces
  ``AUTH_CHANNELS``/per-group ``fsub`` while it exists.  Channels can be
  added, removed and reset to the repo/env list from the panel.

Everything is designed to be used **from the buttons**:

* tapping a button never pops a toast/alert – the panel simply updates,
* ➕ *Add channel* opens a reply box (ForceReply) – paste the ID / ``@name`` /
  ``t.me`` link or forward a post and it is added,
* pasting a channel ID / ``t.me`` link / ``@username`` or forwarding a post in
  the bot's PM at **any time** shows a one-tap "Add this channel?" card, so a
  channel can be added without remembering any command.

Commands (bot admins, private chat)::

    /botpanel                         one panel with both controls
    /sendall on|off|status            premium gate
    /fsub                             channel panel
    /fsub add -100123 @channel ...    add channels
    /fsub remove -100123              remove a channel
    /fsub reset                       fall back to repo/env (AUTH_CHANNELS)

See ``docs/BOT_CONTROLS.md``.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from pyrogram import Client, filters, enums
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardMarkup,
    Message,
)

from dreamxbotz.util import bot_controls as controls
from dreamxbotz.util.buttons import blue, green, red
from info import ADMINS, LOG_CHANNEL

logger = logging.getLogger(__name__)

# Run before the normal handlers (maintenance uses -200).  This lets the
# callback handler stop propagation before pmfilter's catch-all answers the
# same button press a second time.
CONTROL_GROUP = -60

# Waiting for an admin to send a channel id after pressing "➕ Add channel".
_PENDING_ADD: dict = {}
_PENDING_TTL = 300.0

_TITLE_CACHE: dict = {}

# user_id -> (chat_id, message_id, kind) of the last panel we sent, so every
# change can refresh that same message instead of spamming a new one.
_PANEL_MSG: dict = {}

SAVED_NOTE = "Saved in MongoDB — applies instantly, no repo edit or restart."

# A bare channel reference sent in the bot's PM (no command needed).
_ID_RE = re.compile(r"^-100\d{6,}$")
_TME_LINK_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t(?:elegram)?\.me)/[A-Za-z][A-Za-z0-9_]{4,31}/?$",
    re.IGNORECASE,
)
_AT_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _short(text: str, limit: int = 42) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _mode_line(custom) -> str:
    if custom is None:
        return (
            "📦 <b>Mode:</b> repo/env default (<code>AUTH_CHANNELS</code>)\n"
            "<i>Tap ➕ Add channel (or just paste a channel ID / t.me link here) "
            "to manage the list entirely from this bot.</i>"
        )
    if not custom:
        return "📦 <b>Mode:</b> bot-managed — force-sub is disabled (0 channels)"
    return "📦 <b>Mode:</b> bot-managed — repo/env <code>AUTH_CHANNELS</code> is ignored"


def sendall_panel_text(premium_required: bool) -> str:
    if premium_required:
        status = "🔐 <b>ON</b> — only premium users can use Sᴇɴᴅ Aʟʟ"
        detail = (
            "Everyone else gets the premium offer when they tap "
            "Sᴇɴᴅ Aʟʟ, exactly like before."
        )
    else:
        status = "🔓 <b>OFF</b> — every user can use Sᴇɴᴅ Aʟʟ"
        detail = (
            "Any user can now receive every file of the current search page "
            "in PM; the premium offer is skipped. Force-sub and verification "
            "rules still apply."
        )
    return (
        "<b>🔐 Sᴇɴᴅ Aʟʟ — ᴘʀᴇᴍɪᴜᴍ ɢᴀᴛᴇ</b>\n\n"
        f"<b>Status:</b> {status}\n\n"
        f"{detail}\n\n"
        f"<i>{SAVED_NOTE}</i>"
    )


def fsub_panel_text(custom, effective, titles=None) -> str:
    titles = titles or {}
    lines = [
        "<b>📢 ꜰᴏʀᴄᴇ-sᴜʙ ᴄʜᴀɴɴᴇʟs</b>",
        "",
        _mode_line(custom),
        "",
    ]
    if effective:
        lines.append(f"<b>Channels ({len(effective)}):</b>")
        for index, channel_id in enumerate(effective, 1):
            title = html.escape(str(titles.get(channel_id) or channel_id))
            lines.append(f"{index}. <b>{title}</b> — <code>{channel_id}</code>")
    else:
        lines.append("🚫 <b>Force-sub is OFF</b> — no channel is required.")

    if custom is not None:
        lines.extend([
            "",
            "Tap ❌ to remove a channel, ➕ to add one, or ♻️ to go back to the "
            "repo/env list.",
        ])
    else:
        lines.extend([
            "",
            "Tap ❌ to remove a channel or ➕ to add one. Changes are saved in "
            "MongoDB and apply instantly.",
        ])
    lines.append(f"<i>{SAVED_NOTE}</i>")
    return "\n".join(lines)


def main_panel_text(premium_required: bool, custom, effective_count: int) -> str:
    gate = "🔐 ON — premium only" if premium_required else "🔓 OFF — free for everyone"
    mode = "bot-managed" if custom is not None else "repo/env default"
    return (
        "<b>🎛️ ʙᴏᴛ ᴄᴏɴᴛʀᴏʟ ᴘᴀɴᴇʟ</b>\n\n"
        f"<b>Sᴇɴᴅ Aʟʟ premium gate:</b> {gate}\n"
        f"<b>Force-sub channels:</b> {effective_count} · {mode}\n\n"
        "Choose a control below. Everything is stored in MongoDB and applies "
        "immediately — no repo edit, no restart."
    )


async def _channel_title(client, channel_id: int) -> str:
    if channel_id in _TITLE_CACHE:
        return _TITLE_CACHE[channel_id]
    title = str(channel_id)
    try:
        chat = await asyncio.wait_for(client.get_chat(channel_id), timeout=5)
        title = getattr(chat, "title", None) or str(channel_id)
    except Exception as exc:
        logger.debug("Could not fetch title for %s: %s", channel_id, exc)
    _TITLE_CACHE[channel_id] = title
    return title


async def _fetch_titles(client, channel_ids) -> dict:
    if not channel_ids:
        return {}
    results = await asyncio.gather(
        *(_channel_title(client, channel_id) for channel_id in channel_ids),
        return_exceptions=True,
    )
    titles = {}
    for channel_id, title in zip(channel_ids, results):
        if not isinstance(title, str):
            title = _TITLE_CACHE.get(channel_id, str(channel_id))
        titles[channel_id] = title
    return titles


async def _resolve_channel(client, reference):
    """Resolve input to a channel chat, raising a friendly ValueError."""
    try:
        chat = await client.get_chat(reference)
    except Exception as exc:
        raise ValueError(f"`{reference}` not found — is the bot a member? ({exc})")
    if chat.type != enums.ChatType.CHANNEL:
        raise ValueError(f"`{reference}` is not a channel.")
    return chat


async def _bot_is_admin(client, channel_id: int) -> bool:
    try:
        me = getattr(client, "me", None) or await client.get_me()
        member = await client.get_chat_member(channel_id, me.id)
        return member.status in (
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        )
    except Exception:
        return False


async def _log_change(client, text: str) -> None:
    """Best-effort audit line; never breaks the control."""
    try:
        await client.send_message(LOG_CHANNEL, text)
    except Exception as exc:
        logger.debug("Could not log a bot control change: %s", exc)


def _pending_active(user_id: int) -> bool:
    expires = _PENDING_ADD.get(user_id)
    if not expires:
        return False
    if time.monotonic() >= expires:
        _PENDING_ADD.pop(user_id, None)
        return False
    return True


def _set_pending(user_id: int) -> None:
    _PENDING_ADD[user_id] = time.monotonic() + _PENDING_TTL


def _clear_pending(user_id: int) -> None:
    _PENDING_ADD.pop(user_id, None)


def _remember_panel(user_id, message, kind: str) -> None:
    try:
        _PANEL_MSG[int(user_id)] = (int(message.chat.id), int(message.id), kind)
    except Exception:
        pass


def _channel_from_forward(message: Message):
    """Channel id behind a forwarded post, or ``None``."""
    forward = getattr(message, "forward_from_chat", None)
    if forward is not None and getattr(forward, "type", None) == enums.ChatType.CHANNEL:
        return int(forward.id)

    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if chat is not None and getattr(chat, "type", None) == enums.ChatType.CHANNEL:
            return int(chat.id)
    return None


# --------------------------------------------------------------------------- #
# Panels
# --------------------------------------------------------------------------- #
async def _sendall_keyboard(premium_required: bool) -> InlineKeyboardMarkup:
    if premium_required:
        toggle = green("🔓 Turn OFF — free for everyone", callback_data="botctl#sendall#off")
    else:
        toggle = green("🔐 Turn ON — premium only", callback_data="botctl#sendall#on")
    return InlineKeyboardMarkup([
        [toggle],
        [
            blue("📢 Force-sub channels", callback_data="botctl#fsub"),
            blue("🎛️ Panel", callback_data="botctl#panel"),
        ],
    ])


async def _fsub_keyboard(client, custom, effective) -> InlineKeyboardMarkup:
    titles = await _fetch_titles(client, effective)
    rows = []
    for channel_id in effective:
        title = _short(titles.get(channel_id) or str(channel_id), 32)
        rows.append([
            red(f"❌ {title}", callback_data=f"botctl#fsub#rm#{channel_id}")
        ])
    rows.append([green("➕ Add channel", callback_data="botctl#fsub#add")])
    footer = [blue("🎛️ Panel", callback_data="botctl#panel"), blue("🔄 Refresh", callback_data="botctl#fsub")]
    if custom is not None:
        footer.insert(0, red("♻️ Reset to env", callback_data="botctl#fsub#reset"))
    rows.append(footer)
    return InlineKeyboardMarkup(rows)


async def _main_keyboard() -> InlineKeyboardMarkup:
    premium_required = await controls.send_all_requires_premium()
    custom = await controls.get_custom_fsub_channels()
    effective = await controls.effective_fsub_channels(settings={})
    gate = "ON" if premium_required else "OFF"
    mode = "bot-managed" if custom is not None else "repo/env"
    return InlineKeyboardMarkup([
        [blue(f"🔐 Send All premium gate: {gate}", callback_data="botctl#sendall")],
        [blue(f"📢 Force-sub: {len(effective)} channel(s) · {mode}", callback_data="botctl#fsub")],
        [blue("🔄 Refresh", callback_data="botctl#panel")],
    ])


async def _show_main_panel(client, message=None, query=None):
    premium_required = await controls.send_all_requires_premium()
    custom = await controls.get_custom_fsub_channels()
    effective = await controls.effective_fsub_channels(settings={})
    text = main_panel_text(premium_required, custom, len(effective))
    keyboard = await _main_keyboard()
    await _deliver(message, query, text, keyboard, kind="main")


async def _show_sendall_panel(client, message=None, query=None):
    premium_required = await controls.send_all_requires_premium()
    await _deliver(
        message,
        query,
        sendall_panel_text(premium_required),
        await _sendall_keyboard(premium_required),
        kind="sendall",
    )


async def _show_fsub_panel(client, message=None, query=None):
    custom = await controls.get_custom_fsub_channels()
    effective = await controls.effective_fsub_channels(settings={})
    titles = await _fetch_titles(client, effective)
    await _deliver(
        message,
        query,
        fsub_panel_text(custom, effective, titles),
        await _fsub_keyboard(client, custom, effective),
        kind="fsub",
    )


async def _deliver(message=None, query=None, text: str = "", keyboard=None, kind: str = ""):
    kwargs = dict(
        reply_markup=keyboard,
        disable_web_page_preview=True,
        parse_mode=enums.ParseMode.HTML,
    )
    if query is not None:
        try:
            await query.message.edit_text(text, **kwargs)
        except MessageNotModified:
            pass
        except Exception as exc:
            logger.warning("Could not edit the bot control panel: %s", exc)
            try:
                await query.message.reply_text(text, **kwargs)
            except Exception:
                pass
        _remember_panel(query.from_user.id, query.message, kind)
        return
    if message is not None:
        sent = await message.reply_text(text, **kwargs)
        _remember_panel(message.from_user.id, sent, kind)


async def _refresh_panel(client, user_id: int, kind: str = "fsub") -> bool:
    """Edit the last panel message in place (no new messages, no toasts)."""
    ref = _PANEL_MSG.get(int(user_id))
    if not ref or ref[2] != kind:
        return False
    chat_id, message_id, _ = ref

    custom = await controls.get_custom_fsub_channels()
    effective = await controls.effective_fsub_channels(settings={})
    titles = await _fetch_titles(client, effective)
    try:
        await client.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=fsub_panel_text(custom, effective, titles),
            reply_markup=await _fsub_keyboard(client, custom, effective),
            disable_web_page_preview=True,
            parse_mode=enums.ParseMode.HTML,
        )
        return True
    except MessageNotModified:
        return True
    except Exception as exc:
        logger.debug("Could not refresh the force-sub panel in place: %s", exc)
        _PANEL_MSG.pop(int(user_id), None)
        return False


async def _refresh_or_send_panel(client, message: Message):
    """Refresh the remembered panel, otherwise send one new panel message."""
    if not await _refresh_panel(client, message.from_user.id):
        await _show_fsub_panel(client, message=message)


def _add_channel_prompt() -> tuple[str, ForceReply]:
    text = (
        "<b>➕ Add a force-sub channel</b>\n\n"
        "Type it in the reply box below (it is already focused):\n"
        "• channel ID — <code>-1001234567890</code>\n"
        "• username — <code>@YourChannel</code>\n"
        "• link — <code>t.me/YourChannel</code>\n\n"
        "…or simply <b>forward any post</b> from that channel.\n\n"
        "<i>Send /fsub cancel to stop.</i>"
    )
    return text, ForceReply(selective=True, placeholder="Channel ID / @username / t.me link")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@Client.on_message(
    filters.private
    & filters.user(ADMINS)
    & filters.command(["botpanel", "botcontrol", "controls"]),
    group=CONTROL_GROUP,
)
async def bot_panel_cmd(client, message: Message):
    await _show_main_panel(client, message=message)


@Client.on_message(
    filters.private
    & filters.user(ADMINS)
    & filters.command(["sendall", "sendallpremium"]),
    group=CONTROL_GROUP,
)
async def send_all_gate_cmd(client, message: Message):
    args = message.command[1:]
    arg = args[0].lower() if args else ""

    if arg in {"on", "enable", "enabled", "yes", "true", "1"}:
        return await _apply_send_all_gate(client, message, True)
    if arg in {"off", "disable", "disabled", "no", "false", "0"}:
        return await _apply_send_all_gate(client, message, False)
    if arg not in {"", "status", "show"}:
        return await message.reply_text(
            "<b>Usage:</b>\n"
            "<code>/sendall on</code> — premium users only\n"
            "<code>/sendall off</code> — free for everyone\n"
            "<code>/sendall status</code> — current setting",
            parse_mode=enums.ParseMode.HTML,
        )
    await _show_sendall_panel(client, message=message)


async def _apply_send_all_gate(client, message: Message, enabled: bool):
    try:
        await controls.set_send_all_requires_premium(enabled)
    except Exception as exc:
        logger.exception("Could not save the Send All gate: %s", exc)
        return await message.reply_text(
            "⚠️ Could not save the setting (MongoDB error). The old value is unchanged."
        )
    if enabled:
        reply = (
            "🔐 <b>Sᴇɴᴅ Aʟʟ premium gate is ON.</b>\n\n"
            "Only premium users can send a full page of files to their PM now."
        )
        log = "🔐 <b>Send All gate ON</b> (premium only)"
    else:
        reply = (
            "🔓 <b>Sᴇɴᴅ Aʟʟ premium gate is OFF.</b>\n\n"
            "Every user can now use Sᴇɴᴅ Aʟʟ — the premium offer is skipped."
        )
        log = "🔓 <b>Send All gate OFF</b> (free for everyone)"
    await message.reply_text(reply, parse_mode=enums.ParseMode.HTML)
    await _log_change(client, f"{log}\n\nBy: <code>{message.from_user.id}</code>")


@Client.on_message(
    filters.private
    & filters.user(ADMINS)
    & filters.command(["fsub", "fsubs", "forcesub"]),
    group=CONTROL_GROUP,
)
async def fsub_cmd(client, message: Message):
    args = message.command[1:]
    action = args[0].lower() if args else ""

    if action in {"", "status", "show", "list"}:
        return await _show_fsub_panel(client, message=message)

    if action in {"cancel", "stop"}:
        _clear_pending(message.from_user.id)
        return await message.reply_text("❌ Channel input cancelled.")

    if action == "reset":
        return await _reset_fsub(client, message)

    if action == "add":
        references = args[1:]
        if not references:
            _set_pending(message.from_user.id)
            text, markup = _add_channel_prompt()
            return await message.reply_text(
                text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
            )
        return await _add_channels(client, message, references)

    if action == "remove":
        if len(args) < 2:
            return await _show_fsub_panel(client, message=message)
        return await _remove_channel(client, message, args[1])

    return await message.reply_text(
        "<b>Force-sub usage:</b>\n"
        "<code>/fsub</code> — panel\n"
        "<code>/fsub add -1001234567890 @channel</code>\n"
        "<code>/fsub remove -1001234567890</code>\n"
        "<code>/fsub reset</code> — back to repo/env (<code>AUTH_CHANNELS</code>)",
        parse_mode=enums.ParseMode.HTML,
    )


# --------------------------------------------------------------------------- #
# Add / remove / reset
# --------------------------------------------------------------------------- #
async def _apply_channel_additions(client, references):
    """Resolve + save channels.

    Returns ``(added_chats, warnings, errors, save_error)``.
    """
    added, errors, warnings = [], [], []
    for reference in references[:10]:
        parsed = controls.parse_channel_reference(reference)
        if parsed is None:
            errors.append(f"• `{html.escape(str(reference))}` — not a channel ID or username")
            continue
        try:
            chat = await _resolve_channel(client, parsed)
        except ValueError as exc:
            errors.append(f"• {html.escape(str(exc))}")
            continue
        added.append(chat)
        _TITLE_CACHE[chat.id] = chat.title or str(chat.id)
        if not await _bot_is_admin(client, chat.id):
            warnings.append(
                f"⚠️ The bot is not an admin in <b>{html.escape(chat.title or str(chat.id))}</b>. "
                "Add it as admin there, otherwise users will not get a join button."
            )

    if not added:
        return added, warnings, errors, None

    seed = await controls.effective_fsub_channels(settings={})
    try:
        await controls.add_custom_fsub_channels([chat.id for chat in added], seed_from=seed)
    except Exception as exc:
        logger.exception("Could not save force-sub channels: %s", exc)
        return [], warnings, errors, "⚠️ Could not save the channels (MongoDB error). Nothing was changed."
    return added, warnings, errors, None


def _added_summary(added, warnings, errors) -> str:
    lines = [
        "✅ <b>Added to force-sub:</b>",
        *[
            f"• <b>{html.escape(chat.title or str(chat.id))}</b> — <code>{chat.id}</code>"
            for chat in added
        ],
    ]
    if warnings:
        lines.extend(["", *warnings])
    if errors:
        lines.extend(["", "⚠️ Skipped:", *errors])
    return "\n".join(lines)


async def _add_channels(client, message: Message, references):
    added, warnings, errors, save_error = await _apply_channel_additions(client, references)

    if save_error:
        return await message.reply_text(save_error)
    if added:
        await message.reply_text(
            _added_summary(added, warnings, errors), parse_mode=enums.ParseMode.HTML
        )
        await _log_change(
            client,
            "📢 <b>Force-sub channels added</b>\n"
            + "\n".join(f"• {chat.title} (<code>{chat.id}</code>)" for chat in added)
            + f"\n\nBy: <code>{message.from_user.id}</code>",
        )
        return await _refresh_or_send_panel(client, message)

    if errors:
        return await message.reply_text(
            "⚠️ Nothing was added:\n" + "\n".join(errors),
            parse_mode=enums.ParseMode.HTML,
        )
    return await message.reply_text("Send a channel ID, @username or t.me link.")


async def _remove_channel_id(client, user_id: int, channel_id: int):
    """Remove one channel; returns ``(saved, error)``."""
    seed = await controls.effective_fsub_channels(settings={})
    try:
        saved = await controls.remove_custom_fsub_channel(channel_id, seed_from=seed)
    except Exception as exc:
        logger.exception("Could not remove a force-sub channel: %s", exc)
        return None, "⚠️ Could not save the change (MongoDB error). Nothing was changed."

    await _log_change(
        client,
        f"🗑 <b>Force-sub channel removed</b>: <code>{channel_id}</code>\n"
        f"By: <code>{user_id}</code>",
    )
    return saved, None


async def _remove_channel(client, message: Message, reference: str):
    channel_id = controls.parse_channel_reference(reference)
    if channel_id is None:
        return await message.reply_text(
            "⚠️ Send a channel ID (<code>-100…</code>), <code>@username</code> "
            "or a <code>t.me/…</code> link.",
            parse_mode=enums.ParseMode.HTML,
        )
    if isinstance(channel_id, str):                 # @username -> numeric id
        try:
            chat = await _resolve_channel(client, channel_id)
        except ValueError as exc:
            return await message.reply_text(f"⚠️ {html.escape(str(exc))}")
        channel_id = chat.id
        _TITLE_CACHE[channel_id] = chat.title or str(channel_id)

    saved, error = await _remove_channel_id(client, message.from_user.id, channel_id)
    if error:
        return await message.reply_text(error)

    await message.reply_text(
        f"✅ Removed <code>{channel_id}</code> from force-sub.\n\n"
        f"Remaining: <b>{len(saved)}</b> channel(s)"
        + ("" if saved else "\n\n🚫 Force-sub is now disabled."),
        parse_mode=enums.ParseMode.HTML,
    )
    await _refresh_or_send_panel(client, message)


async def _reset_fsub(client, message: Message):
    try:
        await controls.reset_custom_fsub_channels()
    except Exception as exc:
        logger.exception("Could not reset force-sub channels: %s", exc)
        return await message.reply_text(
            "⚠️ Could not reset (MongoDB error). Nothing was changed."
        )
    await message.reply_text(
        "♻️ <b>Force-sub reset.</b>\n\n"
        "The repo/env <code>AUTH_CHANNELS</code> list is active again.",
        parse_mode=enums.ParseMode.HTML,
    )
    await _log_change(
        client,
        f"♻️ <b>Force-sub reset to repo/env</b>\nBy: <code>{message.from_user.id}</code>",
    )
    await _refresh_or_send_panel(client, message)


# --------------------------------------------------------------------------- #
# One-tap "add this channel?" card
# --------------------------------------------------------------------------- #
def _add_confirm_text(chat) -> str:
    title = html.escape(chat.title or str(chat.id))
    return (
        "<b>➕ Add this channel to force-sub?</b>\n\n"
        f"<b>{title}</b>\n<code>{chat.id}</code>\n\n"
        "Users will have to join it before they can get files."
    )


def _add_confirm_keyboard(channel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [green("✅ Add to force-sub", callback_data=f"botctl#fsub#ok#{channel_id}")],
        [red("❌ Cancel", callback_data="botctl#fsub#no")],
    ])


async def _send_add_confirm(client, message: Message, reference):
    parsed = controls.parse_channel_reference(reference)
    if parsed is None:
        return False
    try:
        chat = await _resolve_channel(client, parsed)
    except ValueError:
        # Not a channel: let the message behave normally (e.g. a search).
        return False

    _TITLE_CACHE[chat.id] = chat.title or str(chat.id)
    await message.reply_text(
        _add_confirm_text(chat),
        reply_markup=_add_confirm_keyboard(chat.id),
        parse_mode=enums.ParseMode.HTML,
    )
    return True


# --------------------------------------------------------------------------- #
# Channel input (button flow + "just paste it" flow)
# --------------------------------------------------------------------------- #
@Client.on_message(
    filters.private & filters.incoming & filters.user(ADMINS),
    group=CONTROL_GROUP,
)
async def fsub_channel_input(client, message: Message):
    user_id = message.from_user.id if message.from_user else None
    if user_id is None:
        return
    if message.text and message.text.startswith("/"):
        return                                   # let /fsub cancel etc. work

    pending = _pending_active(user_id)
    text = (message.text or "").strip()

    # 1) Forwarded post -> its channel.
    forward_id = _channel_from_forward(message)
    if forward_id is not None:
        message.stop_propagation()
        if pending:
            _clear_pending(user_id)
            return await _add_channels(client, message, [forward_id])
        return await _send_add_confirm(client, message, forward_id)

    if not text:
        if pending:
            message.stop_propagation()
            await message.reply_text(
                "Send a channel ID / @username / t.me link, or forward a post "
                "from the channel. /fsub cancel to stop."
            )
        return

    # 2) Unambiguous channel references: numeric id / t.me link.
    if _ID_RE.match(text) or _TME_LINK_RE.match(text):
        message.stop_propagation()
        if pending:
            _clear_pending(user_id)
            return await _add_channels(client, message, [text])
        return await _send_add_confirm(client, message, text)

    # 3) @username: only treat it as a channel when it really is one.
    if _AT_RE.match(text):
        if pending:
            message.stop_propagation()
            _clear_pending(user_id)
            return await _add_channels(client, message, [text])
        handled = await _send_add_confirm(client, message, text)
        if handled:
            message.stop_propagation()
        return

    # 4) Something else while we wait for input: say what we expect.
    if pending:
        message.stop_propagation()
        await message.reply_text(
            "That doesn't look like a channel. Send a channel ID "
            "(<code>-100…</code>), <code>@username</code>, a <code>t.me/…</code> "
            "link, or forward a post. /fsub cancel to stop.",
            parse_mode=enums.ParseMode.HTML,
        )


# --------------------------------------------------------------------------- #
# Panel buttons
# --------------------------------------------------------------------------- #
@Client.on_callback_query(
    filters.regex(r"^botctl#") & filters.user(ADMINS),
    group=CONTROL_GROUP,
)
async def bot_control_callback(client, query: CallbackQuery):
    parts = query.data.split("#")
    section = parts[1] if len(parts) > 1 else ""

    try:
        if section == "panel":
            await query.answer()                 # silent: no toast
            return await _show_main_panel(client, query=query)

        if section == "sendall":
            action = parts[2] if len(parts) > 2 else "show"
            if action in {"on", "off"}:
                enabled = action == "on"
                try:
                    await controls.set_send_all_requires_premium(enabled)
                except Exception as exc:
                    logger.exception("Could not save the Send All gate: %s", exc)
                    await query.answer("MongoDB error — nothing changed.", show_alert=True)
                    return
                await query.answer()             # silent: the panel shows the new state
                await _log_change(
                    client,
                    f"{'🔐 Send All gate ON (premium only)' if enabled else '🔓 Send All gate OFF (free for everyone)'}\n\n"
                    f"By: <code>{query.from_user.id}</code>",
                )
            else:
                await query.answer()
            return await _show_sendall_panel(client, query=query)

        if section == "fsub":
            action = parts[2] if len(parts) > 2 else "show"

            if action == "add":
                _set_pending(query.from_user.id)
                await query.answer()             # silent: no toast
                text, markup = _add_channel_prompt()
                await query.message.reply_text(
                    text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
                )
                return

            if action == "ok" and len(parts) > 3:
                try:
                    channel_id = int(parts[3])
                except ValueError:
                    await query.answer("Invalid channel", show_alert=True)
                    return
                await query.answer()             # silent
                added, warnings, errors, save_error = await _apply_channel_additions(
                    client, [channel_id]
                )
                if save_error or not added:
                    detail = save_error or "\n".join(errors) or "Could not add that channel."
                    await query.message.edit_text(detail, parse_mode=enums.ParseMode.HTML)
                    return
                chat = added[0]
                await _log_change(
                    client,
                    f"📢 <b>Force-sub channel added</b>\n"
                    f"• {chat.title} (<code>{chat.id}</code>)\n\n"
                    f"By: <code>{query.from_user.id}</code>",
                )
                note = ""
                if warnings:
                    note = "\n\n" + "\n".join(warnings)
                await query.message.edit_text(
                    f"✅ <b>Added to force-sub</b>\n\n"
                    f"<b>{html.escape(chat.title or str(chat.id))}</b> — "
                    f"<code>{chat.id}</code>{note}",
                    reply_markup=InlineKeyboardMarkup(
                        [[blue("🎛️ Open force-sub panel", callback_data="botctl#fsub")]]
                    ),
                    parse_mode=enums.ParseMode.HTML,
                )
                await _refresh_panel(client, query.from_user.id)
                return

            if action == "no":
                await query.answer()             # silent
                try:
                    await query.message.delete()
                except Exception:
                    pass
                return

            if action == "rm" and len(parts) > 3:
                try:
                    channel_id = int(parts[3])
                except ValueError:
                    await query.answer("Invalid channel", show_alert=True)
                    return
                saved, error = await _remove_channel_id(client, query.from_user.id, channel_id)
                if error:
                    await query.answer("MongoDB error — nothing changed.", show_alert=True)
                    return
                await query.answer()             # silent: the panel updates itself
                return await _show_fsub_panel(client, query=query)

            if action == "reset":
                try:
                    await controls.reset_custom_fsub_channels()
                except Exception as exc:
                    logger.exception("Could not reset force-sub channels: %s", exc)
                    await query.answer("MongoDB error — nothing changed.", show_alert=True)
                    return
                await query.answer()             # silent
                await _log_change(
                    client,
                    f"♻️ <b>Force-sub reset to repo/env</b>\nBy: <code>{query.from_user.id}</code>",
                )
                return await _show_fsub_panel(client, query=query)

            await query.answer()
            return await _show_fsub_panel(client, query=query)

        await query.answer("Unknown control", show_alert=True)
    except Exception as exc:
        logger.exception("Bot control callback failed: %s", exc)
        try:
            await query.answer("Something went wrong — check the bot logs.", show_alert=True)
        except Exception:
            pass
    finally:
        try:
            query.stop_propagation()
        except Exception:
            pass
