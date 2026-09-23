import re
import os
import logging
from info import  *
from imdb import Cinemagoer 
import asyncio
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import InputUserDeactivated, UserNotParticipant, FloodWait, UserIsBlocked, PeerIdInvalid, ChatAdminRequired, MessageNotModified
from pyrogram import enums
from typing import Union
from Script import script
from typing import List
from database.users_chats_db import db
import requests
from shortzy import Shortzy
from dreamxbotz.util.buttons import blue, green, red
# Re-exported so plugins can `from utils import ...` (same as blue/green/red).
from dreamxbotz.util.title_notify import (
    check_new_file,
    notify_keyboard,
    notify_rows,
    register_notify_request,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

BTN_URL_REGEX = re.compile(
    r"(\[([^\[]+?)\]\((buttonurl|buttonalert):(?:/{0,2})(.+?)(:same)?\))"
)


imdb = Cinemagoer() 
BANNED = {}
SMART_OPEN = '“'
SMART_CLOSE = '”'
START_CHAR = ('\'', '"', SMART_OPEN)


class temp(object):   
    BANNED_USERS = []
    BANNED_CHATS = []
    ME = None
    CURRENT=int(os.environ.get("SKIP", 2))
    CANCEL = False
    B_USERS_CANCEL = False
    B_GROUPS_CANCEL = False 
    MELCOW = {}
    U_NAME = None
    B_NAME = None
    B_LINK = None
    SETTINGS = {}
    GETALL = {}
    SHORT = {}
    IMDB_CAP = {}
    VERIFICATIONS = {}
    TEMP_INVITE_LINKS = {}
    LISTENERS = {}   # (chat_id, user_id) -> asyncio.Future, used by wait_for_reply()
    FLASH = {}       # runtime cache for the start-flash / alive-sticker values


def start_buttons():
    """Coloured keyboard for the /start message (PM) and the 'back to home' callback.

    Green  -> main action (add bot to group)
    Blue   -> navigation (help / about / top searching)
    Red    -> upgrade / premium
    """
    return InlineKeyboardMarkup([
        [
            green('➕ ᴀᴅᴅ ᴍᴇ ᴛᴏ ʏᴏᴜʀ ɢʀᴏᴜᴘ ➕', url=f'http://telegram.me/{temp.U_NAME}?startgroup=true')
        ], [
            blue('📚 ʜᴇʟᴘ', callback_data='help'),
            blue('❤️ ᴀʙᴏᴜᴛ', callback_data='about')
        ], [
            blue('🔍 ᴛᴏᴘ sᴇᴀʀᴄʜɪɴɢ', callback_data='topsearch'),
            red('💎 ᴜᴘɢʀᴀᴅᴇ', callback_data='premium_info')
        ]
    ])


async def wait_for_reply(chat_id: int, user_id: int, timeout: int = 60) -> Message:
    """Wait for the next text message from ``user_id`` in ``chat_id``.

    Drop-in replacement for pyromod's ``bot.listen(chat_id=..., user_id=...)``.
    Raises ``asyncio.TimeoutError`` if nothing arrives in ``timeout`` seconds.
    The message is delivered by the ``catch_reply`` handler in plugins/commands.py.
    """
    key = (int(chat_id), int(user_id))
    old = temp.LISTENERS.pop(key, None)
    if old and not old.done():
        old.cancel()
    fut = asyncio.get_running_loop().create_future()
    temp.LISTENERS[key] = fut
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    finally:
        if temp.LISTENERS.get(key) is fut:
            temp.LISTENERS.pop(key, None)


# ---------------------------------------------------------------------------
# Flash media — the little emoji/sticker/GIF shown right before the start
# photo (default 🌿 leaf) and the /alive sticker. Configured with
# /setstartemoji & /setalivesticker (admin) or the START_EMOJI /
# ALIVE_STICKER env vars; runtime values are stored in MongoDB so they
# survive restarts.
# ---------------------------------------------------------------------------

FLASH_KINDS = ("sticker", "anim", "photo", "video", "text")

_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{30,}$")


def parse_flash_value(value):
    """Split a stored flash value into ``(kind, payload)``.

    Supported formats::

        off                        -> ("off", None)
        sticker:<file_id>          -> ("sticker", file_id)
        anim:<file_id|url>         -> ("anim", ...)
        photo:<file_id|url>        -> ("photo", ...)
        video:<file_id>            -> ("video", ...)
        text:<anything>            -> ("text", ...)
        https://...gif/.mp4/...    -> ("anim", url)
        https://...(anything else) -> ("photo", url)
        <raw file_id>              -> best-guess media type
        <emoji / short text>       -> ("text", ...)
    """
    v = (value or "").strip()
    if not v:
        return ("off", None)
    if v.lower() in ("off", "false", "0", "disable", "disabled", "none"):
        return ("off", None)
    if ":" in v:
        kind, payload = v.split(":", 1)
        if kind.lower() in FLASH_KINDS:
            return (kind.lower(), payload.strip())
    if v.startswith(("http://", "https://", "www.")):
        low = v.lower().split("?")[0]
        if low.endswith((".gif", ".mp4", ".webm", ".mov", ".mkv", ".tgs")):
            return ("anim", v)
        return ("photo", v)
    if _FILE_ID_RE.match(v):
        # Raw Telegram file_id (type unknown) — send_flash() probes sticker
        # first, then animation, then photo.
        return ("file", v)
    return ("text", v)


async def get_flash_value(key, env_default):
    """Runtime flash value for ``key``: MongoDB override > env default (cached)."""
    if key in temp.FLASH:
        return temp.FLASH[key]
    from database.config_db import mdb  # lazy import avoids import-order issues
    value = await mdb.get_config(key, env_default)
    temp.FLASH[key] = value
    return value


async def send_start_flash(message, key="start_flash", env_default="🌿"):
    """Send the flash media shown just before the start photo.

    Returns the sent message so the caller can delete it after a moment,
    or ``None`` when the flash is disabled or failed (it is purely
    decorative, so failures must never break /start or /alive).
    """
    value = await get_flash_value(key, env_default)
    kind, payload = parse_flash_value(value)
    if kind == "off":
        return None
    sent = None
    try:
        if kind == "sticker":
            sent = await message.reply_sticker(payload)
        elif kind == "anim":
            try:
                sent = await message.reply_animation(payload)
            except Exception:
                sent = await message.reply_video(payload)
        elif kind == "photo":
            sent = await message.reply_photo(payload)
        elif kind == "video":
            sent = await message.reply_video(payload)
        elif kind == "file":
            # Unknown raw file_id: probe sticker -> animation -> photo.
            for sender in (message.reply_sticker, message.reply_animation, message.reply_photo):
                try:
                    sent = await sender(payload)
                    break
                except Exception:
                    continue
        else:
            sent = await message.reply_text(payload)
    except Exception:
        logging.exception("start-flash send failed for %s (value=%r)", key, value)
    return sent

async def is_req_subscribed(bot, user_id, rqfsub_channels):
    btn = []
    for ch_id in rqfsub_channels:
        if await db.has_joined_channel(user_id, ch_id):
            continue
        try:
            member = await bot.get_chat_member(ch_id, user_id)
            if member.status != enums.ChatMemberStatus.BANNED:
                await db.add_join_req(user_id, ch_id)
                continue
        except UserNotParticipant:
            pass
        except Exception as e:
            logger.error(f"Error checking membership in {ch_id}: {e}")

        try:
            chat   = await bot.get_chat(ch_id)
            invite = await bot.create_chat_invite_link(
                ch_id,
                creates_join_request=True
            )
            btn.append([blue(f"⛔️ Join {chat.title}", url=invite.invite_link)])
        except ChatAdminRequired:
            logger.warning(f"Bot not admin in {ch_id}")
        except Exception as e:
            logger.warning(f"Invite link error for {ch_id}: {e}")
            
    return btn


async def is_subscribed(bot, user_id, fsub_channels):
    btn = []
    for channel_id in fsub_channels:
        try:
            chat = await bot.get_chat(int(channel_id))
            await bot.get_chat_member(channel_id, user_id)
        except UserNotParticipant:
            try:
                invite = await bot.create_chat_invite_link(channel_id, creates_join_request=False)
                btn.append([blue(f"📢 Join {chat.title}", url=invite.invite_link)])
            except Exception as e:
                logger.warning(f"Failed to create invite for {channel_id}: {e}")
        except Exception as e:
            logger.exception(f"is_subscribed error for {channel_id}: {e}")
            pass
    return btn

async def is_check_admin(bot, chat_id, user_id):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]
    except:
        return False
    
