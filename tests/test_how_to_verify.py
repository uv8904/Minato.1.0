"""\"How to verify me\" button + 12-hour verification gap.

The verification screen only ever showed a tutorial *link*; users who got
stuck had no way to ask the bot for the steps.  This adds a
``📖 ʜᴏᴡ ᴛᴏ ᴠᴇʀɪꜰʏ ᴍᴇ ?`` button (next to the tutorial button) that pops up
the steps and says how long the verification stays valid — i.e. when the user
has to verify again.  The default 2nd/3rd verify gaps are now 12 hours
(``TWO_VERIFY_GAP`` / ``THREE_VERIFY_GAP`` = 43200 s).

Covers:

* ``info``                    – 12-hour gap defaults
* ``Script``                  – the alert copy fits Telegram's 200-char alert
* ``plugins/commands.py``     – the button is on the verify screen, the
  ``^howtoverify$`` callback handler is registered (and before pmfilter's
  catch-all handler, so it actually fires) and answers with the steps + gap
* ``plugins/PremiumContent.py`` – same button on the premium verify screen

Run with the project dependencies installed::

    pytest tests/test_how_to_verify.py -q
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("pyrogram")

import info  # noqa: E402
from Script import script  # noqa: E402


TWELVE_HOURS = 12 * 60 * 60


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ #
# 12-hour verification gap
# ------------------------------------------------------------------ #
def test_default_verify_gaps_are_twelve_hours():
    assert info.TWO_VERIFY_GAP == TWELVE_HOURS
    assert info.THREE_VERIFY_GAP == TWELVE_HOURS


def test_readable_time_of_twelve_hours():
    from utils import get_readable_time

    assert get_readable_time(TWELVE_HOURS) == "12h"


# ------------------------------------------------------------------ #
# Button wiring
# ------------------------------------------------------------------ #
def test_verify_screen_has_how_to_verify_button():
    src = (ROOT / "plugins" / "commands.py").read_text(encoding="utf-8")
    assert 'callback_data="howtoverify"' in src
    # It must sit next to the existing tutorial button on the verify screen.
    assert 'blue("⁉️ ʜᴏᴡ ᴛᴏ ᴠᴇʀɪꜰʏ ⁉️", url=howtodownload)' in src
    assert 'blue("📖 ʜᴏᴡ ᴛᴏ ᴠᴇʀɪꜰʏ ᴍᴇ ?", callback_data="howtoverify")' in src


def test_premium_verify_screen_has_how_to_verify_button():
    src = (ROOT / "plugins" / "PremiumContent.py").read_text(encoding="utf-8")
    assert 'callback_data="howtoverify"' in src


def test_alert_text_fits_telegram_alert_limit():
    # Telegram caps show_alert text at 200 characters.
    text = script.HOW_TO_VERIFY_ALERT.format("12h")
    assert len(text) <= 200
    assert "12h" in text
    # Steps are there, in order.
    assert "1." in text and "2." in text and "3." in text


# ------------------------------------------------------------------ #
# Callback handler behaviour
# ------------------------------------------------------------------ #
class _FakeCallbackQuery:
    def __init__(self, chat_id=-1001234567890):
        self.from_user = SimpleNamespace(id=42, mention="@tester")
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id))
        self.answers = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append((text, show_alert))


def _run_handler(monkeypatch, verify_time, chat_id=-1001234567890):
    import plugins.commands as commands

    async def fake_get_settings(group_id):
        return {"verify_time": verify_time}

    monkeypatch.setattr(commands, "get_settings", fake_get_settings)

    query = _FakeCallbackQuery(chat_id)
    run(commands.how_to_verify_callback(object(), query))
    return query


def test_how_to_verify_shows_twelve_hour_validity(monkeypatch):
    query = _run_handler(monkeypatch, TWELVE_HOURS)
    assert query.answers, "callback was not answered"
    text, show_alert = query.answers[-1]
    assert show_alert is True
    assert "12h" in text


def test_how_to_verify_honours_group_override(monkeypatch):
    query = _run_handler(monkeypatch, 3600)
    text, _ = query.answers[-1]
    assert "1h" in text
    assert "12h" not in text


def test_how_to_verify_falls_back_when_gap_is_broken(monkeypatch):
    query = _run_handler(monkeypatch, "not-a-number")
    text, _ = query.answers[-1]
    assert "12h" in text  # TWO_VERIFY_GAP default


def _registered_handler_order():
    """Replicate pyrogram's ``Client.load_plugins()`` scan (used by
    ``Client.start()``): modules in sorted path order, each module's decorated
    handlers registered in that order — same order the live bot sees."""
    from pyrogram.handlers.handler import Handler

    order = []
    for path in sorted((ROOT / "plugins").rglob("*.py")):
        if path.name == "__init__.py":
            continue
        rel = path.relative_to(ROOT)          # plugins/commands.py
        module_path = ".".join((*rel.parent.parts, rel.stem))
        module = sys.modules.get(module_path)
        if module is None:
            continue
        for name in vars(module):
            try:
                for handler, group in getattr(module, name).handlers:
                    if isinstance(handler, Handler) and isinstance(group, int):
                        order.append((module_path, name, handler))
            except Exception:
                pass
    return order


def test_how_to_verify_handler_is_registered_first():
    # pmfilter registers a catch-all @Client.on_callback_query(); only one
    # handler per group fires per update, so ours must be registered before it
    # or the button would do nothing.
    from pyrogram.handlers import CallbackQueryHandler

    import plugins.commands  # noqa: F401  (ensures the module is imported)
    import plugins.pmfilter  # noqa: F401

    order = _registered_handler_order()
    entries = [
        (name, i, handler)
        for i, (module, name, handler) in enumerate(order)
        if name in ("how_to_verify_callback", "cb_handler")
    ]
    by_name = {name: (i, h) for name, i, h in entries}
    assert "how_to_verify_callback" in by_name, "handler not registered"
    assert "cb_handler" in by_name, "pmfilter catch-all not registered"
    mine_i, mine = by_name["how_to_verify_callback"]
    theirs_i, _ = by_name["cb_handler"]
    assert mine_i < theirs_i
    assert isinstance(mine, CallbackQueryHandler)
    assert mine.filters.p.pattern == r"^howtoverify$"
