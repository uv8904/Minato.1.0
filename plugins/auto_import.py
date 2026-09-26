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
      FloodWait auto-handle.
"""

import asyncio
import logging
import re

from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait
from pyrogram.errors.exceptions.bad_request_400 import ChannelInvalid, ChannelPrivate, PeerIdInvalid
from pyrogram.handlers import MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from info import ADMINS, API_ID, API_HASH, CHANNELS, USER_SESSION, AUTO_IMPORT_DELAY
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

grab_lock = asyncio.Lock()                # ek waqt me ek /grab
grab_cancel = False

# Settings (Mongo-backed cache)
settings = {
    "enabled": False,
    "watch": [],      # normalized entries: "@botusername" / "-1001234567890"
    "target": None,   # copy destination chat id
}

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


def chat_keys(message):
    """Ek message ko kin keys se match kar sakte hain (username + ids)."""
    keys = set()
    chat = message.chat
    if chat:
        if getattr(chat, "username", None):
            keys.add("@" + chat.username.lower())
        keys.add(str(chat.id))
    sender = message.from_user
    if sender:
        if getattr(sender, "username", None):
            keys.add("@" + sender.username.lower())
        keys.add(str(sender.id))
    return keys


def is_watched(message):
    return bool(chat_keys(message) & set(settings["watch"]))


def media_of(message):
    if message.media not in MEDIA_TYPES:
        return None
    return getattr(message, message.media.value, None)


async def load_settings():
    settings["enabled"] = bool(await mdb.get_config(CFG_ENABLED, False))
    watch = await mdb.get_config(CFG_WATCH, []) or []
    settings["watch"] = [e for e in (norm_entry(x) for x in watch) if e]
    target = await mdb.get_config(CFG_TARGET, None)
    try:
        settings["target"] = int(target) if target else (CHANNELS[0] if CHANNELS else None)
    except (TypeError, ValueError):
        settings["target"] = target  # "@username" jaisa string target as-is


async def save_enabled():
    await mdb.set_config(CFG_ENABLED, settings["enabled"])


async def save_watch():
    await mdb.set_config(CFG_WATCH, settings["watch"])


async def save_target():
    await mdb.set_config(CFG_TARGET, settings["target"])


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
    try:
        try:
            await ubot.copy_message(target, message.chat.id, message.id)
        except FloodWait as e:
            logger.warning("FloodWait %ss during auto-import copy, waiting", e.value)
            await asyncio.sleep(e.value + 1)
            await ubot.copy_message(target, message.chat.id, message.id)
        pending["copied"] += 1
        pending["names"].append((getattr(media, "file_name", None) or "file")[:64])
        pending["names"] = pending["names"][-3:]
    except Exception as e:
        pending["failed"] += 1
        pending["last_error"] = f"{type(e).__name__}: {e}"[:120]
        logger.error("Auto-import copy failed: %s", pending["last_error"])
        # Target channel tak nahi pahunch sakte — har file pe retry bekaar,
        # user ko turant batao (Saved Messages me).
        if isinstance(e, (ChannelInvalid, ChannelPrivate, PeerIdInvalid)):
            try:
                await ubot.send_message(
                    "me",
                    "⚠️ <b>Auto-Import: target channel access nahi ho pa raha.</b>\n"
                    "Apne userbot account ko destination channel me <b>admin</b> "
                    "banao, phir /autoimport off → on karke test karo.",
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


async def start_userbot():
    """bot.py se best-effort call hota hai. USER_SESSION nahi to kuch nahi hota."""
    global ubot, worker_task, flusher_task
    await load_settings()
    if not USER_SESSION:
        logger.info("USER_SESSION empty – Auto-Import userbot off (docs/AUTO_IMPORT.md)")
        return
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
        worker_task = asyncio.create_task(_copy_worker())
        flusher_task = asyncio.create_task(_summary_flusher())
        logger.info(
            "Auto-Import userbot online as %s | watch=%s target=%s",
            getattr(me, "username", None) or me.id,
            settings["watch"] or "-",
            settings["target"],
        )
    except Exception as e:
        ubot = None
        logger.error("Auto-Import userbot start FAILED (bot normal chalega): %s", e)


async def stop_userbot():
    global ubot
    for task in (worker_task, flusher_task):
        if task:
            task.cancel()
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
            return await message.reply(setup_needed_text())
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
    await message.reply(
        f"<b>⚙️ Auto-Import</b> — {status}\n"
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
    if ubot is None:
        return await message.reply(setup_needed_text())
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
        settings["target"] = int(entry) if not entry.startswith("@") else entry
    except ValueError:
        return await message.reply("❌ Invalid channel id.")
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
    """-100123 -> 100123, '@name' -> '@name' — CHANNELS membership check ke liye."""
    try:
        return abs(int(value))
    except (TypeError, ValueError):
        return str(value)


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
        return await message.reply(setup_needed_text())
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
            return await status.edit(
                "❌ Ye channel tumhare <b>account</b> se accessible nahi hai.\n"
                "Private channel ho to apne account se us channel me <b>join</b> "
                "hona zaroori hai, phir dobara /grab karo."
            )
        if end is None:
            end = await _detect_last_id(chat)
            if end is None:
                return await status.edit("❌ Last message id detect nahi ho paya — explicitly do: <code>/grab @channel 1 5000</code>")
        total = end - start + 1
        if total <= 0:
            return await status.edit("🚫 Copy karne jaisi range nahi mili.")
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
                    await asyncio.sleep(e.value + 1)
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
                        await asyncio.sleep(e.value + 1)
                        try:
                            await ubot.copy_message(target, chat, m.id)
                            copied += 1
                        except Exception:
                            failed += 1
                    except Exception:
                        failed += 1
                    await asyncio.sleep(1.0)
                batch_start = batch_end + 1
                done = copied + failed + skipped
                percent = done * 100 / total
                rate = (asyncio.get_event_loop().time() - started) / max(done, 1)
                await status.edit(
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
            return await status.edit(f"❌ Grab error: <code>{e}</code>")
        await status.edit(
            "✅ <b>Grab khatam!</b>\n"
            f"✅ Copied: <code>{copied}</code> (target <code>{target}</code>)\n"
            f"⏭️ Skipped: <code>{skipped}</code>\n"
            f"❌ Failed: <code>{failed}</code>\n"
            "ℹ️ Copies channel me pahunchte hi bot apne aap index kar dega."
            + ("\n🚫 Cancel kiya gaya tha." if grab_cancel else "")
        )
