"""Best-effort audit of incoming private/group messages; see docs/MESSAGE_LOGGING.md."""
import asyncio
import logging
from html import escape

from pyrogram import Client, enums, filters

from info import LOG_CHANNEL, MESSAGE_LOGGING

logger = logging.getLogger(__name__)


@Client.on_message(
    filters.incoming & (filters.private | filters.group) & ~filters.service,
    group=-300,
)
async def log_incoming_message(client, message):
    # Before maintenance, conversations and search handlers; never consume updates.
    if not MESSAGE_LOGGING or message.chat.id == LOG_CHANNEL:
        return
    user = message.from_user
    if user is not None and user.is_bot:
        return
    sender = user or message.sender_chat
    name = (getattr(sender, "first_name", None)
            or getattr(sender, "title", None) or "Unknown")
    username = getattr(sender, "username", None)
    header = (
        "<b>#USER_MESSAGE</b>\n"
        f"👤 Name: {escape(name)}\n"
        f"🆔 Sender ID: <code>{getattr(sender, 'id', 'Unknown')}</code>\n"
        f"🔗 Username: {escape('@' + username) if username else 'Not set'}\n"
        f"💬 Chat: {escape(message.chat.title or 'Private')} "
        f"(<code>{message.chat.id}</code>)\n"
        f"📩 Message ID: <code>{message.id}</code>"
    )
    try:
        # Keep the original text/entities/caption/media intact, even for long text.
        # Reply to the header so concurrent users' messages stay identifiable.
        async def deliver():
            notice = await client.send_message(
                LOG_CHANNEL, header, parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
            await client.copy_message(
                LOG_CHANNEL, message.chat.id, message.id,
                reply_to_message_id=notice.id,
            )

        await asyncio.wait_for(deliver(), timeout=10)
    except Exception as exc:
        # Protected content / FloodWait / channel permissions must not break search.
        logger.warning("Message log failed for chat %s message %s: %s",
                       message.chat.id, message.id, exc)
