"""Pure helpers behind the Stream Mode "Newly Uploaded Movies" section.

Nothing in this module touches Telegram, MongoDB or the network, so it can be
imported and unit-tested on its own and reused by:

* ``database/recent_movies_db.py``  – persistence of newly uploaded movies
* ``dreamxbotz/util/new_uploaded.py`` – indexing hook + poster worker
* ``dreamxbotz/util/movie_deeplink.py`` – ``/start movie_<MOVIE_ID>`` flow
* ``dreamxbotz/server/movie_api.py`` – the JSON API consumed by the web pages

Terminology
-----------
``MOVIE_ID``
    Deep-link safe, deterministic identifier of one movie: ``slug`` or
    ``slug-year`` (e.g. ``jawan-2023``).  It is what the frontend sends back in
    the Telegram deep link:

        https://t.me/<BOT_USERNAME>?start=movie_<MOVIE_ID>

    Because it is derived from the title (+ year) the same movie always maps to
    the same id, which is what keeps duplicate entries out of the database.

``title_key``
    Lower-case, punctuation-free ``"title year"`` string used for de-duplication
    and for matching a freshly indexed release file to an existing movie.
"""
import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "MOVIE_ID_RE",
    "as_utc",
    "DEEPLINK_RE",
    "MAX_TITLE_LENGTH",
    "canonical_quality",
    "clean_title_text",
    "build_deeplink",
    "human_size_limit",
    "looks_like_series",
    "looks_like_video",
    "movie_id_for",
    "normalize_title",
    "parse_release_name",
    "primary_quality",
    "quality_label",
    "quality_rank",
    "relative_time_label",
    "sanitize_movie_id",
    "slugify",
    "utcnow",
]

# --------------------------------------------------------------------------- #
# Limits / patterns
# --------------------------------------------------------------------------- #

#: Telegram ``?start=`` payloads allow ``A-Z a-z 0-9 _ -`` and at most 64 chars.
MAX_MOVIE_ID_LENGTH = 64
#: ``movie_`` prefix leaves room for the id itself.
MAX_SLUG_LENGTH = 48
MAX_TITLE_LENGTH = 120
#: Longest poster/file-name derived strings we keep in the database.
MAX_STORED_NAME_LENGTH = 300

MOVIE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
DEEPLINK_RE = re.compile(
    r"^https://t\.me/[A-Za-z0-9_]{4,32}\?start=movie_[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
)
BOT_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")

