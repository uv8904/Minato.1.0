"""FamPay (FamApp UPI) payments with **automatic** approval.

FamPay has no merchant API, so this plugin verifies payments with two
independent, account-only verifiers and grants premium the moment one of them
confirms the money – no screenshot, no admin in the loop:

1. **Gmail IMAP scan (primary, free).**  FamApp emails the Gmail address
   linked to the ``@fam`` UPI id on every credit.  A daemon thread watches
   that inbox, parses amount + UTR (:mod:`dreamxbotz.util.fampay_email`) and
   matches the credit against the pending order that reserved that exact
   paise amount.
2. **FamGateway API (fallback, optional).**  With ``FAMGATEWAY_API_KEY`` set,
   the QR/UPI intent is created through famgateway.in, its status endpoint is
   polled by the background worker, and its webhook
   (``POST /fampay/webhook``) approves instantly when the bot has a public
   URL.

Every order reserves a unique paise amount (₹40 → ₹40.07) so simultaneous
buyers of the same plan can never be confused, and every fulfilment path is
idempotent (``mark_paid`` only wins from ``pending``, UTRs are de-duplicated),
so racing verifiers can never double-grant premium.

If nothing verifies within ``FAMPAY_ORDER_EXPIRY_MINUTES`` the order moves to
``manual_pending``: the buyer is told the payment is with the admin, and the
admin gets Approve/Reject buttons (manual fallback).

Setup: docs/FAMPAY_SETUP.md
"""
import asyncio
import imaplib
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from email import message_from_bytes
from email.utils import parseaddr
from io import BytesIO
from typing import Optional

import aiohttp
import pytz
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from info import (
    ADMINS,
    FAMGATEWAY_API_KEY,
    FAMGATEWAY_BASE_URL,
    FAMGATEWAY_ENABLED,
    FAMPAY_EMAIL,
    FAMPAY_EMAIL_PASSWORD,
    FAMPAY_EMAIL_SENDER_FILTER,
    FAMPAY_ENABLED,
    FAMPAY_IMAP_ENABLED,
    FAMPAY_IMAP_HOST,
    FAMPAY_IMAP_MAILBOX,
    FAMPAY_IMAP_PORT,
    FAMPAY_ORDER_EXPIRY_MINUTES,
    FAMPAY_PAYEE_NAME,
    FAMPAY_PLANS,
    FAMPAY_POLL_INTERVAL,
    FAMPAY_UPI_ID,
    PREMIUM_LOGS,
    SUBSCRIPTION,
)
from Script import script
from utils import get_seconds
from database.payment_db import (
    STATUS_CANCELLED,
    STATUS_MANUAL_PENDING,
    STATUS_PAID,
    STATUS_PENDING,
    generate_order_id,
    paydb,
)
from database.users_chats_db import db
from dreamxbotz.server import fampay_webhook
from dreamxbotz.util.fampay_email import parse_payment_email
from dreamxbotz.util.fampay_qr import (
    build_upi_intent,
    format_inr,
    generate_qr_png,
    unique_payable_amount,
)

logger = logging.getLogger(__name__)

#: Seconds between IMAP inbox scans.  FamApp emails land within a few seconds;
#: scanning more often than this only burns Gmail quota.
IMAP_POLL_SECONDS = 15

#: In-memory guards (per process): UTRs already acted on, unmatched credits
#: already reported, orders already escalated to the admin.
_processed_utrs = set()
_reported_unmatched = set()
_escalated_orders = set()
_workers_started = False
_loop: Optional[asyncio.AbstractEventLoop] = None
#: The bot Client, captured once in the main thread so the IMAP worker never
#: has to import dreamxbotz.Bot (which builds a pyrogram Client) from a thread.
_CLIENT = None

_IST = pytz.timezone("Asia/Kolkata")

