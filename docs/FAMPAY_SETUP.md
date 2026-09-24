# ⚡ FamPay Auto-Approval Payments

Premium payments over **FamPay / FamApp UPI** with **automatic approval** — the
buyer scans a QR (or taps a UPI deep link), pays the exact amount shown, and
premium is activated by the bot itself. No screenshot, no waiting for an admin.

Two independent verifiers run side by side; the first one to confirm the money
wins:

| # | Verifier | Needs | Cost | Latency |
|---|----------|-------|------|---------|
| 1 | **Gmail IMAP scan** (primary) | Gmail linked to your FamPay + a Google App Password | free | ~5–20 s |
| 2 | **FamGateway API** (fallback) | `FAMGATEWAY_API_KEY` from famgateway.in | free tier | instant (webhook) / ~20 s (poll) |

If neither verifies within `FAMPAY_ORDER_EXPIRY_MINUTES`, the order moves to
**manual review**: the buyer is told the payment is with the admin, and every
admin gets an **Approve / Reject** button (manual fallback).

---

## 1. How it works

```
/plan  →  ⚡ FamPay  →  pick a plan (₹10 / ₹20 / ₹40 / ₹55 / ₹75)
      →  bot creates order FMP-AB12CD and reserves a UNIQUE paise amount
         (₹40 → ₹40.07)  ── the paise is how the bot knows WHO paid
      →  bot sends: UPI QR + "pay exactly ₹40.07" + green [Pay via UPI app]
         + green [✅ Order placed] button
      →  user pays → taps [✅ Order placed] to get the optional UTR instructions
      →  Gmail email / FamGateway says "₹40.07 received, UTR …"
      →  bot matches the order, marks it paid (idempotent) and grants premium
      →  buyer + PREMIUM_LOGS get the receipt with the UTR
```

Why unique paise? FamPay has no merchant API — every payment lands in the same
personal `@fam` inbox, and many users can buy the same ₹40 plan at the same
time. The paise offset makes each pending order individually identifiable, and
the bank UTR is de-duplicated so one payment can never grant premium twice.

Safety properties:

* `mark_paid()` only wins from `pending` → IMAP, FamGateway polling and the
  webhook racing on the same order can never double-grant premium.
* A UTR that already fulfilled an order is ignored everywhere.
* A pending order is re-shown (not duplicated) if the user taps the same plan
  again.
* A user-entered UTR is **never** automatic proof of payment. It is only shown
  to admins for a bank/FamApp check; the IMAP/FamGateway verifier or an explicit
  admin approval remains required.

### Optional manual UTR route

The normal flow needs no manual input: wait a few seconds for the FamApp email
or FamGateway webhook. If the buyer has paid but the automatic verification is
late, they can tap **✅ Order placed** on their QR and send either:

```text
/utr FMP-ABC123 123456789012
```

or, when they have only one open FamPay order:

```text
/utr 123456789012
```

The bot displays the order, amount and UTR once more. The buyer must tap
**✅ Confirm UTR** before anything is stored or sent to admins. This deliberate
second confirmation prevents a mistyped reference number from entering the
review queue. Admins receive green **Approve** and red **Reject** buttons after
checking the UTR in FamApp/bank history.

### Button colours

The FamPay checkout uses the bot's `COLOR_BUTTONS` setting: green marks the
primary payment/confirmation action, blue marks navigation or refresh, and red
marks cancellation/rejection. Older Telegram clients simply show normal inline
buttons, so the flow still works everywhere.

## 2. Configuration (env vars)

Set these on Heroku/Koyeb/VPS `.env` — never commit them.

| Variable | Required | Default | What it is |
|----------|----------|---------|------------|
| `FAMPAY_ENABLED` | no | `True` | Master switch. |
| `FAMPAY_UPI_ID` | **yes** | falls back to `OWNER_UPI_ID` | Your `yourname@fam` UPI id that receives the money. |
| `FAMPAY_PAYEE_NAME` | no | `DreamXBotz` | Name shown in the payer's UPI app. |
| `FAMPAY_PLANS` | no | `10:7day,20:15day,40:1month,55:45day,75:60day` | `price:duration` pairs (durations understood by `get_seconds`). |
| `FAMPAY_ORDER_EXPIRY_MINUTES` | no | `10` | How long a QR stays payable. |
| `FAMPAY_POLL_INTERVAL` | no | `20` | Seconds between background status checks (min 5). |
| `FAMPAY_ORDERS_COLLECTION` | no | `fampay_orders` | MongoDB collection for orders. |
| `FAMPAY_EMAIL` | **yes\*** | – | Gmail linked to your FamPay account. |
| `FAMPAY_EMAIL_PASSWORD` | **yes\*** | – | 16-char **Google App Password** (not your Gmail password). |
| `FAMPAY_IMAP_ENABLED` | no | `True` | Turn the IMAP verifier off. |
| `FAMPAY_IMAP_HOST` / `FAMPAY_IMAP_PORT` / `FAMPAY_IMAP_MAILBOX` | no | `imap.gmail.com` / `993` / `INBOX` | IMAP settings (other providers work too). |
| `FAMPAY_EMAIL_SENDER_FILTER` | no | `famapp.in` | Only scan mails from this sender. |
| `FAMGATEWAY_API_KEY` | no\*\* | – | famgateway.in API key (fallback verifier + instant webhook). |
| `FAMGATEWAY_BASE_URL` | no | `https://famgateway.in` | API base URL. |
| `FAMGATEWAY_WEBHOOK_SECRET` | no | = API key | Separate webhook signing secret if you want one. |

