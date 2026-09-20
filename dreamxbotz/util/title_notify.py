"""
"Notify me when uploaded" support.

Flow
----
1. A search finds nothing -> plugins/pmfilter.py shows the no-result keyboard
   built by :func:`notify_keyboard` (green *Notify me*, blue *Request movie to
   owner*, blue *Google spelling*, red *Close*).
2. The green button stores the search text through
   :func:`register_notify_request` -> ``database.users_chats_db.db``.
3. Every file that is indexed afterwards is pushed through
   :func:`check_new_file` (called from plugins/channel.py). Matching pending
   requests are PM'd to the user and then deleted.

The matching helpers (:func:`normalize`, :func:`match`) are pure functions with
no I/O so they can be unit tested without a bot or a database.
"""
import logging
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import quote_plus

from pyrogram.errors import FloodWait, InputUserDeactivated, PeerIdInvalid, UserIsBlocked
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from info import OWNER_LNK, TITLE_NOTIFY, TITLE_NOTIFY_TTL_DAYS
from dreamxbotz.util.buttons import blue, green, red

logger = logging.getLogger(__name__)

# search key ("chat_id-message_id") -> the text the user searched for.
# Telegram caps callback_data at 64 bytes, so the title itself is kept here
# (same trick as FRESH in plugins/pmfilter.py) and only the key travels.
PENDING_TEXT = {}

# Words that carry no title information: codecs, sources, qualities, languages,
# release-group noise. They are dropped before two titles are compared.
NOISE_WORDS = {
    "mkv", "mp4", "avi", "xvid", "x264", "x265", "hevc", "avc", "aac", "ac3", "dts",
    "esubs", "sub", "subs", "subtitle", "subtitles", "hin", "hindi", "eng", "english",
    "tam", "tamil", "tel", "telugu", "mal", "malayalam", "kan", "kannada", "ben",
    "bengali", "mar", "marathi", "guj", "gujarati", "urdu", "pun", "punjabi", "multi",
    "audio", "dual", "5 1", "7 1", "2 0",
    "web", "dl", "webdl", "webrip", "bluray", "brrip", "bdrip", "hdrip", "dvdrip",
    "hdtv", "hdcam", "hdts", "cam", "camrip", "predvd", "telesync", "ts", "tc",
    "360p", "480p", "540p", "720p", "1080p", "1440p", "2160p", "4k", "hd", "sd",
    "full", "movie", "film", "episode", "ep", "new", "latest", "official", "proper",
    "extended", "imax", "hdr", "sdr", "10bit", "8bit", "atmos", "remux",
    "nf", "netflix", "amzn", "prime", "hotstar", "zee5", "sonyliv", "jhs", "aha",
    "hbo", "apple", "dsnp", "paramount", "lionsgate",
}
NOISE_WORDS = {w.strip().lower() for w in NOISE_WORDS}

# Everything after these is a container extension, not part of the title.
_EXT_RE = re.compile(r"\b(mkv|mp4|avi|m4v|mov|ts)\s*$", re.IGNORECASE)
# "s01e04" -> "s01 e04" so a "S01E04" query matches an "S01 E04" filename.
_SEASON_EP_RE = re.compile(r"\bs0*(\d+)e0*(\d+)\b", re.IGNORECASE)
# Canonical season/episode numbering: "s1"/"s01" -> "s01", "e4"/"e04" -> "e04".
_SEASON_RE = re.compile(r"\bs0*(\d+)\b")
_EPISODE_RE = re.compile(r"\be0*(\d+)\b")
# Number words -> digits, so "Pushpa Two" matches "Pushpa 2".
_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}