_YEAR_RE = re.compile(r"(?<![A-Za-z0-9])(19\d{2}|20\d{2})(?![A-Za-z0-9])")
_EPISODE_RE = re.compile(
    r"(?:\bS\d{1,2}\s*E\d{1,3}\b"
    r"|\bS\d{2}\b"
    r"|\bSeason\s*\d{1,2}\b"
    r"|\bE(?:p|pisode)\s*0?\d{1,3}\b"
    r"|\bS\d{1,2}\s*[-–]\s*E?\d{1,2}\b)",
    re.IGNORECASE,
)
_VIDEO_EXT_RE = re.compile(
    r"\.(?:mkv|mp4|m4v|avi|mov|webm|flv|wmv|mpg|mpeg|m2ts|mts|ts|vob|ogv|3gp|hevc)$",
    re.IGNORECASE,
)
_VIDEO_MIME_RE = re.compile(r"^video/", re.IGNORECASE)
_JUNK_EXT_RE = re.compile(
    r"\.(?:zip|rar|7z|tar|gz|srt|ass|sub|ssa|nfo|jpg|jpeg|png|webp|mp3|m4a|flac|wav)$",
    re.IGNORECASE,
)
_QUALITY_TOKEN = r"(?:240|360|480|540|576|720|1080|1440|2160)p|4k|8k"
_QUALITY_RE = re.compile(rf"\b({_QUALITY_TOKEN})\b", re.IGNORECASE)
_RELEASE_JUNK_RE = re.compile(
    r"\b(?:"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|aac|aac2\.?0|ac3|eac3|ddp?\+?\s?[257]\.1|dts|truehd|atmos|"
    r"web[\s._-]?dl|webdl|webrip|blu[\s._-]?ray|bluray|brrip|bdrip|bdremux|remux|hdrip|dvdrip|dvdscr|"
    r"predvd|pre[\s._-]?dvd|hdcam|hdtc|camrip|cam[\s._-]?rip|telesync|\bts\b|\btc\b|telecine|"
    r"tvrip|hdtv|sdr|hdr10\+?|dv|10bit|8bit|dual[\s._-]?audio|multi[\s._-]?audio|org[\s._-]?aud|"
    r"esub|esubs|msub|subs|subbed|dubbed|proper|repack|extended|uncut|imax|uhd|hd|fhd|"
    r"full[\s._-]?movie|watch[\s._-]?online|download"
    r")\b",
    re.IGNORECASE,
)
_SITE_JUNK_RE = re.compile(
    r"(?:@[A-Za-z0-9_]+|\bwww\.[^\s]+|\bhttps?://[^\s]+"
    r"|\b[A-Za-z0-9-]+\.(?:com|net|org|in|to|me|xyz|club|pro|app|cc|co|io|link|site|online|top)\b"
    r"|\bwww\b|\btg\b|\btelegram\b)",
    re.IGNORECASE,
)
#: Well-known release-group brands – always removed from a title.  Deliberately
#: curated (instead of reusing ``info.BAD_WORDS``) because that list also holds
#: generic words such as "original"/"villa" that are part of real movie titles.
_BRAND_JUNK_RE = re.compile(
    r"\b(?:"
    r"hdhub4u|hdhub|hub4u|moviesmod|mkvcinemas|mkvhub|1tamilmv|tamilblasters|tamilmv|"
    r"filmyzilla|filmywap|skymovieshd|moviesflix|movieflix|extraflix|private ?moviez|"
    r"toonworld4all|themoviesboss|vegamovies|9xmovies|bolly4u|moviezwap|movierulz|"
    r"ibomma|hdm2|primefix|katmoviehd|uwatchfree|okjatt|moviesverse|tamilrockers|"
    r"isaimini|kuttymovies|jiorockers|movierulz2|rarbg|yts|yify|torrentgalaxy|"
    r"psa|anonymously|minatoverse|dreamxbotz|dreamcinezone"
    r")\b",
    re.IGNORECASE,
)
_BRACKET_RE = re.compile(r"[\[\]{}()<>|~`^*#+=:;!?,]")
#: Leading short ALL-CAPS tag in brackets – release-group markers such as
#: ``[CK] Marco (2024)`` or ``[YTS]``.  Only very short, upper-case tags are
#: dropped so real titles like ``K.G.F`` or ``[REC]``-style names stay intact
#: (a name that ends up empty still falls back to the raw input below).
_LEADING_TAG_RE = re.compile(
    r"^\s*[\[\(\{<]\s*([A-Z0-9][A-Z0-9 ._-]{0,3})\s*[\]\)\}>]\s*[-–—:.]*\s*"
)
_SEPARATOR_RE = re.compile(r"[._]+")
_WHITESPACE_RE = re.compile(r"\s+")

#: Canonical quality labels, ordered from worst to best.
QUALITY_ORDER: Sequence[str] = (
    "140p",
    "240p",
    "360p",
    "480p",
    "540p",
    "576p",
    "720p",
    "1080p",
    "1440p",
    "2160p",
    "4K",
)
_QUALITY_ALIASES = {
    "4k": "4K",
    "8k": "2160p",
    "uhd": "2160p",
    "hd": "720p",
    "fhd": "1080p",
    "fullhd": "1080p",
}
_QUALITY_RANK = {label.lower(): index for index, label in enumerate(QUALITY_ORDER)}


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def utcnow() -> datetime:
    """Timezone-aware UTC ``now`` (never naïve, so it is safe to compare/sort)."""
    return datetime.now(timezone.utc)


def human_size_limit(value: int) -> str:
    """``1536`` -> ``1.5 KB``; used only for log lines."""
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}".replace(".0 ", " ")
        size /= 1024
    return f"{value} B"


def as_utc(value) -> Optional[datetime]:
    """Accept ``datetime``/ISO string/``None`` and always return aware UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:  # motor returns naïve datetimes when stored naïve
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def clean_title_text(value, limit: int = MAX_TITLE_LENGTH) -> str:
    """Collapse whitespace, drop control/angle-bracket chars, hard-cap length.

    Used on **every** string that leaves the API so a hostile file name can
    never smuggle markup into the web page (the client sets the value with
    ``textContent`` as a second line of defence).
    """
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    text = text.replace("<", "").replace(">", "")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def slugify(text: str, max_length: int = MAX_SLUG_LENGTH) -> str:
    """ASCII, lower-case, hyphenated slug (``"Jawan (2023)"`` -> ``"jawan-2023"``)."""
    text = str(text or "").strip().lower()
    text = (
        text.replace("&", " and ")
        .replace("+", " ")
        .replace("@", " ")
        .replace("#", " ")
    )
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if len(text) > max_length:
        text = text[:max_length].rstrip("-")
    return text


def sanitize_movie_id(value) -> str:
    """Return a safe ``MOVIE_ID`` or ``""`` when the input is not acceptable.

    Accepts the raw id, ``movie_<id>`` (deep-link payload) or ``<id>`` with a
    stray ``@``/whitespace.  Everything else (paths, query strings, script
    payloads) is rejected so no user input ever reaches a DB query or a URL
    unvalidated.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if text.startswith("movie_"):
        text = text[len("movie_") :]
    text = text.strip().strip("/").strip()
    if len(text) > MAX_MOVIE_ID_LENGTH:
        return ""
    if not MOVIE_ID_RE.match(text):
        return ""
    return text


