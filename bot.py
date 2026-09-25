import sys
import glob
import importlib
import importlib.util
import types
from pathlib import Path
from pyrogram import Client, idle, __version__
from pyrogram.raw.all import layer
import time
from pyrogram.errors import FloodWait
import asyncio
from datetime import date, datetime
import pytz
from aiohttp import web
from database.ia_filterdb import Media, Media2
from database.users_chats_db import db
from info import *
from utils import temp
from Script import script
from plugins import web_server, check_expired_premium, keep_alive
from dreamxbotz.Bot import dreamxbotz
from dreamxbotz.util.keepalive import ping_server
from dreamxbotz.Bot.clients import initialize_clients
from PIL import Image
Image.MAX_IMAGE_PIXELS = 500_000_000

import logging
import logging.config

logging.config.fileConfig('logging.conf')
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.ERROR)
logging.getLogger("imdbpy").setLevel(logging.ERROR)
logging.getLogger("aiohttp").setLevel(logging.ERROR)
logging.getLogger("aiohttp.web").setLevel(logging.ERROR)
logging.getLogger("pymongo").setLevel(logging.WARNING)

botStartTime = time.time()
ppath = "plugins/*.py"
files = glob.glob(ppath)

# ---------------------------------------------------------------------------
# Startup resilience (docs/STARTUP_RESILIENCE.md)
#
# Only two things are truly required for the bot to be useful: a connected
# Telegram client and its handlers.  Everything else in dreamxbotz_start()
# (extra clients, Mongo indexes, the restart notice in LOG_CHANNEL, the web
# server, ...) is "nice to have" – a failure there is logged loudly but must
# NOT keep the bot from reaching idle().  Hard failures (Telegram unreachable
# while the container is still coming up, bad network, ...) are retried with an
# exponential back-off instead of killing the process with a traceback.
# ---------------------------------------------------------------------------
START_RETRY_BASE_DELAY = 5      # seconds; doubles after every failed attempt ...
START_RETRY_MAX_DELAY = 300     # ... but never waits longer than 5 minutes


def startup_retry_delay(attempt: int) -> int:
    """Back-off for the retry loop in __main__: 5s, 10s, 20s, ... capped at 300s."""
    attempt = max(1, int(attempt))
    return int(min(START_RETRY_BASE_DELAY * 2 ** (attempt - 1), START_RETRY_MAX_DELAY))


def quarantine_broken_plugins(root: str = "plugins") -> list:
    """Syntax-check every plugin *before* the client imports them.

    pyrogram imports ``plugins/**/*.py`` inside ``Client.start()`` and lets any
    error bubble up, so a single typo in one plugin used to take the whole bot
    down.  A file that does not even compile is replaced by an empty module in
    ``sys.modules`` – pyrogram's ``import_module`` then finds no handlers in it
    and every other plugin still loads.  Returns the dotted names it disabled.
    """
    broken = []
    base = Path(root).parent                # "plugins/foo.py" -> "plugins.foo"
    for path in sorted(Path(root).rglob("*.py")):
        if path.name == "__init__.py":      # packages – needed for imports to work at all
            continue
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except Exception as e:              # SyntaxError, UnicodeDecodeError, ...
            module_path = ".".join(path.relative_to(base).with_suffix("").parts)
            sys.modules[module_path] = types.ModuleType(module_path)
            logging.error("Plugin %s is broken and has been DISABLED for this run: %s", module_path, e)
            broken.append(module_path)
    if broken:
        logging.warning("%d broken plugin(s) skipped – fix them and restart: %s", len(broken), ", ".join(broken))
    return broken


def load_plugins(skip=()) -> tuple:
    """Import every plugins/*.py the classic way; one bad plugin is logged and skipped."""
    loaded, failed = [], []
    for name in files:
        plugins_dir = Path(name)
        plugin_name = plugins_dir.stem
        import_path = "plugins.{}".format(plugin_name)
        if import_path in skip:
            continue
        try:
            spec = importlib.util.spec_from_file_location(import_path, plugins_dir)
            load = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(load)
            sys.modules[import_path] = load
            print("DreamxBotz Imported => " + plugin_name)
            loaded.append(plugin_name)
        except Exception as e:
            logging.exception("Plugin %s failed to load and was skipped: %s", plugin_name, e)
            failed.append(plugin_name)
    if failed:
        logging.warning("%d plugin(s) failed to load: %s", len(failed), ", ".join(failed))
    return loaded, failed


async def stop_client_quietly():
    """Disconnect the client (if it got that far) so the next start() attempt is clean."""
    try:
        if getattr(dreamxbotz, "is_connected", False):
            await dreamxbotz.stop()
    except Exception as e:
        logging.warning("Ignoring error while stopping the client before retry: %s", e)


