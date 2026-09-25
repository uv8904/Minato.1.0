# 🚧 Maintenance mode — block normal users with one command

Sometimes the bot needs to be taken offline for normal users (DB migration,
poster rebuild, flood recovery, …) **without** stopping the process. Killing
the bot also kills admin access, takes time to come back up and looks broken
to users. Maintenance mode instead keeps the bot running and simply refuses
every non-admin update with a friendly notice.

## Commands (admins only)

| Command | What it does |
|---|---|
| `/maint on` | Maintenance mode **ON** – normal users are blocked instantly |
| `/maint off` | Maintenance mode **OFF** – the bot is back for everyone instantly |
| `/maint status` | Show the current state |

`/maintenance` works as an alias, and `enable / start / disable / stop` are
accepted as on/off synonyms. Every toggle also posts a heads-up to
`LOG_CHANNEL` (best effort – a wrong log channel never breaks the toggle).

## What normal users experience

* **Private chat** – every message gets the "🚧 Bot is under maintenance"
  notice with a Support button. No search, no files, no `/start`.
* **Groups** – every update is swallowed too, but the notice is only sent in
  reply to `/commands` and `@bot_username` mentions so the chat is not
  spammed with one notice per message.
* **Inline buttons** – pressing any button pops the alert
  *"Bot is under maintenance! Only admins can use it right now."*

Admins (`ADMINS` from the environment, id- **or** username-based) keep full
access the whole time: the blocker lets their updates fall through untouched.

## How it works

```
/maint on ──► temp.MAINTENANCE = True ──► db.set_maintenance_mode(True)
                  │                              │
                  ▼                              ▼
        plugins/maintenance.py          Mongo (bot_settings collection,
        handlers in group -200          doc id "maintenance_mode")
```

* The blocker handlers are registered in handler group **-200**
  (`MAINT_GROUP`), before every other handler in the repo (the lowest group
  used elsewhere is -10). While the flag is on they `stop_propagation()` on
  every non-admin message and callback query, so no search/file/payment
  plugin ever sees the update.
* When maintenance is **off** the custom filter matches nothing and the
  plugin is a zero-cost no-op – the normal handler chain is unchanged.
* The flag is **persisted** in the `bot_settings` collection, and `bot.py`
  restores it right after the banned users/chats at startup. A restart (or
  deploy) during a maintenance window therefore keeps the window closed.
* If Mongo is unreachable while loading or persisting the flag the bot logs a
  warning and keeps running (fail-open for reads, in-memory for writes) –
  never a crash loop.

## Files

| File | Role |
|---|---|
| `plugins/maintenance.py` | Blocker handlers + `/maint` admin command |
| `database/users_chats_db.py` | `set_maintenance_mode()` / `get_maintenance_mode()` |
| `utils.py` | `temp.MAINTENANCE` runtime flag |
| `bot.py` | Restores the flag at startup |
| `Script.py` | User-facing notices (`MAINTENANCE_TXT`, …) |
| `tests/test_maintenance_mode.py` | Behavioural tests (mongomock-backed) |
