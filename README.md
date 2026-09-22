<h1 align="center"><b>🚩 ᴊᴀɪ ꜱʜʀɪ ʀᴀᴍ 🚩</b></h1>

<p align="center">
  <img src="https://github.com/DreamXBotz/Pics/blob/main/dreamxbotz.jpg" alt="DreamxBotz Logo">
</p>

<h1 align="center">𝓓𝓻𝓮𝓪𝓶𝔁𝓑𝓸𝓽𝔃</h1>

---

## 👤 Owner

[![Contact Developer](https://img.shields.io/static/v1?label=Contact+Developer&message=On+Telegram&color=critical)](https://t.me/Deendayal_Support_Group)

---

<!-- > ## ⚠ <u>Under Maintenance</u> ⚠  
> This repository is currently under maintenance. Please **DO NOT deploy** until further notice. -->

## 🚀 Demo Bot


Try the live bot here:

[![Click Here](https://img.shields.io/badge/Demo%20Bot-Click%20Here-blue?style=flat&logo=telegram&labelColor=white)](https://t.me/Princess_V4_bot)

---

## 🔔 New Version Released – V1.4

- ✅ Spell Check Toggle (Group Only)
- ✅ Group Owners Can Manage Settings via Bot PM
- ✅ Reset All Group Settings (Owner Only)
- ✅ 3 Verification System

---

## 🙏 Special Thanks To

- 🌴 [⌯ Ꭺɴᴏɴʏᴍᴏᴜꜱ | ×͜× |](https://t.me/BeingXAnonymous)
- 🌴 [⌯ ᴢɪsʜᴀɴ | ×͜× |](https://t.me/IM_JISSHU)
- 🌴 [⌯ ʙʜᴀʀᴀᴛʜ | ×͜× |](https://t.me/Bharath_boy)
- 🌴 [Harshal Purohit Edits](https://github.com/HarshalPurohitEdits)
- 🌴 [Support Group](https://t.me/Deendayal_Support_Group)

---

## 🛠 Need Help Deploying?

Join our support group for assistance:

[![Join Support Group](https://img.shields.io/badge/Join%20Support%20Group-Click%20Here-blue?style=flat&logo=telegram&labelColor=white)](https://t.me/Deendayal_Support_Group)

---

## 🌟 Features
- ✅ Double db support 
- ✅ Stream Mode Toggle  
- ✅ 3 User Verification  
- ✅ Multi FSub Admin & Group Support  
- ✅ Auto Movie Info Updates  
- ✅ PM Search Toggle  
- ✅ Verified User Counter  
- ✅ Trending Titles  
- ✅ Advanced AI Spelling Correction  
- ✅ "Did You Mean?" - File-DB Fuzzy Suggestions with Coloured Buttons (auto-fixes obvious typos, `DB_SUGGEST` env, default True)  
- ✅ Request to Join via FSub (Admins Only)  
- ✅ Verified User Database Save  
- ✅ Superfast User Broadcast  
- ✅ Refer & Earn Premium  
- ✅ Top Searching  
- ✅ Best Streaming Website Integration
- ✅ Newly Uploaded Movies section (auto-filled from the bot DB, gold/dark theme, Telegram deep links)
- ✅ Movie hero on the watch page (IMDb poster + 16:9 backdrop + deep search link above the player)
- ✅ Premium Membership Management  
- ✅ Online Streaming & Fast Download  
- ✅ File Indexing Above 2GB  
- ✅ PreDVD & CamRip Auto Deletion  
- ✅ Multiple File Deletion  
- ✅ Settings Menu  
- ✅ Welcome Message  
- ✅ Auto File Filtering    
- ✅ Single Filter Button  
- ✅ Bot PM File Send Mode  
- ✅ Auto File Send  
- ✅ Forward Restriction  
- ✅ File Protection  
- ✅ Admin Commands  
- ✅ Group Broadcast  
- ✅ Full File Indexing Support  
- ✅ ID & User Info  
- ✅ Stats & Analytics  
- ✅ User Ban/Unban  
- ✅ Chat Leave/Disable  
- ✅ Auto Delete Old Files  
- ✅ …and more!

📌 *To stay updated with all new features, join our [Updates Channel](https://t.me/dreamxbotz).*

---

## 🆕 Newly Uploaded Movies (Stream Mode website section)

The streaming/download pages now open with a responsive, dark + gold
**“Newly Uploaded Movies”** rail that fills itself from the bot database:

- newest **20** movies, newest first, no duplicates (deterministic `MOVIE_ID`)
- real posters (TMDB → IMDb) with lazy loading, skeletons, empty and error states
- every card deep-links to `https://t.me/BOT_USERNAME?start=movie_MOVIE_ID`
  → the bot opens **that exact movie**, no manual searching
- no file ids, download links or the bot token ever reach the browser

**Setup:** nothing to install — it ships with the bot. See
[`docs/NEWLY_UPLOADED_MOVIES.md`](docs/NEWLY_UPLOADED_MOVIES.md) for the
placeholders (`BOT_USERNAME`, `API_URL`, `MOVIE_ID`, `DATABASE_CONNECTION`,
`TELEGRAM_BOT_TOKEN`), the API reference and the database schema.

### 🎬 Movie hero on the watch page

Every `/watch/…` page now opens with a **movie hero** strip above the player:

- the movie's **poster (2:3 card) and a 16:9 backdrop band**, taken from
  IMDb/TMDB through the bot (never loaded from a third party in the browser)
- title, year, quality badge (480p/720p/1080p/2160p) and language chips, plus
  the upload date of the streamed file
- an **Open in Telegram** button, a *Copy search link* button and a *Play here*
  jump — the poster card itself is the deep link
  `https://t.me/BOT_USERNAME?start=movie_MOVIE_ID`, so the bot opens that exact
  movie with no searching
- clean loading (skeleton), placeholder and error states, responsive down to
  small phones

Artwork is resolved server side (TMDB → IMDb), cached in the `movie_art`
collection and proxied from your own origin; switch it off with
`WATCH_HERO=False`. Details: section 9 of
[`docs/NEWLY_UPLOADED_MOVIES.md`](docs/NEWLY_UPLOADED_MOVIES.md).

Preview it locally without Telegram:

```bash
python tools/preview_section.py   # http://127.0.0.1:8080
#   /               the section (rail)
#   /watch/demo     the watch page with the movie hero
```

---

## ⚙️ Commands

```bash
movie_update        – Toggle movie update notifications
pm_search           – Toggle private message search
verification        – View total verified users
top                 – Search top trending items
start               – Start the bot
settings            – Modify bot settings
plan                – View available premium plans
myplan              – Check your active plan
stats               – View database stats
info                – Get user info
id                  – Get Telegram ID
link                – Create single post link
batch               – Create bulk post link
deleteall           – Delete all files from DB
delete              – Delete a specific file
deletefiles         – Remove PreDVD and CamRip files
broadcast           – Broadcast to users
grp_broadcast       – Broadcast to groups
enable              – Enable group joining
disable             – Disable group
leave               – Leave group
ban                 – Ban user from bot
unban               – Unban user
add_premium         – Add premium access
remove_premium      – Remove premium access
premium_users       – List premium users
restart             – Restart the bot
```

---

## ⚠️ Disclaimer

This repository is intended **strictly for educational purposes only**.  
The authors are **not responsible** for any misuse or abuse of this code.  
Use at your own discretion and **always respect platform rules and copyrights**.

---

## 📜 License

This project is licensed under the [MIT License](https://github.com/MrRaazz/DreamxBotz/blob/main/LICENSE)

---

<p align="center"><b>Jai Shree Krishna 🙏😉</b></p>