def normalize(text):
    """Lowercase ``text``, strip junk/punctuation and drop noise words.

    >>> normalize('Pushpa 2_The Rule (2024) 1080p WEB-DL x264.mkv')
    'pushpa 2 the rule 2024'
    """
    if not text:
        return ""
    text = str(text).lower()
    text = _EXT_RE.sub(" ", text)
    text = _SEASON_EP_RE.sub(lambda m: f"s{m.group(1)} e{m.group(2)}", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = []
    for w in text.split():
        if not w or w in NOISE_WORDS:
            continue
        w = _NUMBER_WORDS.get(w, w)
        w = _SEASON_RE.sub(lambda m: "s%02d" % int(m.group(1)), w)
        w = _EPISODE_RE.sub(lambda m: "e%02d" % int(m.group(1)), w)
        words.append(w)
    return " ".join(words)


def _tokens(text):
    return [w for w in normalize(text).split() if w]


def match(file_name, query, threshold=0.82):
    """Return True when ``file_name`` looks like an upload of ``query``.

    Three tiers, cheapest first:

    1. every word of the query occurs in the file name (year may be missing);
    2. the whole query occurs as a substring of the file name;
    3. fuzzy fallback - a close overall string ratio, so a slightly different
       wording ("Pushpa 2 The Rule" vs "Pushpa Two The Rule") still matches.
    """
    q_norm = normalize(query)
    f_norm = normalize(file_name)
    if not q_norm or not f_norm:
        return False
    if q_norm == f_norm or q_norm in f_norm:
        return True

    q_tokens, f_tokens = q_norm.split(), set(f_norm.split())
    year_re = r"(19|20)\d{2}"
    q_years = [t for t in q_tokens if re.fullmatch(year_re, t)]
    f_years = [t for t in f_tokens if re.fullmatch(year_re, t)]
    # Both sides state a year and they disagree -> different release, never a match.
    if q_years and f_years and not set(q_years) & set(f_years):
        return False
    if q_tokens and all(t in f_tokens for t in q_tokens):
        return True
    # Same, but ignore a year the searcher added and the file name does not carry.
    q_no_year = [t for t in q_tokens if t not in q_years]
    if q_no_year and q_years and not f_years and all(t in f_tokens for t in q_no_year):
        return True

    # Fuzzy: only worth it for titles long enough to be meaningful.
    if len(q_norm) < 6 or len(f_norm) < 6:
        return False
    if not q_tokens:
        return False
    overlap = len([t for t in q_tokens if t in f_tokens]) / len(q_tokens)
    if overlap < 0.6:
        return False
    window = f_norm[: max(len(q_norm) + 6, len(q_norm))]
    ratio = _ratio(q_norm, window)
    best = max(ratio, _ratio(q_norm, f_norm))
    return best >= threshold


def _ratio(a, b):
    """Similarity ratio (0.0 - 1.0) between two normalized strings."""
    return SequenceMatcher(None, a, b).ratio()


def notify_rows(search_key, user_id, search_text=""):
    """Rows of the no-result keyboard *without* the closing row.

    Green -> Notify me when uploaded (the new feature)
    Blue  -> Request movie to owner / Google the spelling

    Use this when the caller already has its own Close button (the spelling
    suggestion list in plugins/pmfilter.py).
    """
    google = f"https://www.google.com/search?q={quote_plus(search_text or '')}"
    return [
        [
            green("🔔 ɴᴏᴛɪꜰʏ ᴍᴇ ᴡʜᴇɴ ᴜᴘʟᴏᴀᴅᴇᴅ",
                  callback_data=f"notify#{search_key}#{user_id}"),
        ],
        [
            blue("📮 ʀᴇǫᴜᴇꜱᴛ ᴍᴏᴠɪᴇ ᴛᴏ ᴏᴡɴᴇʀ", url=OWNER_LNK),
            blue("🔍 ɢᴏᴏɢʟᴇ ꜱᴘᴇʟʟɪɴɢ", url=google),
        ],
    ]


def notify_keyboard(search_key, user_id, search_text=""):
    """The full coloured no-result keyboard (green / blue / blue / red Close)."""
    return InlineKeyboardMarkup(
        notify_rows(search_key, user_id, search_text)
        + [[red("🚫 ᴄʟᴏꜱᴇ", callback_data="close_data")]]
    )


def remember_search(search_key, search_text):
    """Keep ``search_text`` under ``search_key`` so the callback can find it."""
    PENDING_TEXT[search_key] = search_text
    return search_text


async def register_notify_request(user_id, search_text, chat_id=0, message_id=0, name=""):
    """Store a notify-me request. Returns (saved, reason)."""
    from database.users_chats_db import db

    search_text = (search_text or "").strip()
    if not search_text:
        return False, "empty"
    if not TITLE_NOTIFY:
        return False, "disabled"
    try:
        if await db.has_notify_request(user_id, search_text):
            return False, "duplicate"
        await db.add_notify_request(
            user_id=user_id,
            query=search_text,
            chat_id=chat_id,
            message_id=message_id,
            name=name,
        )
        return True, "saved"
    except Exception as e:  # never break the callback over a DB hiccup
        logger.error("register_notify_request failed: %s", e)
        return False, "error"


async def check_new_file(bot, file_name):
    """PM everyone who asked for ``file_name``. Called after a file is indexed.

    Returns the number of users notified.
    """
    if not TITLE_NOTIFY or not file_name:
        return 0
    from database.users_chats_db import db

    notified = 0
    served_ids = []
    try:
        cutoff = datetime.utcnow() - timedelta(days=TITLE_NOTIFY_TTL_DAYS)
        requests = await db.get_notify_requests(cutoff=cutoff)
    except Exception as e:
        logger.error("check_new_file: could not read notify requests: %s", e)
        return 0

    username = getattr(getattr(bot, "me", None), "username", None)
    for req in requests:
        query = req.get("query", "")
        if not match(file_name, query):
            continue
        user_id = req.get("user_id")
        text = (
            "🔔 <b>ᴜᴘʟᴏᴀᴅ ᴀʟᴇʀᴛ</b>\n\n"
            f"ʜᴇʏ, <b>{req.get('name') or 'ᴛʜᴇʀᴇ'}</b> — ᴛʜɪꜱ ɪꜱ ɴᴏᴡ ᴀᴠᴀɪʟᴀʙʟᴇ:\n\n"
            f"🎬 <code>{file_name}</code>\n\n"
            "🔎 ꜱᴇᴀʀᴄʜ ɪᴛ ᴀɢᴀɪɴ ɪɴ ᴛʜᴇ ɢʀᴏᴜᴘ ᴛᴏ ɢᴇᴛ ᴛʜᴇ ᴅᴏᴡɴʟᴏᴀᴅ ʙᴜᴛᴛᴏɴꜱ."
        )
        buttons = None
        if username:
            buttons = InlineKeyboardMarkup([[blue("🔎 ᴏᴘᴇɴ ʙᴏᴛ", url=f"https://t.me/{username}")]])
        try:
            await bot.send_message(chat_id=user_id, text=text, reply_markup=buttons)
            notified += 1
            served_ids.append(req["_id"])
        except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid):
            # The user can never be reached again - drop the request.
            served_ids.append(req["_id"])
        except FloodWait as e:
            logger.warning("check_new_file: flood wait %ss, stopping", e.value)
            break
        except Exception as e:
            logger.warning("check_new_file: could not PM %s: %s", user_id, e)

    if served_ids:
        try:
            await db.delete_notify_requests(served_ids)
        except Exception as e:
            logger.error("check_new_file: could not clear served requests: %s", e)
    return notified