\* Required unless you use FamGateway only.
\*\* Optional — without it the bot still auto-approves via Gmail IMAP.

### Getting the Google App Password (Gmail verifier)

1. On desktop Gmail → ⚙ Settings → **See all settings** → **Forwarding and
   POP/IMAP** → **Enable IMAP** → Save. *(IMAP is off by default — without this
   the login fails even with a correct password.)*
2. Go to <https://myaccount.google.com/apppasswords> (needs 2-Step Verification
   on) → create an app password → copy the **16 characters** (spaces optional).
3. Put them in `FAMPAY_EMAIL` + `FAMPAY_EMAIL_PASSWORD`.

**Self-check before deploying** (password never leaves your machine):

```bash
python tools/test_fampay_imap.py
```

It logs in with your `.env` values, counts unread FamApp mails and tells you
exactly what to fix if something is wrong (wrong password, IMAP off, typo in
the Gmail address, …).

> FamApp sends the credit notification to the Gmail you registered with FamPay.
> The bot only ever reads mail from `famapp.in`, and stores **no** email
> content or credentials.

### FamGateway (optional, recommended)

1. Create a free account at <https://famgateway.in> and connect the same
   FamPay Gmail + UPI id there.
2. Copy the API key into `FAMGATEWAY_API_KEY`.
3. In the FamGateway dashboard → Webhooks, set:
   `https://<your-bot-url>/fampay/webhook` (your `URL` from `info.py`).
   Signature verification (`X-FamGateway-Signature`, HMAC-SHA256) is enforced
   automatically.

With the webhook configured, approval is near-instant; without a public URL the
IMAP scan and the FamGateway status poll still cover everything.

## 3. What the user sees

* `/plan` → **⚡ FamPay — auto approval**, or `/fampay` any time in PM.
* Plan list → tap a green price button → QR photo with **"pay exactly ₹40.07"**,
  the order id, the UPI id and buttons: green **Pay via UPI app**, **Web
  checkout** (FamGateway only), **✅ Order placed**, blue **Check payment**, and
  red **Cancel**.
* **Order placed** keeps the QR available and sends the buyer the optional
  `/utr FMP-… <UTR>` instructions. `/utr` always shows a second **Confirm UTR**
  step; it does not activate premium by itself.
* On payment: the QR message is replaced by a receipt (plan, amount, UTR,
  expiry) and premium is live. `/myplan` shows the new expiry.
* If the order expires unpaid, or a buyer confirms a UTR, admins get green
  **Approve** / red **Reject** buttons (manual fallback). They must check the
  payment before approving.

## 4. Admin commands

| Command | Who | What |
|---------|-----|------|
| `/fampay_orders` | admins | Interactive FamPay panel: live counts, pending/recent views and the UTR/manual-review queue. |
| `/utr [FMP-ORDER] <UTR>` | buyer | Optional post-payment UTR submission. The bot asks for a second confirmation, then sends it to the admin review queue; it does **not** auto-approve. |
| `/add_premium <id> <time>` | admins | Manual grant (unchanged, still works). |

## 5. Files

| File | Role |
|------|------|
| `plugins/FamPay.py` | Plugin: plan menu, order creation, callbacks, workers. |
| `database/payment_db.py` | `fampay_orders` collection (create / match / mark-paid). |
| `dreamxbotz/util/fampay_email.py` | Parses FamApp credit emails → amount + UTR + sender. |
| `dreamxbotz/util/fampay_qr.py` | UPI deep links, unique paise amounts, QR PNGs. |
| `dreamxbotz/server/fampay_webhook.py` | `POST /fampay/webhook` receiver (HMAC-verified). |
| `tests/test_fampay_payments.py` | Tests for the parser, QR/amount logic, store, webhook. |

## 6. Troubleshooting

| Symptom | Fix |
|---------|-----|
| Bot logs `FamPay: enabled but not configured` | Set `FAMPAY_UPI_ID` **and** either Gmail credentials or `FAMGATEWAY_API_KEY`. |
| `IMAP login failed` | Enable IMAP in Gmail settings; use an App Password, not the account password. |
| Payments not auto-approving | Check the logs for `unmatched credit` — the paid amount must match the unique paise amount exactly. |
| `#FamPay_Unmatched_Payment` in admin DM | Someone paid a wrong/rounded amount; grant manually with `/add_premium`. |
| FamPay button says "configure nahi hain" | Same as the first row — the plugin hides itself when it cannot verify. |
