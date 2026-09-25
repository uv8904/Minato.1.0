# 🛡️ Startup resilience — the bot comes up even when something is off

Before this change `bot.py` was one long chain of `await`s and the `__main__`
loop only knew about `FloodWait`. Any other exception anywhere in the chain —
a wrong `LOG_CHANNEL`, Mongo still waking up, the web port already taken, a
typo in one plugin, one bad `MULTI_TOKEN` — killed the process with a
traceback, and on Koyeb/Heroku that means a crash-loop with the bot offline.

Only two things are actually required for the bot to serve users: a connected
Telegram client and its handlers. Everything else is **best effort** now.

```
python bot.py
   │
   ├─ preload_plugins()             plugins/**/*.py are imported exactly once;
   │  └─ quarantine failures        syntax/runtime failures become empty modules,
   │                                so the framework skips only the broken plugin
   ├─ dreamxbotz.start()            REQUIRED — discovers handlers from those same
   │                                cached modules and connects to Telegram
   ├─ get_me()                      REQUIRED
   │
   │   ── best effort from here on (logged, never fatal) ──
   ├─ initialize_clients()          extra MULTI_TOKEN clients
   ├─ FamPay workers                same module instance as registered handlers
   ├─ db.get_banned()               Mongo down → empty ban lists for this run
   ├─ Media/Media2.ensure_indexes() Mongo slow → indexes are created next restart
   ├─ newly-uploaded worker         (already guarded)
   ├─ restart notice → LOG_CHANNEL  PeerIdInvalid / ChatWriteForbidden → warning
   ├─ restart notice → each admin   (already guarded)
   ├─ aiohttp web server on PORT    port taken → Telegram side keeps running
   └─ idle()

__main__ retry loop
   FloodWait          → sleep e.value, retry (as before)
   KeyboardInterrupt  → clean exit (as before)
   any other error    → traceback in the log, client disconnected, retry after
                        5s → 10s → 20s → 40s → … capped at 300s
```

## What you will see in the logs

| Situation | Old behaviour | New behaviour |
|---|---|---|
| Bot is not admin in `LOG_CHANNEL` / wrong id | crash right after `started on @Bot` | `WARNING Couldn't send the restart message to LOG_CHANNEL …` and the bot works |
| Mongo unreachable at boot | crash in `get_banned()` / `ensure_indexes()` | `ERROR … starting with empty lists` / `… will retry next restart`, bot works |
| `PORT` already in use | crash | `ERROR Web server failed to start on port …`, Telegram side works |
| Syntax/import error in `plugins/foo.py` | crash inside `Client.start()` | `ERROR Plugin plugins.foo … DISABLED`, everything else loads |
| Every plugin executed twice | registered handlers and background workers could use different module globals | preload once, then let the framework scan that exact cached module |
| One invalid `MULTI_TOKEN_n` | `TypeError` from `dict([None])` | that client is skipped, the rest are used |
| Telegram unreachable while the container comes up | crash | `ERROR Startup failed (attempt 1) … retrying in 5s`, then 10s, 20s, … |

The premium-expiry background task (`plugins.check_expired_premium`) also
survives a Mongo error now — it logs, sleeps 30 s and tries again instead of
silently dying until the next restart.

## Knobs

`START_RETRY_BASE_DELAY` (5 s) and `START_RETRY_MAX_DELAY` (300 s) at the top of
`bot.py`. There is deliberately no retry limit: the platform's own health check
(the web server on `PORT`) still restarts a container that never gets healthy,
and every attempt is logged with its full traceback.

## Tests

```
pytest tests/test_startup_resilience.py -q
```

covers the back-off curve, plugin quarantine, `dreamxbotz_start()` reaching
`idle()` with every optional step failing (and the happy path), a failing
`start()` still propagating to the retry loop, the `MULTI_TOKEN` fix and the
premium-expiry worker.