_PLAN_UNITS = {
    "day": "ᴅᴀʏꜱ",
    "month": "ᴍᴏɴᴛʜ",
    "year": "ʏᴇᴀʀ",
    "hour": "ʜᴏᴜʀꜱ",
    "min": "ᴍɪɴᴜᴛᴇꜱ",
    "s": "sᴇᴄᴏɴᴅs",
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def bot_client():
    """The shared bot Client (captured on the main thread at startup)."""
    global _CLIENT
    if _CLIENT is None:
        from dreamxbotz.Bot import dreamxbotz

        _CLIENT = dreamxbotz
    return _CLIENT


def plan_label(plan_time: str) -> str:
    """``7day`` → ``07 ᴅᴀʏꜱ`` (falls back to the raw string)."""
    match = re.match(r"\s*(\d+)\s*([a-zA-Z]+)", str(plan_time))
    if not match:
        return str(plan_time)
    number, unit = match.group(1), match.group(2).lower()
    return f"{number.zfill(2)} {_PLAN_UNITS.get(unit, unit)}"


def fampay_configured() -> bool:
    """True when at least one verifier can actually run."""
    if not FAMPAY_ENABLED or not FAMPAY_UPI_ID:
        return False
    imap_ready = bool(FAMPAY_IMAP_ENABLED and FAMPAY_EMAIL and FAMPAY_EMAIL_PASSWORD)
    gateway_ready = bool(FAMGATEWAY_ENABLED and FAMGATEWAY_API_KEY)
    return imap_ready or gateway_ready


def _plan_lines() -> str:
    return "\n".join(
        f"◉ {plan_label(plan_time)} — {format_inr(amount)}"
        for amount, plan_time in sorted(FAMPAY_PLANS.items())
    )


def _plans_caption() -> str:
    return script.FAMPAY_PLANS_TXT.format(
        plan_list=_plan_lines(),
        upi_id=FAMPAY_UPI_ID,
        expiry=FAMPAY_ORDER_EXPIRY_MINUTES,
    )


def _plan_buttons():
    buttons = [
        InlineKeyboardButton(
            f"{format_inr(amount)} · {plan_label(plan_time)}",
            callback_data=f"fampay_{amount}",
        )
        for amount, plan_time in sorted(FAMPAY_PLANS.items())
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("⋞ ʙᴀᴄᴋ", callback_data="buy_info")])
    return InlineKeyboardMarkup(rows)


