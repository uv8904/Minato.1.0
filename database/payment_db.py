"""MongoDB storage for FamPay auto-approval payment orders.

Collection: ``fampay_orders`` inside the bot's own ``DATABASE_NAME`` – the
same database the rest of the bot already uses, so no extra cluster has to be
provisioned.

Document schema (``fampay_orders``)
----------------------------------
=================== ======= ==================================================
field               type    notes
=================== ======= ==================================================
``_id``             str     internal order id, ``FMP-AB12CD``
``order_id``        str     same as ``_id`` (convenience for the webhook)
``user_id``         int     Telegram user who must receive the premium
``user_name``       str     mention/name snapshot for logs
``amount``          float   base plan price in ₹ (10 / 20 / 40 / 55 / 75 …)
``payable_amount``  float   unique paise amount the user must transfer
``plan_time``       str     duration understood by ``utils.get_seconds``
``status``          str     ``pending`` / ``paid`` / ``manual_pending`` /
                             ``expired`` / ``cancelled``
``utr``             str     bank UTR once verified
``sender_name``     str     payer name from the bank/email notification
``verified_by``     str     ``imap`` / ``famgateway`` / ``webhook`` / ``manual``
``fg_order_id``     str     FamGateway order id when that API created the QR
``fg_qr_url``       str     FamGateway QR image URL (when that API was used)
``fg_upi_intent``   str     FamGateway UPI deep link (when that API was used)
``fg_checkout_url`` str     FamGateway hosted checkout page
``chat_id``         int     chat holding the QR message (so it can be edited)
``qr_message_id``   int     the QR message id
``order_placed_at`` datetime buyer tapped the post-payment "Order placed" step
``submitted_utr``   str     buyer-supplied UTR, awaiting an admin's review
``utr_submitted_at`` datetime when ``submitted_utr`` was confirmed by the buyer
``created_at``      datetime naive local time (same convention as the rest of
                             the bot's premium data)
``updated_at``      datetime last write
``expires_at``      datetime after this the order stops auto-approving
``paid_at``         datetime when the payment was confirmed
=================== ======= ==================================================

Only order metadata is stored – never any Gmail/FamPay credential.
"""
from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Order id prefix + length.  Short enough to show in a Telegram caption.
ORDER_PREFIX = "FMP-"
ORDER_ID_LENGTH = 6
ORDER_ID_ALPHABET = string.ascii_uppercase + string.digits

STATUS_PENDING = "pending"
STATUS_PAID = "paid"
STATUS_MANUAL_PENDING = "manual_pending"   # expired unpaid → admin fallback
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"

DEFAULT_COLLECTION_NAME = "fampay_orders"


def generate_order_id() -> str:
    """``FMP-`` + 6 cryptographically random characters."""
    body = "".join(secrets.choice(ORDER_ID_ALPHABET) for _ in range(ORDER_ID_LENGTH))
    return ORDER_PREFIX + body


def _default_database():
    """The bot's own database – built lazily so tests can inject fakes."""
    from motor.motor_asyncio import AsyncIOMotorClient

    from info import DATABASE_NAME, DATABASE_URI

    return AsyncIOMotorClient(DATABASE_URI)[DATABASE_NAME]


