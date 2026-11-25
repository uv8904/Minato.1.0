"""Telegram command that shares the web admin dashboard with bot admins."""
import os

from pyrogram import Client, filters

from info import ADMINS, ADMIN_DASHBOARD_ALLOW_ID, ADMIN_DASHBOARD_TOKEN


@Client.on_message(filters.private & filters.command("admin") & filters.user(ADMINS))
async def admin_dashboard_link(_client, message):
    """Reply with the configured admin dashboard URL.

    A Telegram id is public information, so the old ``?admin_id=<id>`` link
    handed dashboard access to anyone who knew the admin's id. The link now
    carries the operator's secret token instead (sent only in this private
    chat), unless the operator explicitly opted into the legacy id shortcut.
    """
    dashboard_url = os.getenv("ADMIN_DASHBOARD_URL") or os.getenv("WEB_URL")
    if not dashboard_url:
        await message.reply_text(
            "Admin dashboard URL is not configured. Set ADMIN_DASHBOARD_URL "
            "or WEB_URL to the deployed site URL."
        )
        return

    dashboard_url = dashboard_url.rstrip("/")
    if ADMIN_DASHBOARD_TOKEN:
        url = f"{dashboard_url}/admin?token={ADMIN_DASHBOARD_TOKEN}"
        note = "\n\n<i>⚠️ Ye link secret token carry karta hai — kisi ko forward mat karna.</i>"
    elif ADMIN_DASHBOARD_ALLOW_ID:
        url = f"{dashboard_url}/admin?admin_id={message.from_user.id}"
        note = (
            "\n\n<i>⚠️ ADMIN_DASHBOARD_ALLOW_ID on hai: ye link sirf public Telegram id "
            "use karta hai — jo bhi tumhari id jaanta hai dashboard khol sakta hai. "
            "ADMIN_DASHBOARD_TOKEN set karna zyada safe hai.</i>"
        )
    else:
        await message.reply_text(
            "🔐 Dashboard is locked. Set <code>ADMIN_DASHBOARD_TOKEN</code> "
            "(a long random secret) in the environment, restart, then run /admin again.",
            disable_web_page_preview=True,
        )
        return

    await message.reply_text(
        f'🔐 <a href="{url}">Open admin dashboard</a>{note}',
        disable_web_page_preview=True,
    )
