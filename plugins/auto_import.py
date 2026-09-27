"""
Auto-Import Userbot — dusre bots ki files manual copy karne ka jhanjhat khatam.
================================================================================

Problem:
    Doosre filter-bots se files mangwao → har file khud apne channel me forward
    karo → tab jaake bot use index karta hai. Bahut time-consuming.

Solution:
    USER_SESSION env me apne Telegram account ki session do. Ab tumhare account
    pe PM me aayi files (jinhe /watch se watchlist me daala ho) automatically
    copy ho kar file-channel me chale jaati hain — wahan se bot apne aap DB me
    index kar leta hai (plugins/channel.py).

Commands (sirf ADMINS, bot ke PM me):
    /autoimport on|off        Feature on/off + status
    /watch <@bot|chat_id>     Us chat/bot ki files auto-copy me add karo
    /unwatch <n|@bot|id>      Watchlist se hatao
    /watchlist                Watchlist dekho
    /target <channel_id>      Copy destination channel (default: CHANNELS[0])
    /grab <chat> [start] [end]   Pura channel bulk-copy (userbot account se)

Setup guide: docs/AUTO_IMPORT.md
Session banane ke liye: python tools/generate_session.py

NOTES:
    • Userbot client BINA plugins ke start hota hai, isliye bot ke saare
      handlers (start, pmfilter, ...) sirf bot client pe chalte hain — userbot
      par sirf is module ke manually add kiye handlers chalti hain.
    • Settings MongoDB me persist hoti hain (restart-safe).
    • Copy flood-safe hai: har copy ke beech AUTO_IMPORT_DELAY seconds gap +
      FloodWait auto-handle (retries), aur /grab progress edits cosmetic hain —
      edit fail hone se bulk copy kabhi cancel nahi hoti.
    • SELF-HEAL: agar boot par Mongo/Telegram thoda slow ho ya userbot client
      beech me disconnect ho jaye, ek supervisor har AUTO_IMPORT_RETRY_DELAY
      seconds me dobara start karne ki koshish karta hai — bot restart ki
      zaroorat nahi. /autoimport status me "online/retrying" dikhta hai.
"""

import asyncio
import logging
import re

from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait
from pyrogram.errors.exceptions.bad_request_400 import ChannelInvalid, ChannelPrivate, PeerIdInvalid
from pyrogram.handlers import MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from info import ADMINS, API_ID, API_HASH, CHANNELS, USER_SESSION, AUTO_IMPORT_DELAY, AUTO_IMPORT_RETRY_DELAY
from database.config_db import mdb

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------
MEDIA_TYPES = (
    enums.MessageMediaType.VIDEO,
    enums.MessageMediaType.DOCUMENT,
    enums.MessageMediaType.AUDIO,
)

CFG_ENABLED = "autoimport_enabled"
CFG_WATCH = "autoimport_watch"
CFG_TARGET = "autoimport_target"

ubot: Client = None                       # user session client (None = feature off)
queue: asyncio.Queue = asyncio.Queue()    # incoming files awaiting copy
worker_task = None
flusher_task = None
supervisor_task = None
start_lock = asyncio.Lock()               # ek waqt me ek hi start/stop cycle

grab_lock = asyncio.Lock()                # ek waqt me ek /grab
grab_cancel = False

# Settings (Mongo-backed cache)
settings = {
    "enabled": False,
    "watch": [],      # normalized entries: "@botusername" / "-1001234567890"
    "target": None,   # copy destination chat id
}
settings_loaded = False                   # Mongo se load hua ya nahi (self-heal)

# Diagnostics — /autoimport status me dikhta hai
health = {
    "state": "off",          # "off" | "online" | "retrying" | "disabled"
    "last_error": "",
    "restarts": 0,
}

BOOT_RETRY_BASE_DELAY = 2   # boot attempts ke beech backoff (tests ise zero kar dete hain)
GRAB_COPY_DELAY = 1.0        # /grab ke har copy ke beech gap (seconds)

