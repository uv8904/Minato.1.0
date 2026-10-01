# User-created bot clones

The main bot exposes a full-width **Create your own bot — /clone** button on
`/start`, plus `/clone` in private chat. A user must create their own bot with
`@BotFather` (`/newbot`) and provide that bot's token; Telegram does not let a
third-party bot create BotFather bots with a single tap.

## Deployment setup

Set a persistent encryption key in the main bot's environment. Generate one
once and keep the same value across deploys/restarts:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Then configure:

```env
CLONE_ENABLED=True
CLONE_ENCRYPTION_KEY=<the generated key>
CLONE_MAX_BOTS=1
CLONE_MAX_RECORDS=100
CLONE_MIN_FREE_RAM_MB=256
CLONE_START_TIMEOUT=60
CLONE_SESSION_WORKDIR=/tmp/minato_clone_sessions
```

Clone Telegram auth sessions contain reusable credentials. The manager creates
this directory with mode `0700`; if it already exists, it must already be
private and writable. Do not point it at the shared `/tmp` root. Persist the
directory only if you want session reuse across deploys (the BotFather token
is still needed to re-authorize after a session loss).

`CLONE_MAX_BOTS` is a hard cap (the parser caps it at 10); keep it low until
you've measured the host's real memory use. The manager checks the container's
cgroup RAM headroom when available and refuses to start a clone below
`CLONE_MIN_FREE_RAM_MB`. If memory cannot be measured, it fails closed. A
single active clone per owner is allowed. Run one main bot process/replica for
the clone manager; the repository's default worker formation is one.

If cloning is not configured (for example, no valid encryption key, zero
slots, or insufficient free RAM), `/clone` explains that before requesting a
token. Cloning can also be disabled immediately with `CLONE_ENABLED=False`.

## What a clone runs

- Every clone is a separate Python process, so its admin list and plugin globals
  cannot leak into another user's bot. Its `ADMINS` contains only its creator.
- Each clone has a separate Mongo database named `minato_clone_<bot_id>` and a
  separate session file inside the private `CLONE_SESSION_WORKDIR`. Its
  files/users/group settings do not mix with the main bot or another clone.
- To add files, the owner adds their clone as an admin to their own file channel,
  starts the clone, then uses `/index` in its PM. The clone's `CHANNELS` starts
  empty, so it does not attempt to index the main bot's private channels.
- Public website/streaming, auto-import userbot and FamPay workers are disabled
  in clone processes. This keeps one deployment's port, account sessions,
  payment credentials and background workers isolated and conserves resources.
- `/myclone` reports status, `/restartclone` retries a saved clone,
  `/cancelclone` cancels an unsubmitted token prompt, and `/deleteclone` stops
  it and erases the encrypted token plus its Telegram session file. The owner
  should also revoke that bot token with `@BotFather` when they no longer need it.

## Token handling and limits

The clone token is a bearer credential: anyone who has it can control that bot.
The flow is PM-only, warns users before asking, deletes the submitted Telegram
message before validation, validates the token with Telegram's `getMe`, and
stores only Fernet-encrypted token data in MongoDB. If message deletion fails,
the token is not used or stored and the user is told to revoke it. The worker
receives the token through its process environment; it is never placed on the command line or written by the
clone manager to logs. The host process can necessarily access the token while
starting/running the clone, so users should only use this on a host they trust.

One failed or paused clone record remains visible to its owner via `/myclone`
until `/deleteclone`; this prevents a token from being silently replaced by a
second bot. Invalid BotFather tokens are not stored, and `CLONE_MAX_RECORDS`
bounds the total saved clone records (including failures). MongoDB must allow
the configured database user to create/write the per-clone databases.

Clones consume CPU, RAM, Telegram API capacity and MongoDB storage. A hard
process cap and RAM reserve reduce risk, but cannot guarantee a particular
hosting plan will support a clone under every traffic pattern. Increase
`CLONE_MAX_BOTS` only after load-testing on the actual deployment.
