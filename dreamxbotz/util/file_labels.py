"""Inline-button labels for auto-filter (search result) files.

Search results never put file links into the message text any more - every
file gets its own inline button whose label reads::

    {size} • {SxxExx / Exx} • {filename}

The middle part is taken straight from the file name: ``S01E02`` when the
name carries a season + episode, ``E02`` when only an episode number exists
(e.g. ``Show E05.mkv``), ``S01`` for a season pack (``Show S01.mkv``) and
nothing at all for movies, so their label stays ``{size} • {filename}``.

Telegram allows at most 64 characters for a button label, therefore long
file names are trimmed with an ellipsis (``…``) so the size/episode part is
never lost. ``callback_data`` still carries the real ``file_id``.
"""
from __future__ import annotations

import re
from typing import Optional

# Telegram's hard limit for a single inline button label.
MAX_BUTTON_TEXT = 64

# S01E02 | S1 E2 | S01.E02 | S01-E02 | Season 1 Episode 2 | Se01 Ep02
_SEASON_EPISODE_RE = re.compile(
    r"(?<![a-z0-9])s(?:eason|e|zn)?[\s._\-]*(\d{1,3})"
    r"[\s._\-]*(?:episode|eps|ep|e)[\s._\-]*(\d{1,3})(?![0-9])",
    re.IGNORECASE,
)

# 1x02 (another way of writing S01E02)
_X_FORMAT_RE = re.compile(r"(?<![a-z0-9])(\d{1,2})\s*[xX]\s*(\d{1,3})(?![0-9])")

# Episode only: E05 | Ep.05 | EP05 | Episode 5
_EPISODE_RE = re.compile(
    r"(?<![a-z0-9])(?:episode|eps|ep|e)[\s._\-]*(\d{1,3})(?![0-9])",
    re.IGNORECASE,
)

# Season pack: S01 | Season.1 | Se01
_SEASON_RE = re.compile(
    r"(?<![a-z0-9])s(?:eason|e|zn)?[\s._\-]*(\d{1,2})(?![a-z0-9])",
    re.IGNORECASE,
)


def episode_tag(file_name: str) -> Optional[str]:
    """Return ``SxxExx`` / ``Exx`` / ``Sxx`` for a file name, else ``None``."""
    if not file_name:
        return None

    match = _SEASON_EPISODE_RE.search(file_name)
    if match:
        return f"S{int(match.group(1)):02d}E{int(match.group(2)):02d}"

    match = _X_FORMAT_RE.search(file_name)
    if match:
        return f"S{int(match.group(1)):02d}E{int(match.group(2)):02d}"

    match = _EPISODE_RE.search(file_name)
    if match:
        return f"E{int(match.group(1)):02d}"

    match = _SEASON_RE.search(file_name)
    if match:
        return f"S{int(match.group(1)):02d}"

    return None


def normalize_file_name(file_name) -> str:
    """Collapse newlines/spaces so a name always fits on one button line."""
    if not file_name:
        return ""
    return re.sub(r"\s+", " ", str(file_name)).strip()


def _utf16_length(text: str) -> int:
    """Telegram counts a button label in UTF-16 code units, not characters."""
    return len(text.encode("utf-16-le")) // 2


def truncate_button_text(text: str, limit: int = MAX_BUTTON_TEXT) -> str:
    """Shorten ``text`` to ``limit`` UTF-16 code units, ending with ``…``."""
    if _utf16_length(text) <= limit:
        return text

    budget = limit - 1  # room for the ellipsis
    kept = []
    used = 0
    for char in text:
        used += _utf16_length(char)
        if used > budget:
            break
        kept.append(char)
    return "".join(kept).rstrip() + "…"


def file_button_label(file_name, size_text, limit: int = MAX_BUTTON_TEXT) -> str:
    """Build the ``{size} • {SxxExx/Exx} • {filename}`` button label."""
    name = normalize_file_name(file_name) or "File"
    tag = episode_tag(name)
    prefix = f"{size_text} • {tag} • " if tag else f"{size_text} • "
    return truncate_button_text(prefix + name, limit)