async def users_broadcast(user_id, message, is_pin):
    try:
        m=await message.copy(chat_id=user_id)
        if is_pin:
            await m.pin(both_sides=True)
        return True, "Success"
    except FloodWait as e:
        await asyncio.sleep(e.x)
        return await users_broadcast(user_id, message)
    except InputUserDeactivated:
        await db.delete_user(int(user_id))
        logging.info(f"{user_id}-Removed from Database, since deleted account.")
        return False, "Deleted"
    except UserIsBlocked:
        logging.info(f"{user_id} -Blocked the bot.")
        await db.delete_user(user_id)
        return False, "Blocked"
    except PeerIdInvalid:
        await db.delete_user(int(user_id))
        logging.info(f"{user_id} - PeerIdInvalid")
        return False, "Error"
    except Exception as e:
        return False, "Error"

async def groups_broadcast(chat_id, message, is_pin):
    try:
        m = await message.copy(chat_id=chat_id)
        if is_pin:
            try:
                await m.pin()
            except:
                pass
        return "Success"
    except FloodWait as e:
        await asyncio.sleep(e.x)
        return await groups_broadcast(chat_id, message)
    except Exception as e:
        await db.delete_chat(chat_id)
        return "Error"

async def junk_group(chat_id, message):
    try:
        kk = await message.copy(chat_id=chat_id)
        await kk.delete(True)
        return True, "Succes", 'mm'
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await junk_group(chat_id, message)
    except Exception as e:
        await db.delete_chat(int(chat_id))       
        logging.info(f"{chat_id} - PeerIdInvalid")
        return False, "deleted", f'{e}\n\n'
    

