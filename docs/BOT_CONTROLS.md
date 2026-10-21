# 🎛️ Bot Controls — change Send All & FSub from the bot

Do controls badalne ke liye **repo edit / redeploy ki zaroorat nahi**. Bot
admin bot ke private chat me commands (ya panel ke buttons) use karke dono
cheezein live change kar sakta hai:

| Control | Kya badalta hai | Kahan save hota hai |
|---|---|---|
| **Send All premium gate** | "Sᴇɴᴅ Aʟʟ" sirf premium users ke liye rahe ya sabke liye free ho | MongoDB → `bot_settings` → doc `runtime_config` |
| **Force-sub channels** | Kaun-kaun se channel join karna zaroori hai (`AUTH_CHANNELS` ki jagah) | same doc, `fsub_channels` field |

Changes **turant** apply hote hain: search results, file delivery, `/start`
deep links — sab jagah wahi value use hoti hai. Restart/redeploy ke baad bhi
value bani rehti hai (MongoDB me hai).

---

## 🔐 Send All premium gate

Default: **ON** (purana behaviour — sirf premium users Sᴇɴᴅ Aʟʟ kar sakte hain).

```
/sendall            → panel (current status + toggle button)
/sendall status     → wahi panel
/sendall on         → premium-only (default)
/sendall off        → sabke liye free
/sendallpremium ... → same command
```

Panel me ek button bhi hai:

* **🔓 Turn OFF — free for everyone** – gate off, premium offer skip ho jata hai.
* **🔐 Turn ON — premium only** – wapas purana premium-only behaviour.

Gate **OFF** hone par bhi force-sub aur verification rules **lagu rehte hain** —
sirf premium ka check skip hota hai. Admin IDs hamesha Sᴇɴᴅ Aʟʟ kar sakte hain.

Ye switch yahan use hota hai:

* search results ka `Sᴇɴᴅ Aʟʟ` button (`confirmfiles#…` / `sendfiles#…`),
* `allfiles_…` / `sendall_…` deep links,
* verify ke baad wala send-all flow.

---

## 📢 Force-sub channels

Purana tareeka: `info.py` / `AUTH_CHANNELS` env edit karo, ya group me
`/set_fsub` chalao. Naya tareeka — **bot PM se**:

```
/fsub                          → panel (current mode + channels)
/fsub add -1001234567890       → ek ya zyada channels add karo
/fsub add @channel t.me/other  → username / link bhi chalega
/fsub remove -1001234567890    → ek channel hatao
/fsub reset                    → repo/env (AUTH_CHANNELS) par wapas
/fsub cancel                   → "add channel" input cancel
```

Panel me:

* har channel ke saath **❌ Remove** button,
* **➕ Add channel** — dabao aur next message me ID / `@username` /
  `t.me/...` bhejo, ya channel ki koi post **forward** kar do,
* **♻️ Reset to env** (sirf tab jab bot-managed list active ho),
* **🔄 Refresh** aur **🎛️ Panel**.

### Mode kaise kaam karta hai

* **Repo/env default** (`AUTH_CHANNELS` + group ki `fsub` setting): jab tak
  bot-managed list save nahi hui. Panel me mode "repo/env default" dikhta hai.
* **Bot-managed**: pehli baar add/remove karne par current list (env wali)
  MongoDB me copy ho jati hai, phir wahi list har jagah use hoti hai. Env
  list **ignore** ho jati hai — isliye aap repo ke channels bhi panel se
  hata sakte ho.
* **0 channels**: agar aap sab channels remove kar dete ho to force-sub
  **disable** ho jata hai (panel saaf-saaf bata deta hai).
* **Reset**: bot-managed list delete ho jati hai; wapas env + group setting
  wala purana behaviour.

Group me `/set_fsub` abhi bhi kaam karta hai (group admin apne group ke liye),
lekin jab bot-managed list active ho to woh ignore hoti hai — panel me note
bhi dikhta hai. `/remove_fsub` group ki custom list ko default par reset karta
hai; bot PM me iski jagah `/fsub reset` use karo.

> ℹ️ Channel add karte waqt bot ko us channel me **admin** hona chahiye,
> warna invite link nahi banega. Bot check karke warning deta hai.

---

## 🎛️ Ek hi panel

```
/botpanel
/botcontrol
/controls
```

Panel dono controls dikhata hai + current force-sub count/mode, aur seedha
Send All / FSub panel kholne ke buttons deta hai. Sab kuch **sirf bot admins**
(`ADMINS`) ke liye, sirf private chat me.

---

## ⚙️ Kaise kaam karta hai

```
/sendall off ──► db.set_runtime_setting("send_all_premium", False)
                          │
                          ▼
              dreamxbotz/util/bot_controls.py (20s cache)
                          │
        ┌─────────────────┼──────────────────┐
        ▼                 ▼                  ▼
  pmfilter send-all   commands deep links   /start allfiles flow
```

* `database/users_chats_db.py` → `get_runtime_setting` /
  `set_runtime_setting` / `unset_runtime_setting`
  (`bot_settings` collection, doc id `runtime_config`).
* `dreamxbotz/util/bot_controls.py` → cached reads, writers,
  `effective_fsub_channels(...)`, `send_all_allowed(...)`, input parsing.
* `plugins/bot_controls.py` → `/botpanel`, `/sendall`, `/fsub`, inline
  buttons aur "next message = channel" flow (group `-60`, so pmfilter ka
  catch-all double-answer nahi karta).
* `plugins/commands.py` / `plugins/pmfilter.py` → gates aur force-sub checks
  ab runtime helper use karte hain.
* `bot.py` → startup par values load + har 60s refresh (multi-process setups
  sync rahein).

MongoDB down ho to **safe defaults**: premium gate ON aur env `AUTH_CHANNELS`
— koi search/file flow nahi tootta.

---

## 🧪 Tests

```bash
pytest tests/test_bot_controls.py -q
```
