"""Tests for the FamPay auto-approval flow.

Covers the parts that can be verified without Telegram, Gmail or a real
MongoDB:

* ``dreamxbotz/util/fampay_email.py`` – FamApp credit-email parsing
* ``dreamxbotz/util/fampay_qr.py``    – UPI intent, unique paise, QR PNG
* ``database/payment_db.py``          – order lifecycle + idempotent mark-paid
* ``dreamxbotz/server/fampay_webhook.py`` – HMAC-verified webhook receiver

Run with the project dependencies installed::

    pytest tests/test_fampay_payments.py -q
"""
import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dreamxbotz.server import fampay_webhook  # noqa: E402
from dreamxbotz.util import fampay_email as fe  # noqa: E402
from dreamxbotz.util import fampay_qr as fq  # noqa: E402

WEBHOOK_SECRET = "test-secret"


# --------------------------------------------------------------------------- #
# Existing manual-approval UPI screen
# --------------------------------------------------------------------------- #
def test_manual_upi_template_accepts_configured_owner_upi_id():
    """The manual UPI callback must not treat an old literal VPA as a key."""
    from Script import script

    rendered = script.PREMIUM_UPI_TEXT.format("owner@fam")
    assert "<code>owner@fam</code>" in rendered


def test_fampay_upi_id_falls_back_to_owner_upi_id():
    """A legacy OWNER_UPI_ID deployment keeps the new FamPay checkout usable."""
    env = os.environ.copy()
    env.pop("FAMPAY_UPI_ID", None)
    env["OWNER_UPI_ID"] = "owner@fam"
    output = subprocess.check_output(
        [sys.executable, "-c", "import info; print(info.FAMPAY_UPI_ID)"],
        cwd=ROOT,
        env=env,
        text=True,
    )
    assert output.strip() == "owner@fam"


def test_plan_detail_page_carries_features_and_proof_steps():
    """The pre-checkout detail page must show features + UTR/screenshot steps."""
    from Script import script

    rendered = script.FAMPAY_PLAN_DETAIL_TXT.format(
        plan="30 ᴅᴀʏꜱ", amount="₹40.00", upi_id="owner@fam", expiry=10
    )
    assert "30 ᴅᴀʏꜱ" in rendered
    assert "₹40.00" in rendered
    assert "<code>owner@fam</code>" in rendered
    # features and the post-payment proof instructions are both on the page
    assert "ᴘʀᴇᴍɪᴜᴍ" in rendered.lower() or "ᴍɪʟᴇɢᴀ" in rendered
    assert "sᴄʀᴇᴇɴsʜᴏᴛ" in rendered
    assert "ᴜᴛʀ ɴᴜᴍʙᴇʀ" in rendered


# --------------------------------------------------------------------------- #
# FamApp email parsing
# --------------------------------------------------------------------------- #
CREDIT_EMAIL = """Subject: You received Rs 40.07
From: FamApp <no-reply@famapp.in>

Hi! You have received Rs 40.07 from Rahul Sharma via UPI.
UPI Ref No 420987654321
"""

DEBIT_EMAIL = """Subject: Payment sent
From: FamApp <no-reply@famapp.in>

Rs 250.00 was debited from your FamPay wallet for your order.
"""


def test_credit_email_is_parsed():
    parsed = fe.parse_payment_email(CREDIT_EMAIL)
    assert parsed is not None
    assert parsed.amount == 40.07
    assert parsed.utr == "420987654321"
    assert parsed.sender_name == "Rahul Sharma"


def test_debit_email_is_ignored():
    assert fe.parse_payment_email(DEBIT_EMAIL) is None


def test_amount_with_rupee_symbol_and_commas():
    parsed = fe.parse_payment_email("You received ₹1,250.50 from Anita. UTR: 123456789012")
    assert parsed is not None
    assert parsed.amount == 1250.50
    assert parsed.utr == "123456789012"


