"""Shared pytest setup.

Importing ``plugins.*`` builds a pyrogram/electrogram ``Client``, whose
``Dispatcher`` calls ``asyncio.get_event_loop()``. Under Python 3.11+ that
raises once a previous ``asyncio.run()`` has closed the default loop, so make
sure a loop exists before every test.
"""
import asyncio

import pytest


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield
