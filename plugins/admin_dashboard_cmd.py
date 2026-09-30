"""Telegram command that shares the web admin dashboard with bot admins."""
import os

from pyrogram import Client, filters

from info import ADMINS


@Client.on_message(filters.private & filters.command("admin") & filters.user(ADMINS))
async def admin_dashboard_link(_client, message):
    """Reply with the configured admin dashboard URL."""
    dashboard_url = os.getenv("ADMIN_DASHBOARD_URL") or os.getenv("WEB_URL")
    if not dashboard_url:
        await message.reply_text(
            "Admin dashboard URL is not configured. Set ADMIN_DASHBOARD_URL "
            "or WEB_URL to the deployed site URL."
        )
        return

    dashboard_url = dashboard_url.rstrip("/")
    admin_id = message.from_user.id
    await message.reply_text(
        f"🔐 <a href=\"{dashboard_url}/admin?admin_id={admin_id}\">Open admin dashboard</a>",
        disable_web_page_preview=True,
    )
