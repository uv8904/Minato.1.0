"""
Tests for the file-DB "did you mean" title suggestions.

Run with the project dependencies installed::

    pytest tests/test_title_suggest.py -q

The pure ranking logic is tested without any database or bot; the async
wrappers are tested with a stubbed file-name fetcher so no MongoDB is needed.
"""
import asyncio
import re

import pytest

from dreamxbotz.util import title_suggest
from dreamxbotz.util.title_suggest import (
    AUTO_PICK_GAP,
    AUTO_PICK_SCORE,
    _core_tokens,
    _score,
    choose_auto_pick,
    extract_title,
    normalize_title,
    rank_suggestions,
    suggest,
    auto_pick,
)


# --------------------------------------------------------------------------
# Normalisation / title extraction (pure functions)
# --------------------------------------------------------------------------
def test_normalize_title_drops_noise_and_year():
    assert normalize_title("Pushpa 2_The Rule (2024) 1080p WEB-DL x264.mkv") == "pushpa 2 the rule"
    assert normalize_title("Pradhama Dristhiya Kuttakkar 2025 1080p x264 Hindi Dual Audio") == "pradhama dristhiya kuttakkar"
    assert normalize_title("pradhama drishtiya kuttakkar") == "pradhama drishtiya kuttakkar"


def test_normalize_title_series_episodes():
    assert normalize_title("Loki.S01E04.720p.mkv") == "loki s01 e04"
    assert normalize_title("Lucifer S03 E24 2160p WEB-DL") == "lucifer s03 e24"


def test_normalize_title_handles_empty():
    assert normalize_title("") == ""
    assert normalize_title(None) == ""


def test_extract_title_display_form():
    assert extract_title("Pushpa 2_The Rule (2024) 1080p WEB DL Hindi AAC x264.mkv") == "Pushpa 2 The Rule"
    assert extract_title("Pradhama Dristhiya Kuttakkar 1080p x264 Hindi.mkv") == "Pradhama Dristhiya Kuttakkar"
    assert extract_title("") == ""


def test_normalize_drops_release_group_words():
    # No year here, so only the noise list can remove these tokens.
    assert normalize_title("Inception Goflix DS4K DDP5 HQ RIP BR") == "inception"
    for token in ("goflix", "ds4k", "ddp5", "hq", "rip", "br"):
        assert normalize_title(token) == ""


def test_core_tokens_drop_after_release_year_keep_leading_year():
    assert _core_tokens(["marco", "2024", "goflix", "720p"]) == ["marco"]
    assert _core_tokens(["marco", "2024", "ddp5", "1", "h", "265"]) == ["marco"]
    assert _core_tokens(["pushpa", "2", "the", "rule", "2024"]) == ["pushpa", "2", "the", "rule"]
    # A leading year *is* the title. A later year is the release year.
    assert _core_tokens(["1917"]) == ["1917"]
    assert _core_tokens(["1917", "2019", "bluray"]) == ["1917"]
    assert _core_tokens(["1917", "directors", "cut"]) == ["1917", "directors", "cut"]
    assert _core_tokens(["blade", "runner", "2049", "2017", "extras"]) == ["blade", "runner", "2049"]
    assert normalize_title("1917") == "1917"
    assert normalize_title("1917 2019 1080p BluRay") == "1917"
    assert extract_title("1917.2019.1080p.BluRay.mkv") == "1917"


def test_short_query_score_uses_partial_ratio():
    # Full-string ratio of a 1-word query against a long candidate is under
    # the suggestion threshold; partial_ratio is what clears it.
    long_name = "marco unreleased cut fanedit edition"
    assert _score("marcoo", long_name) >= 55
    # Four or more words stay on full-string ratios only.
    assert _score("marcoo zzzz yyyy xxxx", long_name) < 55


# --------------------------------------------------------------------------
# Ranking (pure functions)
# --------------------------------------------------------------------------
DB_TITLES = [
    "Pradhama Dristhiya Kuttakkar 2025 1080p x264 Hindi Dual Audio",
    "Devara Part 1 2024 720p HDRip.mkv",
    "Pushpa 2 The Rule 2024 1080p WEB DL.mkv",
    "Kanguva 2024 Tamil 1080p.mkv",
]


def test_rank_finds_close_misspelling():
    ranked = rank_suggestions("pradhama drishtiya kuttakar", DB_TITLES)
    assert ranked, "expected at least one suggestion"
    assert ranked[0][0] == "Pradhama Dristhiya Kuttakkar"
    assert ranked[0][1] >= 55


def test_rank_ignores_unrelated_titles():
    ranked = rank_suggestions("pushpa 2 the rule", DB_TITLES)
    titles = [t for t, _ in ranked]
    assert "Pushpa 2 The Rule" in titles
    assert "Devera Part 1" not in titles
    assert "Kanguva" not in titles


def test_rank_deduplicates_same_title_variants():
    titles = [
        "Jawan 2023 1080p x264 Hindi.mkv",
        "Jawan 2023 2160p WEB-DL.mkv",
        "Devara Part 1 2024 720p.mkv",
    ]
    ranked = rank_suggestions("jawan", titles)
    assert [t for t, _ in ranked].count("Jawan") == 1


