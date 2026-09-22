import re
import os
from os import environ, getenv
from Script import script

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Utility functions
id_pattern = re.compile(r'^.\d+$')

def is_enabled(value, default):
    """Parse an env value as a boolean. Accepts true/false/yes/no/1/0/on/off."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    value = str(value).strip().lower()
    if value in ("true", "yes", "1", "enable", "enabled", "y", "on"):
        return True
    if value in ("false", "no", "0", "disable", "disabled", "n", "off"):
        return False
    return default

def env_int(key, default=0):
    raw = environ.get(key, '')
    if raw is None or str(raw).strip() == '':
        return int(default)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return int(default)

def env_str(*keys, default=''):
    """First non-empty env value. Strips whitespace and surrounding quotes (Koyeb/Heroku paste)."""
    for key in keys:
        raw = environ.get(key)
        if raw is None:
            continue
        value = str(raw).strip().strip('"').strip("'")
        if value:
            return value
    return default

# ============================
# Bot Information Configuration
# ============================
SESSION = environ.get('SESSION', 'dreamxbotz_search')   # Session name for the bot
API_ID = env_int('API_ID', 20803355)  # API ID from my.telegram.org — set via env, do not hardcode
API_HASH = environ.get('API_HASH', 'caa85d91bcde4e8826ad697de02af771')  # API Hash from my.telegram.org — set via env
BOT_TOKEN = environ.get('BOT_TOKEN', '')    # Bot token from @BotFather

# ============================
# Bot Settings Configuration
# ============================
CACHE_TIME = env_int('CACHE_TIME', 300)    # Cache time in seconds (default: 5 minutes)
USE_CAPTION_FILTER = is_enabled(environ.get('USE_CAPTION_FILTER', 'True'), True)  # Use caption filter for search results
INDEX_CAPTION = is_enabled(environ.get('SAVE_CAPTION', 'True'), True) # Save caption in db when indexing; set False if you don't use USE_CAPTION_FILTER
#Making it false will not save caption in db SO you can save some storage space


PICS = (environ.get('PICS', 'https://graph.org/vTelegraphBot-08-29-30')).split()  # Sample pic
NOR_IMG = environ.get("NOR_IMG", "https://graph.org/file/6e86ea65aeb5fc8ab8472-383f063d185e0df342.jpg")
MELCOW_PHOTO = environ.get("MELCOW_PHOTO", "https://graph.org/file/3da6943698d9832d65e1f-64ceff21cf6121c3bb.jpg")
SPELL_IMG = environ.get("SPELL_IMG", "https://graph.org/file/5c07ed7076cce434a6147-8731b76b6031d4512f.jpg")
SUBSCRIPTION = (environ.get('SUBSCRIPTION', 'https://graph.org/file/29f442bf51cc185974822-00901f9d2f2fc1ee3d.jpg'))
FSUB_PICS = (environ.get('FSUB_PICS', 'https://graph.org/file/6d70cacead407c34d0606-1d86dd5024e769358c.jpg')).split()  # Fsub pic

# ============================
# Admin, Channels & Users Configuration
# ============================
ADMINS = [int(admin) if id_pattern.search(admin) else admin for admin in environ.get('ADMINS', '8023726997 6389414945').split()] # Replace with the actual admin ID(s) to add
CHANNELS = [int(ch) if id_pattern.search(ch) else ch for ch in environ.get('CHANNELS', '-1002086319581 -1003276318629 -1003453367461 -1003016600468').split()]  # Channel id for auto indexing (make sure bot is admin)

LOG_CHANNEL = int(environ.get('LOG_CHANNEL', '-1002573823548'))  # Log channel id (make sure bot is admin)
BIN_CHANNEL = int(environ.get('BIN_CHANNEL', '-1002551533001'))  # Bin channel id (make sure bot is admin)
PREMIUM_LOGS = int(environ.get('PREMIUM_LOGS', '-1002648671971'))  # Premium logs channel id
DELETE_CHANNELS = [int(dch) if id_pattern.search(dch) else dch for dch in environ.get('DELETE_CHANNELS', '-1002635477308').split()] #(make sure bot is admin)
support_chat_id = environ.get('SUPPORT_CHAT_ID', '-1003506315650')  # Support group id (make sure bot is admin)
reqst_channel = environ.get('REQST_CHANNEL_ID', '-1003494610843')  # Request channel id (make sure bot is admin)
SUPPORT_CHAT = environ.get('SUPPORT_CHAT', 'https://t.me/premiumottreleases')  # Support group link (make sure bot is admin)

# FORCE_SUB 
auth_req_channels = environ.get("AUTH_REQ_CHANNELS", "-1003565940256")# requst to join Channel for force sub (make sure bot is admin) only for bot ADMINS  
auth_channels     = environ.get("AUTH_CHANNELS", "-1003140286992 -1002536320197 -1003192727274 -1003696183217 -1003435139728 -1003752571755")# Channels for force sub (make sure bot is admin)

# ============================
# Payment Configuration
# ============================
QR_CODE = environ.get('QR_CODE', '')    # QR code image for payments
OWNER_UPI_ID = environ.get('OWNER_UPI_ID', 'ɴᴏ ᴀᴠᴀɪʟᴀʙʟᴇ ʀɪɢʜᴛ ɴᴏᴡ')    # Owner UPI ID for payments

STAR_PREMIUM_PLANS = {
    10: "7day",
    20: "15day",    
    40: "1month", 
    55: "45day",
    75: "60day",
}  # Premium plans with their respective durations in days

# ============================
# MongoDB Configuration
# ============================
DATABASE_URI = environ.get('DATABASE_URI', 'mongodb+srv://uvjangra:uvjangra@cluster0.cmjdvgq.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0')  # MongoDB URI — set via env, never commit credentials
DATABASE_NAME = environ.get('DATABASE_NAME', "Cluster0") # Database name (default: cluster)
COLLECTION_NAME = environ.get('COLLECTION_NAME', 'dreamcinezone_files') # Collection name (default: dreamcinezone_files)

# If MULTIPLE_DB Is True Then Fill DATABASE_URI2 Value Else You Will Get Error.
MULTIPLE_DB = is_enabled(os.environ.get('MULTIPLE_DB', "True"), False) # Type True For Turn On MULTIPLE DB FUNTION 
DATABASE_URI2 = environ.get('DATABASE_URI2', 'mongodb+srv://yuvi123:yuvi123@cluster0.hlyhypg.mongodb.net/?appName=Cluster0')  # Second MongoDB URI (required when MULTIPLE_DB is True)
# ============================
# Movie Notification & Update Settings
# ============================
MOVIE_UPDATE_NOTIFICATION = is_enabled(environ.get('MOVIE_UPDATE_NOTIFICATION', 'True'), True)  # Notification On / Off
MOVIE_UPDATE_CHANNEL = int(environ.get('MOVIE_UPDATE_CHANNEL', '-1003192727274'))  # Notification of sent to your channel
DREAMXBOTZ_IMAGE_FETCH = is_enabled(environ.get('DREAMXBOTZ_IMAGE_FETCH', 'True'), True)  # On / Off
LINK_PREVIEW = is_enabled(environ.get('LINK_PREVIEW', 'True'), True) # Shows link preview in notification msg instead of image
ABOVE_PREVIEW = is_enabled(environ.get('ABOVE_PREVIEW', 'True'), True) # Shows link preview above the text in notification msg if True else below the msg
# Accepted spellings of the TMDB key variable.  ``TMDB_API_KEY`` is the documented
# one; the rest are forgiving fall-backs for owners who typed the name slightly
# differently in their host's config panel ("tmdb api key", "TMDB_KEY", …).
TMDB_KEY_VARIABLES = ('TMDB_API_KEY', 'TMDB_KEY', 'TMDB_API', 'TMDB_TOKEN', 'TMDB_API_TOKEN',
                      'TMDB_READ_ACCESS_TOKEN', 'TMDB_V4_TOKEN', 'TMDB_BEARER_TOKEN')
_KEY_JUNK = ' \t\r\n*`"\'<>«»“”‘’'


def _clean_tmdb_key(raw):
    """Drop quotes/markdown/`TMDB_API_KEY=` that get pasted along with the key."""
    key = str(raw or '').strip().strip(_KEY_JUNK)
    for _ in range(2):
        key = re.sub(r'^(?:tmdb[_ -]?api[_ -]?key|api[_ -]?key|bearer)\s*[:=]?\s*', '', key, flags=re.IGNORECASE).strip(_KEY_JUNK)
    return key


def tmdb_key_from_env(env=None):
    """First non-empty TMDB key in ``env`` – exact names first, then case/space-insensitive ones."""
    env = environ if env is None else env
    for name in TMDB_KEY_VARIABLES:
        key = _clean_tmdb_key(env.get(name))
        if key:
            return key
    wanted = set(TMDB_KEY_VARIABLES)
    for name, value in env.items():  # "tmdb api key", "Tmdb-Api-Key", "tmdb_api_key" …
        normalised = re.sub(r'[^A-Z0-9]+', '_', str(name).upper()).strip('_')
        if normalised in wanted:
            key = _clean_tmdb_key(value)
            if key:
                return key
    return ''


TMDB_API_KEY = tmdb_key_from_env() # prefer to use your own tmdb API Key get it from https://www.themoviedb.org/settings/api
TMDB_POSTER = is_enabled(environ.get('TMDB_POSTER', 'True'), True) # Shows TMDB poster in notification msg
LANDSCAPE_POSTER = is_enabled(environ.get('LANDSCAPE_POSTER', 'True'), True) # Shows landscape poster in notification msg

# ============================
# Verification Settings
# ============================
IS_VERIFY = is_enabled(environ.get('IS_VERIFY', 'True'), True)  # Verification On (True) / Off (False)
LOG_VR_CHANNEL = int(environ.get('LOG_VR_CHANNEL', '-1003536704514')) #Verification Channel Id 
LOG_API_CHANNEL = int(environ.get('LOG_API_CHANNEL', '-1003625579058')) #If Anyone Set Your Bot In Any Group And Set Shortner In That Group Then In This Channel The All Details Come
VERIFY_IMG = environ.get("VERIFY_IMG", "https://telegra.ph/file/9ecc5d6e4df5b83424896.jpg")

TUTORIAL = environ.get("TUTORIAL", "https://t.me/hmmmmw876/785")   # Tutorial link for verification
TUTORIAL_2 = environ.get("TUTORIAL_2", "https://t.me/hmmmmw876/785")   # Second tutorial link for verification
TUTORIAL_3 = environ.get("TUTORIAL_3", "https://t.me/hmmmmw876/785")   # Third tutorial link for verification

# Verification (Must Fill All Variables. Else You Got Error)
SHORTENER_API = environ.get("SHORTENER_API", "ef7e0434f2fc6e97dbf4f981f9bb3ed5aa90bae8") # Shortener API key — set via env
SHORTENER_WEBSITE = environ.get("SHORTENER_WEBSITE", "https://arolinks.com") # Shortener website

SHORTENER_API2 = environ.get("SHORTENER_API2", "ef7e0434f2fc6e97dbf4f981f9bb3ed5aa90bae8")  # Shortener API key for second website
SHORTENER_WEBSITE2 = environ.get("SHORTENER_WEBSITE2", "https://arolinks.com") # Shortener website for second website

SHORTENER_API3 = environ.get("SHORTENER_API3", "ef7e0434f2fc6e97dbf4f981f9bb3ed5aa90bae8")
SHORTENER_WEBSITE3 = environ.get("SHORTENER_WEBSITE3", "https://arolinks.com") # Shortener website for third website

TWO_VERIFY_GAP = int(environ.get('TWO_VERIFY_GAP', "1200")) # Time gap for two-step verification in seconds (default: 20 minutes)
THREE_VERIFY_GAP = int(environ.get('THREE_VERIFY_GAP', "54000"))    

# ============================
# Channel & Group Links Configuration
# ============================
GRP_LNK = environ.get('GRP_LNK', 'https://t.me/movie_requestss_group') # Group link for the bot
OWNER_LNK = environ.get('OWNER_LNK', 'https://t.me/yuviiiii_i') # Owner link for the bot
UPDATE_CHNL_LNK = environ.get('UPDATE_CHNL_LNK', 'https://t.me/wanda_movies_update') # Update channel link for the bot

# ============================
# User Configuration
# ============================
auth_users = [int(user) if id_pattern.search(user) else user for user in environ.get('AUTH_USERS', '').split()]
AUTH_USERS = (auth_users + ADMINS) if auth_users else []
PREMIUM_USER = [int(user) if id_pattern.search(user) else user for user in environ.get('PREMIUM_USER', '').split()]

# ============================
# Miscellaneous Configuration
# ============================
MAX_B_TN = environ.get("MAX_B_TN", "10") # Maximum number of buttons in a row (default: 5)
PORT = environ.get("PORT", "8080")  # Port for the web server (default: 8080)
MSG_ALRT = environ.get('MSG_ALRT', 'Welcome to Hidden Leaf Village 🌿') # Alert message for users
DELETE_TIME = env_int("DELETE_TIME", 300)  # deletion time in seconds (default: 5 minutes). Adjust as per your needs.
CUSTOM_FILE_CAPTION = environ.get("CUSTOM_FILE_CAPTION", f"{script.CAPTION}")   # Custom caption for files
BATCH_FILE_CAPTION = environ.get("BATCH_FILE_CAPTION", CUSTOM_FILE_CAPTION) # Custom caption for batch files
IMDB_TEMPLATE = environ.get("IMDB_TEMPLATE", f"{script.IMDB_TEMPLATE_TXT}")     # Custom IMDB template 
MAX_LIST_ELM = environ.get("MAX_LIST_ELM", None) # Maximum number of elements in a list (default: None, no limit)
INDEX_REQ_CHANNEL = int(environ.get('INDEX_REQ_CHANNEL', LOG_CHANNEL))  # Index Request Channel ID (make sure bot is admin)
NO_RESULTS_MSG = is_enabled(environ.get("NO_RESULTS_MSG", "True"), True)  # True if you want no results messages in Log Channel
MAX_BTN = is_enabled(environ.get('MAX_BTN', "True"), True)    # Max Button On (True) / Off (False)
P_TTI_SHOW_OFF = is_enabled(environ.get('P_TTI_SHOW_OFF', "False"), False)    # P_TTI_SHOW_OFF On (True) / Off (False)
IMDB = is_enabled(environ.get('IMDB', "False"), False)    # IMDB Results On (True) / Off (False)
AUTO_FFILTER = is_enabled(environ.get('AUTO_FFILTER', "True"), True) # Auto Filter On (True) / Off (False)
AUTO_DELETE = is_enabled(environ.get('AUTO_DELETE', "True"), True) # Auto Delete On (True) / Off (False)
LONG_IMDB_DESCRIPTION = is_enabled(environ.get("LONG_IMDB_DESCRIPTION", "False"), False) # Long IMDB Description On (True) / Off (False)
SPELL_CHECK_REPLY = is_enabled(environ.get("SPELL_CHECK_REPLY", "True"), True) # Spell Check Mode On (True) / Off (False)
MELCOW_NEW_USERS = is_enabled(environ.get('MELCOW_NEW_USERS', "False"), False) # Melcow New Users On (True) / Off (False)
PROTECT_CONTENT = is_enabled(environ.get('PROTECT_CONTENT', "False"), False) # Protect Content On (True) / Off (False)
PM_SEARCH = is_enabled(environ.get('PM_SEARCH', "True"), True)  # PM Search On (True) / Off (False)
EMOJI_MODE = is_enabled(environ.get('EMOJI_MODE', "False"), False)  # Emoji status On (True) / Off (False)
BUTTON_MODE = is_enabled(environ.get('BUTTON_MODE', "False"), False) # pm & Group button or link mode (True) / Off (False)
STREAM_MODE = is_enabled(environ.get('STREAM_MODE', "True"), True) # Set Stream mode True or False
PREMIUM_STREAM_MODE = is_enabled(environ.get('PREMIUM_STREAM_MODE', "False"), False) # Stream mode only for premium users
COLOR_BUTTONS = is_enabled(environ.get('COLOR_BUTTONS', "True"), True) # Coloured inline buttons (blue/green/red) On (True) / Off (False)

# ============================
# AI Spell Check (Groq + IMDb fallback)
# ============================
AI_SPELL_CHECK = is_enabled(environ.get('AI_SPELL_CHECK', "True"), True)  # Use Groq/IMDb to fix misspelled titles
# On a search miss, fuzzy-match the typed text against titles that exist in
# the file DB and show coloured "did you mean" buttons (or auto-fix when the
# match is obvious). See dreamxbotz/util/title_suggest.py.
DB_SUGGEST = is_enabled(environ.get('DB_SUGGEST', "True"), True)  # File-DB "did you mean" suggestions On/Off
# Koyeb/Heroku: set GROQ_API_KEY (gsk_... from https://console.groq.com/keys).
# GROK_API_KEY is accepted as a common typo/alias. This is Groq, not xAI Grok.
GROQ_API_KEY = env_str('GROQ_API_KEY', 'GROK_API_KEY', default='regsk_mZGhZ131cAQeY1y4vO0yWGdyb3FYNwcYcq4IWfPsfIgae1M54e2h')
GROQ_MODEL = env_str('GROQ_MODEL', 'GROK_MODEL', default='llama-3.1-8b-instant')

# ============================
# Notify Me When Uploaded
# ============================
# Shown on the "no files found" screen; the user is PM'd once a matching file
# is indexed (see dreamxbotz/util/title_notify.py).
TITLE_NOTIFY = is_enabled(environ.get('TITLE_NOTIFY', "True"), True)  # Notify-me button On (True) / Off (False)
TITLE_NOTIFY_TTL_DAYS = env_int('TITLE_NOTIFY_TTL_DAYS', 30)  # Forget a request after this many days
TITLE_NOTIFY_MAX_PER_USER = env_int('TITLE_NOTIFY_MAX_PER_USER', 5)  # Max pending requests kept per user


# ============================================================
# Stream Mode · "Newly Uploaded Movies" web section
# ============================================================
# Every newly indexed movie is stored in a small `recent_movies` collection and
# rendered by /api/movies/new on the bot's own web server.  Posters are resolved
# in the background from TMDB/IMDb.  See docs/NEWLY_UPLOADED_MOVIES.md.
NEW_UPLOADED_MOVIES = is_enabled(environ.get('NEW_UPLOADED_MOVIES', "True"), True)  # Master switch: tracking + API + section
NEW_UPLOADED_LIMIT = min(max(env_int('NEW_UPLOADED_LIMIT', 20), 1), 20)  # Cards shown (hard cap: 20)
NEW_UPLOADED_ONLY_MOVIES = is_enabled(environ.get('NEW_UPLOADED_ONLY_MOVIES', "True"), True)  # Skip S01E02 style series for this section
NEW_UPLOADED_POSTER_FETCH = is_enabled(environ.get('NEW_UPLOADED_POSTER_FETCH', "True"), True)  # Look posters up automatically (TMDB → IMDb)
NEW_UPLOADED_POSTER_LOOKUPS = min(max(env_int('NEW_UPLOADED_POSTER_LOOKUPS', 60), 1), 200)  # Max posters backfilled per run
NEW_UPLOADED_POSTER_RETRY_HOURS = max(env_int('NEW_UPLOADED_POSTER_RETRY_HOURS', 48), 1)  # Re-try a failed poster after N hours
try:
    NEW_UPLOADED_POSTER_TIMEOUT = float(environ.get('NEW_UPLOADED_POSTER_TIMEOUT') or 25)  # Per-lookup timeout (seconds)
except (TypeError, ValueError):
    NEW_UPLOADED_POSTER_TIMEOUT = 25.0
NEW_UPLOADED_POSTER_HOSTS = environ.get('NEW_UPLOADED_POSTER_HOSTS', '')  # Extra allowed poster hosts (space separated)
NEW_UPLOADED_POSTER_ANY_HOST = is_enabled(environ.get('NEW_UPLOADED_POSTER_ANY_HOST', "False"), False)  # Allow any https poster host (trusted sources only)
NEW_UPLOADED_CACHE_TTL = min(max(env_int('NEW_UPLOADED_CACHE_TTL', 60), 0), 3600)  # Browser cache for /api/movies/new (seconds)
NEW_UPLOADED_POLL = min(max(env_int('NEW_UPLOADED_POLL', 60), 0), 3600)  # Live refresh: page re-checks /api/movies/new every N seconds while visible (0 = off)
NEW_UPLOADED_COLLECTION = environ.get('NEW_UPLOADED_COLLECTION', 'recent_movies')  # Mongo collection name
NEW_UPLOADED_MAX_MOVIES = max(env_int('NEW_UPLOADED_MAX_MOVIES', 500), 20)  # Housekeeping: keep only the newest N entries
NEW_UPLOADED_CORS_ORIGIN = environ.get('NEW_UPLOADED_CORS_ORIGIN', '')  # Only needed when the website is hosted elsewhere
# API_URL — base URL of the Stream Mode movie API used by the web pages.
#   Leave EMPTY (recommended) when the pages are served by this bot's own web
#   server: the section then calls "/api/movies/new" on the same origin.
#   Set it only if the website lives on another domain, e.g.
#       API_URL = https://my-minato-api.onrender.com
#   The endpoint itself is always NEW_UPLOADED_API_PATH (or API_URL + that path).
API_URL = env_str('API_URL', 'NEW_UPLOADED_API_URL', default='')
NEW_UPLOADED_API_PATH = env_str('NEW_UPLOADED_API_PATH', default='/api/movies/new')

# ----- Watch-page movie hero (strip above the video player) ----------------- #
#   Shows the poster of the movie that is streaming plus its Telegram deep link
#   (https://t.me/BOT_USERNAME?start=movie_MOVIE_ID).  The strip itself is
#   rendered server side by render_template.py; only the artwork is fetched by
#   /static/watch_hero.js from WATCH_HERO_API_PATH.
WATCH_HERO = is_enabled(environ.get('WATCH_HERO', "True"), True)  # Master switch for the hero strip
WATCH_HERO_ART_FETCH = is_enabled(environ.get('WATCH_HERO_ART_FETCH', "True"), True)  # Look missing artwork up on demand (TMDB → IMDb)
try:
    WATCH_HERO_ART_TIMEOUT = float(environ.get('WATCH_HERO_ART_TIMEOUT') or 8)  # Per-lookup timeout (seconds) - a page view must stay snappy
except (TypeError, ValueError):
    WATCH_HERO_ART_TIMEOUT = 8.0
WATCH_HERO_ART_RETRY_HOURS = max(env_int('WATCH_HERO_ART_RETRY_HOURS', 24), 1)  # Re-try missing artwork after N hours
WATCH_HERO_API_PATH = env_str('WATCH_HERO_API_PATH', default='/api/movies/art')  # Artwork endpoint (API_URL + this path)
MOVIE_ART_COLLECTION = environ.get('MOVIE_ART_COLLECTION', 'movie_art')  # Mongo collection caching poster/backdrop URLs


# ============================
# Bot Configuration
# ============================

AUTH_REQ_CHANNELS = [int(ch) for ch in auth_req_channels.split() if ch and id_pattern.match(ch)] 
AUTH_CHANNELS = [int(ch) for ch in auth_channels.split() if ch and id_pattern.match(ch)]
REQST_CHANNEL = int(reqst_channel) if reqst_channel and id_pattern.search(reqst_channel) else None
SUPPORT_CHAT_ID = int(support_chat_id) if support_chat_id and id_pattern.search(support_chat_id) else None
LANGUAGES = {"ᴍᴀʟᴀʏᴀʟᴀᴍ":"mal","ᴛᴀᴍɪʟ":"tam","ᴇɴɢʟɪsʜ":"eng","ʜɪɴᴅɪ":"hin","ᴛᴇʟᴜɢᴜ":"tel","ᴋᴀɴɴᴀᴅᴀ":"kan","ɢᴜᴊᴀʀᴀᴛɪ":"guj","ᴍᴀʀᴀᴛʜɪ":"mar","ᴘᴜɴᴊᴀʙɪ":"pun"}
QUALITIES = ["360P", "480P", "720P", "1080P", "1440P", "2160P", "4K"]

SEASON_COUNT = 12
SEASONS = [f"S{str(i).zfill(2)}" for i in range(1, SEASON_COUNT + 1)]

BAD_WORDS = {
    "PrivateMovieZ",
    "toonworld4all",
    "themoviesboss",
    "1tamilmv",
    "tamilblasters",
    "1tamilblasters",
    "skymovieshd",
    "extraflix",
    "hdm2",
    "moviesmod",
    "hdhub4u",
    "mkvcinemas",
    "primefix",
    "join",
    "www",
    "villa",
    "tg",
    "original"
} # Set of bad words to filter out
   

# ============================
# Server & Web Configuration
# ============================

NO_PORT = is_enabled(environ.get('NO_PORT', 'False'), False)
ON_HEROKU = 'DYNO' in environ
APP_NAME = environ.get('APP_NAME') if ON_HEROKU else None
BIND_ADRESS = str(getenv('WEB_SERVER_BIND_ADDRESS', '0.0.0.0'))
if getenv('FQDN'):
    FQDN = str(getenv('FQDN'))
elif ON_HEROKU and APP_NAME:
    FQDN = APP_NAME + '.herokuapp.com'
else:
    FQDN = BIND_ADRESS
HAS_SSL = is_enabled(getenv('HAS_SSL', 'True'), True)
_scheme = 'https' if HAS_SSL else 'http'
ON_KOYEB = 'KOYEB_APP_NAME' in environ or str(FQDN).endswith('.koyeb.app')
if ON_HEROKU or ON_KOYEB or NO_PORT:
    URL = f"{_scheme}://{FQDN}/"
else:
    URL = f"{_scheme}://{FQDN}:{PORT}/"
SLEEP_THRESHOLD = env_int('SLEEP_THRESHOLD', 60)
WORKERS = env_int('WORKERS', 4)
SESSION_NAME = str(environ.get('SESSION_NAME', 'dreamXBotz'))
MULTI_CLIENT = False
name = str(environ.get('name', 'DREAMXBOTZ'))
PING_INTERVAL = env_int("PING_INTERVAL", 1200)  # 20 minutes

# ============================
# Reactions Configuration
# ============================
REACTIONS = ["🤝", "😇", "🤗", "😍", "👍", "🎅", "😐", "🥰", "🤩", "😱", "🤣", "😘", "👏", "😛", "😈", "🎉", "⚡️", "🫡", "🤓", "😎", "🏆", "🔥", "🤭", "🌚", "🆒", "👻", "😁"]

# ============================
# Commands Bot
# ============================
Bot_cmds = {
    "start": "Sᴛᴀʀᴛ Mᴇ Bᴀʙʏ",
    "stats": "Gᴇᴛ Bᴏᴛ Sᴛᴀᴛs",
    "alive": " Cʜᴇᴄᴋ Bᴏᴛ Aʟɪᴠᴇ ᴏʀ Nᴏᴛ ",
    "settings": "ᴄʜᴀɴɢᴇ sᴇᴛᴛɪɴɢs",
    "id": "ɢᴇᴛ ɪᴅ ᴛᴇʟᴇɢʀᴀᴍ ",
    "info": "Gᴇᴛ Usᴇʀ ɪɴғᴏ ",
    "del_msg": "ʀᴇᴍᴏᴠᴇ ғɪʟᴇ ɴᴀᴍᴇ ᴄᴏʟʟᴇᴄᴛɪᴏɴ ɴᴏтɪғɪᴄᴀᴛɪᴏɴ...",
    "movie_update": "ᴏɴ ᴏғғ ᴀᴄᴄᴏʀᴅɪɴɢ ʏᴏᴜʀ ɴᴇᴇᴅᴇᴅ...",
    "posters": "ꜱᴛʀᴇᴀᴍ ᴍᴏᴅᴇ ᴘᴏꜱᴛᴇʀ ꜱᴛᴀᴛᴜꜱ (ᴀᴅᴍɪɴ) · /posters retry",
    "setposter": "ꜱᴇᴛ ᴀ ᴍᴏᴠɪᴇ ᴘᴏꜱᴛᴇʀ ʙʏ ʜᴀɴᴅ (ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴘʜᴏᴛᴏ)",
    "pm_search": "ᴘᴍ sᴇᴀʀᴄʜ ᴏɴ ᴏғғ ᴀᴄᴄᴏʀᴅɪɴɢ ʏᴏᴜʀ ɴᴇᴇᴅᴇᴅ...",
    "trendlist": "Gᴇᴛ Tᴏᴘ Tʀᴀɴᴅɪɴɢ Sᴇᴀʀᴄʜ Lɪsᴛ",
    "broadcast": "ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴀ ᴍᴇꜱꜱᴀɢᴇ ᴛᴏ ᴀʟʟ ᴜꜱᴇʀꜱ.",
    "grp_broadcast": "ʙʀᴏᴀᴅᴄᴀsᴛ ᴀ ᴍᴇssᴀɢᴇ ᴛᴏ ᴀʟʟ ᴄᴏɴɴᴇᴄᴛᴇᴅ ɢʀᴏᴜᴘs",
    "send": "ꜱᴇɴᴅ ᴍᴇꜱꜱᴀɢᴇ ᴛᴏ ᴀ ᴘᴀʀᴛɪᴄᴜʟᴀʀ ᴜꜱᴇʀ.",
    "add_premium": "ᴀᴅᴅ ᴀɴʏ ᴜꜱᴇʀ ᴛᴏ ᴘʀᴇᴍɪᴜᴍ.",
    "remove_premium": "ʀᴇᴍᴏᴠᴇ ᴀɴʏ ᴜꜱᴇʀ ꜰʀᴏᴍ ᴘʀᴇᴍɪᴜᴍ.",
    "premium_users": "ɢᴇᴛ ʟɪꜱᴛ ᴏꜰ ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀꜱ.",
    "restart": "ʀᴇꜱᴛᴀʀᴛ ᴛʜᴇ ʙᴏᴛ.",
    "group_cmd": "ɢʀᴏᴜᴘ ᴄᴏᴍᴍᴀɴᴅ ʟɪsᴛ",
    "admin_cmd": "ᴀᴅᴍɪɴ ᴄᴏᴍᴍᴀɴᴅs ʟɪsᴛ.",
    "reset_group": "Group Setting Default",
    "trial_reset": "User Trial Reset",
    "aispell": "AI movie title spell check"
}


# When a second DB is not configured, reuse the primary URI.
if not MULTIPLE_DB or not DATABASE_URI2:
    DATABASE_URI2 = DATABASE_URI

# ============================
# Logs Configuration
# ============================
LOG_STR = "Current Customized Configurations are:-\n"
LOG_STR += ("IMDB Results are enabled, Bot will be showing imdb details for your queries.\n" if IMDB else "IMDB Results are disabled.\n")
LOG_STR += ("P_TTI_SHOW_OFF found, Users will be redirected to send /start to Bot PM instead of sending file directly.\n" if P_TTI_SHOW_OFF else "P_TTI_SHOW_OFF is disabled, files will be sent in PM instead of starting the bot.\n")
LOG_STR += ("BUTTON_MODE is found, filename and file size will be shown in a single button instead of two separate buttons.\n" if BUTTON_MODE else "BUTTON_MODE is disabled, filename and file size will be shown as different buttons.\n")
LOG_STR += (f"CUSTOM_FILE_CAPTION enabled with value {CUSTOM_FILE_CAPTION}, your files will be sent along with this customized caption.\n" if CUSTOM_FILE_CAPTION else "No CUSTOM_FILE_CAPTION Found, Default captions of file will be used.\n")
LOG_STR += ("Long IMDB storyline enabled." if LONG_IMDB_DESCRIPTION else "LONG_IMDB_DESCRIPTION is disabled, Plot will be shorter.\n")
LOG_STR += ("Spell Check Mode is enabled, bot will be suggesting related movies if movie name is misspelled.\n" if SPELL_CHECK_REPLY else "Spell Check Mode is disabled.\n")