async def clear_junk(user_id, message):
    try:
        key = await message.copy(chat_id=user_id)
        await key.delete(True)
        return True, "Success"
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await clear_junk(user_id, message)
    except InputUserDeactivated:
        await db.delete_user(int(user_id))
        logging.info(f"{user_id}-Removed from Database, since deleted account.")
        return False, "Deleted"
    except UserIsBlocked:
        logging.info(f"{user_id} -Blocked the bot.")
        return False, "Blocked"
    except PeerIdInvalid:
        await db.delete_user(int(user_id))
        logging.info(f"{user_id} - PeerIdInvalid")
        return False, "Error"
    except Exception as e:
        return False, "Error"  

async def get_poster(query, bulk=False, id=False, file=None):
    if not id:
        query = (query.strip()).lower()
        title = query
        year = re.findall(r'[1-2]\d{3}$', query, re.IGNORECASE)
        imdb
        if year:
            year = list_to_str(year[:1])
            title = (query.replace(year, "")).strip()
        elif file is not None:
            year = re.findall(r'[1-2]\d{3}', file, re.IGNORECASE)
            if year:
                year = list_to_str(year[:1]) 
        else:
            year = None
        movieid = imdb.search_movie(title.lower(), results=10)
        if not movieid:
            return None
        if year:
            filtered=list(filter(lambda k: str(k.get('year')) == str(year), movieid))
            if not filtered:
                filtered = movieid
        else:
            filtered = movieid
        movieid=list(filter(lambda k: k.get('kind') in ['movie', 'tv series'], filtered))
        if not movieid:
            movieid = filtered
        if bulk:
            return movieid
        movieid = movieid[0].movieID
    else:
        movieid = query
    movie = imdb.get_movie(movieid)
    imdb.update(movie, info=['main', 'vote details'])
    if movie.get("original air date"):
        date = movie["original air date"]
    elif movie.get("year"):
        date = movie.get("year")
    else:
        date = "N/A"
    plot = ""
    if not LONG_IMDB_DESCRIPTION:
        plot = movie.get('plot')
        if plot and len(plot) > 0:
            plot = plot[0]
    else:
        plot = movie.get('plot outline')
    if plot and len(plot) > 800:
        plot = plot[0:800] + "..."

    return {
        'title': movie.get('title'),
        'votes': movie.get('votes'),
        "aka": list_to_str(movie.get("akas")),
        "seasons": movie.get("number of seasons"),
        "box_office": movie.get('box office'),
        'localized_title': movie.get('localized title'),
        'kind': movie.get("kind"),
        "imdb_id": f"tt{movie.get('imdbID')}",
        "cast": list_to_str(movie.get("cast")),
        "runtime": list_to_str(movie.get("runtimes")),
        "countries": list_to_str(movie.get("countries")),
        "certificates": list_to_str(movie.get("certificates")),
        "languages": list_to_str(movie.get("languages")),
        "director": list_to_str(movie.get("director")),
        "writer":list_to_str(movie.get("writer")),
        "producer":list_to_str(movie.get("producer")),
        "composer":list_to_str(movie.get("composer")) ,
        "cinematographer":list_to_str(movie.get("cinematographer")),
        "music_team": list_to_str(movie.get("music department")),
        "distributors": list_to_str(movie.get("distributors")),
        'release_date': date,
        'year': movie.get('year'),
        'genres': list_to_str(movie.get("genres")),
        'poster': movie.get('full-size cover url'),
        'plot': plot,
        'rating': str(movie.get("rating")),
        'url':f'https://www.imdb.com/title/tt{movieid}'
    }

