"""Parse FamApp / FamPay payment-notification emails.

FamPay has no public API, so the *primary* auto-approval signal is the email
FamApp sends to the Gmail address linked to the UPI id when money arrives.
This module turns that raw email into a small, testable
:class:`ParsedPayment` – amount, UTR and sender name – that the IMAP worker
matches against pending orders.

The parser is deliberately forgiving: FamApp's email wording has changed more
than once, so we look for *shapes* (a ₹ amount, a 12-digit UTR, "received"
vs "debited") instead of exact sentences.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

#: ₹120.50 / Rs 120.50 / Rs.120 / INR 1,200.75
_AMOUNT_RE = re.compile(
    r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)

#: "UTR: 420987654321", "UPI Ref No 420987654321", "Reference ID - 420987654321"
_UTR_RE = re.compile(
    r"(?:utr|upi\s*ref(?:erence)?|ref(?:erence)?)\s*(?:no\.?|number|id)?\s*[:\-\s]\s*"
    r"([0-9]{6,22})",
    re.IGNORECASE,
)

#: Any standalone 12-digit number – the NPCI UTR shape – used when the email
#: never labels it.
_BARE_UTR_RE = re.compile(r"(?<![0-9])([0-9]{12})(?![0-9])")

#: "from Rahul Sharma", "from RAHUL SHARMA via UPI"
_SENDER_RE = re.compile(
    r"from\s+([A-Za-z][A-Za-z .'\-]{2,40}?)\s*(?:via|using|on\b|,|\.|;|\n|$)",
    re.IGNORECASE,
)

_CREDIT_WORDS = ("received", "credit", "added to", "money received", "deposit")
_DEBIT_WORDS = ("debited", "debit", "sent to", "paid to", "withdrawn", "deducted")


@dataclass(frozen=True)
class ParsedPayment:
    """A credit (money-in) event extracted from an email."""

    amount: float          # rupees, rounded to paise
    utr: Optional[str]     # 12-digit bank UTR when the email exposes one
    sender_name: str       # best-effort payer name ("" when unknown)
    raw_excerpt: str       # short snippet, for logs only


def _clean_amount(raw: str) -> Optional[float]:
    try:
        return round(float(raw.replace(",", "")), 2)
    except (TypeError, ValueError):
        return None


def _looks_like_debit(text: str) -> bool:
    lowered = text.lower()
    credited = any(word in lowered for word in _CREDIT_WORDS)
    debited = any(word in lowered for word in _DEBIT_WORDS)
    # A "credited" mail that merely mentions "not debited" is still a credit.
    return debited and not credited


def parse_payment_email(raw_text: str) -> Optional[ParsedPayment]:
    """Extract a credit payment from an email body, or return ``None``.

    ``None`` means "this email is not a usable money-in notification": either
    it is a debit/other notification, or no ₹ amount could be found at all.
    """
    if not raw_text:
        return None

    text = re.sub(r"\s+", " ", str(raw_text)).strip()
    if not text or _looks_like_debit(text):
        return None

    amount = None
    for match in _AMOUNT_RE.finditer(text):
        candidate = _clean_amount(match.group(1))
        if candidate and candidate > 0:
            amount = candidate
            break
    if amount is None:
        return None

    utr = None
    utr_match = _UTR_RE.search(text)
    if utr_match:
        utr = utr_match.group(1)
    else:
        bare = _BARE_UTR_RE.search(text)
        if bare:
            utr = bare.group(1)

    sender_name = ""
    sender_match = _SENDER_RE.search(text)
    if sender_match:
        sender_name = sender_match.group(1).strip(" .'-")

    return ParsedPayment(
        amount=amount,
        utr=utr,
        sender_name=sender_name,
        raw_excerpt=text[:180],
    )
