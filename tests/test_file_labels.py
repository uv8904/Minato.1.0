"""Unit tests for ``dreamxbotz.util.file_labels``.

The search-result buttons carry ``{size} • {SxxExx/Exx} • {filename}``; these
tests pin down how the episode part is read out of real-world file names and
that a label can never exceed Telegram's 64 character limit.
"""
import pytest

from dreamxbotz.util.file_labels import (
    MAX_BUTTON_TEXT,
    episode_tag,
    file_button_label,
    normalize_file_name,
    truncate_button_text,
)


@pytest.mark.parametrize(
    "file_name,expected",
    [
        ("Flex x Cop S01E03 1080p WEB-DL AAC.mkv", "S01E03"),
        ("Flex x Cop S1 E3 1080p.mkv", "S01E03"),
        ("Show.Season.1.Episode.2.720p.mkv", "S01E02"),
        ("Show.Se01.Ep02.mkv", "S01E02"),
        ("Anime 1x05 [1080p].mkv", "S01E05"),
        ("Show E05.mkv", "E05"),
        ("Show Ep.05.mkv", "E05"),
        ("Show Episode 5.mkv", "E05"),
        ("Show.E7.1080p.mkv", "E07"),
        ("Movie S01.mkv", "S01"),              # season pack
        ("Season.2.Complete.mkv", "S02"),
        # No episode information -> no tag
        ("Jawan (2023) 1080p WEB-DL.mkv", None),
        ("Avatar 2009 1080p x264.mkv", None),
        ("Movie 1920x1080.mp4", None),
        ("The.Secret.Of.2023.mkv", None),
        ("BluRay.RE.2.mkv", None),
        ("", None),
    ],
)
def test_episode_tag(file_name, expected):
    assert episode_tag(file_name) == expected


def test_normalize_file_name():
    assert normalize_file_name("  Movie\n\n2023\t1080p.mkv ") == "Movie 2023 1080p.mkv"
    assert normalize_file_name(None) == ""


def test_file_button_label_format():
    assert file_button_label("Show S01E02 1080p.mkv", "1.82 GB") == "1.82 GB • S01E02 • Show S01E02 1080p.mkv"
    # movies have no episode part
    assert file_button_label("Jawan 2023.mkv", "2.00 GB") == "2.00 GB • Jawan 2023.mkv"
    # a missing name still renders a usable button
    assert file_button_label(None, "1.00 GB") == "1.00 GB • File"


def test_truncate_button_text():
    assert truncate_button_text("short", 64) == "short"

    long_text = "x" * 100
    trimmed = truncate_button_text(long_text, 64)
    assert len(trimmed) == 64
    assert trimmed.endswith("…")

    # emoji/CJK count as two UTF-16 code units, Telegram counts the same way
    gurmukhi = "ਫਿਲਮ" * 40
    trimmed = truncate_button_text(gurmukhi, 64)
    assert len(trimmed.encode("utf-16-le")) // 2 <= 64


def test_file_button_label_fits_telegram_limit():
    label = file_button_label("Flex x Cop DSNP WEB DL AAC x264 ESub [@Team] S01E07 1080p.mkv", "1.82 GB")
    assert len(label.encode("utf-16-le")) // 2 <= MAX_BUTTON_TEXT
    assert label.startswith("1.82 GB • S01E07 • ")