async def get_shortlink(link, grp_id, is_second_shortener=False, is_third_shortener=False):
    settings = await get_settings(grp_id)
    if is_third_shortener:             
        api, site = settings['api_three'], settings['shortner_three']
    else:
        if is_second_shortener:
            api, site = settings['api_two'], settings['shortner_two']
        else:
            api, site = settings['api'], settings['shortner']
    shortzy = Shortzy(api, site)
    try:
        link = await shortzy.convert(link)
    except Exception as e:
        link = await shortzy.get_quick_link(link)
    return link

async def get_settings(group_id):
    settings = temp.SETTINGS.get(group_id)
    if not settings:
        settings = await db.get_settings(group_id)
        temp.SETTINGS.update({group_id: settings})
    return settings
    
async def save_group_settings(group_id, key, value):
    current = await get_settings(group_id)
    current.update({key: value})
    temp.SETTINGS.update({group_id: current})
    await db.update_settings(group_id, current)

def clean_filename(file_name):
    prefixes = ('[', '@', 'www.')
    unwanted = {word.lower() for word in BAD_WORDS}
    
    file_name = ' '.join(
        word for word in file_name.split()
        if not (word.startswith(prefixes) or word.lower() in unwanted)
    )
    return file_name

def get_size(size):
    units = ["Bytes", "KB", "MB", "GB", "TB", "PB", "EB"]
    size = float(size)
    i = 0
    while size >= 1024.0 and i < len(units):
        i += 1
        size /= 1024.0
    return "%.2f %s" % (size, units[i])

def extract_request_content(message_text):
    match = re.search(r"<u>(.*?)</u>", message_text)
    if match:
        return match.group(1).strip()
    match = re.search(r"📝 ʀᴇǫᴜᴇꜱᴛ ?: ?(.*?)(?:\n|$)", message_text)
    if match:
        return match.group(1).strip()
    return message_text.strip()

