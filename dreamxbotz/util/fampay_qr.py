"""UPI intent links and QR images for the FamPay auto-approval flow.

Everything here is a **pure function** – no Telegram, no MongoDB, no network –
so the plugin and the tests can share exactly the same logic.

Why a per-order amount?
----------------------
FamPay has no merchant API: every payment lands in the same personal ``@fam``
UPI inbox, and dozens of users can be buying the same ₹40 plan at the same
time.  The only reliable way to tell *who* paid is to give every pending order
its own unique paise (₹40 → ₹40.07) and match the incoming credit against it.
``unique_payable_amount()`` keeps that promise: it never hands out a paise
offset that a still-pending order already uses.
"""
from __future__ import annotations

import io
from typing import Iterable, Set
from urllib.parse import quote

#: UPI deep links are plain ``upi://pay?...`` URLs; Telegram opens them in the
#: user's installed UPI app (PhonePe / Google Pay / Paytm / FamApp…).
UPI_SCHEME = "upi://pay?"


def build_upi_intent(upi_id: str, payee_name: str, amount: float, note: str = "") -> str:
    """Return a NPCI ``upi://pay`` deep link for ``amount`` INR.

    ``note`` is embedded as the UPI transaction note (``tn``) – UPI apps show
    it in the payment screen, which helps a payer double-check the order id.
    """
    upi_id = (upi_id or "").strip()
    payee_name = (payee_name or "").strip() or "Merchant"
    amount = round(float(amount), 2)
    params = {
        "pa": upi_id,                       # payee VPA
        "pn": payee_name,                   # payee name
        "am": f"{amount:.2f}",              # exact amount, 2 decimals
        "cu": "INR",                        # currency
    }
    if note:
        params["tn"] = note
    query = "&".join(f"{key}={quote(str(value), safe='')}" for key, value in params.items())
    return UPI_SCHEME + query


def generate_qr_png(data: str, box_size: int = 10, border: int = 2) -> bytes:
    """Render ``data`` (usually a UPI intent) as PNG bytes, ready to upload."""
    import qrcode  # imported lazily so the module stays import-safe

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def round2(value: float) -> float:
    """Round to paise – the only precision UPI understands."""
    return round(float(value) + 0.0, 2)


def unique_payable_amount(base_amount: float, taken: Iterable[float]) -> float:
    """Return ``base_amount`` plus a unique paise offset.

    ``taken`` is the set of payable amounts currently reserved by pending
    orders.  The first free offset (₹0.01 … ₹0.99) wins; if all 99 offsets are
    somehow in use we fall back to the bare amount (the order id in the UPI
    note plus UTR de-duplication still keep matching safe).
    """
    base = round2(base_amount)
    taken_set: Set[float] = {round2(value) for value in (taken or ())}
    for paise in range(1, 100):
        candidate = round2(base + paise / 100.0)
        if candidate not in taken_set:
            return candidate
    return base


def pay_link_html(upi_intent: str) -> str:
    """Render a UPI deep link as a caption-safe inline link.

    Inline-keyboard URL buttons may only use http(s)/tg schemes: Telegram
    rejects anything else (``upi://``, ``intent://`` …) with
    ``400 BUTTON_URL_INVALID`` **before the message is even sent** – on both
    the Bot API and the MTProto layer pyrogram/electrogram speaks.  Text-link
    entities inside a caption have no such scheme restriction, so the
    checkout keeps its "Pay in UPI app" tap target here instead of a button.

    ``&`` must be HTML-escaped in the href attribute, otherwise the caption
    fails entity parsing and the send errors out the same way.
    """
    href = (upi_intent or "").strip()
    if not href:
        return ""
    href = href.replace("&", "&amp;").replace('"', "%22").replace("<", "%3C").replace(">", "%3E")
    return f'<a href="{href}">📲 ᴛᴀᴘ ʜᴇʀᴇ ᴛᴏ ᴘᴀʏ ɪɴ ᴜᴘɪ ᴀᴘᴘ</a>'


def format_inr(amount: float) -> str:
    """``10.07`` → ``₹10.07`` (always two decimals, Indian grouping)."""
    return f"₹{round2(amount):,.2f}"


def plan_button_label(amount: float, plan_time: str) -> str:
    """Button text for the plan list, e.g. ``₹10 · 7 ᴅᴀʏꜱ``."""
    return f"{format_inr(amount)} · {plan_time}"
