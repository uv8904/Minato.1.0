"""Regression tests for the OTT storefront's real store getters.

The bug: ``ott_catalog._upcoming_store()`` imported a non-existent
``upcoming_movies`` singleton from ``database.upcoming_db`` (only the
``UpcomingMoviesStore`` class lives there).  The import blew up on the live
site, so ``/api/ott/home``, ``/api/ott/search``, ``/api/ott/genres`` and
``/api/ott/suggest`` all answered ``{"ok": false, "error": "unavailable"}``
and ``/home`` showed "Your storefront is warming up".

The suite missed it because the tests and ``tools/preview_section.py`` always
inject fake stores through ``override_stores()``, which short-circuits the
real getters.  These tests deliberately run against the *real* getters.

    pytest tests/test_ott_store_getters.py -q
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from dreamxbotz.util import ott_catalog  # noqa: E402


@pytest.fixture(autouse=True)
def _real_stores():
    """Drop any store/trending overrides before and after every test."""
    ott_catalog.override_stores(reset=True)
    ott_catalog.override_trending(reset=True)
    yield
    ott_catalog.override_stores(reset=True)
    ott_catalog.override_trending(reset=True)


# --------------------------------------------------------------------------- #
# (a) The real getters must resolve with no override_stores() in play
# --------------------------------------------------------------------------- #
def test_real_store_getters_resolve_without_overrides():
    """``_upcoming_store()`` must return a memoised ``UpcomingMoviesStore``.

    Before the fix this raised ``ImportError``: ``database.upcoming_db`` has
    no ``upcoming_movies`` singleton – and that single bad import took down
    every OTT endpoint.
    """
    from database.ott_meta_db import OttMetaStore
    from database.recent_movies_db import RecentMoviesStore
    from database.upcoming_db import UpcomingMoviesStore

    recent = ott_catalog._recent_store()
    meta = ott_catalog._meta_store()
    upcoming = ott_catalog._upcoming_store()

    assert isinstance(recent, RecentMoviesStore)
    assert isinstance(meta, OttMetaStore)
    assert isinstance(upcoming, UpcomingMoviesStore)
    # Memoised per process, exactly like movie_api._upcoming_store().
    assert ott_catalog._upcoming_store() is upcoming

    # A configured override still wins over the built-in store.
    sentinel = object()
    ott_catalog.override_stores(upcoming=sentinel)
    assert ott_catalog._upcoming_store() is sentinel
    ott_catalog.override_stores(reset=True)
    assert isinstance(ott_catalog._upcoming_store(), UpcomingMoviesStore)


def test_store_loaders_work_end_to_end_without_overrides(monkeypatch):
    """``load_upcoming``/``build_home_catalog`` run on the real getters.

    Only the Mongo query layer is stubbed (class methods), never the getters
    – pre-fix, ``load_upcoming`` crashed on the ``upcoming_movies`` import
    before it could reach ``list_upcoming`` at all.
    """
    from database.ott_meta_db import OttMetaStore
    from database.recent_movies_db import RecentMoviesStore
    from database.upcoming_db import UpcomingMoviesStore

    rows = [{"_id": "kgf-chapter-3-2026", "title": "KGF Chapter 3", "year": 2026}]

    async def fake_list_upcoming(self, limit=20, **kwargs):
        return rows[:limit]

    async def fake_list_recent(self, limit=20, **kwargs):
        return [{"_id": "jawan-2023", "title": "Jawan", "year": 2023, "qualities": ["1080p"]}]

    async def fake_get_many(self, movie_ids):
        return {}

    monkeypatch.setattr(UpcomingMoviesStore, "list_upcoming", fake_list_upcoming)
    monkeypatch.setattr(RecentMoviesStore, "list_recent", fake_list_recent)
    monkeypatch.setattr(OttMetaStore, "get_many", fake_get_many)

    upcoming = asyncio.run(ott_catalog.load_upcoming(5))
    assert [row["id"] for row in upcoming] == ["kgf-chapter-3-2026"]
    assert upcoming[0]["upcoming"] is True

    catalog, upcoming_cards = asyncio.run(ott_catalog.build_home_catalog())
    assert len(catalog) == 1
    assert [row["id"] for row in upcoming_cards] == ["kgf-chapter-3-2026"]


# --------------------------------------------------------------------------- #
# (b) A raising store getter must never break the payload
# --------------------------------------------------------------------------- #
def test_home_payload_is_ok_even_when_store_getters_raise(monkeypatch):
    """Store that cannot even be built ⇒ empty storefront, not "unavailable"."""

    def boom():
        raise RuntimeError("store construction failed")

    monkeypatch.setattr(ott_catalog, "_recent_store", boom)
    monkeypatch.setattr(ott_catalog, "_meta_store", boom)
    monkeypatch.setattr(ott_catalog, "_upcoming_store", boom)
    ott_catalog.override_trending([])

    payload = asyncio.run(ott_catalog.home_payload())

    assert payload["ok"] is True
    assert payload["hero"] == []
    assert payload["rails"] == []
    assert payload["counts"]["movies"] == 0

    # The other guarded entry points stay green as well.
    assert asyncio.run(ott_catalog.load_movies()) == []
    assert asyncio.run(ott_catalog.load_metas(["jawan-2023"])) == {}
    assert asyncio.run(ott_catalog.load_upcoming(5)) == []
    assert asyncio.run(ott_catalog.enrich_movie({"_id": "jawan-2023", "title": "Jawan"})) is False
    assert asyncio.run(ott_catalog.enrich_pending()) == 0
    stats = asyncio.run(ott_catalog.diagnostics())
    assert isinstance(stats, dict) and "enabled" in stats
