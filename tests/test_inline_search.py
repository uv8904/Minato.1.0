"""Inline Mode (Option B): ``@bot <movie>`` — pure helpers + handler states.

The handler is called directly with a fake ``InlineQuery`` (no Telegram), and
the file search / poster lookup are stubbed, so every branch is covered
offline::

    pytest tests/test_inline_search.py -q
"""
import asyncio
import base64
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import info  # noqa: E402
import plugins.inline_search as inline  # noqa: E402
from pyrogram.types import InlineQueryResultArticle, InlineQueryResultPhoto  # noqa: E402

BOT = "MyMovieBot"


class FakeFile:
    def __init__(self, file_name, file_size=0):
        self.file_name = file_name
        self.file_size = file_size


class FakeQuery:
    def __init__(self, query="", chat_type="private", user_id=7):
        self.query = query
        self.chat_type = SimpleNamespace(value=chat_type)
        self.from_user = SimpleNamespace(id=user_id)
        self.calls = []

    async def answer(self, results, **kwargs):
        self.calls.append({"results": list(results), "kwargs": kwargs})

    @property
    def results(self):
        return self.calls[-1]["results"] if self.calls else []

    @property
    def kwargs(self):
        return self.calls[-1]["kwargs"] if self.calls else {}


def fake_client():
    return SimpleNamespace(me=SimpleNamespace(username=BOT, first_name="Minato"))


def movie(movie_id="jawan-2023", title="Jawan", **over):
    data = {
        "id": movie_id,
        "movie_id": movie_id,
        "title": title,
        "year": 2023,
        "qualities": ["1080p", "720p"],
        "files": 2,
        "bytes": int(1.5 * 1024 ** 3),
        "is_series": False,
        "sample": f"{title} 2023 Hindi 1080p WEB-DL",
    }
    data.update(over)
    return data


def stub_search(monkeypatch, movies, posters=None):
    async def fake_search(_query, _limit):
        return list(movies)

    async def fake_posters(_ids):
        return dict(posters or {})

    async def fake_trends(_limit):
        return []

    monkeypatch.setattr(inline, "search_movies", fake_search)
    monkeypatch.setattr(inline, "posters_for", fake_posters)
    monkeypatch.setattr(inline, "top_queries", fake_trends)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_sanitize_query_cleans_control_chars_prefix_and_length():
    assert inline.sanitize_query("  @MyMovieBot   jawan  ") == "jawan"
    assert inline.sanitize_query("jawan\n2023") == "jawan 2023"
    long = "a" * (info.INLINE_SEARCH_MAX_QUERY + 40)
    assert len(inline.sanitize_query(long)) == info.INLINE_SEARCH_MAX_QUERY
    assert inline.sanitize_query(None) == ""


def test_rate_limit_blocks_after_the_hourly_budget(monkeypatch):
    monkeypatch.setattr(inline, "INLINE_SEARCH_MAX_REQUESTS", 2)
    inline.reset_rate_limits()

    assert inline.rate_limited(1, now=1000.0) is False
    assert inline.rate_limited(1, now=1001.0) is False
    assert inline.rate_limited(1, now=1002.0) is True       # budget spent
    assert inline.rate_limited(2, now=1002.0) is False      # other users unaffected
    assert inline.rate_limited(1, now=1000.0 + 3601) is False  # window slides

    inline.reset_rate_limits()
    assert inline.rate_limited(1, now=1000.0) is False


def test_rate_limit_can_be_disabled(monkeypatch):
    monkeypatch.setattr(inline, "INLINE_SEARCH_MAX_REQUESTS", 0)
    inline.reset_rate_limits()

    for _ in range(50):
        assert inline.rate_limited(9) is False


def test_movie_groups_merges_qualities_and_flags_series():
    files = [
        FakeFile("Jawan 2023 Hindi 1080p WEB-DL x264.mkv", 3_000_000_000),
        FakeFile("Jawan 2023 Hindi 720p HDRip.mkv", 1_000_000_000),
        FakeFile("Panchayat S01E02 720p WEB-DL.mkv", 500_000_000),
    ]

    groups = inline.movie_groups(files)

    assert [group["movie_id"] for group in groups] == ["jawan-2023", "panchayat"]
    jawan = groups[0]
    assert jawan["title"] == "Jawan"
    assert jawan["year"] == 2023
    assert jawan["files"] == 2
    assert jawan["qualities"] == ["1080p", "720p"]
    assert jawan["bytes"] == 4_000_000_000
    assert jawan["is_series"] is False
    assert groups[1]["is_series"] is True
    assert inline.movie_groups(files, limit=1) == [jawan]
    assert inline.movie_groups([]) == []


def test_start_search_link_round_trips_the_query():
    link = inline.start_search_link(BOT, "jawan 2023")

    assert link.startswith(f"https://t.me/{BOT}?start=msrch_")
    payload = link.rsplit("msrch_", 1)[1]
    padded = payload + "=" * (-len(payload) % 4)
    assert base64.urlsafe_b64decode(padded).decode() == "jawan 2023"
    assert inline.start_search_link("", "jawan") == ""


def test_site_search_url_uses_the_public_url(monkeypatch):
    monkeypatch.setattr(info, "URL", "https://minato.example/")
    assert inline.site_search_url("jawan 2023") == "https://minato.example/search?q=jawan+2023"
    assert inline.site_search_url() == "https://minato.example/search"

    monkeypatch.setattr(info, "URL", "")
    assert inline.site_search_url("jawan") == ""


