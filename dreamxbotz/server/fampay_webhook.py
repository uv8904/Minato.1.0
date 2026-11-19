"""FamGateway webhook receiver for the FamPay auto-approval flow.

FamGateway POSTs a signed JSON payload here the moment a UPI credit lands in
the merchant's FamPay inbox – typically within a few seconds of the transfer,
long before the bot's own IMAP scan notices the email.  When the bot is
deployed with a public URL (``URL``/``PORT`` in ``info.py``) this makes
approval near-instant; without a public URL the IMAP worker and the
FamGateway status poll still cover everything, just a few seconds slower.

Security: an unsigned webhook is *never* trusted. If no signing secret is
configured the endpoint returns 503 instead of accepting arbitrary "success"
posts from the internet.

Route (registered in ``plugins/__init__.py`` **before** the catch-all stream
route so it can never be swallowed)::

    POST /fampay/webhook      X-FamGateway-Signature: <hmac-sha256 hex>

The signing secret is the merchant's FamGateway API key (that is how
FamGateway signs), or ``FAMGATEWAY_WEBHOOK_SECRET`` when the operator prefers
a separate one.  A request carrying a signature that does not verify is
rejected with 401 *before* any order is touched.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Awaitable, Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

#: ``async def handler(order_id, utr, sender_name) -> bool`` – registered by
#: plugins/FamPay.py so this module never has to import the plugin layer.
_fulfilment_handler: Optional[Callable[[str, str, str], Awaitable[bool]]] = None


def set_fulfilment_handler(handler: Callable[[str, str, str], Awaitable[bool]]) -> None:
    """Register the coroutine that grants premium for a verified order."""
    global _fulfilment_handler
    _fulfilment_handler = handler


def _webhook_secret() -> str:
    from info import FAMGATEWAY_API_KEY, FAMGATEWAY_WEBHOOK_SECRET

    return FAMGATEWAY_WEBHOOK_SECRET or FAMGATEWAY_API_KEY or ""


def _signature_ok(body: bytes, header_value: str) -> bool:
    secret = _webhook_secret()
    if not secret:
        # Fail closed. Accepting unsigned payloads meant anyone who could see
        # their own order id could grant themselves premium in the documented
        # IMAP-only setup (no FamGateway key = no signing secret).
        return False
    if not header_value:
        return False
    provided = header_value.strip()
    if provided.lower().startswith("sha256="):
        provided = provided[7:]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.lower())


def _first(payload: dict, *keys, default=""):
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return default


def _amount_matches(order: dict, payload: dict) -> bool:
    """Reject a signed webhook whose amount belongs to a different order."""
    raw = _first(payload, "amount", "payable_amount", "total", "amount_paid", default="")
    if raw in (None, ""):
        return True  # some gateways omit the amount; signature still required
    try:
        paid = round(float(str(raw).replace(",", "").replace("₹", "")), 2)
    except (TypeError, ValueError):
        return False
    expected = {
        round(float(order.get("amount") or 0), 2),
        round(float(order.get("payable_amount") or 0), 2),
    }
    return paid in expected


def _looks_paid(payload: dict) -> bool:
    status = str(_first(payload, "status", "event", "type", default="")).lower()
    if status in ("success", "paid", "payment.success", "completed", "credit"):
        return True
    # Some payloads only carry the UTR – treat that as a success event too.
    return not status and bool(_first(payload, "utr"))


@routes.post("/fampay/webhook")
async def famgateway_webhook(request: web.Request):
    from database.payment_db import paydb

    if not _webhook_secret():
        logger.error("FamPay webhook disabled: no signing secret configured")
        return web.json_response(
            {"status": "error", "reason": "webhook not configured"}, status=503
        )
    body = await request.read()
    signature = request.headers.get("X-FamGateway-Signature", "")
    if not _signature_ok(body, signature):
        logger.warning("FamPay webhook: rejected request with bad signature.")
        return web.json_response({"status": "error", "reason": "invalid signature"}, status=401)

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return web.json_response({"status": "error", "reason": "invalid json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"status": "error", "reason": "invalid payload"}, status=400)

    # FamGateway sometimes nests the transaction under "data".
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    merged = {**payload, **data}

    if not _looks_paid(merged):
        # pending/expired notifications are acknowledged but not acted on.
        return web.json_response({"status": "ignored"})

    order_ref = str(_first(merged, "order_id", "orderId", "order", "orderid"))
    utr = str(_first(merged, "utr", "utr_number", "reference", default=""))
    sender_name = str(_first(merged, "sender_name", "payer_name", "customer_name", default=""))

    order = await paydb.get_order(order_ref) if order_ref else None
    if order is None:
        order = await paydb.get_order_by_fg_id(order_ref)
    if order is None:
        logger.info("FamPay webhook: no pending order matches '%s'.", order_ref)
        return web.json_response({"status": "unknown_order"})
    if paydb.is_expired(order):
        logger.warning("FamPay webhook: expired order %s ignored.", order["order_id"])
        return web.json_response({"status": "expired_order"}, status=400)
    if not _amount_matches(order, merged):
        logger.warning("FamPay webhook: amount mismatch for %s ignored.", order["order_id"])
        return web.json_response({"status": "amount_mismatch"}, status=400)

    if _fulfilment_handler is None:
        logger.error("FamPay webhook: no fulfilment handler registered.")
        return web.json_response({"status": "error", "reason": "no handler"}, status=503)

    try:
        granted = await _fulfilment_handler(order["order_id"], utr, sender_name)
    except Exception:
        logger.exception("FamPay webhook: fulfilment failed for %s", order_ref)
        return web.json_response({"status": "error", "reason": "fulfilment failed"}, status=500)

    if granted:
        return web.json_response({"status": "ok", "order_id": order["order_id"]})
    # Already fulfilled by another verifier (IMAP/FamGateway poll) – still a
    # 200 so FamGateway stops retrying.
    return web.json_response({"status": "already_processed", "order_id": order["order_id"]})