async def dreamxbotz_start():
    print('\n\nInitalizing DreamxBotz')
    broken_plugins = quarantine_broken_plugins()
    # --- required: without a connected client there is no bot -------------
    await dreamxbotz.start()
    bot_info = await dreamxbotz.get_me()
    dreamxbotz.username = bot_info.username
    # --- everything below is best effort -----------------------------------
    try:
        await initialize_clients()
    except Exception as e:
        logging.exception("Multi-client init failed – continuing with the main bot only: %s", e)
    load_plugins(skip=broken_plugins)
    # FamPay auto-approval: start the IMAP scanner + status poller now that the
    # plugins have registered their handlers (docs/FAMPAY_SETUP.md).
    try:
        from plugins.FamPay import start_fampay_workers

        start_fampay_workers()
    except Exception as e:
        logging.warning(f"FamPay workers not started: {e}")
    if ON_HEROKU:
        asyncio.create_task(ping_server()) 
    # Banned users/chats: a Mongo hiccup at boot means "nobody banned" for this
    # run instead of a dead bot.
    try:
        b_users, b_chats = await db.get_banned()
    except Exception as e:
        logging.exception("Couldn't load banned users/chats – starting with empty lists: %s", e)
        b_users, b_chats = [], []
    temp.BANNED_USERS = b_users
    temp.BANNED_CHATS = b_chats
    # Index creation is idempotent, Mongo is often still slow while the
    # container comes up – don't let it block the start.
    try:
        await Media.ensure_indexes()
        if MULTIPLE_DB:
            await Media2.ensure_indexes()
            print("Multiple Database Mode On. Now Files Will Be Save In Second DB If First DB Is Full")
        else:
            print("Single DB Mode On ! Files Will Be Save In First Database")
    except Exception as e:
        logging.exception("Couldn't ensure the Mongo indexes (search still works, will retry next restart): %s", e)
    # Stream Mode · "Newly Uploaded Movies" (docs/NEWLY_UPLOADED_MOVIES.md):
    # prepare the section's collection (indexes + poster backfill for the newest
    # movies).  Best effort – a failure here never blocks the bot from starting.
    try:
        from dreamxbotz.util.new_uploaded import start_worker as start_new_uploaded

        await start_new_uploaded()
    except Exception as e:
        logging.warning("Newly-uploaded movies worker not started: %s", e)
    me = bot_info
    temp.ME = me.id
    temp.U_NAME = me.username
    temp.B_NAME = me.first_name
    temp.B_LINK = me.mention
    dreamxbotz.username = '@' + me.username
    dreamxbotz.loop.create_task(check_expired_premium(dreamxbotz))
    logging.info(f"{me.first_name} with Pyrogram v{__version__} (Layer {layer}) started on {me.username}.")
    logging.info(LOG_STR)
    logging.info(script.LOGO)
    tz = pytz.timezone('Asia/Kolkata')
    today = date.today()
    now = datetime.now(tz)
    restart_time = now.strftime("%H:%M:%S %p")
    # Restart notice.  A wrong LOG_CHANNEL id or a bot that isn't admin there
    # raises PeerIdInvalid / ChatWriteForbidden – the #1 reason these bots used
    # to die right after "started on @...".  Warn and carry on.
    try:
        await dreamxbotz.send_message(chat_id=LOG_CHANNEL, text=script.RESTART_TXT.format(temp.B_LINK, today, restart_time))
    except Exception as e:
        logging.warning(f"Couldn't send the restart message to LOG_CHANNEL {LOG_CHANNEL} (is the bot admin there?): {e}")
    for admin_id in ADMINS:
        try:
            await dreamxbotz.send_message(chat_id=int(admin_id), text=f"🤖 {temp.B_NAME} Restarted Successfully ✅")
        except Exception as e:
            logging.warning(f"Couldn't send restart message to admin {admin_id}: {e}")
    # Web server (stream links, health checks on Koyeb/Heroku).  If the port is
    # taken or the app fails to build, the Telegram side keeps working.
    try:
        app = web.AppRunner(await web_server())
        await app.setup()
        bind_address = "0.0.0.0"
        await web.TCPSite(app, bind_address, PORT).start()
        logging.info(f"Web server listening on {bind_address}:{PORT}")
    except Exception as e:
        logging.exception(f"Web server failed to start on port {PORT} – bot keeps running without it: {e}")
    dreamxbotz.loop.create_task(keep_alive())
    await idle()
    
if __name__ == '__main__':
    # Use the loop the Client captured in its __init__ so dreamxbotz.loop and
    # the running loop are always the same object.
    loop = getattr(dreamxbotz, "loop", None) or asyncio.get_event_loop()
    attempt = 0
    while True:
        try:
            loop.run_until_complete(dreamxbotz_start())
            break  
        except FloodWait as e:
            print(f"FloodWait! Sleeping for {e.value} seconds.")
            time.sleep(e.value) 
        except KeyboardInterrupt:
            logging.info('Service Stopped Bye 👋')
            break
        except Exception as e:
            attempt += 1
            delay = startup_retry_delay(attempt)
            logging.exception(f"Startup failed (attempt {attempt}): {e!r} – retrying in {delay}s")
            loop.run_until_complete(stop_client_quietly())
            time.sleep(delay)
