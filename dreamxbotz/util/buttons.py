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

from info import COLOR_BUTTONS

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


def btn(text, style=None, **kwargs):
    """Build an ``InlineKeyboardButton`` with an optional colour style.

    ``kwargs`` are the usual button kwargs (``callback_data=``, ``url=`` ...).
    When colours are disabled/unsupported the style is silently dropped.
    """
    if style is not None and COLOR_BUTTONS and _SUPPORTED:
        kwargs["style"] = style
    return InlineKeyboardButton(text, **kwargs)


def blue(text, **kwargs):
    """Blue (primary) button - use for navigation / informational actions."""
    return btn(text, PRIMARY, **kwargs)


def green(text, **kwargs):
    """Green (success) button - use for the main / positive action."""
    return btn(text, SUCCESS, **kwargs)


def red(text, **kwargs):
    """Red (danger) button - use for close / cancel / destructive actions."""
    return btn(text, DANGER, **kwargs)
