"""Shared pytest setup.

Importing ``plugins.*`` builds a pyrogram/electrogram ``Client``, whose
``Dispatcher`` calls ``asyncio.get_event_loop()``. Under Python 3.11+ that
raises once a previous ``asyncio.run()`` has closed the default loop, so make
sure a loop exists before every test.
"""
import asyncio
import sys
from pathlib import Path

import pytest


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