def generate_settings_text(settings, title, reset_done=False):
    note = "\n<b>📌 ɴᴏᴛᴇ :- ʀᴇꜱᴇᴛ ꜱᴜᴄᴄᴇꜱꜱғᴜʟʟʏ ✅</b>" if reset_done else ""
    return f"""<b>⚙️ ʏᴏᴜʀ sᴇᴛᴛɪɴɢs ꜰᴏʀ - {title}</b>

✅️ <b><u>1sᴛ ᴠᴇʀɪꜰʏ sʜᴏʀᴛɴᴇʀ</u></b>
<b>ɴᴀᴍᴇ</b> - <code>{settings.get("shortner", "N/A")}</code>
<b>ᴀᴘɪ</b> - <code>{settings.get("api", "N/A")}</code>

✅️ <b><u>2ɴᴅ ᴠᴇʀɪꜰʏ sʜᴏʀᴛɴᴇʀ</u></b>
<b>ɴᴀᴍᴇ</b> - <code>{settings.get("shortner_two", "N/A")}</code>
<b>ᴀᴘɪ</b> - <code>{settings.get("api_two", "N/A")}</code>

✅️ <b><u>𝟹ʀᴅ ᴠᴇʀɪꜰʏ sʜᴏʀᴛɴᴇʀ</u></b>
<b>ɴᴀᴍᴇ</b> - <code>{settings.get("shortner_three", "N/A")}</code>
<b>ᴀᴘɪ</b> - <code>{settings.get("api_three", "N/A")}</code>

⏰ <b>2ɴᴅ ᴠᴇʀɪꜰɪᴄᴀᴛɪᴏɴ ᴛɪᴍᴇ</b> - <code>{settings.get("verify_time", "N/A")}</code>
⏰ <b>𝟹ʀᴅ ᴠᴇʀɪꜰɪᴄᴀᴛɪᴏɴ ᴛɪᴍᴇ</b> - <code>{settings.get("third_verify_time", "N/A")}</code>

1️⃣ <b>ᴛᴜᴛᴏʀɪᴀʟ ʟɪɴᴋ 1</b> - {settings.get("tutorial", TUTORIAL)}
2️⃣ <b>ᴛᴜᴛᴏʀɪᴀʟ ʟɪɴᴋ 2</b> - {settings.get("tutorial_2", TUTORIAL_2)}
3️⃣ <b>ᴛᴜᴛᴏʀɪᴀʟ ʟɪɴᴋ 3</b> - {settings.get("tutorial_3", TUTORIAL_3)}

📝 <b>ʟᴏɢ ᴄʜᴀɴɴᴇʟ ɪᴅ</b> - <code>{settings.get("log", "N/A")}</code>
🚫 <b>ꜰꜱᴜʙ ᴄʜᴀɴɴᴇʟ ɪᴅ</b> - <code>{settings.get("fsub", "N/A")}</code>


🎯 <b>ɪᴍᴅʙ ᴛᴇᴍᴘʟᴀᴛᴇ</b> - <code>{settings.get("template", "N/A")}</code>

📂 <b>ꜰɪʟᴇ ᴄᴀᴘᴛɪᴏɴ</b> - <code>{settings.get("caption", "N/A")}</code>
{note}
"""