def test_build_results_prefers_poster_cards_and_never_leaks_files():
    results = inline.build_results(
        [movie()],
        query="jawan",
        bot_username=BOT,
        posters={"jawan-2023": "https://cdn.example/poster.jpg"},
        site_url="https://minato.example/search?q=jawan",
    )

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, InlineQueryResultPhoto)
    assert result.photo_url == "https://cdn.example/poster.jpg"
    assert result.id.startswith("inl") and len(result.id) <= 64
    assert "Jawan" in result.caption and "1080p" in result.caption

    urls = [button.url for row in result.reply_markup.inline_keyboard for button in row if button.url]
    assert f"https://t.me/{BOT}?start=movie_jawan-2023" in urls
    assert "https://minato.example/search?q=jawan" in urls
    shares = [
        button.switch_inline_query
        for row in result.reply_markup.inline_keyboard
        for button in row
        if button.switch_inline_query
    ]
    assert shares == ["Jawan"]
    assert "file_id" not in result.caption and "BOT_TOKEN" not in result.caption


def test_build_results_falls_back_to_text_cards():
    results = inline.build_results(
        [movie()], query="jawan", bot_username=BOT, posters={}, use_posters=False
    )

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, InlineQueryResultArticle)
    assert result.title.startswith("Jawan")
    assert "1080p" in result.description


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #
def test_handler_answers_with_cards(monkeypatch):
    stub_search(monkeypatch, [movie()], {"jawan-2023": "https://cdn.example/poster.jpg"})
    query = FakeQuery("jawan")

    run(inline.inline_search(fake_client(), query))

    assert len(query.calls) == 1
    assert query.kwargs["is_personal"] is True
    assert query.kwargs["cache_time"] == info.INLINE_SEARCH_CACHE_TTL
    assert query.kwargs["switch_pm_parameter"] == "inline"
    cards = [row for row in query.results if isinstance(row, InlineQueryResultPhoto)]
    assert len(cards) == 1
    assert isinstance(query.results[-1], InlineQueryResultArticle)  # "all results" card


def test_handler_reports_no_result_with_a_search_link(monkeypatch):
    stub_search(monkeypatch, [])
    monkeypatch.setattr(info, "URL", "https://minato.example")
    query = FakeQuery("nonexistent movie")

    run(inline.inline_search(fake_client(), query))

    note = query.results[0]
    assert "No file for" in note.title
    urls = [button.url for row in note.reply_markup.inline_keyboard for button in row if button.url]
    assert any(url.startswith(f"https://t.me/{BOT}?start=msrch_") for url in urls)
    assert "https://minato.example/search?q=nonexistent+movie" in urls


def test_handler_suggests_top_searches_for_an_empty_query(monkeypatch):
    stub_search(monkeypatch, [movie()])

    async def fake_trends(_limit):
        return ["jawan"]

    monkeypatch.setattr(inline, "top_queries", fake_trends)
    query = FakeQuery("")

    run(inline.inline_search(fake_client(), query))

    assert any("Jawan" in getattr(row, "caption", getattr(row, "title", "")) for row in query.results)
    assert any(row.id == "inl-hint" for row in query.results)


def test_handler_shows_a_prompt_when_there_is_nothing_to_suggest(monkeypatch):
    stub_search(monkeypatch, [])
    query = FakeQuery("")

    run(inline.inline_search(fake_client(), query))

    assert "Search a movie" in query.results[0].title
    assert query.kwargs["switch_pm_parameter"] == "inline"


def test_handler_stops_at_the_rate_limit(monkeypatch):
    stub_search(monkeypatch, [movie()], {"jawan-2023": "https://cdn.example/poster.jpg"})
    monkeypatch.setattr(inline, "INLINE_SEARCH_MAX_REQUESTS", 1)
    inline.reset_rate_limits()

    first, second = FakeQuery("jawan"), FakeQuery("jawan")
    run(inline.inline_search(fake_client(), first))
    run(inline.inline_search(fake_client(), second))

    assert any(isinstance(row, InlineQueryResultPhoto) for row in first.results)
    assert "Slow down" in second.results[0].title
    inline.reset_rate_limits()


def test_handler_can_be_disabled(monkeypatch):
    monkeypatch.setattr(inline, "INLINE_SEARCH", False)
    query = FakeQuery("jawan")

    run(inline.inline_search(fake_client(), query))

    assert "disabled" in query.results[0].title.lower()


def test_handler_can_be_restricted_to_private_chats(monkeypatch):
    monkeypatch.setattr(inline, "INLINE_SEARCH_GROUPS", False)
    query = FakeQuery("jawan", chat_type="supergroup")

    run(inline.inline_search(fake_client(), query))

    assert "private chat" in query.results[0].title.lower()
    assert query.kwargs["switch_pm_text"] == "Open the bot"


def test_handler_survives_a_failing_answer(monkeypatch):
    """A Telegram hiccup must never crash the bot (the answer is wrapped)."""
    stub_search(monkeypatch, [movie()])

    class Exploding(FakeQuery):
        async def answer(self, results, **kwargs):
            raise RuntimeError("400: QUERY_ID_INVALID")

    query = Exploding("jawan")
    run(inline.inline_search(fake_client(), query))  # no exception

    assert query.calls == []