def test_bare_12_digit_utr_is_found():
    parsed = fe.parse_payment_email("Money received INR 75.99 reference 987654321000 ok")
    assert parsed is not None
    assert parsed.amount == 75.99
    assert parsed.utr == "987654321000"


def test_non_payment_email_returns_none():
    assert fe.parse_payment_email("Welcome to FamApp! Verify your account.") is None
    assert fe.parse_payment_email("") is None


def test_famapp_system_mail_sender_gate():
    assert fe.is_famapp_system_mail("FamApp <system@famapp.in>")
    assert fe.is_famapp_system_mail("FamApp <no-reply@system.famapp.in>")
    assert fe.is_famapp_system_mail("FamApp <noreply@famapp.in>")
    assert not fe.is_famapp_system_mail("FamApp Support <support@famapp.in>")
    assert not fe.is_famapp_system_mail("Fake <system@evilfamapp.in>")
    assert not fe.is_famapp_system_mail("Other <system@example.com>")
    assert fe.is_famapp_system_mail("System <system@famapp.in>", "system@famapp.in")
    assert not fe.is_famapp_system_mail("System <system@famapp.in>", "no-reply@famapp.in")


# --------------------------------------------------------------------------- #
# UPI intent / unique amounts / QR
# --------------------------------------------------------------------------- #def test_upi_intent_shape():
    intent = fq.build_upi_intent("me@fam", "DreamXBotz", 40.07, note="FMP-ABC123")
    assert intent.startswith("upi://pay?")
    assert "pa=me%40fam" in intent
    assert "pn=DreamXBotz" in intent
    assert "am=40.07" in intent
    assert "cu=INR" in intent
    assert "tn=FMP-ABC123" in intent


def test_unique_payable_amount_skips_taken_paise():
    assert fq.unique_payable_amount(40, []) == 40.01
    assert fq.unique_payable_amount(40, [40.01, 40.02]) == 40.03
    assert fq.unique_payable_amount(10, [10.05]) == 10.01


def test_unique_payable_amount_falls_back_when_exhausted():
    taken = [round(40 + i / 100, 2) for i in range(1, 100)]
    assert fq.unique_payable_amount(40, taken) == 40.0


def test_qr_png_bytes():
    png = fq.generate_qr_png(fq.build_upi_intent("me@fam", "Shop", 10.05))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 100


def test_format_inr():
    assert fq.format_inr(10) == "₹10.00"
    assert fq.format_inr(1250.5) == "₹1,250.50"


# --------------------------------------------------------------------------- #
# Order store (fake in-memory collection)
# --------------------------------------------------------------------------- #
class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, count):
        self._docs = self._docs[:count]
        return self

    def __aiter__(self):
        async def _gen():
            for doc in self._docs:
                yield doc

        return _gen()


class FakeCollection:
    """Just enough motor API for ``FamPayOrderStore``."""

    def __init__(self):
        self.docs = {}

    @staticmethod
    def _matches(doc, filt):
        return all(doc.get(key) == value for key, value in (filt or {}).items())

    async def insert_one(self, document):
        if document["_id"] in self.docs:
            raise DuplicateKeyError("duplicate _id")
        self.docs[document["_id"]] = dict(document)

    async def find_one(self, filt=None, projection=None):
        for doc in self.docs.values():
            if self._matches(doc, filt):
                return dict(doc)
        return None

    async def find_one_and_update(self, filt, update, return_document=False):
        for doc in self.docs.values():
            if self._matches(doc, filt):
                for key, value in update.get("$set", {}).items():
                    doc[key] = value
                return dict(doc) if return_document else None
        return None

    def find(self, filt=None, projection=None):
        return FakeCursor(
            dict(doc) for doc in self.docs.values() if self._matches(doc, filt)
        )

    async def update_one(self, filt, update, upsert=False):
        for doc in self.docs.values():
            if self._matches(doc, filt):
                for key, value in update.get("$set", {}).items():
                    doc[key] = value
                return

    async def count_documents(self, filt):
        return sum(1 for doc in self.docs.values() if self._matches(doc, filt))

    async def create_index(self, *_args, **_kwargs):
        return "index"


