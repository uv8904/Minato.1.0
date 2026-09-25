"""Deep-link movie search: t.me/<bot>?start=msrch_<b64url title>.

The MinatoVerse web pages send users straight to the bot with a
``msrch_`` start payload; the bot must decode it and run the normal
auto-filter search for that title. This test executes the real
``plugins.commands.start`` handler — only the search engine, the
database and the incoming message are faked.

Run with the project dependencies installed::

    pytest tests/test_deeplink_search.py -q
"""
import asyncio
import base64
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

# The project pins electrogram, which installs itself as the `pyrogram` package.
pytest.importorskip("pyrogram")
pytest.importorskip("imdb")

# dreamxbotz.Bot configures logging from logging.conf at import time.
os.chdir(Path(__file__).resolve().parents[1])

import plugins.commands as cmds  # noqa: E402


def b64url(text: str) -> str:
    """Mirror of the b64url() helper used by the web templates."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode().rstrip("=")


class Boom:
    """Any attribute access fails — the deep-link branch must not touch the DB."""

    def __getattr__(self, name):
        raise RuntimeError("database touched in deep-link branch: " + name)


def make_message(payload):
    async def react(**kwargs):
        return None

    return SimpleNamespace(
        command=["start", payload],
        text="/start " + payload,
        react=react,
        from_user=SimpleNamespace(id=123, first_name="Tester"),
        chat=SimpleNamespace(id=456, type="private", title="pm"),
    )


def run_start(payload, searches):
    async def fake_auto_filter(client, msg, spoll=False):
        searches.append(msg.text)
        return True

    old_filter, old_db, old_mdb = cmds.auto_filter, cmds.db, cmds.mdb
    cmds.auto_filter, cmds.db, cmds.mdb = fake_auto_filter, Boom(), Boom()
    try:
        asyncio.run(cmds.start(object(), make_message(payload)))
    finally:
        cmds.auto_filter, cmds.db, cmds.mdb = old_filter, old_db, old_mdb


def test_msrch_payload_searches_title_directly():
    searches = []
    run_start("msrch_" + b64url("Jawan"), searches)
    assert searches == ["Jawan"]


def test_msrch_payload_with_spaces():
    searches = []
    run_start("msrch_" + b64url("Pushpa 2 The Rule"), searches)
    assert searches == ["Pushpa 2 The Rule"]


def test_unrelated_payload_does_not_search():
    searches = []
    try:
        run_start("unknown", searches)
    except RuntimeError:
        # Normal start flow continues and needs the (faked) database — fine.
        pass
    assert searches == []
