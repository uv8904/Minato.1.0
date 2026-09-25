
from aiohttp import web
from .route import routes
from asyncio import sleep 
from datetime import datetime
from database.users_chats_db import db
from dreamxbotz.server import movie_api, static_assets
from info import LOG_CHANNEL, URL
import aiohttp
import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.ERROR)

async def web_server():
    web_app = web.Application(client_max_size=30000000)
    # Stream Mode · "Newly Uploaded Movies" — the JSON API and its static
    # assets must be registered *before* the catch-all stream route in
    # plugins/route.py, otherwise "/{path:\S+}" would swallow them.
    # Docs: docs/NEWLY_UPLOADED_MOVIES.md
    web_app.add_routes(static_assets.routes)
    web_app.add_routes(movie_api.routes)
    # Analytics + AI Recommendations (MinatoVerse Netflix Pack)
    try:
        from dreamxbotz.server import analytics_api

        web_app.add_routes(analytics_api.routes)
    except Exception as e:
        logging.warning(f"Analytics routes not registered: {e}")
    # FamPay · FamGateway webhook receiver (docs/FAMPAY_SETUP.md) — also before
    # the catch-all, so POSTs to /fampay/webhook always reach it.
    try:
        from dreamxbotz.server import fampay_webhook

        web_app.add_routes(fampay_webhook.routes)
    except Exception as e:
        logging.warning(f"FamPay webhook route not registered: {e}")
    web_app.add_routes(routes)
    return web_app

async def check_expired_premium(client):
    while 1:
        # A Mongo blip (very common right after a restart) must not kill this
        # task for good – log it and try again on the next round.
        try:
            data = await db.get_expired(datetime.now())
        except Exception as e:
            logging.warning(f"Premium expiry check failed, retrying in 30s: {e}")
            await sleep(30)
            continue
        for user in data:
            user_id = user["id"]
            await db.remove_premium_access(user_id)
            try:
                user = await client.get_users(user_id)
                await client.send_message(
                    chat_id=user_id,
                    text=f"<b>ʜᴇʏ {user.mention},\n\n𝑌𝑜𝑢𝑟 𝑃𝑟𝑒𝑚𝑖𝑢𝑚 𝐴𝑐𝑐𝑒𝑠𝑠 𝐻𝑎𝑠 𝐸𝑥𝑝𝑖𝑟𝑒𝑑 𝑇ℎ𝑎𝑛𝑘 𝑌𝑜𝑢 𝐹𝑜𝑟 𝑈𝑠𝑖𝑛𝑔 𝑂𝑢𝑟 𝑆𝑒𝑟𝑣𝑖𝑐𝑒 😊. 𝐼𝑓 𝑌𝑜𝑢 𝑊𝑎𝑛𝑡 𝑇𝑜 𝑇𝑎𝑘𝑒 𝑃𝑟𝑒𝑚𝑖𝑢𝑚 𝐴𝑔𝑎𝑖𝑛, 𝑇ℎ𝑒𝑛 𝐶𝑙𝑖𝑐𝑘 𝑂𝑛 𝑇ℎ𝑒 /plan 𝐹𝑜𝑟 𝑇ℎ𝑒 𝐷𝑒𝑡𝑎𝑖𝑙𝑠 𝑂𝐹 𝑇ℎ𝑒 𝑃𝑙𝑎𝑛𝑠..\n\n\n<blockquote>आपका 𝑷𝒓𝒆𝒎𝒊𝒖𝒎 𝑨𝒄𝒄𝒆𝒔𝒔 समाप्त हो गया है हमारी सेवा का उपयोग करने के लिए धन्यवाद 😊। यदि आप फिर से 𝑷𝒓𝒆𝒎𝒊𝒖𝒎 लेना चाहते हैं, तो योजनाओं के विवरण के लिए /plan पर 𝑪𝒍𝒊𝒄𝒌 करें।</blockquote></b>"
                )
                await client.send_message(LOG_CHANNEL, text=f"<b>#Premium_Expire\n\nUser name: {user.mention}\nUser id: <code>{user_id}</code>")
            except Exception as e:
                print(e)
            await sleep(0.5)
        await sleep(1)

async def keep_alive():
    """Keep bot alive by sending periodic pings."""
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(298)
            try:
                async with session.get(URL) as resp:
                    if resp.status != 200:
                        logging.warning(f"⚠️ Ping Error! Status: {resp.status}")
            except Exception as e:
                logging.error(f"❌ Ping Failed: {e}")           

