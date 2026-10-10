"""Leading emoji on coloured buttons can become Telegram Premium animated icons."""
import pytest

pytest.importorskip("pyrogram")

from dreamxbotz.util import buttons


@pytest.fixture(autouse=True)
def _restore_map():
    before = buttons.get_premium_emoji()
    yield
    buttons.set_premium_emoji(before)


def test_parse_skips_invalid_entries():
    assert buttons.parse_premium_emoji("🎬=123;🔍=abc;⚡️=9") == {"🎬": 123, "⚡️": 9}
    assert buttons.parse_premium_emoji("") == {}


def test_mapped_leading_emoji_becomes_icon_and_is_removed_from_text():
    buttons.set_premium_emoji({"🎬": 5368324170671202286})
    b = buttons.blue("🎬 ᴇxᴛᴇɴᴅ ᴘʟᴀɴ", callback_data="x")
    assert b.text == "ᴇxᴛᴇɴᴅ ᴘʟᴀɴ"
    assert b.style.icon == 5368324170671202286
    assert b.style.bg_primary is True


def test_unmapped_emoji_is_left_alone():
    buttons.set_premium_emoji({"🎬": 1})
    b = buttons.green("🚀 Download", url="https://example.com")
    assert b.text == "🚀 Download"
    assert b.style.icon is None


def test_longest_key_wins():
    buttons.set_premium_emoji({"⚡": 1, "⚡️": 2})
    assert buttons.red("⚡️ Fast", callback_data="x").style.icon == 2
