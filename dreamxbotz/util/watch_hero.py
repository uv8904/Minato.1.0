"""Movie hero strip shown above the player on the Stream Mode watch page.

What it is
----------
``/watch/<id>/<file>?hash=…`` (``req.html``) now opens with a full-width
"movie hero": a 16:9 backdrop band with a 2:3 poster card of the movie that is
being streamed, a Telegram deep-link button for that exact movie
(``https://t.me/<BOT_USERNAME>?start=movie_<MOVIE_ID>``) and a copy button.

Why it is server rendered
-------------------------
The title, year, quality chips, upload date and deep link are all derived from
the file name plus the Telegram message that is being streamed, so the strip is
complete **before** JavaScript runs (and still shows meaningfully when it is
disabled).  Only the artwork is fetched afterwards from

    GET /api/movies/art/<MOVIE_ID>        (dreamxbotz/server/movie_api.py)

which resolves the poster/backdrop through TMDB → IMDb, remembers the result in
the ``movie_art`` collection and serves the bytes from our own origin – the
browser never talks to a third party and never sees the bot token.

Reference: docs/NEWLY_UPLOADED_MOVIES.md
"""
import re
from typing import Any, Dict, List, Optional

from dreamxbotz.util.movie_titles import (
    MAX_STORED_NAME_LENGTH,
    MAX_TITLE_LENGTH,
    build_deeplink,
    clean_title_text,
    looks_like_series,
    movie_id_for,
    parse_release_name,
    relative_time_label,
)

__all__ = ["build_context", "hero_api_url", "hero_enabled", "hero_path"]

DEFAULT_ART_PATH = "/api/movies/art"
DEFAULT_TITLE = "Movie"

#: Human labels for the pieces of a release name that make good chips.
_LANGUAGE_RE = re.compile(
    r"\b(Hindi|Tamil|Telugu|Malayalam|Kannada|Bengali|Marathi|Punjabi|Gujarati|"
    r"English|Dual[\s._-]?Audio|Multi[\s._-]?Audio|ORG)\b",
    re.IGNORECASE,
)
_SOURCE_RE = re.compile(
    r"\b(WEB[\s._-]?DL|WEBRip|Blu[\s._-]?Ray|BluRay|BDRip|BRRip|BR[\s._-]?Rip|"
    r"HDRip|HDTV|DVDRip|PRE[\s._-]?DVD|HDCAM|HDTS|CAM)\b",
    re.IGNORECASE,
)
_CODEC_RE = re.compile(r"\b(x264|x265|HEVC|AVC|H\.?264|H\.?265|10[\s._-]?bit)\b", re.IGNORECASE)
_SOURCE_LABELS = {
    "webdl": "WEB-DL",
    "webrip": "WEBRip",
    "bluray": "BluRay",
    "bdrip": "BDRip",
    "brrip": "BRRip",
    "hdrip": "HDRip",
    "hdtv": "HDTV",
    "dvdrip": "DVDRip",
    "predvd": "PRE-DVD",
    "hdcam": "HDCAM",
    "hdts": "HDTS",
    "cam": "CAM",
}
_CODEC_LABELS = {"h264": "x264", "h265": "x265", "10bit": "10bit", "avc": "x264"}


def _cfg(name: str, default):
    """Read a setting from ``info.py`` without breaking when it is missing."""
    try:
        import info  # type: ignore

        return getattr(info, name, default)
    except Exception:
        return default


def hero_enabled() -> bool:
    """Master switch for the watch-page hero: ``WATCH_HERO`` (default ``True``)."""
    return bool(_cfg("WATCH_HERO", True))


def hero_path() -> str:
    """Endpoint the hero asks for poster/backdrop URLs."""
    path = str(_cfg("WATCH_HERO_API_PATH", DEFAULT_ART_PATH) or DEFAULT_ART_PATH)
    return path if path.startswith("/") else "/" + path


def hero_api_url(base: Optional[str] = None) -> str:
    """Full art endpoint – same origin by default, ``API_URL`` when hosted elsewhere."""
    if base is None:
        base = str(_cfg("API_URL", "") or "")
    base = str(base or "").strip().rstrip("/")
    return f"{base}{hero_path()}" if base else hero_path()


def _clean_token(value: Optional[str], limit: int = 24) -> str:
    """One short, markup-free chip label."""
    return clean_title_text(value or "", limit=limit)


def _release_tags(file_name: str) -> List[str]:
    """``["Malayalam", "BR-Rip", "x264"]`` – the extra chips of a release name."""
    name = str(file_name or "")
    tags: List[str] = []

    language = _LANGUAGE_RE.search(name)
    if language:
        label = re.sub(r"[\s._-]+", " ", language.group(1)).strip()
        tags.append(_clean_token(label.title() if label.islower() else label))

    source = _SOURCE_RE.search(name)
    if source:
        key = re.sub(r"[^a-z0-9]", "", source.group(1).lower())
        tags.append(_clean_token(_SOURCE_LABELS.get(key, source.group(1))))

    codec = _CODEC_RE.search(name)
    if codec:
        key = re.sub(r"[^a-z0-9]", "", codec.group(1).lower())
        tags.append(_clean_token(_CODEC_LABELS.get(key, codec.group(1))))

    seen: set = set()
    return [tag for tag in tags if tag and not (tag.lower() in seen or seen.add(tag.lower()))]


def build_context(
    file_name: str,
    bot_username: str = "",
    uploaded_at=None,
    *,
    api_base: Optional[str] = None,
    enabled: Optional[bool] = None,
    now=None,
) -> Dict[str, Any]:
    """Everything ``req.html`` needs for the hero strip.

    ``file_name`` is the name of the file being streamed, ``uploaded_at`` the
    date of its Telegram message (used as the upload date) and
    ``bot_username`` the **public** bot username (``BOT_USERNAME``) – the bot
    token is never part of the context.
    """
    raw_name = clean_title_text(file_name or "", limit=MAX_STORED_NAME_LENGTH)
    parsed = parse_release_name(raw_name)

    title = parsed["title"] or clean_title_text(raw_name, limit=MAX_TITLE_LENGTH) or DEFAULT_TITLE
    title = clean_title_text(title, limit=MAX_TITLE_LENGTH) or DEFAULT_TITLE
    year = parsed["year"]
    movie_id = movie_id_for(title, year)
    deeplink = build_deeplink(bot_username, movie_id)
    added = relative_time_label(uploaded_at, now=now)

    chips: List[str] = []
    if year:
        chips.append(str(year))
    if parsed["quality"]:
        chips.append(parsed["quality"])
    for tag in _release_tags(raw_name):
        if tag.lower() not in {chip.lower() for chip in chips}:
            chips.append(tag)

    is_series = bool(parsed["is_series"]) or looks_like_series(raw_name)

    return {
        "watch_hero_enabled": bool(hero_enabled() if enabled is None else enabled),
        "watch_hero_art_url": hero_api_url(api_base),
        "watch_hero_movie_id": movie_id,
        "watch_hero_title": title,
        "watch_hero_year": year or "",
        "watch_hero_quality": parsed["quality"] or "",
        "watch_hero_chips": chips,
        "watch_hero_added": added,
        "watch_hero_kind": "Series" if is_series else "Movie",
        "watch_hero_deeplink": deeplink,
        "watch_hero_ready": bool(deeplink),
    }