async def group_setting_buttons(grp_id):
    settings = await get_settings(grp_id)
    buttons = [[
                InlineKeyboardButton('ʀᴇꜱᴜʟᴛ ᴘᴀɢᴇ', callback_data=f'setgs#button#{settings.get("button")}#{grp_id}',),
                InlineKeyboardButton('ʙᴜᴛᴛᴏɴ' if settings.get("button") else 'ᴛᴇxᴛ', callback_data=f'setgs#button#{settings.get("button")}#{grp_id}',),
            ],[
                InlineKeyboardButton('ꜰɪʟᴇ ꜱᴇᴄᴜʀᴇ', callback_data=f'setgs#file_secure#{settings["file_secure"]}#{grp_id}',),
                InlineKeyboardButton('✔ Oɴ' if settings["file_secure"] else '✘ Oғғ', callback_data=f'setgs#file_secure#{settings["file_secure"]}#{grp_id}',),
            ],[
                InlineKeyboardButton('ɪᴍᴅʙ ᴘᴏꜱᴛᴇʀ', callback_data=f'setgs#imdb#{settings["imdb"]}#{grp_id}',),
                InlineKeyboardButton('✔ Oɴ' if settings["imdb"] else '✘ Oғғ', callback_data=f'setgs#imdb#{settings["imdb"]}#{grp_id}',),
            ],[
                InlineKeyboardButton('ᴡᴇʟᴄᴏᴍᴇ ᴍꜱɢ', callback_data=f'setgs#welcome#{settings["welcome"]}#{grp_id}',),
                InlineKeyboardButton('✔ Oɴ' if settings["welcome"] else '✘ Oғғ', callback_data=f'setgs#welcome#{settings["welcome"]}#{grp_id}',),
            ],[
                InlineKeyboardButton('ᴀᴜᴛᴏ ᴅᴇʟᴇᴛᴇ', callback_data=f'setgs#auto_delete#{settings["auto_delete"]}#{grp_id}',),
                InlineKeyboardButton('✔ Oɴ' if settings["auto_delete"] else '✘ Oғғ', callback_data=f'setgs#auto_delete#{settings["auto_delete"]}#{grp_id}',),
            ],[
                InlineKeyboardButton('ᴍᴀx ʙᴜᴛᴛᴏɴꜱ', callback_data=f'setgs#max_btn#{settings["max_btn"]}#{grp_id}',),
                InlineKeyboardButton('10' if settings["max_btn"] else f'{MAX_B_TN}', callback_data=f'setgs#max_btn#{settings["max_btn"]}#{grp_id}',),
            ],[
                InlineKeyboardButton('ꜱᴘᴇʟʟ ᴄʜᴇᴄᴋ',callback_data=f'setgs#spell_check#{settings["spell_check"]}#{str(grp_id)}'),
                InlineKeyboardButton('✔ Oɴ' if settings["spell_check"] else '✘ Oғғ',callback_data=f'setgs#spell_check#{settings["spell_check"]}#{str(grp_id)}')
            ],[
                InlineKeyboardButton('Vᴇʀɪғʏ', callback_data=f'setgs#is_verify#{settings.get("is_verify", IS_VERIFY)}#{grp_id}'),
                InlineKeyboardButton('✔ Oɴ' if settings.get("is_verify", IS_VERIFY) else '✘ Oғғ', callback_data=f'setgs#is_verify#{settings.get("is_verify", IS_VERIFY)}#{grp_id}'),
            ],
            [
                red("❌ Remove ❌ ", callback_data=f"removegrp#{grp_id}")
            ],
            [
                red('⇋ ᴄʟᴏꜱᴇ ꜱᴇᴛᴛɪɴɢꜱ ᴍᴇɴᴜ ⇋', callback_data='close_data')
    ]]
    return buttons

def get_file_id(msg: Message):
    if msg.media:
        for message_type in (
            "photo",
            "animation",
            "audio",
            "document",
            "video",
            "video_note",
            "voice",
            "sticker"
        ):
            obj = getattr(msg, message_type)
            if obj:
                setattr(obj, "message_type", message_type)
                return obj

def extract_user(message: Message) -> Union[int, str]:
    user_id = None
    user_first_name = None
    if message.reply_to_message:
        user_id = message.reply_to_message.from_user.id
        user_first_name = message.reply_to_message.from_user.first_name

    elif len(message.command) > 1:
        if (
            len(message.entities) > 1 and
            message.entities[1].type == enums.MessageEntityType.TEXT_MENTION
        ):
           
            required_entity = message.entities[1]
            user_id = required_entity.user.id
            user_first_name = required_entity.user.first_name
        else:
            user_id = message.command[1]
            # don't want to make a request -_-
            user_first_name = user_id
        try:
            user_id = int(user_id)
        except ValueError:
            pass
    else:
        user_id = message.from_user.id
        user_first_name = message.from_user.first_name
    return (user_id, user_first_name)

