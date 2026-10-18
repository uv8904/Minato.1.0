# 💎 Premium Exclusive / Early Access

> Owner kisi bhi movie/series ko **premium-only** mark kar sakta hai. Premium
> users ko wo title bot par search karte hi **instant** mil jata hai — normal
> users ko ek lock screen dikhta hai jisme saaf likha hota hai ki ye abhi sirf
> premium users ke liye available hai, kuch time baad sabke liye aayega. Lock
> screen par **do options** hote hain: (1) 🆓 **Free — Verify & Unlock** (ek
> chhota shortlink verify karke title turant unlock) ya (2) 💎 **Buy Premium**
> (sab kuch instant). Release kab hoga — owner decide karta hai (turant, ya N
> minutes ka schedule).

---

## ✨ Kya-kya milta hai

| User | Kya hota hai |
|------|----------------|
| 🔍 **Premium user** | Locked title search kare → turant normal results + `💎 PREMIUM EARLY ACCESS` badge. Koi wait nahi, koi lock nahi. |
| 🔍 **Normal user** | Locked title search kare (ya uski file ka link khole) → lock screen with **two options**: 🆓 **Free — Verify & Unlock** (shortlink verify se title turant unlock) ya 💎 **Buy Premium**; saath me **🎁 Free Trial**, **⚡ FamPay**, **🔔 Notify when public**. |
| 👑 **Owner/Admin** | `/add_premium_content` se lock, `/release_premium` se public, `/schedule_release` se auto-release. Release hote hi 🔔 dabane wale sab users ko PM. |

Lock screen ki copy (`Script.py` → `PREMIUM_LOCK_TXT`) exactly yahi kehti hai:
premium users ko instant mil raha hai, normal users ko owner ke release karne
par milega. Do options saaf likhe hain: 🆓 free shortlink-verify (turant
unlock) ya 💎 Buy Premium (Stars / UPI / FamPay — kabhi verify nahi).

### 🆓 Free shortlink-verify unlock (option 1)

1. User lock screen par **🆓 Free — Verify & Unlock** dabata hai
   (`pcunlock#<keyword>` callback → `plugins/PremiumContent.py`).
2. Bot verify screen dikhata hai (`PREMIUM_VERIFY_TXT`) with a **✅ Verify
   now** shortlink button — link `get_shortlink()` se banta hai, yani group ki
   `/set_shortner` settings, warna `info.py` ki `SHORTENER_API` /
   `SHORTENER_WEBSITE`. Saath me 📖 **How to verify** (`TUTORIAL`) aur 💎 Buy
   Premium buttons.
3. Shortlink complete karke user bot me wapas aata hai (`/start pcv_...` deep
   link). Bot one-time verify-id check karta hai (`db.create_verify_id` →
   `get_verify_id_info` — wahi verification system jo file-verify flow use
   karta hai), user ko us title ke liye mark kar deta hai (`verified_users`),
   aur **turant search results** dikha deta hai (`PREMIUM_UNLOCKED_TXT`).
4. Dubara search karne par bhi title unlocked rehta hai. Title ke release
   hone par sabke liye normal ho jata hai.

Verification **per user + per title** hai — ek title verify karne se dusre
locked titles unlock nahi hote (unke liye premium, ya har title par ek verify).
Yeh deliberately same shortener/verify flow hai jo `info.py` me configured hai
— koi naya alag system nahi.

---

## 🔧 Commands (admin only)

| Command | Kya karta hai |
|---------|----------------|
| `/add_premium_content <keyword> [display title]` | Title ko lock karta hai. `keyword` = wo text jis par search ye lock lagu (e.g. `jawan`, `kalki 2898 ad`). |
| `/del_premium_content <keyword>` | Lock hata deta hai (content phir normal hai). |
| `/release_premium <keyword>` | Sabke liye public + waiting users ko notify. |
| `/schedule_release <keyword> <minutes>` | N minutes baad **auto-release + auto-notify** (restart ke baad bhi schedule wapas uthta hai). |
| `/premium_content` | Locked + released ki list (hits aur waiting count ke saath). |
| `/premium_content_status` | Feature ON/OFF + kitne locked/released hain. |
| `/notify_premium_release <keyword>` | Bina release kiye, waiting users ko PM (jaise "aa raha hai"). |

Examples:

```text
/add_premium_content jawan Jawan (2023)
/add_premium_content kalki 2898 ad Kalki 2898 AD (2024)
/schedule_release jawan 120        ← 2 ghante baad auto public + notify
/release_premium jawan             ← abhi public karo
```

---

## 🧠 Matching rules (search gate)

* Keyword matching **whole-token** hai: `jawan` lock par `jawan 2023`,
  `jawan 1080p` match karte hain — lekin `jawans` nahi.
* Multi-word keyword query me (order me) milna chahiye: `kalki 2898 ad` lock →
  `kalki 2898 ad 2024` match. User `kalki 2898` likhe to bhi match (subset).
* Search gate **fail-open** hai: DB slow/down ho to search block nahi hota
  (locked list 30s ke liye cache hoti hai, lookup budget 0.5s).
* File delivery par bhi gate lagta hai — normal user kisi locked file ka
  deep-link (`/start=file_...`) khole to lock screen hi milega.
* Gate se **premium users, admins, aur shortlink-verified users** guzar jaate
  hain — verified user wahi results dekhta hai jo premium user dekhta hai.
* `PREMIUM_CONTENT_ENABLED=False` env se feature poori tarah band.

---

## 🔔 Notify-when-public flow

1. Normal user lock screen par **🔔 Notify when public** dabata hai.
2. Owner `/release_premium` chalata hai (ya `schedule_release` ka time poora
   hota hai).
3. Bot sab waiting users ko PM karta hai — "ab sabke liye available hai 🔍"
   + inline **Search now** button. List phir clear ho jati hai.

---

## 🗄 Storage

MongoDB collection: `premium_contents` (`PREMIUM_CONTENT_COLLECTION` env se
badal sakte ho). Har doc ek keyword ka hai — keyword, display title, hit
count, subscribers, release schedule.

```text
{_id: "jawan", keyword: "jawan", display_title: "Jawan (2023)",
 is_released: false, released_at: null, release_at: null,
 hit_count: 42, subscribers: [123, 456], verified_users: [789]}
```

`verified_users` = wo users jinhone free shortlink-verify se ye title unlock
kiya hai (`add_verified_user` / `is_verified_user`).

---

## 💰 Plans (updated — premium thoda expensive)

Early access jaise premium-only value ki wajah se plans update kiye gaye hain
(`info.py` → `STAR_PREMIUM_PLANS`, Stars = FamPay ₹):

| Plan | ₹ / Stars |
|------|-----------|
| 7 days | 29 ₹ / 25 ⭐ |
| 15 days | 49 ₹ / 45 ⭐ |
| 30 days | 79 ₹ / 79 ⭐ |
| 45 days | 109 ₹ / 109 ⭐ |
| 60 days | 149 ₹ / 149 ⭐ |

`FAMPAY_PLANS` env na diya ho to FamPay wahi table use karta hai.

---

## ⚙️ Config

| Env | Default | Matlab |
|-----|---------|--------|
| `PREMIUM_CONTENT_ENABLED` | `True` | Feature master switch |
| `PREMIUM_CONTENT_COLLECTION` | `premium_contents` | Mongo collection name |
