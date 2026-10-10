"""
Coloured inline-button helpers.

Telegram (Bot API 9.4 / MTProto layer 227+) lets a bot colour its inline
buttons: ``primary`` (blue), ``success`` (green) and ``danger`` (red).
Electrogram exposes this through ``InlineKeyboardButton(style=...)``.

Use the helpers below instead of hard-coding ``style=`` everywhere so the
colour scheme stays consistent and can be switched off with one setting
(``COLOR_BUTTONS`` in ``info.py``).

Clients older than Feb 2026 simply ignore the style and show a normal button,
so it is always safe to send.
"""
import logging

from pyrogram.types import InlineKeyboardButton

from info import COLOR_BUTTONS, BUTTON_PREMIUM_EMOJI

logger = logging.getLogger(__name__)

try:
    from pyrogram.enums import ButtonStyle as _ButtonStyle

    PRIMARY = _ButtonStyle.PRIMARY    # blue   -> navigation / info buttons
    SUCCESS = _ButtonStyle.SUCCESS    # green  -> main / positive actions
    DANGER = _ButtonStyle.DANGER      # red    -> close / cancel / warnings
    _SUPPORTED = True
except ImportError:  # very old pyrogram fork without button styles
    PRIMARY = SUCCESS = DANGER = None
    _SUPPORTED = False
    logger.warning("Installed pyrogram fork has no button styles; buttons will be plain.")


# ---------------------------------------------------------------------------
# Premium animated emoji on buttons
# ---------------------------------------------------------------------------
# Telegram Premium custom emoji can be shown as the icon of an inline button.
# A label such as ``"🎬 ᴇxᴛᴇɴᴅ ᴘʟᴀɴ"`` is rendered as the animated emoji with
# the same document id, and the plain emoji is removed from the text so it is
# not shown twice.  Mapping = { "🎬": <custom emoji document id>, ... }.
#
# Source of truth: ``BUTTON_PREMIUM_EMOJI`` in info.py / env, e.g.
#   BUTTON_PREMIUM_EMOJI=🎬=5368324170671202286;🔍=5368324170671202287
# ``set_premium_emoji(...)`` changes it at runtime (used by /btnemoji).
_PREMIUM_EMOJI: dict[str, int] = {}


def parse_premium_emoji(raw) -> dict:
    """Parse ``"🎬=123;🔍=456"`` (or a dict) into ``{emoji: int_id}``.

    Invalid entries are skipped instead of breaking the bot at import time.
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        pairs = raw.items()
    else:
        pairs = []
        for item in str(raw).replace("\n", ";").split(";"):
            if "=" in item:
                key, _, value = item.rpartition("=")
                pairs.append((key.strip(), value.strip()))
    result = {}
    for key, value in pairs:
        try:
            result[str(key).strip()] = int(value)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid BUTTON_PREMIUM_EMOJI entry: %r", key)
    return result


def set_premium_emoji(mapping) -> None:
    """Replace the active emoji -> premium-id mapping."""
    _PREMIUM_EMOJI.clear()
    _PREMIUM_EMOJI.update(parse_premium_emoji(mapping))


def get_premium_emoji() -> dict:
    return dict(_PREMIUM_EMOJI)


set_premium_emoji(BUTTON_PREMIUM_EMOJI)


def _split_premium_icon(text):
    """Return ``(clean_text, icon_id)`` if ``text`` starts with a mapped emoji."""
    if not _PREMIUM_EMOJI or not isinstance(text, str):
        return text, None
    # Longest key first so "⚡️" wins over a shorter prefix.
    for emoji in sorted(_PREMIUM_EMOJI, key=len, reverse=True):
        if text.startswith(emoji):
            rest = text[len(emoji):]
            return rest.lstrip(" ") or text, _PREMIUM_EMOJI[emoji]
    return text, None


def btn(text, style=None, **kwargs):
    """Build an ``InlineKeyboardButton`` with an optional colour style.

    ``kwargs`` are the usual button kwargs (``callback_data=``, ``url=`` ...).
    When colours are disabled/unsupported the style is silently dropped.
    A leading emoji that has a premium mapping becomes an animated icon.
    """
    icon = None
    if _SUPPORTED:
        text, icon = _split_premium_icon(text)
    if style is not None and COLOR_BUTTONS and _SUPPORTED:
        kwargs["style"] = style
    button = InlineKeyboardButton(text, **kwargs)
    if icon is not None:
        # The electrogram fork stores the custom emoji id on the button style.
        from pyrogram.types import KeyboardButtonStyle
        if button.style is None:
            button.style = KeyboardButtonStyle(icon=icon)
        else:
            button.style.icon = icon
    return button


def blue(text, **kwargs):
    """Blue (primary) button - use for navigation / informational actions."""
    return btn(text, PRIMARY, **kwargs)


def green(text, **kwargs):
    """Green (success) button - use for the main / positive action."""
    return btn(text, SUCCESS, **kwargs)


def red(text, **kwargs):
    """Red (danger) button - use for close / cancel / destructive actions."""
    return btn(text, DANGER, **kwargs)