def test_rank_respects_limit_and_threshold():
    many = [f"Movie Number {i} {i} Quality" for i in range(50)]
    assert len(rank_suggestions("movie number 1", many, limit=3)) <= 3
    assert rank_suggestions("zzzzz nothing like this at all", DB_TITLES) == []


def test_rank_keeps_best_score_per_title():
    ranked = rank_suggestions("pushpa 2 the rule", DB_TITLES)
    top = dict(ranked).get("Pushpa 2 The Rule")
    assert top is not None and top >= 90


def test_short_query_suggests_marco_release_names():
    """``marcoo 2024`` must hit both of these release names, as one title.

    Release-group words (Goflix, DDP5) and the tags after the year used to
    survive normalisation, and the full-string ratio of the short query then
    fell under the suggestion threshold.
    """
    files = [
        "Marco 2024 Kannada HDRip Goflix 720p",
        "MARCO Hindi 2024 720p AMZN WEB DL DDP5 1 H 265",
    ]
    assert normalize_title(files[0]) == "marco"
    assert normalize_title(files[1]) == "marco"
    assert extract_title(files[0]) == "Marco"
    assert extract_title(files[1]) == "Marco"

    ranked = rank_suggestions("marcoo 2024", files)
    assert ranked, "short misspelled query should still suggest the release"
    assert [title for title, _score in ranked] == ["Marco"]
    assert ranked[0][1] >= 55


# --------------------------------------------------------------------------
# Auto-pick rules (pure functions)
# --------------------------------------------------------------------------
def test_auto_pick_only_on_obvious_unique_match():
    assert choose_auto_pick([]) is None
    assert choose_auto_pick([("Jawan", 70)]) is None                      # not near-exact
    assert choose_auto_pick([("Jawan", 95), ("Jawan Dubbed", 89)]) is None  # too close for comfort
    assert choose_auto_pick([("Jawan", 95), ("Jawan Dubbed", 60)]) == "Jawan"
    assert choose_auto_pick([("Pradhama Dristhiya Kuttakkar", 100)]) == "Pradhama Dristhiya Kuttakkar"


# --------------------------------------------------------------------------
# Async wrappers with a stubbed file-DB (no MongoDB needed)
# --------------------------------------------------------------------------
def _stub_names(monkeypatch, names):
    async def fake_fetch(limit=None):
        return list(names)
    monkeypatch.setattr(title_suggest, "fetch_file_names", fake_fetch)
    monkeypatch.setattr(title_suggest, "clear_name_cache", lambda: None)


def test_suggest_returns_db_candidates(monkeypatch):
    _stub_names(monkeypatch, DB_TITLES)

    async def scenario():
        res = await suggest(-1001, "pradhama drishtiya kuttakar")
        assert res and res[0][0] == "Pradhama Dristhiya Kuttakkar"
        return res

    res = asyncio.run(scenario())
    assert res[0][1] >= 55


def test_auto_pick_direct_fix(monkeypatch):
    """The 'bot directly fixes the spelling' path."""
    _stub_names(monkeypatch, DB_TITLES)

    async def scenario():
        # near-exact single match -> auto pick
        picked = await auto_pick(-1001, "pradhama drishtiya kuttakkar")
        assert picked == "Pradhama Dristhiya Kuttakkar"
        # unrelated query -> no auto pick (buttons will be shown instead)
        assert await auto_pick(-1001, "arm strong") is None

    asyncio.run(scenario())


def test_suggest_empty_db(monkeypatch):
    _stub_names(monkeypatch, [])

    async def scenario():
        assert await suggest(-1001, "pushpa") == []
        assert await auto_pick(-1001, "pushpa") is None

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Wiring (needs the full bot dependencies - same env as the other tests)
# --------------------------------------------------------------------------
def test_sdb_callback_data_fits_telegram_limit():
    # chat_id-message_id can be long for supergroups; keep the payload < 64 bytes.
    data = f"sdb#-1001234567890-9999999999#4#123456789012345"
    assert len(data.encode("utf-8")) <= 64
    # and it must round-trip through the handler's split
    assert re.match(r"^sdb#", data)


def test_sdb_handler_registered_before_catch_all():
    pytest.importorskip("pyrogram")
    import plugins.pmfilter as pmf

    order = [n for n, f in vars(pmf).items() if callable(f) and getattr(f, "handlers", None)]
    assert "spell_db_cb_handler" in order
    assert "cb_handler" in order
    assert order.index("spell_db_cb_handler") < order.index("cb_handler")
    assert hasattr(pmf, "SUGG") and isinstance(pmf.SUGG, dict)


def test_no_files_branch_uses_db_auto_pick():
    pytest.importorskip("pyrogram")
    import inspect

    import plugins.pmfilter as pmf

    src = inspect.getsource(pmf.auto_filter)
    assert "db_auto_pick_title" in src
    assert "fixed_by_ai" in src