def _fallback_id(title: str, year: Optional[int]) -> str:
    """Stable id for titles without any ASCII characters (e.g. ``கபாலி``)."""
    digest = hashlib.sha1(f"{title}|{year or ''}".encode("utf-8")).hexdigest()
    return f"m-{digest[:12]}"


# --------------------------------------------------------------------------- #
# Title / release-name parsing
# --------------------------------------------------------------------------- #
def normalize_title(
    title: str, year: Optional[int] = None
) -> Tuple[str, Optional[int], str]:
    """Return ``(title, year, title_key)`` for a movie title or release name.

    ``title_key`` is the de-duplication key: lower-case, punctuation free and
    always ending with the year when one is known.
    """
    raw = str(title or "")
    raw = _JUNK_EXT_RE.sub(" ", raw)
    raw = _VIDEO_EXT_RE.sub(" ", raw)
    # "[CK] Marco (2024) …" -> "Marco (2024) …" (release-group tag, not a title)
    for _ in range(2):
        stripped = _LEADING_TAG_RE.sub("", raw)
        if stripped == raw or not stripped.strip():
            break
        raw = stripped
    raw = raw.replace("_", " ").replace(".", " ")
    raw = _SITE_JUNK_RE.sub(" ", raw)
    raw = _BRAND_JUNK_RE.sub(" ", raw)

    found_year = None
    match = _YEAR_RE.search(raw)
    if match:
        found_year = int(match.group(1))
        raw = raw[: match.start()]
    if year is None:
        year = found_year
    elif found_year and found_year != year:
        # Keep the caller's year (it comes from a curated source such as IMDb).
        year = int(year)

    raw = _RELEASE_JUNK_RE.sub(" ", raw)
    # Quality and episode markers are stored in their own fields, not in the
    # title – drop them so cards read "The Family Man" instead of
    # "The Family Man S02E05 1080p".
    raw = _EPISODE_RE.sub(" ", raw)
    raw = _QUALITY_RE.sub(" ", raw)
    raw = _BRACKET_RE.sub(" ", raw)
    raw = _SEPARATOR_RE.sub(" ", raw)
    raw = _WHITESPACE_RE.sub(" ", raw).strip(" -–—")

    if not raw:
        raw = clean_title_text(title, limit=MAX_TITLE_LENGTH)

    title = _smart_case(raw)
    slug = slugify(title)
    if slug:
        key_base = slug.replace("-", " ")
    else:  # non ASCII title (e.g. "கபாலி") – fall back to a stable hash
        key_base = _fallback_id(title, year)
    title_key = " ".join(part for part in (key_base, str(year or "")) if part)
    return title, (int(year) if year else None), title_key


def _smart_case(text: str) -> str:
    """Title-Case only all-lower/all-UPPER names, leave mixed case untouched."""
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return text
    if text.islower() or text.isupper():
        return " ".join(word.capitalize() for word in text.split())
    return text


def looks_like_series(file_name: str) -> bool:
    """``True`` for episode/season style names (``Show S02E05``)."""
    return bool(_EPISODE_RE.search(str(file_name or "")))


def looks_like_video(file_name: str, mime_type: str = "", file_type: str = "") -> bool:
    """Cheap gate so archives/subtitles/audio never show up as "movies"."""
    name = str(file_name or "")
    if _JUNK_EXT_RE.search(name):
        return False
    if _VIDEO_EXT_RE.search(name):
        return True
    if _VIDEO_MIME_RE.match(str(mime_type or "")):
        return True
    return str(file_type or "").lower() in ("video", "document", "video_note")


def parse_release_name(file_name: str) -> dict:
    """Best-effort split of a release file name into title/year/quality.

    Example::

        Jawan.2023.1080p.WEB-DL.Hindi.x264.mkv
        -> {"title": "Jawan", "year": 2023, "quality": "1080p", ...}
    """
    name = str(file_name or "").strip()
    name = _JUNK_EXT_RE.sub(" ", name)
    name = _VIDEO_EXT_RE.sub(" ", name)

    qualities = [q for q in (canonical_quality(m) for m in _QUALITY_RE.findall(name)) if q]
    quality = primary_quality(qualities)

    title, year, title_key = normalize_title(name)
    return {
        "title": title,
        "year": year,
        "title_key": title_key,
        "quality": quality,
        "qualities": sorted(set(qualities), key=quality_rank, reverse=True),
        "is_series": looks_like_series(name),
    }