# Summary counters (Saved Messages me flush hote hain)
pending = {"copied": 0, "failed": 0, "last_error": "", "names": []}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def progress_bar(percent, length=10):
    filled = int(length * percent / 100)
    return "🟩" * filled + "⬜️" * (length - filled)


def fmt_secs(seconds):
    seconds = int(max(0, seconds))
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")


def norm_entry(entry):
    """'@Bot_Name' -> '@bot_name', '123' -> '123', '-100 123' invalid ko None."""
    entry = str(entry or "").strip().lstrip("@").strip()
    if not entry:
        return None
    if entry.lstrip("-").isdigit():
        try:
            return str(int(entry))
        except ValueError:
            return None
    return "@" + entry.lower()


def _id_aliases(value):
    """Ek numeric chat-id ke sabhi matching forms.

    '-1002086319581' (supergroup) ⇄ '2086319581' (t.me/c/ link me aata hai).
    Users ke plain ids bhi rakhe jate hain — matching form-insensitive rahe.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return set()
    out = {str(n)}
    if n <= -1000000000000 and str(n).startswith("-100"):
        out.add(str(abs(n) - 1000000000000))
    elif n > 0:
        out.add(str(-(1000000000000 + n)))
    return out


def chat_keys(message):
    """Ek message ko kin keys se match kar sakte hain (username + ids + aliases)."""
    keys = set()
    chat = message.chat
    if chat:
        if getattr(chat, "username", None):
            keys.add("@" + chat.username.lower())
        keys |= _id_aliases(chat.id)
    sender = message.from_user
    if sender:
        if getattr(sender, "username", None):
            keys.add("@" + sender.username.lower())
        keys |= _id_aliases(sender.id)
    return keys


def is_watched(message):
    watched = set()
    for entry in settings["watch"]:
        watched |= _id_aliases(entry) if str(entry).lstrip("-").isdigit() else {entry}
    return bool(chat_keys(message) & watched)


def media_of(message):
    if message.media not in MEDIA_TYPES:
        return None
    return getattr(message, message.media.value, None)


async def load_settings():
    """Mongo se settings load karo. Mongo down ho to RAISE mat karo — purane
    in-memory values rakho aur supervisor baad me retry karega (self-heal)."""
    global settings_loaded
    try:
        settings["enabled"] = bool(await mdb.get_config(CFG_ENABLED, False))
        watch = await mdb.get_config(CFG_WATCH, []) or []
        settings["watch"] = [e for e in (norm_entry(x) for x in watch) if e]
        target = await mdb.get_config(CFG_TARGET, None)
        try:
            settings["target"] = int(target) if target else (CHANNELS[0] if CHANNELS else None)
        except (TypeError, ValueError):
            settings["target"] = target  # "@username" jaisa string target as-is
        settings_loaded = True
        health["last_error"] = ""
    except Exception as e:
        settings_loaded = False
        health["last_error"] = f"settings load: {type(e).__name__}: {e}"[:120]
        logger.warning("Auto-Import settings load failed (will retry): %s", e)


async def save_enabled():
    try:
        await mdb.set_config(CFG_ENABLED, settings["enabled"])
    except Exception as e:
        logger.warning("Auto-Import: enabled flag persist nahi ho paya (in-memory chalega): %s", e)


async def save_watch():
    try:
        await mdb.set_config(CFG_WATCH, settings["watch"])
    except Exception as e:
        logger.warning("Auto-Import: watchlist persist nahi ho payi (in-memory chalegi): %s", e)


async def save_target():
    try:
        await mdb.set_config(CFG_TARGET, settings["target"])
    except Exception as e:
        logger.warning("Auto-Import: target persist nahi ho paya (in-memory chalega): %s", e)


def setup_needed_text():
    return (
        "<b>⚙️ Auto-Import setup adhura hai</b>\n\n"
        "Is feature ke liye <code>USER_SESSION</code> env var me apne Telegram "
        "account ki session string chahiye.\n\n"
        "<b>Kaise banaye:</b>\n"
        "1. PC/termux pe: <code>pip install electrogram</code>\n"
        "2. <code>python tools/generate_session.py</code> chalao\n"
        "3. API_ID, API_HASH, phone number, login code daalo\n"
        "4. Jo <code>SESSION</code> string mile use hosting env "
        "<code>USER_SESSION</code> me daalo aur bot restart karo\n\n"
        "Full guide: <code>docs/AUTO_IMPORT.md</code>"
    )


def offline_text():
    err = health.get("last_error") or "unknown"
    return (
        "<b>⚠️ Auto-Import userbot abhi offline hai.</b>\n\n"
        f"<code>USER_SESSION</code> set hai, par userbot account connect nahi "
        f"ho paya (last error: <code>{err}</code>).\n\n"
        f"Bot khud har <code>{AUTO_IMPORT_RETRY_DELAY}s</code> me retry karta "
        "rahega — kuch der baad <code>/autoimport</code> se status dekho.\n\n"
        "Agar lagataar na chale:\n"
        "• Session string sahi hai? (regenerate: <code>tools/generate_session.py</code>)\n"
        "• Telegram ne session revoke to nahi kiya (Settings → Active Sessions)?\n"
        "• Logs me <code>Auto-Import userbot start FAILED</code> dhoondho."
    )


def userbot_problem_text():
    """ubot None hai to sahi wajah batao — 'setup adhura' har baar nahi."""
    return setup_needed_text() if not USER_SESSION else offline_text()


async def _safe_edit(status, text, reply_markup=None):
    """Progress edit cosmetic hai — fail hone par kabhi grab/copy na toote."""
    try:
        await status.edit(text, reply_markup=reply_markup)
        return True
    except FloodWait as e:
        await asyncio.sleep(int(getattr(e, "value", 1) or 1) + 1)
        try:
            await status.edit(text, reply_markup=reply_markup)
            return True
        except Exception:
            return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Userbot side (client = tumhara personal account)
# ---------------------------------------------------------------------------
async def _on_incoming(client, message):
    """Userbot pe har incoming message — watched chat ki media ko queue karo."""
    try:
        if not settings["enabled"] or ubot is None:
            return
        if message.empty or message.media not in MEDIA_TYPES:
            return
        if not is_watched(message):
            return
        await queue.put(message)
    except Exception:
        logger.exception("auto-import incoming handler error")


async def _copy_one(message):
    target = settings["target"]
    media = media_of(message)
    last_err = None
    for attempt in range(3):
        try:
            await ubot.copy_message(target, message.chat.id, message.id)
            pending["copied"] += 1
            pending["names"].append((getattr(media, "file_name", None) or "file")[:64])
            pending["names"] = pending["names"][-3:]
            return
        except FloodWait as e:
            # Flood limit: utha ke so jao, phir retry. Long waits yahin absorb
            # hote hain — isliye copy fail nahi hoti, sirf slow hoti hai.
            wait = int(getattr(e, "value", 1) or 1) + 1
            logger.warning("Auto-import: FloodWait %ss (attempt %d), waiting", wait, attempt + 1)
            await asyncio.sleep(wait)
            last_err = e
        except (ChannelInvalid, ChannelPrivate, PeerIdInvalid) as e:
            last_err = e
            break
        except Exception as e:
            last_err = e
            break
    pending["failed"] += 1
    pending["last_error"] = f"{type(last_err).__name__}: {last_err}"[:120]
    logger.error("Auto-import copy failed: %s", pending["last_error"])
    # Target channel tak nahi pahunch sakte — har file pe retry bekaar,
    # user ko turant batao (Saved Messages me).
    if isinstance(last_err, (ChannelInvalid, ChannelPrivate, PeerIdInvalid)):
        try:
            await ubot.send_message(
                "me",
                "⚠️ <b>Auto-Import: target channel access nahi ho pa raha.</b>\n"
                "Apne userbot account ko destination channel me <b>admin</b> "
                "banao, phir /autoimport off → on karke test karo.",
            )
        except Exception:
            pass
    elif isinstance(last_err, FloodWait):
        try:
            await ubot.send_message(
                "me",
                f"⏳ <b>Auto-Import: Telegram flood limit</b> — copy abhi fail hui "
                f"(<code>{pending['last_error']}</code>). Bulk /grab ke baad ye "
                "normal hai; thodi der baad files dobara aane par auto-copy chalu.",
            )
        except Exception:
            pass


async def _copy_worker():
    """Queue se files utha kar target channel me copy karta hai (flood-safe)."""
    while True:
        message = await queue.get()
        try:
            await _copy_one(message)
        except Exception:
            logger.exception("auto-import worker error")
        finally:
            queue.task_done()
            await asyncio.sleep(max(1, AUTO_IMPORT_DELAY))


async def _summary_flusher():
    """Har 20s me pending copies ka short summary tumhari Saved Messages me."""
    while True:
        await asyncio.sleep(20)
        if not (pending["copied"] or pending["failed"]):
            continue
        if ubot is None:
            pending["copied"] = 0
            pending["failed"] = 0
            pending["last_error"] = ""
            pending["names"] = []
            continue
        lines = []
        if pending["copied"]:
            lines.append(f"✅ <b>{pending['copied']}</b> file(s) auto-import ho gayi")
        if pending["failed"]:
            lines.append(f"❌ {pending['failed']} fail — {pending['last_error']}")
        for name in pending["names"]:
            lines.append(f"📦 <code>{name}</code>")
        try:
            await ubot.send_message("me", "\n".join(lines))
        except Exception:
            pass
        pending["copied"] = 0
        pending["failed"] = 0
        pending["last_error"] = ""
        pending["names"] = []


# ---------------------------------------------------------------------------
# Userbot lifecycle (boot + self-healing supervisor)
# ---------------------------------------------------------------------------
async def _boot_userbot():
    """Client banao + start karo. Exception propagate nahi karti — health me
    record karti hai. Success par True return."""
    global ubot, worker_task, flusher_task
    if not USER_SESSION:
        health["state"] = "disabled"
        logger.info("USER_SESSION empty – Auto-Import userbot off (docs/AUTO_IMPORT.md)")
        return False
    last_err = None
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(BOOT_RETRY_BASE_DELAY * attempt)   # transient net/Mongo errors
        try:
            client = Client(
                name="autoimport_userbot",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=USER_SESSION,
                in_memory=True,
                sleep_threshold=30,
            )
            await client.start()
            me = await client.get_me()
            ubot = client
            # SIRF is module ka handler — bina plugins ke start hua hai isliye bot
            # ke saare handlers (start/pmfilter/...) userbot pe kabhi nahi chalenge.
            client.add_handler(
                MessageHandler(_on_incoming, filters.incoming & ~filters.service),
                group=-3,
            )
            for task in (worker_task, flusher_task):
                if task:
                    task.cancel()
            worker_task = asyncio.create_task(_copy_worker())
            flusher_task = asyncio.create_task(_summary_flusher())
            health["state"] = "online"
            health["last_error"] = ""
            health["restarts"] += 1
            logger.info(
                "Auto-Import userbot online as %s | watch=%s target=%s",
                getattr(me, "username", None) or me.id,
                settings["watch"] or "-",
                settings["target"],
            )
            return True
        except Exception as e:
            last_err = e
            ubot = None
            logger.warning(
                "Auto-Import userbot start attempt %d/3 failed: %s", attempt + 1, e
            )
    health["state"] = "retrying"
    health["last_error"] = f"{type(last_err).__name__}: {last_err}"[:120]
    logger.error(
        "Auto-Import userbot start FAILED (bot normal chalega, %ss me auto-retry): %s",
        AUTO_IMPORT_RETRY_DELAY,
        health["last_error"],
    )
    return False


async def start_userbot():
    """bot.py se best-effort call hota hai. USER_SESSION nahi to kuch nahi hota.

    Idempotent: dobara call par pehle wala client/tasks band karke naya start
    hota hai (duplicate handlers/copies rokne ke liye). Kabhi raise nahi karti.
    """
    global supervisor_task
    try:
        async with start_lock:
            await load_settings()
            if ubot is not None:
                await stop_userbot()
            await _boot_userbot()
        if supervisor_task is None or supervisor_task.done():
            supervisor_task = asyncio.create_task(_supervisor())
    except Exception as e:
        health["state"] = "retrying"
        health["last_error"] = f"{type(e).__name__}: {e}"[:120]
        logger.error("Auto-Import userbot not started (will auto-retry): %s", e)
        if supervisor_task is None or supervisor_task.done():
            try:
                supervisor_task = asyncio.create_task(_supervisor())
            except Exception:
                logger.exception("Auto-Import supervisor bhi start nahi ho paya")


async def _supervisor_tick():
    """Ek self-heal cycle: settings reload karo, dead client hatao, offline
    userbot dobara start karo. Tests ise directly call kar sakte hain."""
    async with start_lock:
        if not settings_loaded:
            await load_settings()
        if ubot is not None and not getattr(ubot, "is_connected", True):
            logger.warning("Auto-Import: userbot connection dead — restarting")
            await stop_userbot()
        if ubot is None and USER_SESSION:
            await _boot_userbot()


async def _supervisor():
    """Har AUTO_IMPORT_RETRY_DELAY seconds me self-heal. Zombie mode: kabhi
    marne nahi deta (exceptions yahin dab jate hain)."""
    while True:
        await asyncio.sleep(max(5, AUTO_IMPORT_RETRY_DELAY))
        try:
            await _supervisor_tick()
        except Exception:
            logger.exception("auto-import supervisor error")


async def stop_userbot():
    global ubot, worker_task, flusher_task
    for task in (worker_task, flusher_task):
        if task:
            task.cancel()
    worker_task = None
    flusher_task = None
    if ubot:
        try:
            await ubot.stop()
        except Exception:
            pass
    ubot = None


# ---------------------------------------------------------------------------
# Bot side — admin commands
# ---------------------------------------------------------------------------
@Client.on_message(filters.command("autoimport") & filters.private & filters.user(ADMINS), group=1)
async def autoimport_cmd(client, message):
    args = (message.text or "").split()
    if len(args) > 1 and args[1].lower() in ("on", "off"):
        want = args[1].lower() == "on"
        if want and ubot is None:
            if not USER_SESSION:
                return await message.reply(setup_needed_text())
            # Session set hai par userbot offline — turant retry kick karo.
            asyncio.create_task(start_userbot())
            return await message.reply(
                offline_text()
                + "\n\n🔄 <b>Retry kick kar diya</b> — kuch seconds baad "
                "<code>/autoimport</code> dobara bhejo."
            )
        settings["enabled"] = want
        await save_enabled()
        if want:
            await message.reply(
                "<b>✅ Auto-Import ON</b>\n\n"
                f"👀 Watchlist: <code>{len(settings['watch'])}</code> chat(s)\n"
                f"🎯 Target: <code>{settings['target']}</code>\n\n"
                "Ab jis bot/channel ko <code>/watch</code> kiya hai, uski files "
                "tumhare account pe aane par automatic copy ho kar channel me "
                "chali jayengi. Confirmations tumhari <b>Saved Messages</b> me "
                "milengi."
            )
        else:
            await message.reply("<b>⛔ Auto-Import OFF</b> — koi file copy nahi hogi.")
        return

    status = "🟢 ON" if settings["enabled"] else "🔴 OFF"
    if not USER_SESSION:
        link = "🔴 userbot off (USER_SESSION missing)"
    elif ubot is not None:
        link = "🟢 userbot online"
    else:
        link = f"🟡 userbot offline — auto-retry me ({health.get('last_error') or 'starting…'})"
    await message.reply(
        f"<b>⚙️ Auto-Import</b> — {status}\n"
        f"🔗 Userbot: {link}\n"
        f"👀 Watchlist: <code>{len(settings['watch'])}</code> chat(s)\n"
        f"🎯 Target: <code>{settings['target']}</code>\n\n"
        "<b>Commands:</b>\n"
        "<code>/autoimport on</code> — feature chalu karo\n"
        "<code>/autoimport off</code> — band karo\n"
        "<code>/watch @botname</code> — us bot ki files auto-copy\n"
        "<code>/watch -1001234567</code> — us channel/group ki files\n"
        "<code>/watchlist</code> — list dekho\n"
        "<code>/unwatch 1</code> — list se hatao\n"
        "<code>/target -1009876543</code> — copy destination badlo\n"
        "<code>/grab @channel 1 5000</code> — pura channel bulk-copy"
    )


@Client.on_message(filters.command("watch") & filters.private & filters.user(ADMINS), group=1)
async def watch_cmd(client, message):
    parts = (message.text or "").split(maxsplit=1)
    entry = norm_entry(parts[1]) if len(parts) > 1 else None
    if not entry:
        return await message.reply(
            "<b>Usage:</b> <code>/watch @botname</code> ya "
            "<code>/watch -1001234567890</code>\n\n"
            "Jo bot/channel watch karoge, uski media files auto-copy hongi."
        )
    if entry in settings["watch"]:
        return await message.reply(f"⚠️ <code>{entry}</code> pehle se watchlist me hai.")
    settings["watch"].append(entry)
    await save_watch()
    await message.reply(
        f"<b>✅ Watchlist me add ho gaya:</b> <code>{entry}</code>\n\n"
        + ("🟢 Auto-Import ON hai — is chat ki files ab copy hongi."
           if settings["enabled"] else
           "🔴 Auto-Import abhi <b>OFF</b> hai — <code>/autoimport on</code> se chalu karo.")
        + ("\n⚠️ Userbot abhi offline hai — " + offline_text().split("\n")[0]
           if ubot is None and USER_SESSION else "")
    )


@Client.on_message(filters.command("watchlist") & filters.private & filters.user(ADMINS), group=1)
async def watchlist_cmd(client, message):
    if not settings["watch"]:
        return await message.reply(
            "👀 <b>Watchlist khali hai.</b>\n\n"
            "<code>/watch @botname</code> se add karo — phir us bot se mangwai "
            "files khud-ba-khud channel me copy hongi."
        )
    lines = [f"<b>👀 Watchlist</b> ({len(settings['watch'])})\n"]
    for i, entry in enumerate(settings["watch"], 1):
        lines.append(f"  <code>{i}.</code> <code>{entry}</code>")
    lines.append(
        f"\n🎯 Target: <code>{settings['target']}</code> • "
        f"Status: {'🟢 ON' if settings['enabled'] else '🔴 OFF'}"
    )
    await message.reply("\n".join(lines))


@Client.on_message(filters.command("unwatch") & filters.private & filters.user(ADMINS), group=1)
async def unwatch_cmd(client, message):
    parts = (message.text or "").split(maxsplit=1)
    if not settings["watch"]:
        return await message.reply("👀 Watchlist pehle se khali hai.")
    arg = parts[1].strip() if len(parts) > 1 else ""
    removed = None
    if arg.isdigit() and 1 <= int(arg) <= len(settings["watch"]):
        removed = settings["watch"].pop(int(arg) - 1)
    else:
        entry = norm_entry(arg)
        if entry in settings["watch"]:
            settings["watch"].remove(entry)
            removed = entry
    if removed is None:
        return await message.reply(
            "<b>Usage:</b> <code>/unwatch 1</code> (list number) ya "
            "<code>/unwatch @botname</code>"
        )
    await save_watch()
    await message.reply(f"🗑️ <code>{removed}</code> watchlist se hata diya.")


@Client.on_message(filters.command("target") & filters.private & filters.user(ADMINS), group=1)
async def target_cmd(client, message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply(
            f"<b>🎯 Current target:</b> <code>{settings['target']}</code>\n\n"
            "<b>Badalne ke liye:</b> <code>/target -1001234567890</code>"
        )
    entry = norm_entry(parts[1])
    if not entry:
        return await message.reply("❌ Invalid channel id/username.")
    try:
        new_target = int(entry) if not entry.startswith("@") else entry
    except ValueError:
        return await message.reply("❌ Invalid channel id.")
    # t.me/c/ link wala bare id (e.g. 2086319581) bhi accept karo — jo form
    # actually resolve hoti hai wahi store karo.
    if ubot is not None and isinstance(new_target, int):
        candidates = [new_target]
        if abs(new_target) < 1000000000000:
            candidates.append(-(1000000000000 + abs(new_target)))
        for cand in candidates:
            try:
                await ubot.get_chat(cand)
                new_target = cand
                break
            except Exception:
                continue
    settings["target"] = new_target
    await save_target()
    notes = []
    in_channels = any(abs_chat_id(c) == abs_chat_id(settings["target"]) for c in CHANNELS)
    if not in_channels:
        notes.append(
            "⚠️ Ye channel <code>CHANNELS</code> env me nahi hai — is channel me "
            "<b>BOT ko bhi admin</b> banao, warna files copy to hongi par search "
            "me nahi aayengi."
        )
    if ubot is not None:
        try:
            await ubot.get_chat(settings["target"])
        except Exception:
            notes.append(
                "⚠️ Tumhara account is channel ko access nahi kar pa raha — "
                "userbot account ko bhi is channel me admin banao."
            )
    text = f"<b>✅ Target set:</b> <code>{settings['target']}</code>"
    if notes:
        text += "\n\n" + "\n".join(notes)
    await message.reply(text)


# ---------------------------------------------------------------------------
# /grab — pura channel bulk-copy (userbot account ki access se)
# ---------------------------------------------------------------------------
LINK_RE = (
    r"(?:https://)?(?:t\.me/|telegram\.me/|telegram\.dog/)(c/)?"
    r"(\d+|[a-zA-Z_0-9]+)(?:/(\d+))?"
)


def abs_chat_id(value):
    """Canonical chat id — '-1002086319581' aur '2086319581' same samjho
    (CHANNELS membership check ke liye)."""
    try:
        n = abs(int(value))
    except (TypeError, ValueError):
        return str(value)
    return n - 1000000000000 if n >= 1000000000000 else n


def parse_grab_args(text):
    """'/grab <chat> [start] [end]' → (chat, start, end) ya (None, reason)."""
    raw = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
    parts = raw.split()
    if not parts:
        return None, None, None, "Chat link/id/username do — <code>/grab @channel</code> ya <code>/grab -1001234 5000</code>"
    chat_arg, start_arg, end_arg = parts[0], (parts[1] if len(parts) > 1 else None), (parts[2] if len(parts) > 2 else None)
    chat, link_msg = None, None
    m = re.fullmatch(LINK_RE, chat_arg)
    if m:
        is_private, ident, msg_in_link = m.group(1), m.group(2), m.group(3)
        chat = int("-100" + ident) if (is_private and ident.isdigit()) else ident
        link_msg = int(msg_in_link) if msg_in_link else None
    elif chat_arg.lstrip("-").isdigit():
        chat = int(chat_arg)
    else:
        chat = chat_arg.lstrip("@")
    try:
        if start_arg and end_arg:
            start, end = int(start_arg), int(end_arg)
        elif start_arg:
            start, end = 1, int(start_arg)   # ek hi number diya to wo END hai
        else:
            start, end = 1, link_msg
    except ValueError:
        return None, None, None, "start/end numbers hone chahiye."
    if start < 1:
        start = 1
    if end is not None and end < start:
        return None, None, None, "end, start se bada hona chahiye."
    return chat, start, end, None


@Client.on_callback_query(filters.regex(r"^grab_cancel"))
async def grab_cancel_cb(client, query):
    global grab_cancel
    grab_cancel = True
    await query.answer("Cancelling grab…", show_alert=False)


@Client.on_message(filters.command("grab") & filters.private & filters.user(ADMINS), group=1)
async def grab_cmd(client, message):
    if ubot is None:
        return await message.reply(userbot_problem_text())
    chat, start, end, err = parse_grab_args(message.text or "")
    if err:
        return await message.reply(f"❌ {err}")
    if grab_lock.locked():
        return await message.reply("⏳ Ek /grab pehle se chal raha hai — pehle wo khatam hone do.")
    asyncio.create_task(_run_grab(client, message, chat, start, end))


async def _detect_last_id(chat):
    try:
        async for m in ubot.search_messages(chat, limit=1):
            return m.id
    except Exception:
        pass
    return None


async def _run_grab(client, message, chat, start, end):
    global grab_cancel
    async with grab_lock:
        grab_cancel = False
        target = settings["target"]
        status = await message.reply("🔍 <code>Channel scan ho raha hai…</code>")
        try:
            await ubot.get_chat(chat)
        except (ChannelInvalid, ChannelPrivate, PeerIdInvalid):
            return await _safe_edit(
                status,
                "❌ Ye channel tumhare <b>account</b> se accessible nahi hai.\n"
                "Private channel ho to apne account se us channel me <b>join</b> "
                "hona zaroori hai, phir dobara /grab karo."
            )
        except Exception as e:
            return await _safe_edit(status, f"❌ Channel resolve nahi ho paya: <code>{e}</code>")
        if end is None:
            end = await _detect_last_id(chat)
            if end is None:
                return await _safe_edit(status, "❌ Last message id detect nahi ho paya — explicitly do: <code>/grab @channel 1 5000</code>")
        total = end - start + 1
        if total <= 0:
            return await _safe_edit(status, "🚫 Copy karne jaisi range nahi mili.")
        copied = failed = skipped = 0
        started = asyncio.get_event_loop().time()
        BATCH = 200
        try:
            batch_start = start
            while batch_start <= end:
                if grab_cancel:
                    break
                batch_end = min(batch_start + BATCH - 1, end)
                try:
                    msgs = await ubot.get_messages(chat, list(range(batch_start, batch_end + 1)))
                    if not isinstance(msgs, list):
                        msgs = [msgs]
                except FloodWait as e:
                    await asyncio.sleep(int(getattr(e, "value", 1) or 1) + 1)
                    continue
                except Exception:
                    skipped += batch_end - batch_start + 1
                    batch_start = batch_end + 1
                    continue
                for m in msgs:
                    if m is None or m.empty or m.media not in MEDIA_TYPES:
                        skipped += 1
                        continue
                    try:
                        await ubot.copy_message(target, chat, m.id)
                        copied += 1
                    except FloodWait as e:
                        await asyncio.sleep(int(getattr(e, "value", 1) or 1) + 1)
                        try:
                            await ubot.copy_message(target, chat, m.id)
                            copied += 1
                        except Exception:
                            failed += 1
                    except Exception:
                        failed += 1
                    await asyncio.sleep(GRAB_COPY_DELAY)
                batch_start = batch_end + 1
                done = copied + failed + skipped
                percent = done * 100 / total
                rate = (asyncio.get_event_loop().time() - started) / max(done, 1)
                await _safe_edit(
                    status,
                    "📥 <b>Grab Progress</b>\n"
                    f"{progress_bar(percent)} <code>{percent:.1f}%</code>\n\n"
                    f"✅ Copied: <code>{copied}</code>\n"
                    f"⏭️ Skipped: <code>{skipped}</code>\n"
                    f"❌ Failed: <code>{failed}</code>\n"
                    f"⏱️ ETA: <code>{fmt_secs((total - done) * rate)}</code>",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Cancel", callback_data="grab_cancel")]]
                    ),
                )
        except Exception as e:
            logger.exception("grab failed")
            return await _safe_edit(status, f"❌ Grab error: <code>{e}</code>")
        summary = (
            "✅ <b>Grab khatam!</b>\n"
            f"✅ Copied: <code>{copied}</code> (target <code>{target}</code>)\n"
            f"⏭️ Skipped: <code>{skipped}</code>\n"
            f"❌ Failed: <code>{failed}</code>\n"
            "ℹ️ Copies channel me pahunchte hi bot apne aap index kar dega."
            + ("\n🚫 Cancel kiya gaya tha." if grab_cancel else "")
        )
        if not await _safe_edit(status, summary):
            # status message delete/edit fail ho gaya — kam se kam result bhej do
            try:
                await message.reply(summary)
            except Exception:
                pass
