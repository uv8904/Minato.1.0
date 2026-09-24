"""Check the FamPay Gmail/IMAP setup without starting the bot.

Run it on your own machine/VPS — the app password is only ever read from your
local ``.env`` (or environment) and is never printed, logged or sent anywhere::

    python tools/test_fampay_imap.py

What it does:
    1. Reads FAMPAY_EMAIL / FAMPAY_EMAIL_PASSWORD / FAMPAY_UPI_ID from .env
    2. Logs into Gmail over IMAP (same call the bot's worker makes)
    3. Counts unseen mails from FamApp and shows the newest one parsed
    4. Prints a plain-language fix for every common failure

Exit code 0 = everything the bot needs is working.
"""
import imaplib
import os
import sys
from email import message_from_bytes
from email.utils import parseaddr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

EMAIL = os.environ.get("FAMPAY_EMAIL", "").strip()
PASSWORD = os.environ.get("FAMPAY_EMAIL_PASSWORD", "").strip().replace(" ", "")
UPI_ID = os.environ.get("FAMPAY_UPI_ID", "").strip()
HOST = os.environ.get("FAMPAY_IMAP_HOST", "imap.gmail.com")
PORT = int(os.environ.get("FAMPAY_IMAP_PORT", "993"))
MAILBOX = os.environ.get("FAMPAY_IMAP_MAILBOX", "INBOX")
SENDER_FILTER = os.environ.get("FAMPAY_EMAIL_SENDER_FILTER", "famapp.in").lower()

OK, FAIL = "✅", "❌"


def fail(hint: str) -> int:
    print(f"\n{FAIL} {hint}")
    return 1


def main() -> int:
    print("── FamPay IMAP check ─────────────────────────────────────────")
    print(f"  Gmail      : {EMAIL or '(not set)'}")
    print(f"  Password   : {'(set, %d chars)' % len(PASSWORD) if PASSWORD else '(not set)'}")
    print(f"  UPI id     : {UPI_ID or '(not set)'}")
    print(f"  Server     : {HOST}:{PORT} mailbox={MAILBOX}")

    if not EMAIL:
        return fail("FAMPAY_EMAIL .env me nahi hai — apna FamPay wala Gmail daalo.")
    if "@" not in EMAIL or EMAIL.count("@") != 1:
        return fail(f"FAMPAY_EMAIL galat lagta hai: '{EMAIL}' (@ ek hi baar hona chahiye).")
    if not PASSWORD:
        return fail(
            "FAMPAY_EMAIL_PASSWORD .env me nahi hai.\n"
            "  → myaccount.google.com/apppasswords kholo (2-Step Verification ON hona chahiye),\n"
            "    app name likho (e.g. MinatoBot), Create dabao, 16-character password copy karo."
        )
    if len(PASSWORD) != 16:
        print(f"  ⚠️  password {len(PASSWORD)} characters ka hai — Google App Password hamesha 16 ka hota hai.")
    if not UPI_ID or "@" not in UPI_ID:
        print("  ⚠️  FAMPAY_UPI_ID set nahi hai — payment QR iske bina nahi banega.")

    print("\n  connecting …")
    try:
        mail = imaplib.IMAP4_SSL(HOST, PORT)
    except Exception as exc:
        return fail(f"IMAP server se connect nahi ho paya ({exc}). Internet/dns check karo.")

    try:
        mail.login(EMAIL, PASSWORD)
    except imaplib.IMAP4.error as exc:
        message = str(exc)
        mail.logout()
        if "AUTHENTICATIONFAILED" in message.upper().replace(" ", ""):
            return fail(
                "Login fail — Gmail ne password reject kar diya.\n"
                "  99% chance: ye aapka NORMAL Gmail password hai, App Password nahi.\n"
                "  → https://myaccount.google.com/apppasswords  (2-Step Verification pehle ON karo)\n"
                "  → naya 16-char password banao aur .env me daalo (spaces hata do)."
            )
        return fail(f"IMAP login error: {message}")

    print(f"{OK} login successful")

    try:
        status, _ = mail.select(MAILBOX)
        if status != "OK":
            return fail(f"Mailbox '{MAILBOX}' select nahi hua. Gmail me 'Show all labels' / IMAP access on hai?")
        print(f"{OK} mailbox '{MAILBOX}' selected")

        status, data = mail.search(None, "UNSEEN")
        unseen = len(data[0].split()) if status == "OK" and data[0] else 0
        print(f"  unread mails: {unseen}")

        fampay_unseen = 0
        newest = None
        status, data = mail.search(None, "UNSEEN")
        if status == "OK":
            for num in data[0].split():
                status, fetched = mail.fetch(num, "(RFC822)")
                if status != "OK" or not fetched or not fetched[0]:
                    continue
                raw = fetched[0][1]
                sender = parseaddr(message_from_bytes(raw).get("From", ""))[1].lower()
                if SENDER_FILTER and SENDER_FILTER in sender:
                    fampay_unseen += 1
                    newest = newest or raw

        if newest:
            from dreamxbotz.util.fampay_email import parse_payment_email

            parsed = parse_payment_email(newest.decode("utf-8", "replace"))
            subject = message_from_bytes(newest).get("Subject", "")
            print(f"{OK} FamApp mail mila: \"{subject}\"")
            if parsed:
                print(
                    f"{OK} parser ne padha: amount=₹{parsed.amount} utr={parsed.utr or 'n/a'} "
                    f"sender={parsed.sender_name or 'n/a'}"
                )
            else:
                print("  ⚠️  mail mila lekin parser ko credit payment nahi mila — "
                      "ye normally tab hota hai jab mail payment ki nahi hoti.")
        elif fampay_unseen == 0:
            print(
                "  ℹ️  koi unread FamApp mail nahi mila (ye OK hai — bot naye mails ka wait karega).\n"
                "     Test karna ho to FamApp se apne aap ko ₹1 bhejo, phir ye script dobara chalao."
            )
    finally:
        try:
            mail.close()
        except Exception:
            pass
        mail.logout()

    print(f"\n{OK} Sab theek — bot is Gmail se payment emails padh payega.")
    print("   Bot restart karo; logs me 'FamPay: IMAP verifier started' dikhna chahiye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
