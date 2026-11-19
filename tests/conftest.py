"""Shared pytest setup.

Importing ``plugins.*`` builds a pyrogram/electrogram ``Client``, whose
``Dispatcher`` calls ``asyncio.get_event_loop()``. Under Python 3.11+ that
raises once a previous ``asyncio.run()`` has closed the default loop, so make
sure a loop exists before every test.
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest


# Offline-safe stand-ins for credentials that are now *required* in production.
# conftest is imported before any test module (and therefore before info.py),
# so imports/repo construction stay network-free without reintroducing shipped
# secrets. Real deployments must set these through their environment.
for _key, _value in {
    "BOT_TOKEN": "123456:OFFLINE_TEST_TOKEN",
    "API_ID": "12345",
    "API_HASH": "0" * 32,
    "DATABASE_URI": "mongodb://localhost:27017",
    "DATABASE_NAME": "minato_tests",
    "ADMINS": "123456789",
    "LOG_CHANNEL": "-1001234567890",
    "SHORTENER_API": "offline-shortener-key",
    "SHORTENER_WEBSITE": "arolinks.com",
    "SHORTENER_API2": "offline-shortener-key",
    "SHORTENER_WEBSITE2": "arolinks.com",
    "SHORTENER_API3": "offline-shortener-key",
    "SHORTENER_WEBSITE3": "arolinks.com",
    "CLONE_ENABLED": "False",
}.items():
    os.environ.setdefault(_key, _value)


# Pytest can put only ``tests/`` on sys.path when a single file is selected.
# Keep imports deterministic instead of relying on another test module to add
# the repository root as an order-dependent side effect.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield
