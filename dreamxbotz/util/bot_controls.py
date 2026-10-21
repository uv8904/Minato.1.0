"""Runtime controls an admin changes **from the bot itself**.

Two things used to require editing the repo / environment and redeploying:

* the "Sᴇɴᴅ Aʟʟ" premium gate (premium-only or free for everyone), and
* the force-subscribe channel list (``AUTH_CHANNELS``).

Both are now small runtime settings stored in the ``bot_settings`` Mongo
collection (doc id ``runtime_config``) and edited with ``/sendall`` and
``/fsub`` in the bot's private chat (see ``plugins/bot_controls.py`` and
``docs/BOT_CONTROLS.md``).

Reads are cached for a few seconds so file delivery does not hit Mongo on
every tap; every write updates the cache immediately.  ``bot.py`` warms the
cache at startup and refreshes it periodically so several dynos/processes
converge on the value saved by the admin.

Nothing here is allowed to break a search: when Mongo is unreachable the
historical defaults are used (premium gate ON, force-sub from ``info.py``).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Iterable, Optional, Union

from database.users_chats_db import db
from info import AUTH_CHANNELS

logger = logging.getLogger(__name__)

SEND_ALL_PREMIUM_KEY = "send_all_premium"
FSUB_CHANNELS_KEY = "fsub_channels"

# Historical behaviour: Sᴇɴᴅ Aʟʟ is a premium feature until an admin turns
# the gate off from the bot.
SEND_ALL_PREMIUM_DEFAULT = True

# Short process-local cache.  A small TTL keeps multi-dyno setups in sync
# without querying Mongo for every single file a user opens.
CACHE_TTL = 20.0
_DB_TIMEOUT = 3.0

_MISSING = object()
_cache: dict = {}          # key -> (value, expires_at)

ChannelRef = Union[int, str]


def invalidate_runtime_cache(*keys: str) -> None:
    """Drop cached values (all of them when no key is given)."""
    if not keys:
        _cache.clear()
        return
    for key in keys:
        _cache.pop(key, None)


def _cached(key):
    entry = _cache.get(key)
    if entry is None:
        return _MISSING
    value, expires_at = entry
    if time.monotonic() >= expires_at:
        return _MISSING
    return value


def _store(key, value) -> None:
    _cache[key] = (value, time.monotonic() + CACHE_TTL)


async def _load_setting(key, default):
    try:
        value = await asyncio.wait_for(
            db.get_runtime_setting(key, _MISSING), timeout=_DB_TIMEOUT
        )
    except Exception as exc:
        # Mongo blip -> use the safe default, but cache it briefly so every
        # send-all tap does not wait for the database timeout again.
        logger.warning("Could not read runtime setting %r: %s", key, exc)
        _store(key, default)
        return default
    if value is _MISSING:
        value = default
    _store(key, value)
    return value


async def _setting(key, default):
    value = _cached(key)
    if value is not _MISSING:
        return value
    return await _load_setting(key, default)


async def refresh_runtime_controls() -> None:
    """Load both controls into the cache (startup / periodic refresh)."""
    await asyncio.gather(
        _load_setting(SEND_ALL_PREMIUM_KEY, SEND_ALL_PREMIUM_DEFAULT),
        _load_setting(FSUB_CHANNELS_KEY, None),
        return_exceptions=True,
    )


async def runtime_controls_refresher(interval: float = 60.0) -> None:
    """Keep the cache fresh in the background (best effort, never raises)."""
    while True:
        await asyncio.sleep(interval)
        try:
            await refresh_runtime_controls()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Runtime controls refresh failed: %s", exc)


# --------------------------------------------------------------------------- #
# Sᴇɴᴅ Aʟʟ premium gate
# --------------------------------------------------------------------------- #
async def send_all_requires_premium() -> bool:
    """True while "Sᴇɴᴅ Aʟʟ" may only be used by premium members."""
    value = await _setting(SEND_ALL_PREMIUM_KEY, SEND_ALL_PREMIUM_DEFAULT)
    if value is _MISSING:                          # Mongo down -> safe default
        return SEND_ALL_PREMIUM_DEFAULT
    return bool(value)


async def set_send_all_requires_premium(enabled: bool) -> bool:
    """Persist the gate and update the local cache. Returns the saved value."""
    enabled = bool(enabled)
    await db.set_runtime_setting(SEND_ALL_PREMIUM_KEY, enabled)
    _store(SEND_ALL_PREMIUM_KEY, enabled)
    return enabled


async def send_all_allowed(user_id: int) -> bool:
    """Whether ``user_id`` may run Sᴇɴᴅ Aʟʟ right now.

    Admins always may.  When the gate is off everyone may; otherwise the
    normal premium check decides.
    """
    from info import ADMINS
    from utils import temp

    if int(user_id) in ADMINS or int(user_id) == getattr(temp, "ME", None):
        return True
    if not await send_all_requires_premium():
        return True
    try:
        return bool(await db.has_premium_access(user_id))
    except Exception as exc:
        logger.warning("Premium check failed for %s: %s", user_id, exc)
        return False


# --------------------------------------------------------------------------- #
# Force-subscribe channels
# --------------------------------------------------------------------------- #
def clean_channel_ids(channels: Iterable[ChannelRef]) -> list:
    """Normalise any mix of ids/strings into a unique list of ints."""
    cleaned = []
    for channel in channels or ():
        try:
            channel_id = int(channel)
        except (TypeError, ValueError):
            continue
        if channel_id not in cleaned:
            cleaned.append(channel_id)
    return cleaned


async def get_custom_fsub_channels() -> Optional[list]:
    """Bot-managed list, or ``None`` when the repo/env default is in use.

    An empty list is meaningful: it means the admin explicitly removed every
    channel and force-sub is therefore disabled.
    """
    value = await _setting(FSUB_CHANNELS_KEY, None)
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, (list, tuple, set)):
        logger.warning("Ignoring invalid %r runtime value: %r", FSUB_CHANNELS_KEY, value)
        return None
    return clean_channel_ids(value)


async def save_custom_fsub_channels(channels: Iterable[ChannelRef]) -> list:
    """Persist an explicit bot-managed force-sub list (may be empty)."""
    cleaned = clean_channel_ids(channels)
    await db.set_runtime_setting(FSUB_CHANNELS_KEY, cleaned)
    _store(FSUB_CHANNELS_KEY, cleaned)
    return cleaned


async def reset_custom_fsub_channels() -> None:
    """Forget the bot-managed list so the repo/env default applies again."""
    await db.unset_runtime_setting(FSUB_CHANNELS_KEY)
    _store(FSUB_CHANNELS_KEY, None)


async def effective_fsub_channels(settings=None, chat_id=None) -> list:
    """The channels a user must join right now.

    A bot-managed list replaces everything.  Without one we keep the old
    behaviour: the group's ``fsub`` setting joined with ``AUTH_CHANNELS`` from
    ``info.py``/env.
    """
    custom = await get_custom_fsub_channels()
    if custom is not None:
        return custom

    if settings is None and chat_id is not None:
        try:
            from utils import get_settings

            settings = await get_settings(chat_id)
        except Exception as exc:
            logger.warning("Could not read group settings for %s: %s", chat_id, exc)
            settings = {}

    configured = (settings or {}).get("fsub") or []
    return clean_channel_ids(list(configured) + list(AUTH_CHANNELS))


async def add_custom_fsub_channels(channels: Iterable[ChannelRef], seed_from=None) -> list:
    """Append channels, first copying the current list when it is not managed.

    ``seed_from`` should be the current effective list (usually
    ``effective_fsub_channels(settings={})``) so the first edit starts from
    what users actually see (the env list) instead of an empty list.
    """
    current = await get_custom_fsub_channels()
    if current is None:
        current = clean_channel_ids(seed_from or [])
    return await save_custom_fsub_channels(list(current) + list(channels))


async def remove_custom_fsub_channel(channel_id: int, seed_from=None) -> list:
    """Remove one channel, seeding a bot-managed list from ``seed_from`` first."""
    current = await get_custom_fsub_channels()
    if current is None:
        current = clean_channel_ids(seed_from or [])
    target = int(channel_id)
    return await save_custom_fsub_channels([c for c in current if c != target])


# --------------------------------------------------------------------------- #
# Input parsing (for /fsub add @channel / t.me links)
# --------------------------------------------------------------------------- #
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_TME_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t(?:elegram)?\.me)/([A-Za-z][A-Za-z0-9_]{4,31})/?$",
    re.IGNORECASE,
)
_NUMERIC_RE = re.compile(r"^-?\d+$")


def parse_channel_reference(raw: Optional[str]) -> Optional[ChannelRef]:
    """Turn admin input into an int id or ``@username``.

    Accepts ``-1001234567890``, ``@channel``, ``channel``, ``t.me/channel``
    and ``https://telegram.me/channel``.  Returns ``None`` for anything else
    (including private invite links, which the Bot API cannot resolve).
    """
    if isinstance(raw, int):
        return raw

    text = (raw or "").strip()
    if not text:
        return None

    if _NUMERIC_RE.match(text):
        try:
            return int(text)
        except ValueError:
            return None

    match = _TME_RE.match(text)
    if match:
        return "@" + match.group(1)

    if text.startswith("@"):
        text = text[1:]
    if _USERNAME_RE.match(text):
        return "@" + text
    return None
