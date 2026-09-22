"""Fuzzy "did you mean?" title suggestions from the file database.

Why
---
When a user types a misspelled title (``pradhama drishtiya kuttakkar``) and
the exact-phrase search misses, the old flow sent the user to Google. This
module compares the typed text against titles that *actually exist* in the
file database and returns the closest matches, so the bot can answer with
coloured "did you mean" buttons (see ``plugins/pmfilter.py``) - or, when the
match is obviously right, silently re-search with the corrected title.

The pure helpers (``normalize_title`` / ``extract_title`` /
``rank_suggestions`` / ``choose_auto_pick``) have no I/O and are unit tested
in ``tests/test_title_suggest.py``.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Iterable

logger = logging.getLogger(__name__)

# --- tuning knobs (module constants, no env needed) -------------------------
SCAN_LIMIT = 5000          # how many recent files to compare against
NAME_CACHE_TTL = 300       # seconds to reuse the fetched file names
SUGGEST_LIMIT = 5          # maximum "did you mean" buttons
MIN_SCORE = 55             # fuzzy score (0-100) at which a candidate is shown
AUTO_PICK_SCORE = 90       # score at which a single candidate is "obvious"
AUTO_PICK_GAP = 6          # ...and must beat the runner-up by at least this

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
# Junk tokens that never belong to a title: containers, codecs, qualities,
# languages, sources, release-group noise. (Same idea as title_notify.)
_NOISE_RE = re.compile(
    r"^(?:mkv|mp4|avi|m4v|mov|webm|ts|"
    r"x264|x265|h264|h265|hevc|avc|aac|ac3|eac3|dts|truehd|flac|opus|mp3|"
    r"360p|480p|540p|720p|1080p|1440p|2160p|4k|8k|hdr|hdr10|sdr|10bit|8bit|atmos|"
    r"web|dl|webdl|webrip|bluray|brrip|bdrip|hdrip|dvdrip|hdtv|hdcam|hdts|cam|camrip|"
    r"predvd|telesync|tc|hd|sd|remux|"
    r"hindi|english|tamil|telugu|malayalam|kannada|bengali|marathi|gujarati|punjabi|urdu|"
    r"hin|eng|tam|tel|mal|kan|ben|mar|guj|pun|multi|dual|audio|sub|subs|subbed|dubbed|esubs|"
    r"full|movie|film|new|latest|official|proper|extended|imax|complete|"
    r"nf|amzn|prime|hotstar|zee5|sonyliv|jhs|aha|hbo|apple|dsnp|paramount|lionsgate)$"
)
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_EXT_RE = re.compile(r"\.(?:mkv|mp4|avi|m4v|mov|webm|ts)$", re.IGNORECASE)
# "s01e04" -> "s01 e04" so "Loki S01E04" matches "Loki S01 E04" file names
# (kept consistent with title_notify.normalize).
_SEASON_EP_RE = re.compile(r"\bs0*(\d+)e0*(\d+)\b")
# "season 1" / "episode 4" (plain words) -> the same canonical s01/e04 tokens.
_SEASON_WORD_RE = re.compile(r"\bseasons?\s*(\d+)\b")
_EPISODE_WORD_RE = re.compile(r"\bepisodes?\s*(\d+)\b")


def normalize_title(text) -> str:
    """Lowercase ``text`` and drop everything that is not the title.

    >>> normalize_title('Pushpa 2_The Rule (2024) 1080p WEB-DL x264.mkv')
    'pushpa 2 the rule'
    >>> normalize_title('Loki.S01E04.720p.mkv')
    'loki s01 e04'
    """
    if not text:
        return ""
    t = str(text).lower()
    t = _EXT_RE.sub(" ", t)
    t = _SEASON_EP_RE.sub(lambda m: "s%02d e%02d" % (int(m.group(1)), int(m.group(2))), t)
    t = _SEASON_WORD_RE.sub(lambda m: "s%02d" % int(m.group(1)), t)
    t = _EPISODE_WORD_RE.sub(lambda m: "e%02d" % int(m.group(1)), t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    tokens = [tok for tok in t.split() if not _NOISE_RE.match(tok)]
    # A trailing release year ("... 2025") is not part of the title.
    if len(tokens) > 1 and _YEAR_RE.match(tokens[-1]):
        tokens = tokens[:-1]
    return " ".join(tokens)


def extract_title(file_name) -> str:
    """A clean display title from a raw file name.

    >>> extract_title('Pushpa 2_The Rule (2024) 1080p WEB DL Hindi AAC x264.mkv')
    'Pushpa 2 The Rule'
    """
    if not file_name:
        return ""
    t = _EXT_RE.sub(" ", str(file_name)).lower()
    t = _SEASON_EP_RE.sub(lambda m: "s%02d e%02d" % (int(m.group(1)), int(m.group(2))), t)
    t = _SEASON_WORD_RE.sub(lambda m: "s%02d" % int(m.group(1)), t)
    t = _EPISODE_WORD_RE.sub(lambda m: "e%02d" % int(m.group(1)), t)
    tokens = [tok for tok in re.sub(r"[^a-z0-9]+", " ", t).split()
              if not _NOISE_RE.match(tok)]
    if len(tokens) > 1 and _YEAR_RE.match(tokens[-1]):
        tokens = tokens[:-1]
    if not tokens:
        return ""
    return " ".join(tok.capitalize() for tok in tokens).strip()


# ---------------------------------------------------------------------------
# Ranking (pure)
# ---------------------------------------------------------------------------
def _score(query_norm: str, cand_norm: str) -> int:
    from fuzzywuzzy import fuzz  # lazy: keeps the module importable without it

    if not query_norm or not cand_norm:
        return 0
    if query_norm == cand_norm:
        return 100
    return max(fuzz.ratio(query_norm, cand_norm), fuzz.token_sort_ratio(query_norm, cand_norm))


def _token_matched(query_token: str, cand_tokens) -> bool:
    """True when ``query_token`` is close to at least one candidate token.

    Fuzzy on purpose: the query itself is misspelled ("drishtiya" for
    "dristhiya"), so exact token equality would reject real matches.
    """
    from fuzzywuzzy import fuzz

    return any(fuzz.ratio(query_token, c) >= 78 for c in cand_tokens)


def rank_suggestions(query: str, candidates: Iterable[str],
                     limit: int = SUGGEST_LIMIT, min_score: int = MIN_SCORE):
    """Fuzzy-match ``query`` against raw titles/file names.

    Returns ``[(display_title, score), ...]`` best first, de-duplicated by
    normalized title. Candidates with no meaningful token overlap with the
    query are ignored so "devara" is never suggested for "pushpa".
    """
    q = normalize_title(query)
    if not q:
        return []
    q_tokens = q.split()
    best: dict[str, tuple[str, int]] = {}
    for raw in candidates or []:
        c = normalize_title(raw)
        if not c or len(c) < 3:
            continue
        c_tokens = set(c.split())
        # At least a third of the query words must resemble a candidate word
        # (fuzzily - the query itself is misspelled), otherwise it is a
        # different movie entirely.
        if q_tokens and c_tokens:
            overlap = sum(1 for t in q_tokens if _token_matched(t, c_tokens)) / len(q_tokens)
            if overlap < 0.34:
                continue
        score = _score(q, c)
        if score < min_score:
            continue
        if c not in best or score > best[c][1]:
            best[c] = (extract_title(raw) or c.title(), score)
    ranked = sorted(((title, score) for title, score in best.values()),
                    key=lambda item: (-item[1], item[0]))
    return ranked[:limit]


def choose_auto_pick(ranked) -> str | None:
    """Return the title to re-search silently, or ``None``.

    Only when one candidate is near-exact *and* clearly the best, so the bot
    "just fixes it" instead of showing buttons (anything less obvious is left
    to the user's choice).
    """
    if not ranked:
        return None
    title, score = ranked[0]
    if score < AUTO_PICK_SCORE:
        return None
    if len(ranked) > 1 and score - ranked[1][1] <= AUTO_PICK_GAP:
        return None
    return title


# ---------------------------------------------------------------------------
# Database access (async, lazy imports)
# ---------------------------------------------------------------------------
_name_cache = {"ts": 0.0, "names": []}


def clear_name_cache() -> None:
    """Drop the in-memory file-name cache (test hook)."""
    _name_cache["ts"] = 0.0
    _name_cache["names"] = []


async def fetch_file_names(limit: int = SCAN_LIMIT) -> list[str]:
    """Recent file names from the file DB (both DBs when ``MULTIPLE_DB``).

    The result is cached for ``NAME_CACHE_TTL`` seconds so a burst of misses
    does not re-scan the collection every time.
    """
    now = time.monotonic()
    if _name_cache["names"] and now - _name_cache["ts"] < NAME_CACHE_TTL:
        return _name_cache["names"]

    from database.ia_filterdb import Media, Media2, MULTIPLE_DB

    names: list[str] = []
    try:
        docs = await Media.find().sort("$natural", -1).limit(limit).to_list(length=limit)
        names.extend(d.file_name for d in docs if getattr(d, "file_name", None))
        if MULTIPLE_DB:
            docs2 = await Media2.find().sort("$natural", -1).limit(limit).to_list(length=limit)
            names.extend(d.file_name for d in docs2 if getattr(d, "file_name", None))
    except Exception as e:
        logger.warning("fetch_file_names failed: %s", e)
        return list(_name_cache["names"])  # stale beats nothing

    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            unique.append(name)
    _name_cache["ts"] = now
    _name_cache["names"] = unique
    return unique


async def suggest(chat_id, query: str, limit: int = SUGGEST_LIMIT):
    """Fuzzy matches for ``query`` against the file DB.

    Returns ``[(title, score), ...]`` - an empty list when nothing is close
    (or the feature is switched off / the DB is unreachable).
    """
    try:
        from info import DB_SUGGEST
    except Exception:  # pragma: no cover - info not importable in unit tests
        DB_SUGGEST = True
    if not DB_SUGGEST:
        return []
    names = await fetch_file_names()
    if not names:
        return []
    return rank_suggestions(query, names, limit=limit)


async def auto_pick(chat_id, query: str) -> str | None:
    """The single best title to re-search silently, or ``None``.

    This is the "bot directly fixes the spelling" path: only a near-exact,
    unambiguous match is picked (see :func:`choose_auto_pick`).
    """
    try:
        ranked = await suggest(chat_id, query, limit=10)
    except Exception as e:
        logger.warning("auto_pick failed: %s", e)
        return None
    return choose_auto_pick(ranked)
