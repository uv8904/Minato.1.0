#Thanks @dreamxbotz for helping in this journey 

import jinja2
from info import *
from dreamxbotz.Bot import dreamxbotz
from dreamxbotz.util.human_readable import humanbytes
from dreamxbotz.util.file_properties import get_file_ids
from dreamxbotz.server.exceptions import InvalidHash
import urllib.parse
import logging
import aiohttp


# --------------------------------------------------------------------------- #
# Stream Mode · "Newly Uploaded Movies" section
# --------------------------------------------------------------------------- #
# The guarded imports keep this renderer usable even when `info` is replaced by
# a minimal stub (tests) – the section then falls back to sane defaults.
# Docs: docs/NEWLY_UPLOADED_MOVIES.md
try:
    from info import NEW_UPLOADED_MOVIES as _NU_ENABLED
except Exception:  # pragma: no cover - defensive
    _NU_ENABLED = True
try:
    from info import NEW_UPLOADED_LIMIT as _NU_LIMIT
except Exception:  # pragma: no cover - defensive
    _NU_LIMIT = 20
try:
    from info import NEW_UPLOADED_POLL as _NU_POLL
except Exception:  # pragma: no cover - defensive
    _NU_POLL = 60
try:
    from info import API_URL as _NU_API_URL
except Exception:  # pragma: no cover - defensive
    _NU_API_URL = ""
try:
    from info import NEW_UPLOADED_API_PATH as _NU_API_PATH
except Exception:  # pragma: no cover - defensive
    _NU_API_PATH = "/api/movies/new"
try:
    from dreamxbotz.zzint import __version__ as _MV_VERSION
except Exception:  # pragma: no cover - defensive
    _MV_VERSION = "1.1"
try:
    from dreamxbotz.server.static_assets import version_token as _asset_token

    ASSET_VERSION = f"{_MV_VERSION}-{_asset_token()}"
except Exception:  # pragma: no cover - defensive
    ASSET_VERSION = str(_MV_VERSION)
try:
    from dreamxbotz.util.watch_hero import build_context as _build_watch_hero
    from dreamxbotz.util.watch_hero import hero_enabled as _hero_enabled
except Exception:  # pragma: no cover - defensive
    _build_watch_hero = None

    def _hero_enabled() -> bool:  # pragma: no cover - defensive
        return False


def newly_uploaded_api_url() -> str:
    """Endpoint the web pages call for the "Newly Uploaded Movies" section.

    Same-origin by default (``/api/movies/new``); when the website is hosted
    elsewhere, set ``API_URL`` in ``info.py`` / the environment.
    """
    base = str(_NU_API_URL or "").strip().rstrip("/")
    path = str(_NU_API_PATH or "/api/movies/new")
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}" if base else path


async def render_page(id, secure_hash, src=None):
    file = await dreamxbotz.get_messages(int(BIN_CHANNEL), int(id))
    file_data = await get_file_ids(dreamxbotz, int(BIN_CHANNEL), int(id))
    if file_data.unique_id[:6] != secure_hash:
        logging.debug(f"link hash: {secure_hash} - {file_data.unique_id[:6]}")
        logging.debug(f"Invalid hash for message with - ID {id}")
        raise InvalidHash

    src = urllib.parse.urljoin(
        URL,
        f"{id}/{urllib.parse.quote_plus(file_data.file_name)}?hash={secure_hash}",
    )

    tag = file_data.mime_type.split("/")[0].strip()
    file_size = humanbytes(file_data.file_size)
    if tag in ["video", "audio"]:
        template_file = "dreamxbotz/template/req.html"
    else:
        template_file = "dreamxbotz/template/dl.html"
        async with aiohttp.ClientSession() as s:
            async with s.get(src) as u:
                file_size = humanbytes(int(u.headers.get("Content-Length")))

    with open(template_file) as f:
        template = jinja2.Template(f.read())

    file_name = file_data.file_name.replace("_", " ")

    bot_username = str(getattr(dreamxbotz, "username", "") or "").lstrip("@")

    # Watch-page movie hero (req.html): title/year/quality chips, the upload
    # date of the streamed file and its Telegram deep link, rendered server
    # side; only the artwork is fetched by watch_hero.js afterwards.
    # Docs: docs/NEWLY_UPLOADED_MOVIES.md
    hero_context: dict = {}
    if _build_watch_hero is not None:
        try:
            hero_context = _build_watch_hero(
                file_name,
                bot_username,
                getattr(file, "date", None),
                api_base=_NU_API_URL,
                # Only video gets the movie hero: req.html also renders audio
                # files (songs), where a poster strip would be misleading.
                enabled=_hero_enabled() and tag != "audio",
            )
        except Exception as exc:  # never break a page view because of the hero
            logging.debug("Watch hero context unavailable: %s", exc)

    return template.render(
        **hero_context,
        file_name=file_name,
        file_url=src,
        file_size=file_size,
        file_unique_id=file_data.unique_id,
        update_channel_url=UPDATE_CHNL_LNK,
        bot_username=bot_username,
        # "Newly Uploaded Movies" section (see docs/NEWLY_UPLOADED_MOVIES.md).
        # `bot_username` is the public @username – the TELEGRAM_BOT_TOKEN never
        # reaches the browser.
        newly_uploaded_enabled=_NU_ENABLED,
        newly_uploaded_api=newly_uploaded_api_url(),
        newly_uploaded_limit=_NU_LIMIT,
        # Live refresh interval (seconds, 0 = off): a movie uploaded to the bot
        # shows up in the spotlight + rail without the visitor reloading.
        newly_uploaded_poll=_NU_POLL,
        asset_version=ASSET_VERSION,
    )