from database.payment_db import (  # noqa: E402
    STATUS_CANCELLED,
    STATUS_MANUAL_PENDING,
    STATUS_PAID,
    STATUS_PENDING,
    DuplicateKeyError,
    FamPayEventStore,
    FamPayOrderStore,
    generate_order_id,
)
from datetime import datetime, timedelta  # noqa: E402


@pytest.fixture()
def store():
    return FamPayOrderStore(collection=FakeCollection())


@pytest.fixture()
def event_store():
    return FamPayEventStore(collection=FakeCollection())


async def _create(store, order_id="FMP-TEST01", amount=40, payable=40.07, user_id=99):
    return await store.create_order(
        order_id=order_id,
        user_id=user_id,
        user_name="Tester",
        amount=amount,
        payable_amount=payable,
        plan_time="1month",
        expires_at=datetime.now() + timedelta(minutes=10),
    )


def test_order_id_format():
    order_id = generate_order_id()
    assert order_id.startswith("FMP-")
    assert len(order_id) == 10


def test_create_and_find_order(store):
    async def _run():
        order = await _create(store)
        assert order["status"] == STATUS_PENDING
        assert (await store.get_order("FMP-TEST01"))["payable_amount"] == 40.07
        assert (await store.find_pending_by_amount(40.07))["order_id"] == "FMP-TEST01"
        assert await store.find_pending_by_amount(40.08) is None
        assert (await store.get_active_order(99))["order_id"] == "FMP-TEST01"

    asyncio.run(_run())



def test_mark_paid_is_idempotent(store):
    async def _run():
        await _create(store)
        first = await store.mark_paid("FMP-TEST01", utr="420987654321", verified_by="imap")
        assert first["status"] == STATUS_PAID
        assert first["utr"] == "420987654321"
        # A second verifier (webhook/poll) must not win again.
        assert await store.mark_paid("FMP-TEST01", utr="420987654321", verified_by="webhook") is None
        assert await store.utr_already_used("420987654321")
        assert not await store.utr_already_used("111111111111")

    asyncio.run(_run())



def test_transition_status_is_compare_and_swap(store):
    async def _run():
        await _create(store)
        moved = await store.transition_status("FMP-TEST01", STATUS_PENDING, STATUS_MANUAL_PENDING)
        assert moved["status"] == STATUS_MANUAL_PENDING
        # Second caller loses the transition.
        assert await store.transition_status("FMP-TEST01", STATUS_PENDING, STATUS_CANCELLED) is None
        assert (await store.get_order("FMP-TEST01"))["status"] == STATUS_MANUAL_PENDING

    asyncio.run(_run())



def test_pending_amounts_and_expiry(store):
    async def _run():
        await _create(store, order_id="FMP-A", payable=40.01)
        await _create(store, order_id="FMP-B", payable=40.02)
        await store.mark_status("FMP-B", STATUS_PAID)
        assert sorted(await store.pending_amounts()) == [40.01]
        assert not FamPayOrderStore.is_expired({"expires_at": datetime.now() + timedelta(minutes=5)})
        assert FamPayOrderStore.is_expired({"expires_at": datetime.now() - timedelta(minutes=5)})
        assert not FamPayOrderStore.is_expired({})

    asyncio.run(_run())