class FamPayOrderStore:
    """Thin, testable wrapper around the ``fampay_orders`` collection."""

    def __init__(self, database=None, collection=None, collection_name: Optional[str] = None):
        self._database = database
        self._collection = collection
        self._collection_name = collection_name
        self._indexes_ready = False

    # ------------------------------------------------------------------ #
    # Connection helpers
    # ------------------------------------------------------------------ #
    @property
    def collection_name(self) -> str:
        if self._collection_name:
            return self._collection_name
        try:  # imported lazily so the module stays import-safe without info.py
            from info import FAMPAY_ORDERS_COLLECTION  # type: ignore

            return FAMPAY_ORDERS_COLLECTION
        except Exception:
            return DEFAULT_COLLECTION_NAME

    @property
    def col(self):
        """The motor collection (memoised)."""
        if self._collection is None:
            if self._database is None:
                self._database = _default_database()
            self._collection = self._database[self.collection_name]
        return self._collection

    async def ensure_indexes(self) -> None:
        """Indexes for the worker scans and the per-user lookups."""
        if self._indexes_ready:
            return
        try:
            await self.col.create_index("status", name="fp_status", background=True)
            await self.col.create_index("user_id", name="fp_user", background=True)
            await self.col.create_index("payable_amount", name="fp_payable", background=True)
            await self.col.create_index("created_at", name="fp_created", background=True)
            self._indexes_ready = True
            logger.info("FamPay: indexes ready on '%s'.", self.collection_name)
        except Exception as exc:  # never break bot startup because of an index
            logger.warning("FamPay: could not create indexes: %s", exc)

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    async def create_order(
        self,
        *,
        order_id: str,
        user_id: int,
        user_name: str,
        amount: float,
        payable_amount: float,
        plan_time: str,
        expires_at: datetime,
        fg_order_id: Optional[str] = None,
        chat_id: Optional[int] = None,
        qr_message_id: Optional[int] = None,
        fg_qr_url: Optional[str] = None,
        fg_upi_intent: Optional[str] = None,
        fg_checkout_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert a fresh pending order and return the stored document."""
        now = datetime.now()
        document = {
            "_id": order_id,
            "order_id": order_id,
            "user_id": int(user_id),
            "user_name": user_name or "",
            "amount": round(float(amount), 2),
            "payable_amount": round(float(payable_amount), 2),
            "plan_time": plan_time,
            "status": STATUS_PENDING,
            "utr": "",
            "sender_name": "",
            "verified_by": "",
            "fg_order_id": fg_order_id or "",
            "fg_qr_url": fg_qr_url or "",
            "fg_upi_intent": fg_upi_intent or "",
            "fg_checkout_url": fg_checkout_url or "",
            "chat_id": int(chat_id) if chat_id is not None else None,
            "qr_message_id": int(qr_message_id) if qr_message_id else None,
            # A buyer may mark an order as placed and submit their bank UTR for
            # an admin to inspect. Neither value is treated as payment proof:
            # IMAP/FamGateway remains the automatic verifier, and manual
            # approval is still an explicit admin action.
            "order_placed_at": None,
            "submitted_utr": "",
            "utr_submitted_at": None,
            "created_at": now,
            "updated_at": now,
            "expires_at": expires_at,
            "paid_at": None,
        }
        await self.col.insert_one(document)
        return document

    async def set_message_ref(self, order_id: str, chat_id: int, message_id: int) -> None:
        """Remember the QR message so it can be edited once payment lands."""
        await self.col.update_one(
            {"_id": order_id},
            {"$set": {"chat_id": int(chat_id), "qr_message_id": int(message_id)}},
        )

    async def mark_order_placed(
        self, order_id: str, *, user_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """Record the buyer's *post-payment* acknowledgement.

        This is deliberately separate from :meth:`mark_paid`: tapping
        "Order placed" only opens the optional UTR-review flow.  A payment is
        still fulfilled exclusively by an automatic verifier or an admin.
        """
        current = await self.get_order(order_id)
        if not current or current.get("status") not in (STATUS_PENDING, STATUS_MANUAL_PENDING):
            return None
        if user_id is not None and current.get("user_id") != int(user_id):
            return None
        now = datetime.now()
        filters = {"_id": order_id, "status": current["status"]}
        if user_id is not None:
            filters["user_id"] = int(user_id)
        return await self.col.find_one_and_update(
            filters,
            {"$set": {"order_placed_at": now, "updated_at": now}},
            return_document=True,
        )

    async def submit_utr(
        self, order_id: str, *, user_id: int, utr: str
    ) -> Optional[Dict[str, Any]]:
        """Save a buyer-confirmed UTR for **manual review**, never auto-approve it.

        The status guard makes a late callback harmless if the order becomes
        paid/cancelled between the command and the confirmation tap.
        """
        current = await self.get_order(order_id)
        if (
            not current
            or current.get("user_id") != int(user_id)
            or current.get("status") not in (STATUS_PENDING, STATUS_MANUAL_PENDING)
        ):
            return None
        now = datetime.now()
        return await self.col.find_one_and_update(
            {
                "_id": order_id,
                "user_id": int(user_id),
                "status": current["status"],
            },
            {
                "$set": {
                    "order_placed_at": current.get("order_placed_at") or now,
                    "submitted_utr": str(utr or ""),
                    "utr_submitted_at": now,
                    "updated_at": now,
                }
            },
            return_document=True,
        )

    async def transition_status(self, order_id: str, from_status: str, to_status: str) -> Optional[Dict[str, Any]]:
        """Atomically move an order between statuses.

        The ``from_status`` guard makes it a compare-and-swap: only one caller
        (expiry scan, admin button, verifier…) can win the transition, which is
        what keeps "notify admin once" and "grant premium once" honest.
        """
        now = datetime.now()
        return await self.col.find_one_and_update(
            {"_id": order_id, "status": from_status},
            {"$set": {"status": to_status, "updated_at": now}},
            return_document=True,
        )

    async def mark_paid(
        self,
        order_id: str,
        *,
        utr: str = "",
        sender_name: str = "",
        verified_by: str,
    ) -> Optional[Dict[str, Any]]:
        """Flip a *pending* order to ``paid``.

        The ``status: pending`` guard in the update makes this idempotent: two
        verifiers (IMAP email + FamGateway poll + webhook) racing on the same
        order can never double-grant premium – only the first one wins and the
        others get ``None`` back.
        """
        now = datetime.now()
        result = await self.col.find_one_and_update(
            {"_id": order_id, "status": STATUS_PENDING},
            {
                "$set": {
                    "status": STATUS_PAID,
                    "utr": utr or "",
                    "sender_name": sender_name or "",
                    "verified_by": verified_by,
                    "paid_at": now,
                    "updated_at": now,
                }
            },
            return_document=True,
        )
        return result

    async def mark_status(self, order_id: str, status: str) -> Optional[Dict[str, Any]]:
        """Force an order into ``status`` (expire / cancel / admin overrides)."""
        now = datetime.now()
        return await self.col.find_one_and_update(
            {"_id": order_id},
            {"$set": {"status": status, "updated_at": now}},
            return_document=True,
        )

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    async def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        return await self.col.find_one({"_id": order_id})

    async def get_order_by_fg_id(self, fg_order_id: str) -> Optional[Dict[str, Any]]:
        if not fg_order_id:
            return None
        return await self.col.find_one({"fg_order_id": fg_order_id})

    async def get_active_order(self, user_id: int) -> Optional[Dict[str, Any]]:
        """The user's most recent still-pending order, if any."""
        cursor = (
            self.col.find({"user_id": int(user_id), "status": STATUS_PENDING})
            .sort("created_at", -1)
            .limit(1)
        )
        async for order in cursor:
            return order
        return None

    async def get_reviewable_order(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Latest pending/manual-review order for the buyer's ``/utr`` command."""
        # Filtering the two legal statuses in Python avoids a database-specific
        # ``$in`` expression and keeps this tiny store easy to fake in tests.
        cursor = self.col.find({"user_id": int(user_id)}).sort("created_at", -1)
        async for order in cursor:
            if order.get("status") in (STATUS_PENDING, STATUS_MANUAL_PENDING):
                return order
        return None

    async def pending_orders(self) -> List[Dict[str, Any]]:
        cursor = self.col.find({"status": STATUS_PENDING}).sort("created_at", 1)
        return [order async for order in cursor]

    async def pending_amounts(self) -> List[float]:
        """Payable amounts reserved by pending orders (offset collision check)."""
        cursor = self.col.find({"status": STATUS_PENDING}, {"payable_amount": 1})
        amounts = []
        async for order in cursor:
            value = order.get("payable_amount")
            if value is not None:
                amounts.append(round(float(value), 2))
        return amounts

    async def find_pending_by_amount(self, payable_amount: float) -> Optional[Dict[str, Any]]:
        cursor = (
            self.col.find(
                {
                    "status": STATUS_PENDING,
                    "payable_amount": round(float(payable_amount), 2),
                }
            )
            .sort("created_at", 1)
            .limit(1)
        )
        async for order in cursor:
            return order
        return None

    async def utr_already_used(self, utr: str) -> bool:
        """Bank-UTR idempotency: one UTR can fulfil at most one order."""
        if not utr:
            return False
        return bool(
            await self.col.find_one(
                {"status": STATUS_PAID, "utr": utr}, {"_id": 1}
            )
        )

    async def recent_orders(self, limit: int = 10) -> List[Dict[str, Any]]:
        cursor = self.col.find().sort("created_at", -1).limit(int(limit))
        return [order async for order in cursor]

    async def orders_by_status(self, status: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Newest orders in one state, for the compact admin panel."""
        cursor = (
            self.col.find({"status": status})
            .sort("created_at", -1)
            .limit(max(1, int(limit)))
        )
        return [order async for order in cursor]

    async def count_by_status(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for status in (
            STATUS_PENDING,
            STATUS_PAID,
            STATUS_MANUAL_PENDING,
            STATUS_EXPIRED,
            STATUS_CANCELLED,
        ):
            counts[status] = await self.col.count_documents({"status": status})
        return counts

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def is_expired(order: Dict[str, Any], now: Optional[datetime] = None) -> bool:
        expires_at = order.get("expires_at")
        if not expires_at:
            return False
        return (now or datetime.now()) >= expires_at

    @staticmethod
    def expiry_from_now(minutes: int) -> datetime:
        return datetime.now() + timedelta(minutes=max(1, int(minutes)))


#: Shared instance used by the plugin (lazy connection, fake-injectable).
paydb = FamPayOrderStore()
