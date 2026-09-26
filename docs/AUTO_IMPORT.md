# 📥 Auto-Import Userbot — Manual Copy Khatam!

## Problem

Doosre filter-bots se files mangwate ho → har file khud apne channel me
forward karte ho → tab jaake bot index karta hai. Har file pe wahi
copy-paste ka jhanjhat. 😩

## Solution

Bot me ab ek **optional userbot** hai — `USER_SESSION` env me **apne
Telegram account** ki session doge to:

1. Tum doosre bot se normal movie request karoge
2. Wo bot file tumhare **PM** me bhejega
3. **Tumhara account (userbot) wo file pakad ke automatic tumhare
   file-channel me copy kar dega** — bas, bot khud index kar lega ✅
4. Kaunse bot/channel ki files leni hain wo tum `/watch` se decide karte ho
   — "konse bot se file lena hai konsa nahi" wahi control 😎

Confirmations tumhari **Saved Messages** me aati hain (kitni files copy
hui, kaunsi, koi fail hui to reason).

## Setup (5 minute)

### Step 1 — Session string banao (apne PC/Termux pe)

```bash
pip install electrogram
python tools/generate_session.py
```

- `API_ID` / `API_HASH` daalo ([my.telegram.org](https://my.telegram.org) → API development tools)
- Phone number (`+91xxxxxxxxxx`) → Telegram pe aaya login code → (2FA ho to password)
- Last me jo **session string** print hogi, use copy karo

> ⚠️ Ye string tumhare account ka **full access** hai. Kisi se share mat
> karo, aur sirf apne bot ke env vars me hi daalo.

### Step 2 — Env var set karo

Hosting (Koyeb/Heroku/Railway) ke env vars me:

```
USER_SESSION = <step 1 wali string>
AUTO_IMPORT_DELAY = 3        # optional, copies ke beech gap (seconds)
```

Bot restart karo. Logs me dikhega:
`Auto-Import userbot online as your_username | ...`

### Step 3 — Bot me on karo (bot ke PM me)

```
/autoimport on
```

### Step 4 — Kaunse bot ki files leni hain, watch karo

```
/watch @wo_movie_bot           ← us bot ki files auto-copy hongi
/watch @dusra_bot              ← dusra bot bhi
/watchlist                     ← list dekho
/unwatch 1                     ← list se hatana ho to
```

Bas ho gaya. 🎉 Ab jo bhi file watched bot se tumhare account pe aayegi,
automatic channel me copy + index.

## Optional

### Target channel badalna

By default copy **`CHANNELS` ke pehle channel** me hoti hai. Alag channel
me karni ho:

```
/target -1001234567890
```

⚠️ **Zaroori:** target channel me **bot bhi admin ho** (taaki copy hui
file search me aaye) **aur tumhara account bhi admin ho** (taaki copy ho
paye). Dono condition na ho to bot khud warn karega.

### Pura channel bulk-copy — `/grab`

Agar kisi channel me pehle se hazaaron files padi hain (jisme tumhara
account member hai):

```
/grab @channel_username              ← pura channel
/grab -1001234567890 5000            ← message 1 se 5000 tak
/grab -1001234567890 2000 5000       ← sirf 2000-5000
/grab https://t.me/c/1234567/8900    ← private channel link se bhi
```

Progress bar + Cancel button milta hai. `/grab` ke liye bhi **tumhara
account** use hota hai (bot ko us channel me admin hona zaroori nahi —
bas tumhara account member ho).

Private channel ho to pehle **apne account se us channel ko join karo**,
phir grab karo.

## Commands Summary

| Command | Kaam |
|---|---|
| `/autoimport on\|off` | Feature on/off + status |
| `/watch @bot\|chat_id` | Watchlist me add |
| `/watchlist` | Watchlist dekho |
| `/unwatch <n\|@bot>` | Watchlist se hatao |
| `/target <channel_id>` | Copy destination badlo |
| `/grab <chat> [start] [end]` | Pura channel bulk-copy |

## Safety Notes

- `USER_SESSION` **khali** hai to ye poora feature off rehta hai — bot ki
  koi aur cheez affect nahi hoti.
- Copy ke beech `AUTO_IMPORT_DELAY` (default 3s) ka gap + `FloodWait`
  auto-handling — reasonable rate pe account limit nahi hoti.
- Userbot client **bina plugins** start hota hai, isliye bot ke saare
  handlers (start, pmfilter, admin, ...) **sirf bot pe** chalte hain —
  tumhara account kisi user ko reply nahi karta.
- Settings (watchlist/target/on-off) **MongoDB me persist** hoti hain —
  restart ke baad bhi bane rehti hain.
- Sirf `ADMINS` ye commands use kar sakte hain.

## Tech Notes (dev)

- Plugin: `plugins/auto_import.py` — userbot `plugins/auto_import.start_userbot()`
  se `bot.py` me start hota hai (best-effort; fail hone par sirf warning).
- Userbot handlers `Client.add_handler()` se add hote hain, class-decorator se
  NAHI — warna main bot ke saare handlers userbot pe bhi register ho jaate
  (electrogram me plugin handlers per-instance `load_plugins` se judte hain,
  aur userbot `plugins` param ke bina bana hai isliye unhe load hi nahi karta).
- Session generate karne ke liye userbot framework ka
  `export_session_string()` use hota hai (`tools/generate_session.py`).