def _order_buttons(order_id: str, upi_intent: str, checkout_url: str = ""):
    rows = []
    if upi_intent:
        rows.append([InlineKeyboardButton("📲 ᴘᴀʏ ᴠɪᴀ ᴜᴘɪ ᴀᴘᴘ", url=upi_intent)])
    if checkout_url:
        rows.append([InlineKeyboardButton("🌐 ᴡᴇʙ ᴄʜᴇᴄᴋᴏᴜᴛ", url=checkout_url)])
    rows.append(
        [
            InlineKeyboardButton("✅ ᴘᴀɪᴅ? ᴠᴇʀɪꜰʏ", callback_data=f"famcheck_{order_id}"),
            InlineKeyboardButton("🚫 ᴄᴀɴᴄᴇʟ", callback_data=f"famcancel_{order_id}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def notify_admins(text: str, reply_markup=None):
    """Best-effort admin notification (a blocked admin never breaks a payment)."""
    client = bot_client()
    for admin_id in ADMINS:
        try:
            await client.send_message(
                chat_id=int(admin_id),
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.warning("FamPay: could not notify admin %s: %s", admin_id, exc)


# --------------------------------------------------------------------------- #
# Premium fulfilment (shared by every verifier)
# --------------------------------------------------------------------------- #
async def grant_premium(client: Client, user_id: int, plan_time: str) -> Optional[str]:
    """Give ``user_id`` premium for ``plan_time``; return the IST expiry string.

    Mirrors ``/add_premium`` and the Telegram-Stars handler, so every payment
    method writes the exact same ``expiry_time`` the rest of the bot reads.
    """
    seconds = await get_seconds(plan_time)
    if seconds <= 0:
        logger.error("FamPay: invalid plan_time %r for user %s", plan_time, user_id)
        return None
    expiry_time = datetime.now() + timedelta(seconds=seconds)
    await db.update_user({"id": user_id, "expiry_time": expiry_time})
    data = await db.get_user(user_id)
    expiry = (data or {}).get("expiry_time") or expiry_time
    if expiry.tzinfo is None:  # keep the bot's naive-IST convention
        expiry = _IST.localize(expiry)
    return expiry.astimezone(_IST).strftime("%d-%m-%Y | %I:%M:%S %p")


def _paid_text(plan_time: str, payable: str, utr: str, expiry: str) -> str:
    return script.FAMPAY_PAID_TXT.format(
        plan=plan_label(plan_time),
        payable=payable,
        utr=utr or "ɴ/ᴀ",
        expiry=expiry,
    )


async def fulfil_order(client: Client, order_id: str, utr: str = "", sender_name: str = "",
                      verified_by: str = "imap") -> bool:
    """Mark an order paid (once!) and activate its premium.

    Returns ``True`` when *this* call performed the fulfilment and ``False``
    when another verifier had already done it (idempotency guard).
    """
    order = await paydb.get_order(order_id)
    if not order or order.get("status") != STATUS_PENDING:
        return False
    if utr and await paydb.utr_already_used(utr):
        logger.warning("FamPay: UTR %s already used, ignoring order %s", utr, order_id)
        return False

    paid = await paydb.mark_paid(order_id, utr=utr, sender_name=sender_name, verified_by=verified_by)
    if not paid:  # lost the race against another verifier
        return False

    user_id = order["user_id"]
    plan_time = order["plan_time"]
    payable = format_inr(order["payable_amount"])
    expiry_str = await grant_premium(client, user_id, plan_time)
    if expiry_str is None:
        # Should never happen (plans are static) – escalate to the admin.
        await paydb.transition_status(order_id, STATUS_PAID, STATUS_MANUAL_PENDING)
        await notify_admins(
            f"⚠️ <b>FamPay order {order_id} paid but plan '{plan_time}' is invalid.</b>\n"
            f"User: <code>{user_id}</code> · Amount: {payable} · UTR: <code>{utr or 'n/a'}</code>\n"
            f"Add manually: <code>/add_premium {user_id} {plan_time}</code>"
        )
        return False

    # 1) tell the buyer
    try:
        mention = (await client.get_users(user_id)).mention
    except Exception:
        mention = f"<code>{user_id}</code>"
    try:
        await client.send_message(
            chat_id=user_id,
            text=_paid_text(plan_time, payable, utr, expiry_str),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.warning("FamPay: could not message user %s: %s", user_id, exc)

    # 2) replace the QR caption so the paid order stops looking payable
    chat_id = paid.get("chat_id")
    qr_message_id = paid.get("qr_message_id")
    if chat_id and qr_message_id:
        try:
            await client.edit_message_caption(
                chat_id,
                qr_message_id,
                caption=_paid_text(plan_time, payable, utr, expiry_str),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    # 3) log channel
    try:
        await client.send_message(
            chat_id=PREMIUM_LOGS,
            text=(
                f"#FamPay_Purchase\n\n"
                f"👤 ᴜꜱᴇʀ - {mention}\n"
                f"⚡ ᴜꜱᴇʀ ɪᴅ - <code>{user_id}</code>\n"
                f"💳 ᴀᴍᴏᴜɴᴛ - {payable}\n"
                f"🧾 ᴜᴛʀ - <code>{utr or 'ɴ/ᴀ'}</code>\n"
                f"👤 ꜱᴇɴᴅᴇʀ - {sender_name or 'ɴ/ᴀ'}\n"
                f"🔍 ᴠᴇʀɪꜰɪᴇᴅ ʙʏ - {verified_by}\n"
                f"⏰ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇss - <code>{plan_label(plan_time)}</code>\n"
                f"⌛️ ᴇxᴘɪʀʏ ᴅᴀᴛᴇ - {expiry_str}"
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.warning("FamPay: could not log to PREMIUM_LOGS: %s", exc)
    return True


async def webhook_fulfil(order_id: str, utr: str, sender_name: str) -> bool:
    """Entry point used by the FamGateway webhook route."""
    return await fulfil_order(bot_client(), order_id, utr=utr, sender_name=sender_name,
                              verified_by="webhook")


# --------------------------------------------------------------------------- #
# FamGateway REST helpers (fallback verifier)
# --------------------------------------------------------------------------- #
async def famgateway_create_order(amount: float, customer_name: str) -> Optional[dict]:
    """Create the QR/intent through FamGateway; ``None`` on any failure."""
    if not (FAMGATEWAY_ENABLED and FAMGATEWAY_API_KEY):
        return None
    url = f"{FAMGATEWAY_BASE_URL}/api/create-order"
    payload = {
        "api_key": FAMGATEWAY_API_KEY,
        "amount": round(float(amount), 2),
        "customer_name": (customer_name or "")[:64],
    }
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                data = await resp.json(content_type=None)
    except Exception as exc:
        logger.warning("FamPay: FamGateway create-order failed: %s", exc)
        return None
    if str(data.get("status", "")).lower() != "success":
        logger.warning("FamPay: FamGateway create-order rejected: %s", data)
        return None
    return data.get("data") or None


async def famgateway_order_status(fg_order_id: str) -> Optional[dict]:
    """Ask FamGateway whether an order was paid."""
    if not (FAMGATEWAY_ENABLED and FAMGATEWAY_API_KEY and fg_order_id):
        return None
    url = f"{FAMGATEWAY_BASE_URL}/api/verify-order.php"
    params = {"api_key": FAMGATEWAY_API_KEY, "order_id": fg_order_id}
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                data = await resp.json(content_type=None)
    except Exception as exc:
        logger.warning("FamPay: FamGateway verify-order failed: %s", exc)
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# Order creation / payment message
# --------------------------------------------------------------------------- #
async def _qr_photo(order: dict, upi_intent: str) -> tuple:
    """Return ``(photo, upi_intent, checkout_url)`` for the payment message.

    Preference order: FamGateway's QR image → its image URL → a locally
    generated QR of the UPI intent (works with zero third-party services).
    """
    checkout_url = order.get("fg_checkout_url") or ""
    fg_qr_url = order.get("fg_qr_url") or ""
    if fg_qr_url:
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(fg_qr_url) as resp:
                    if resp.status == 200:
                        return BytesIO(await resp.read()), upi_intent, checkout_url
        except Exception as exc:
            logger.warning("FamPay: could not download FamGateway QR: %s", exc)
        return fg_qr_url, upi_intent, checkout_url
    return BytesIO(generate_qr_png(upi_intent)), upi_intent, checkout_url


async def send_payment_message(client: Client, chat_id: int, order: dict):
    """(Re)send the QR + payable-amount message for ``order``."""
    plan_time = order["plan_time"]
    payable = round(float(order["payable_amount"]), 2)
    paise = f"{payable:.2f}".split(".")[1]
    upi_intent = order.get("fg_upi_intent") or build_upi_intent(
        FAMPAY_UPI_ID, FAMPAY_PAYEE_NAME, payable, note=order["order_id"]
    )

    photo, intent, checkout_url = await _qr_photo(order, upi_intent)
    caption = script.FAMPAY_ORDER_TXT.format(
        plan=plan_label(plan_time),
        payable=format_inr(payable),
        paise=paise,
        order_id=order["order_id"],
        upi_id=FAMPAY_UPI_ID,
        expiry=FAMPAY_ORDER_EXPIRY_MINUTES,
    )
    sent = await client.send_photo(
        chat_id=chat_id,
        photo=photo,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=_order_buttons(order["order_id"], intent, checkout_url),
    )
    await paydb.set_message_ref(order["order_id"], chat_id, sent.id)
    return sent


async def create_order_for_user(client: Client, chat_id: int, user, amount: float) -> Optional[dict]:
    """Create (or reuse) a pending order for ``user`` and show its QR."""
    active = await paydb.get_active_order(user.id)
    if active and not paydb.is_expired(active):
        await send_payment_message(client, chat_id, active)
        return active

    taken = await paydb.pending_amounts()
    payable = unique_payable_amount(amount, taken)

    fg_data = await famgateway_create_order(amount, f"{user.first_name or ''} ({user.id})".strip())
    fg_order_id = fg_qr_url = fg_intent = fg_checkout = ""
    if fg_data:
        # Trust FamGateway's own session (amount + QR + intent) so both
        # verifiers watch the exact same rupee figure.
        fg_order_id = str(fg_data.get("order_id") or "")
        fg_qr_url = str(fg_data.get("qr_url") or "")
        fg_intent = str(fg_data.get("upi_intent") or "")
        fg_checkout = str(fg_data.get("checkout_url") or "")
        try:
            payable = round(float(fg_data.get("payable_amount") or payable), 2)
        except (TypeError, ValueError):
            pass

    order_id = generate_order_id()
    plan_time = FAMPAY_PLANS[int(amount)]
    order = await paydb.create_order(
        order_id=order_id,
        user_id=user.id,
        user_name=user.first_name or "",
        amount=amount,
        payable_amount=payable,
        plan_time=plan_time,
        expires_at=paydb.expiry_from_now(FAMPAY_ORDER_EXPIRY_MINUTES),
        fg_order_id=fg_order_id,
        chat_id=chat_id,
        fg_qr_url=fg_qr_url,
        fg_upi_intent=fg_intent,
        fg_checkout_url=fg_checkout,
    )
    logger.info(
        "FamPay: order %s created for %s (₹%.2f → ₹%.2f, %s, fg=%s)",
        order_id, user.id, amount, payable, plan_time, fg_order_id or "-",
    )
    await send_payment_message(client, chat_id, order)
    return order


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
@Client.on_message(filters.command("fampay") & filters.private)
async def fampay_command(client: Client, message: Message):
    """Show the FamPay plan menu (handy after /plan auto-deletes)."""
    if not fampay_configured():
        return await message.reply_text(
            "⚠️ <b>FamPay payments abhi configure nahi hain.</b>\n"
            "Owner se contact karein ya screenshot method use karein (/plan).",
            parse_mode=ParseMode.HTML,
        )
    await message.reply_photo(
        photo=SUBSCRIPTION,
        caption=_plans_caption(),
        parse_mode=ParseMode.HTML,
        reply_markup=_plan_buttons(),
    )


@Client.on_callback_query(filters.regex(r"^fampay_info$"))
async def fampay_info_callback(client: Client, callback_query: CallbackQuery):
    if not fampay_configured():
        return await callback_query.answer(
            "⚠️ FamPay payments abhi configure nahi hain.", show_alert=True
        )
    try:
        await client.edit_message_media(
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.id,
            media=InputMediaPhoto(SUBSCRIPTION, caption=_plans_caption()),
            reply_markup=_plan_buttons(),
        )
    except Exception:
        await client.send_photo(
            chat_id=callback_query.message.chat.id,
            photo=SUBSCRIPTION,
            caption=_plans_caption(),
            parse_mode=ParseMode.HTML,
            reply_markup=_plan_buttons(),
        )
    await callback_query.answer()


@Client.on_callback_query(filters.regex(r"^fampay_(\d+)$"))
async def fampay_buy_callback(client: Client, callback_query: CallbackQuery):
    amount = int(callback_query.data.split("_")[-1])
    if amount not in FAMPAY_PLANS:
        return await callback_query.answer("⚠️ Invalid plan.", show_alert=True)
    if not fampay_configured():
        return await callback_query.answer(
            "⚠️ FamPay payments abhi configure nahi hain.", show_alert=True
        )
    await callback_query.answer()
    try:
        await create_order_for_user(
            client, callback_query.message.chat.id, callback_query.from_user, amount
        )
    except Exception:
        logger.exception("FamPay: could not create order for %s", callback_query.from_user.id)
        await callback_query.answer("🚫 Order ban nahi paya, dobara try karein.", show_alert=True)


@Client.on_callback_query(filters.regex(r"^famcheck_(\S+)$"))
async def fampay_check_callback(client: Client, callback_query: CallbackQuery):
    order_id = callback_query.data.split("_", 1)[1]
    order = await paydb.get_order(order_id)
    if not order or order.get("user_id") != callback_query.from_user.id:
        return await callback_query.answer("🚫 Order not found.", show_alert=True)

    if order.get("status") == STATUS_PAID:
        return await callback_query.answer("✅ Payment already verified!", show_alert=True)

    # Fallback verifier: ask FamGateway right now (if this order came from it).
    if order.get("fg_order_id"):
        status = await famgateway_order_status(order["fg_order_id"])
        if status and str(status.get("status", "")).lower() == "success":
            data = status.get("data") or {}
            granted = await fulfil_order(
                client,
                order_id,
                utr=str(data.get("utr") or ""),
                sender_name=str(data.get("sender_name") or ""),
                verified_by="famgateway",
            )
            if granted:
                return await callback_query.answer("✅ Payment verified! Premium active.", show_alert=True)

    if paydb.is_expired(order):
        await paydb.transition_status(order_id, STATUS_PENDING, STATUS_MANUAL_PENDING)
        return await callback_query.answer(
            "⌛ Order expire ho gaya. Agar aapne pay kiya hai to admin ko bata diya gaya hai.",
            show_alert=True,
        )

    await callback_query.answer(
        "⏳ Payment abhi receive nahi hua. Pay karne ke 10-20 second baad dobara check karein.",
        show_alert=True,
    )


@Client.on_callback_query(filters.regex(r"^famcancel_(\S+)$"))
async def fampay_cancel_callback(client: Client, callback_query: CallbackQuery):
    order_id = callback_query.data.split("_", 1)[1]
    order = await paydb.get_order(order_id)
    if not order or order.get("user_id") != callback_query.from_user.id:
        return await callback_query.answer("🚫 Order not found.", show_alert=True)
    if order.get("status") != STATUS_PENDING:
        return await callback_query.answer("Ye order already process ho chuka hai.", show_alert=True)
    await paydb.transition_status(order_id, STATUS_PENDING, STATUS_CANCELLED)
    try:
        await client.edit_message_caption(
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.id,
            caption=script.FAMPAY_CANCELLED_TXT.format(order_id=order_id),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    await callback_query.answer("Order cancel kar diya gaya.")


@Client.on_callback_query(filters.regex(r"^famapprove_(\S+)$") & filters.user(ADMINS))
async def fampay_approve_callback(client: Client, callback_query: CallbackQuery):
    """Manual fallback: admin approves an expired/unverified order."""
    order_id = callback_query.data.split("_", 1)[1]
    order = await paydb.get_order(order_id)
    if not order:
        return await callback_query.answer("🚫 Order not found.", show_alert=True)
    if order.get("status") == STATUS_PAID:
        return await callback_query.answer("Already paid/approved.", show_alert=True)
    if order.get("status") == STATUS_CANCELLED:
        return await callback_query.answer("Ye order cancel tha.", show_alert=True)

    # fulfil_order only accepts pending orders – move manual_pending → pending
    # atomically first so two admin taps can never double-grant.
    if order.get("status") != STATUS_PENDING:
        moved = await paydb.transition_status(order_id, order["status"], STATUS_PENDING)
        if not moved:
            return await callback_query.answer("Order already process ho chuka hai.", show_alert=True)

    granted = await fulfil_order(client, order_id, verified_by="manual")
    if granted:
        _escalated_orders.discard(order_id)
        await callback_query.answer("✅ Premium granted manually.")
        try:
            await callback_query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    else:
        await callback_query.answer("🚫 Could not grant premium, logs check karein.", show_alert=True)


@Client.on_callback_query(filters.regex(r"^famreject_(\S+)$") & filters.user(ADMINS))
async def fampay_reject_callback(client: Client, callback_query: CallbackQuery):
    order_id = callback_query.data.split("_", 1)[1]
    await paydb.mark_status(order_id, STATUS_CANCELLED)
    _escalated_orders.discard(order_id)
    await callback_query.answer("Order reject kar diya gaya.")
    try:
        await callback_query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


@Client.on_message(filters.command("fampay_orders") & filters.user(ADMINS))
async def fampay_orders_command(client: Client, message: Message):
    counts = await paydb.count_by_status()
    recent = await paydb.recent_orders(10)
    lines = [
        "<b>⚡ FamPay orders</b>",
        "",
        f"• ᴘᴇɴᴅɪɴɢ: <code>{counts.get(STATUS_PENDING, 0)}</code>",
        f"• ᴘᴀɪᴅ: <code>{counts.get(STATUS_PAID, 0)}</code>",
        f"• ᴍᴀɴᴜᴀʟ ʀᴇᴠɪᴇᴡ: <code>{counts.get(STATUS_MANUAL_PENDING, 0)}</code>",
        f"• ᴇxᴘɪʀᴇᴅ: <code>{counts.get('expired', 0)}</code>",
        f"• ᴄᴀɴᴄᴇʟʟᴇᴅ: <code>{counts.get(STATUS_CANCELLED, 0)}</code>",
        "",
        "<b>ʟᴀᴛᴇsᴛ 10:</b>",
    ]
    for order in recent:
        line = (
            f"• <code>{order['order_id']}</code> · <code>{order['user_id']}</code> · "
            f"₹{order['payable_amount']:.2f} · {order['status']}"
        )
        if order.get("utr"):
            line += f" · utr <code>{order['utr']}</code>"
        lines.append(line)
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# --------------------------------------------------------------------------- #
# Background workers
# --------------------------------------------------------------------------- #
async def escalate_to_admin(order: dict):
    """Expired-unpaid order → tell the buyer and offer the admin a decision."""
    client = bot_client()
    order_id = order["order_id"]
    moved = await paydb.transition_status(order_id, STATUS_PENDING, STATUS_MANUAL_PENDING)
    if not moved:  # another caller already transitioned it
        return
    try:
        await client.send_message(
            chat_id=order.get("user_id"),
            text=script.FAMPAY_EXPIRED_TXT.format(order_id=order_id),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    if order_id in _escalated_orders:
        return
    _escalated_orders.add(order_id)
    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ ᴀᴘᴘʀᴏᴠᴇ", callback_data=f"famapprove_{order_id}"),
                InlineKeyboardButton("🚫 ʀᴇᴊᴇᴄᴛ", callback_data=f"famreject_{order_id}"),
            ]
        ]
    )
    await notify_admins(
        f"<b>#FamPay_Manual_Review</b>\n\n"
        f"ᴏʀᴅᴇʀ <code>{order_id}</code> ᴇxᴘɪʀᴇ ʜᴏ ɢᴀʏᴀ (ᴀᴜᴛᴏ-ᴠᴇʀɪꜰʏ ɴᴀʜɪ ʜᴜᴀ)।\n"
        f"ᴜsᴇʀ: <code>{order.get('user_id')}</code> ({order.get('user_name') or 'ɴ/ᴀ'})\n"
        f"ᴀᴍᴏᴜɴᴛ: ₹{order.get('payable_amount', 0):.2f} · "
        f"ᴘʟᴀɴ: {plan_label(order.get('plan_time', ''))}\n\n"
        f"ᴀɢᴀʀ ᴜsᴇʀ ɴᴇ ᴘᴀʏ ᴋᴀʀ ᴅɪʏᴀ ᴛᴏ ✅ ᴅᴀʙᴀʏᴇᴍ, ᴡᴀʀɴᴀ 🚫।",
        reply_markup=buttons,
    )


async def fampay_poll_cycle():
    """One pass: expire stale orders, poll FamGateway for pending ones."""
    client = bot_client()
    await paydb.ensure_indexes()
    now = datetime.now()
    for order in await paydb.pending_orders():
        if paydb.is_expired(order, now):
            await escalate_to_admin(order)
            continue
        fg_order_id = order.get("fg_order_id")
        if not fg_order_id:
            continue
        status = await famgateway_order_status(fg_order_id)
        if not status:
            continue
        state = str(status.get("status", "")).lower()
        if state == "success":
            data = status.get("data") or {}
            await fulfil_order(
                client,
                order["order_id"],
                utr=str(data.get("utr") or ""),
                sender_name=str(data.get("sender_name") or ""),
                verified_by="famgateway",
            )
        elif state == "expired":
            await escalate_to_admin(order)


async def fampay_poll_worker():
    while True:
        try:
            await fampay_poll_cycle()
        except Exception:
            logger.exception("FamPay: poll cycle failed")
        await asyncio.sleep(max(5, int(FAMPAY_POLL_INTERVAL)))


# --------------------------------------------------------------------------- #
# IMAP worker (primary verifier) – runs in its own daemon thread
# --------------------------------------------------------------------------- #
def email_text(raw_bytes: bytes) -> str:
    """Flatten an email (headers + text parts) into one string."""
    message = message_from_bytes(raw_bytes)
    chunks = [str(message.get("Subject", "")), str(message.get("From", ""))]
    if message.is_multipart():
        parts = message.walk()
    else:
        parts = [message]
    for part in parts:
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            chunks.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    return "\n".join(chunks)


def _run_coro(coro, loop, timeout=20):
    """Run a coroutine from the IMAP thread and wait for its result."""
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)


def imap_scan_once(loop) -> int:
    """Scan unseen FamApp emails and fulfil matching orders; return match count."""
    client = bot_client()
    matches = 0
    mail = imaplib.IMAP4_SSL(FAMPAY_IMAP_HOST, FAMPAY_IMAP_PORT)
    try:
        # Google shows app passwords as "abcd efgh ijkl mnop" – IMAP wants the
        # bare 16 characters, so strip any spaces the operator pasted.
        mail.login(FAMPAY_EMAIL, FAMPAY_EMAIL_PASSWORD.replace(" ", ""))
        mail.select(FAMPAY_IMAP_MAILBOX)
        typ, data = mail.search(None, "UNSEEN")
        if typ != "OK":
            return 0
        for num in data[0].split():
            typ, fetched = mail.fetch(num, "(RFC822)")
            if typ != "OK" or not fetched or not fetched[0]:
                continue
            raw = fetched[0][1]
            sender = parseaddr(message_from_bytes(raw).get("From", ""))[1].lower()
            if FAMPAY_EMAIL_SENDER_FILTER and FAMPAY_EMAIL_SENDER_FILTER.lower() not in sender:
                continue  # not a FamApp notification – leave it unread
            parsed = parse_payment_email(email_text(raw))
            if not parsed:
                mail.store(num, "+FLAGS", "\\Seen")  # nothing to match; never re-scan
                continue

            order = _run_coro(paydb.find_pending_by_amount(parsed.amount), loop)
            if order and not (parsed.utr and parsed.utr in _processed_utrs):
                if parsed.utr:
                    _processed_utrs.add(parsed.utr)
                granted = _run_coro(
                    fulfil_order(client, order["order_id"], utr=parsed.utr or "",
                                 sender_name=parsed.sender_name, verified_by="imap"),
                    loop,
                )
                mail.store(num, "+FLAGS", "\\Seen")
                if granted:
                    matches += 1
            else:
                # A credit we cannot attribute (wrong amount, or a UTR already
                # used).  Report once so the admin can still settle by hand.
                mail.store(num, "+FLAGS", "\\Seen")
                key = parsed.utr or f"{parsed.amount}:{parsed.sender_name}"
                if key not in _reported_unmatched:
                    _reported_unmatched.add(key)
                    logger.info("FamPay IMAP: unmatched credit ₹%.2f utr=%s from=%s",
                                parsed.amount, parsed.utr, parsed.sender_name)
                    try:
                        _run_coro(
                            notify_admins(
                                f"<b>#FamPay_Unmatched_Payment</b>\n\n"
                                f"ᴀᴍᴏᴜɴᴛ: ₹{parsed.amount:.2f}\n"
                                f"ᴜᴛʀ: <code>{parsed.utr or 'ɴ/ᴀ'}</code>\n"
                                f"sᴇɴᴅᴇʀ: {parsed.sender_name or 'ɴ/ᴀ'}\n\n"
                                f"ᴋᴏɪ ᴘᴇɴᴅɪɴɢ ᴏʀᴅᴇʀ ɴᴀʜɪ ᴍɪʟᴀ — ᴊᴀɴʙᴜᴊʜ ᴋᴀʀ ᴅɪʏᴀ ɢᴀʏᴀ ʜᴀɪ।"
                            ),
                            loop,
                            timeout=10,
                        )
                    except Exception:
                        pass
    finally:
        try:
            mail.close()
        except Exception:
            pass
        try:
            mail.logout()
        except Exception:
            pass
    return matches


def fampay_imap_worker(loop):
    """Blocking IMAP loop with backoff – never let it kill the thread."""
    backoff = 30
    while True:
        try:
            matches = imap_scan_once(loop)
            if matches:
                logger.info("FamPay IMAP: fulfilled %s order(s) this scan.", matches)
            backoff = 30  # healthy scan → short sleep
            time.sleep(IMAP_POLL_SECONDS)
        except Exception as exc:
            logger.warning("FamPay IMAP: scan failed (%s); retrying in %ss", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


def start_fampay_workers():
    """Start the poll worker and (when configured) the IMAP thread.

    Called from ``bot.py`` right after the plugins are loaded, so the event
    loop is already running.
    """
    global _workers_started, _loop, _CLIENT
    if _workers_started or not FAMPAY_ENABLED:
        return
    _workers_started = True

    try:  # capture on the main thread – the IMAP worker must not import it
        from dreamxbotz.Bot import dreamxbotz

        _CLIENT = dreamxbotz
    except Exception as exc:
        logger.warning("FamPay: could not capture bot client: %s", exc)

    fampay_webhook.set_fulfilment_handler(webhook_fulfil)

    if not fampay_configured():
        logger.warning(
            "FamPay: enabled but not configured (need FAMPAY_UPI_ID plus either "
            "FAMPAY_EMAIL/FAMPAY_EMAIL_PASSWORD or FAMGATEWAY_API_KEY)."
        )
        return

    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        _loop = asyncio.get_event_loop()
    _loop.create_task(fampay_poll_worker())

    if FAMPAY_IMAP_ENABLED and FAMPAY_EMAIL and FAMPAY_EMAIL_PASSWORD:
        threading.Thread(
            target=fampay_imap_worker,
            args=(_loop,),
            name="fampay-imap",
            daemon=True,
        ).start()
        logger.info("FamPay: IMAP verifier started for %s.", FAMPAY_EMAIL)
    else:
        logger.warning("FamPay: IMAP verifier disabled (no Gmail credentials).")