def list_to_str(k):
    if not k:
        return "N/A"
    elif len(k) == 1:
        return str(k[0])
    elif MAX_LIST_ELM:
        k = k[:int(MAX_LIST_ELM)]
        return ' '.join(f'{elem}, ' for elem in k)
    else:
        return ' '.join(f'{elem}, ' for elem in k)

async def log_error(client, error_message):
    try:
        await client.send_message(
            chat_id=LOG_CHANNEL, 
            text=f"<b>⚠️ Error Log:</b>\n<code>{error_message}</code>"
        )
    except Exception as e:
        print(f"Failed to log error: {e}")


def get_time(seconds):
    periods = [(' ᴅᴀʏs', 86400), (' ʜᴏᴜʀ', 3600), (' ᴍɪɴᴜᴛᴇ', 60), (' sᴇᴄᴏɴᴅ', 1)]
    result = ''
    for period_name, period_seconds in periods:
        if seconds >= period_seconds:
            period_value, seconds = divmod(seconds, period_seconds)
            result += f'{int(period_value)}{period_name}'
    return result

def get_readable_time(seconds):
    periods = [('d', 86400), ('h', 3600), ('m', 60), ('s', 1)]
    result = []
    for period_name, period_seconds in periods:
        if seconds >= period_seconds:
            period_value, seconds = divmod(seconds, period_seconds)
            result.append(f'{int(period_value)}{period_name}')
    return ' '.join(result)  

def generate_season_variations(search_raw: str, season_number: int):
    return [
        f"{search_raw} s{season_number:02}",
        f"{search_raw} season {season_number}",
        f"{search_raw} season {season_number:02}",
    ]



async def get_seconds(time_string):
    def extract_value_and_unit(ts):
        value = ""
        unit = ""
        index = 0
        while index < len(ts) and ts[index].isdigit():
            value += ts[index]
            index += 1
        unit = ts[index:].lstrip()
        if value:
            value = int(value)
        return value, unit
    value, unit = extract_value_and_unit(time_string)
    if unit == 's':
        return value
    elif unit == 'min':
        return value * 60
    elif unit == 'hour':
        return value * 3600
    elif unit == 'day':
        return value * 86400
    elif unit == 'month':
        return value * 86400 * 30
    elif unit == 'year':
        return value * 86400 * 365
    else:
        return 0
    

def clean_search_text(search_raw: str) -> str:
    search_lower = search_raw.lower()
    phrases = re.split(r'\s{2,}', search_lower.strip())
    lang_pattern = r'\b(hin(di)?|eng(lish)?|mal(ayalam)?|tam(il)?|tel(ugu)?|kan(nada)?|ben(gali)?|mar(athi)?|urdu|guj(arat)?|punj(abi)?)\b'
    season_pattern = r's(eason)?\s*0*\d+'
    quality_pattern = r'\b(360p|480p|720p|1080p|1440p|2160p|4k)\b'  
    cleaned_phrases = []
    for phrase in phrases:
        phrase = re.sub(season_pattern, '', phrase, flags=re.IGNORECASE)
        phrase = re.sub(lang_pattern, '', phrase, flags=re.IGNORECASE)
        phrase = re.sub(quality_pattern, '', phrase, flags=re.IGNORECASE)
        phrase = re.sub(r'\s+', ' ', phrase).strip()
        if phrase:
            cleaned_phrases.append(phrase)
    unique_phrases = []
    seen = set()
    for cp in cleaned_phrases:
        if cp not in seen:
            unique_phrases.append(cp)
            seen.add(cp)
    if unique_phrases:
        return unique_phrases[0].title()
    else:
        return ""