def test_order_placed_and_utr_are_review_metadata_not_payment(store):
    async def _run():
        await _create(store)
        placed = await store.mark_order_placed("FMP-TEST01", user_id=99)
        assert placed["status"] == STATUS_PENDING
        assert placed["order_placed_at"] is not None

        review = await store.submit_utr("FMP-TEST01", user_id=99, utr="420987654321")
        assert review["status"] == STATUS_PENDING  # only a verifier/admin can mark paid
        assert review["submitted_utr"] == "420987654321"
        assert review["utr_submitted_at"] is not None
        assert (await store.get_reviewable_order(99))["order_id"] == "FMP-TEST01"
        assert await store.submit_utr("FMP-TEST01", user_id=100, utr="420987654321") is None

        shot = await store.submit_screenshot(
            "FMP-TEST01", user_id=99, file_id="AgAC_test_shot"
        )
        assert shot["status"] == STATUS_PENDING
        assert shot["screenshot_file_id"] == "AgAC_test_shot"
        assert shot["screenshot_submitted_at"] is not None
        assert await store.submit_screenshot(
            "FMP-TEST01", user_id=100, file_id="AgAC_other"
        ) is None

        await store.mark_paid("FMP-TEST01", utr="420987654321", verified_by="imap")
        assert await store.submit_utr("FMP-TEST01", user_id=99, utr="420987654321") is None
        assert await store.submit_screenshot(
            "FMP-TEST01", user_id=99, file_id="AgAC_late"
        ) is None

    asyncio.run(_run())


def test_plain_utr_extractor_accepts_free_text():
    """Buyers type the UTR directly — no /utr command required."""
    import re as _re
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "plugins" / "FamPay.py"
    source = path.read_text(encoding="utf-8")
    # Pull the pure helper without importing pyrogram-heavy module.
    ns = {
        "re": _re,
        "_UTR_VALUE_RE": _re.compile(r"^[0-9]{6,22}$"),
    }
    start = source.index("def _extract_utr_from_text")
    end = source.index("\nasync def _prompt_utr_confirmation", start)
    exec(source[start:end], ns)
    extract = ns["_extract_utr_from_text"]
    assert extract("420987654321") == "420987654321"
    assert extract("UTR: 420987654321") == "420987654321"
    assert extract("ref 987654321000 paid") == "987654321000"
    assert extract("hello") == ""


def test_unmatched_utr_event_claim_is_restart_safe(event_store):
    async def _run():
        # A fresh store instance represents a new worker after a Koyeb restart.
        assert await event_store.claim_unmatched_payment(
            utr="420987654321", amount=40.07, sender_name="Rahul"
        )
        restarted_store = FamPayEventStore(collection=event_store.col)
        assert not await restarted_store.claim_unmatched_payment(
            utr="420987654321", amount=40.07, sender_name="Rahul"
        )
        assert await restarted_store.claim_unmatched_payment(
            utr="420987654322", amount=40.07, sender_name="Rahul"
        )
        doc = event_store.col.docs["unmatched:420987654321"]
        assert doc["kind"] == "unmatched_payment"
        assert doc["utr"] == "420987654321"
        assert doc["amount"] == 40.07

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# Webhook
# --------------------------------------------------------------------------- #
@pytest.fixture()
def webhook_client(store, monkeypatch):
    granted = []

    async def handler(order_id, utr, sender_name):
        granted.append((order_id, utr, sender_name))
        return True

    fampay_webhook.set_fulfilment_handler(handler)
    monkeypatch.setattr(fampay_webhook, "_webhook_secret", lambda: WEBHOOK_SECRET)
    monkeypatch.setattr("database.payment_db.paydb", store)

    app = web.Application()
    app.add_routes(fampay_webhook.routes)

    async def _client():
        return TestClient(TestServer(app))

    return _client, granted


def _signed(payload, secret=WEBHOOK_SECRET):
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-FamGateway-Signature": signature}


def test_webhook_rejects_bad_signature(webhook_client, store):
    async def _run():
        client_factory, granted = webhook_client
        await _create(store)
        async with await client_factory() as client:
            resp = await client.post(
                "/fampay/webhook",
                data=json.dumps({"status": "success", "order_id": "fg_XYZ"}),
                headers={"X-FamGateway-Signature": "deadbeef"},
            )
            assert resp.status == 401
            assert granted == []

    asyncio.run(_run())