# --------------------------------------------------------------------------- #
# Quality helpers
# --------------------------------------------------------------------------- #
def canonical_quality(value) -> str:
    """``"1080P"`` -> ``"1080p"``, ``"4K"`` -> ``"4K"``, ``"n/a"`` -> ``""``."""
    if value is None:
        return ""
    text = str(value).strip().lower().replace(" ", "").replace("·", "").replace(",", "")
    if not text or text in ("n/a", "na", "none", "unknown"):
        return ""
    if text in _QUALITY_ALIASES:
        return _QUALITY_ALIASES[text]
    match = re.match(rf"^({_QUALITY_TOKEN})$", text, re.IGNORECASE)
    if not match:
        return ""
    token = match.group(1).lower()
    return "4K" if token == "4k" else token


def quality_rank(value) -> int:
    """Sort key: ``1080p`` ranks above ``480p``; unknown values rank lowest."""
    return _QUALITY_RANK.get(str(value or "").strip().lower(), -1)


def primary_quality(qualities: Iterable) -> str:
    """Best quality of a collection (used for the badge on the poster)."""
    cleaned = [canonical_quality(q) for q in qualities or []]
    cleaned = [q for q in cleaned if q]
    if not cleaned:
        return ""
    return max(cleaned, key=quality_rank)


def quality_label(qualities: Iterable) -> str:
    """Human badge label, best first: ``["480p","1080p"] -> "1080p, 480p"``."""
    cleaned = sorted(
        {q for q in (canonical_quality(q) for q in qualities or []) if q},
        key=quality_rank,
        reverse=True,
    )
    return ", ".join(cleaned)


# --------------------------------------------------------------------------- #
# Deep links
# --------------------------------------------------------------------------- #
def movie_id_for(title: str, year: Optional[int] = None) -> str:
    """Deterministic ``MOVIE_ID`` for a movie (``jawan`` / ``jawan-2023``).

    Deterministic ids are what make de-duplication work: the same movie always
    lands on the same document, no matter how many files/qualities arrive.
    """
    title, year, _ = normalize_title(title, year)
    slug = slugify(title)
    if not slug:
        return _fallback_id(title, year)
    if year:
        slug = f"{slug}-{year}"
    if len(slug) > MAX_MOVIE_ID_LENGTH:
        slug = slug[:MAX_MOVIE_ID_LENGTH].rstrip("-")
    return slug


def build_deeplink(bot_username: str, movie_id: str) -> str:
    """``https://t.me/<BOT_USERNAME>?start=movie_<MOVIE_ID>`` or ``""``.

    The bot token is never involved and never leaves the server – only the
    public ``@username`` (``BOT_USERNAME``) is used.
    """
    username = str(bot_username or "").strip().lstrip("@")
    safe_id = sanitize_movie_id(movie_id)
    if not username or not safe_id or not BOT_USERNAME_RE.match(username):
        return ""
    return f"https://t.me/{username}?start=movie_{safe_id}"


def relative_time_label(value, now: Optional[datetime] = None) -> str:
    """``"2 days ago"`` / ``"12 Sep 2026"`` label for the upload date badge."""
    moment = as_utc(value)
    if moment is None:
        return ""
    reference = as_utc(now) or utcnow()
    seconds = (reference - moment).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    if seconds < 172800:
        return "yesterday"
    days = int(seconds // 86400)
    if days < 30:
        return f"{days} days ago"
    if days < 365:
        months = max(1, days // 30)
        return f"{months} month{'s' if months > 1 else ''} ago"
    return moment.strftime("%d %b %Y")


def in_future(value, seconds: int = 60, now: Optional[datetime] = None) -> bool:
    """``True`` when ``value`` is more than ``seconds`` ahead of ``now``."""
    moment = as_utc(value)
    if moment is None:
        return False
    reference = as_utc(now) or utcnow()
    return moment > reference + timedelta(seconds=seconds)


def truncate(value, limit: int = MAX_STORED_NAME_LENGTH) -> str:
    """Hard cap for values stored in MongoDB."""
    text = str(value if value is not None else "")
    return text if len(text) <= limit else text[:limit]


def title_tokens(title: str) -> List[str]:
    """Lower-case word list (handy for fuzzy de-duplication checks)."""
    return [token for token in re.split(r"[^0-9a-z]+", str(title or "").lower()) if token]