def test_webhook_fulfils_matching_order(webhook_client, store):
    async def _run():
        client_factory, granted = webhook_client
        order = await _create(store)
        await store.col.update_one(
            {"_id": order["order_id"]}, {"$set": {"fg_order_id": "fg_XYZ"}}
        )
        body, headers = _signed(
            {
                "status": "success",
                "order_id": "fg_XYZ",
                "utr": "420987654321",
                "sender_name": "Rahul Sharma",
                "amount": 40.07,
            }
        )
        async with await client_factory() as client:
            resp = await client.post("/fampay/webhook", data=body, headers=headers)
            assert resp.status == 200
            assert (await resp.json())["status"] == "ok"
        assert granted == [("FMP-TEST01", "420987654321", "Rahul Sharma")]

    asyncio.run(_run())



def test_webhook_ignores_pending_and_unknown_orders(webhook_client, store):
    async def _run():
        client_factory, granted = webhook_client
        await _create(store)
        async with await client_factory() as client:
            pending_body, pending_headers = _signed({"status": "pending", "order_id": "fg_NONE"})
            resp = await client.post("/fampay/webhook", data=pending_body, headers=pending_headers)
            assert resp.status == 200
            assert (await resp.json())["status"] == "ignored"

            unknown_body, unknown_headers = _signed(
                {"status": "success", "order_id": "fg_UNKNOWN", "utr": "1"}
            )
            resp = await client.post("/fampay/webhook", data=unknown_body, headers=unknown_headers)
            assert resp.status == 200
            assert (await resp.json())["status"] == "unknown_order"
        assert granted == []

    asyncio.run(_run())


def test_webhook_accepts_nested_data_payload(webhook_client, store):
    async def _run():
        client_factory, granted = webhook_client
        order = await _create(store)
        await store.col.update_one(
            {"_id": order["order_id"]}, {"$set": {"fg_order_id": "fg_NESTED"}}
        )
        body, headers = _signed(
            {"event": "payment.success", "data": {"order_id": "fg_NESTED", "utr": "999888777666"}}
        )
        async with await client_factory() as client:
            resp = await client.post("/fampay/webhook", data=body, headers=headers)
            assert resp.status == 200
        assert granted == [("FMP-TEST01", "999888777666", "")]

    asyncio.run(_run())



# --------------------------------------------------------------------------- #
# Plugin sanity (no pyrogram import – syntax + wiring only)
# --------------------------------------------------------------------------- #
def test_plugin_registers_expected_handlers():
    """The plugin must define the handlers/callbacks the UI links to."""
    source = (ROOT / "plugins" / "FamPay.py").read_text(encoding="utf-8")
    for needle in (
        'filters.command("fampay")',
        'filters.command("fampay_orders")',
        'filters.command("utr")',
        "fampay_plain_utr_message",
        "fampay_screenshot_message",
        'filters.regex(r"^fampay_info$")',
        'filters.regex(r"^fampay_(\\d+)$")',
        'filters.regex(r"^fampaybuy_(\\d+)$")',
        'filters.regex(r"^famplaced_(\\S+)$")',
        'filters.regex(r"^famcheck_(\\S+)$")',
        'filters.regex(r"^famcancel_(\\S+)$")',
        'filters.regex(r"^famapprove_(\\S+)$")',
        'filters.regex(r"^famreject_(\\S+)$")',
        "def start_fampay_workers",
        "def imap_scan_once",
        "submit_screenshot",
        "_extract_utr_from_text",
        "_plan_detail_caption",
        "FAMPAY_PLAN_DETAIL_TXT",
    ):
        assert needle in source, f"missing {needle}"


def test_admin_action_callbacks_do_not_use_message_only_user_filter():
    """Electrogram's filters.user rejects CallbackQuery updates before a handler runs."""
    source = (ROOT / "plugins" / "FamPay.py").read_text(encoding="utf-8")
    assert 'filters.regex(r"^famapprove_(\\S+)$") & filters.user(ADMINS)' not in source
    assert 'filters.regex(r"^famreject_(\\S+)$") & filters.user(ADMINS)' not in source
    assert source.count("if not await _require_fampay_admin(callback_query):") >= 3
    assert "def _alert_destinations" in source
    assert "[*ADMINS, PREMIUM_LOGS]" in source
