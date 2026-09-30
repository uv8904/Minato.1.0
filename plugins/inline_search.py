"""Inline Mode · ``@YourBot <movie>`` — Option B.

What it does
------------
Type ``@YourBot jawan`` in **any** chat (PM, a group, even another bot's chat)
and Telegram asks this handler for results.  The bot searches its own file
index and answers with poster cards — title, year, quality, how many files and
their size — each one carrying:

* **▶️ Play / Download** → ``https://t.me/<BOT_USERNAME>?start=movie_<MOVIE_ID>``
  (``plugins/commands.py`` resolves that payload and runs the search *inside*
  the bot, so premium / FSub / verification rules still apply),
* **🌐 Open website** → the OTT page this project ships (``/search?q=…``),
* **🔗 Share** → re-opens inline mode with the same title.

Nothing sensitive leaves the bot: an inline result never contains a file id, a
download URL or the token — the buttons are public links, and tapping a card in
a group simply *sends that card*, exactly like any other inline bot.

Configuration (``info.py`` / environment)
-----------------------------------------
``INLINE_SEARCH``              master switch (default ``True``)
``INLINE_SEARCH_LIMIT``        movie cards per query (default 20, cap 50)
``INLINE_SEARCH_POSTERS``      poster cards on/off (falls back to text cards)
``INLINE_SEARCH_GROUPS``       allow inline use outside PM
``INLINE_SEARCH_TOP``          results for an *empty* query (top searches)
``INLINE_SEARCH_CACHE_TTL``    Telegram-side answer cache (default 60 s)
``INLINE_SEARCH_MAX_QUERY``    longest accepted query (default 64 chars)
``INLINE_SEARCH_MAX_REQUESTS`` per-user queries per hour (default 240, 0 = ∞)

Reference: docs/INLINE_SEARCH.md
"""
import base64
import logging
import re
import time
from collections import deque
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote_plus

from pyrogram import Client, enums, filters
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResult,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InputTextMessageContent,
)

from dreamxbotz.util.file_labels import normalize_file_name
from dreamxbotz.util.movie_titles import (
    build_deeplink,
    clean_title_text,
    looks_like_series,
    movie_id_for,
    normalize_title,
    parse_release_name,
    primary_quality,
    quality_label,
    sanitize_movie_id,
)
from info import (
    INLINE_SEARCH,
    INLINE_SEARCH_CACHE_TTL,
    INLINE_SEARCH_GROUPS,
    INLINE_SEARCH_LIMIT,
    INLINE_SEARCH_MAX_QUERY,
    INLINE_SEARCH_MAX_REQUESTS,
    INLINE_SEARCH_POSTERS,
    INLINE_SEARCH_TOP,
)

logger = logging.getLogger(__name__)

#: Telegram photo cards need a public HTTP(S) image; ``tg://file/…`` posters
#: (attached by an admin with ``/setposter``) cannot be fetched by Telegram.
URL_RE = re.compile(r"^https?://", re.IGNORECASE)
CONTROL_RE = re.compile(r"[\u0000-\u001f\u007f]")
MAX_TRACKED_USERS = 5000
MAX_FILES_PER_MOVIE = 40
#: ``user_id -> deque[monotonic timestamps]`` backing the per-user rate limit.
_RATE: Dict[int, deque] = {}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _bot_username(client) -> str:
    """The public @username of the bot (never the token)."""
    try:
        username = getattr(getattr(client, "me", None), "username", None)
        if username:
            return str(username).strip().lstrip("@")
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        from utils import temp  # type: ignore

        return str(getattr(temp, "U_NAME", "") or "").strip().lstrip("@")
    except Exception:
        return ""


def _bot_name(client) -> str:
    try:
        name = getattr(getattr(client, "me", None), "first_name", None)
        if name:
            return clean_title_text(name, limit=40)
    except Exception:  # pragma: no cover - defensive
        pass
    return "the bot"


def site_search_url(query: str = "") -> str:
    """Link to the OTT search page (empty when the bot has no public URL)."""
    try:
        from info import URL  # type: ignore

        base = str(URL or "").strip().rstrip("/")
        if not base:
            return ""
        try:
            from info import OTT_SEARCH_PATH  # type: ignore

            path = str(OTT_SEARCH_PATH or "/search")
        except Exception:
            path = "/search"
        path = path if path.startswith("/") else "/" + path
        return f"{base}{path}" + (f"?q={quote_plus(str(query)[:80])}" if query else "")
    except Exception:
        return ""


def sanitize_query(raw: Any) -> str:
    """Clean one inline query: control characters out, length capped."""
    text = CONTROL_RE.sub(" ", str(raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if text.startswith("@") and " " in text:  # "@MovieBot jawan" → "jawan"
        text = text.split(" ", 1)[1].strip()
    if len(text) > INLINE_SEARCH_MAX_QUERY:
        text = text[:INLINE_SEARCH_MAX_QUERY].strip()
    return text


def rate_limited(user_id: int, *, now: Optional[float] = None) -> bool:
    """Per-user query budget (``INLINE_SEARCH_MAX_REQUESTS`` per hour)."""
    limit = int(INLINE_SEARCH_MAX_REQUESTS or 0)
    if limit <= 0:
        return False
    moment = now if now is not None else time.monotonic()
    bucket = _RATE.get(int(user_id))
    if bucket is None:
        if len(_RATE) >= MAX_TRACKED_USERS:  # cheap eviction of the oldest keys
            for key in list(_RATE)[: MAX_TRACKED_USERS // 10]:
                _RATE.pop(key, None)
        bucket = deque(maxlen=limit + 5)
        _RATE[int(user_id)] = bucket
    while bucket and moment - bucket[0] > 3600:
        bucket.popleft()
    if len(bucket) >= limit:
        return True
    bucket.append(moment)
    return False


def reset_rate_limits() -> None:
    """Forget the rate-limit buckets (tests)."""
    _RATE.clear()


def movie_groups(files: Sequence[Any], limit: int = 20) -> List[Dict[str, Any]]:
    """Group raw file rows into movie cards (pure – easy to unit test).

    Files of the same movie (``Jawan 2023 1080p`` + ``Jawan 2023 720p``) become
    **one** card that lists both qualities, so ``@bot jawan`` shows *Jawan*
    once instead of twice.  Series episodes keep their own card but are flagged
    so the caption can say "Series".  Order follows the search order, which is
    already relevance-ordered by the file database.
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for file in files or []:
        name = normalize_file_name(getattr(file, "file_name", None) or "")
        if not name:
            continue
        parsed = parse_release_name(name)
        title = clean_title_text(parsed.get("title") or name, limit=80) or name
        year = parsed.get("year")
        title, year, _key = normalize_title(title, year)
        movie_id = sanitize_movie_id(movie_id_for(title, year) or "")
        if not movie_id:
            continue
        quality = parsed.get("quality") or ""
        size = int(getattr(file, "file_size", 0) or 0)
        row = grouped.get(movie_id)
        if row is None:
            row = {
                "id": movie_id,
                "movie_id": movie_id,
                "title": title or "Untitled",
                "year": year,
                "qualities": [],
                "files": 0,
                "bytes": 0,
                "is_series": bool(parsed.get("is_series")) or looks_like_series(name),
                "sample": clean_title_text(name, limit=72),
            }
            grouped[movie_id] = row
            order.append(movie_id)
        if row["files"] >= MAX_FILES_PER_MOVIE:
            continue
        row["files"] += 1
        row["bytes"] += size
        if quality and quality not in row["qualities"]:
            row["qualities"].append(quality)
    return [grouped[movie_id] for movie_id in order][: max(1, int(limit))]


async def posters_for(movie_ids: Sequence[str]) -> Dict[str, str]:
    """``{MOVIE_ID: poster_url}`` – one database round trip, never raises."""
    ids = [sanitize_movie_id(movie_id) for movie_id in movie_ids or []]
    ids = [movie_id for movie_id in ids if movie_id]
    if not ids:
        return {}
    try:
        from database.recent_movies_db import recent_movies

        cursor = recent_movies.col.find({"_id": {"$in": ids}}, {"poster_url": 1})
        rows = [doc async for doc in cursor]
    except Exception as exc:  # pragma: no cover - DB down / not configured
        logger.debug("Inline search: poster lookup failed: %s", exc)
        return {}
    out = {}
    for doc in rows:
        url = str(doc.get("poster_url") or "")
        if URL_RE.match(url):  # Telegram must be able to fetch it itself
            out[str(doc.get("_id"))] = url
    return out


async def search_movies(query: str, limit: int) -> List[Dict[str, Any]]:
    """Run the bot's own file search and group the result into movie cards."""
    from database.ia_filterdb import get_search_results

    try:
        files, _offset, _total = await get_search_results(
            chat_id=None, query=query, offset=0, max_results=max(10, int(limit) * 4), filter=True
        )
    except Exception as exc:
        logger.warning("Inline search failed for %r: %s", query, exc)
        return []
    return movie_groups(files, limit=limit)


async def top_queries(limit: int) -> List[str]:
    """The bot's own "top searches" list (used for an empty inline query)."""
    if not limit:
        return []
    try:
        from database.config_db import mdb

        rows = await mdb.get_top_messages(max(2, int(limit)))
    except Exception:
        return []
    out = []
    for row in rows or []:
        text = sanitize_query(row)
        if 2 <= len(text) <= 40 and not text.startswith(("/", "#", "http")):
            out.append(text)
    return out[: max(1, int(limit))]


def start_search_link(username: str, query: str) -> str:
    """``https://t.me/<bot>?start=msrch_<b64url>`` – opens the bot *and* searches.

    ``plugins/commands.py`` understands the ``msrch_`` payload and runs the
    query straight away, which is what the "open in the bot" buttons use.
    """
    if not username:
        return ""
    raw = base64.urlsafe_b64encode(str(query or "").encode("utf-8")).decode("ascii").rstrip("=")
    return f"https://t.me/{username}?start=msrch_{raw[:64]}"


# --------------------------------------------------------------------------- #
# Result building (pure)
# --------------------------------------------------------------------------- #
def build_movie_result(
    movie: Dict[str, Any],
    *,
    index: int = 0,
    bot_username: str = "",
    bot_name: str = "the bot",
    poster: str = "",
    site_url: str = "",
    use_posters: Optional[bool] = None,
    query: str = "",
) -> Optional[InlineQueryResult]:
    """One movie → one inline result (a photo card, or a text card)."""
    movie_id = sanitize_movie_id(movie.get("movie_id") or movie.get("id") or "")
    if not movie_id:
        return None
    title = clean_title_text(movie.get("title") or "Untitled", limit=80)
    year = movie.get("year")
    qualities = list(movie.get("qualities") or [])
    quality = primary_quality(qualities)
    quality_text = quality_label(qualities) or "—"
    files = int(movie.get("files") or 0)
    size_mb = round((movie.get("bytes") or 0) / (1024 * 1024))
    series = bool(movie.get("is_series"))

    headline = f"{title} ({year})" if year else title
    chips = [part for part in [
        str(year) if year else "",
        str(quality).upper() if quality else "",
        f"{files} file{'s' if files != 1 else ''}" if files else "",
        f"{size_mb} MB" if size_mb else "",
        "Series" if series else "",
    ] if part]
    description = " · ".join([part for part in [quality_text, f"{files} file(s)" if files else "", "Series" if series else ""] if part])
    sample = clean_title_text(movie.get("sample") or "", limit=72)
    caption = (
        f"<b>{headline}</b>\n"
        f"<blockquote>{' · '.join(chips)}</blockquote>\n"
        f"🎞 <b>{quality_text}</b>"
        + (f" · 📦 <b>{files}</b> file{'s' if files != 1 else ''}" if files else "")
        + (f"\n📄 <i>{sample}</i>" if sample else "")
        + f"\n\n▶️ ᴛᴀᴘ ᴛʜᴇ ʙᴜᴛᴛᴏɴ ᴛᴏ ᴏᴘᴇɴ ᴛʜɪs ᴍᴏᴠɪᴇ ɪɴ @{bot_username or bot_name}."
    )
    buttons: List[List[InlineKeyboardButton]] = []
    deeplink = build_deeplink(bot_username, movie_id)
    if deeplink:
        buttons.append([InlineKeyboardButton("▶️ Play / Download", url=deeplink)])
    row: List[InlineKeyboardButton] = []
    site = site_url or site_search_url(title)
    if site:
        row.append(InlineKeyboardButton("🌐 Watch on website", url=site))
    row.append(InlineKeyboardButton("🔗 Share", switch_inline_query=title))
    if row:
        buttons.append(row)
    keyboard = InlineKeyboardMarkup(buttons) if buttons else None
    result_id = f"inl{index}-{movie_id}"[:64]

    if use_posters is None:
        use_posters = bool(INLINE_SEARCH_POSTERS)
    if use_posters and URL_RE.match(poster or ""):
        return InlineQueryResultPhoto(
            photo_url=poster,
            thumb_url=poster,
            id=result_id,
            title=headline,
            description=description or title,
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=keyboard,
        )
    return InlineQueryResultArticle(
        title=headline,
        description=description or query or title,
        id=result_id,
        input_message_content=InputTextMessageContent(
            caption, parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True
        ),
        reply_markup=keyboard,
    )


def build_results(
    movies: Sequence[Dict[str, Any]],
    *,
    query: str = "",
    bot_username: str = "",
    bot_name: str = "the bot",
    posters: Optional[Dict[str, str]] = None,
    site_url: str = "",
    use_posters: Optional[bool] = None,
) -> List[InlineQueryResult]:
    """Turn movie cards into Telegram inline results (pure – no network)."""
    posters = posters or {}
    results: List[InlineQueryResult] = []
    for index, movie in enumerate(movies or []):
        result = build_movie_result(
            movie,
            index=index,
            bot_username=bot_username,
            bot_name=bot_name,
            poster=posters.get(sanitize_movie_id(movie.get("movie_id") or movie.get("id") or "") or "", ""),
            site_url=site_url,
            use_posters=use_posters,
            query=query,
        )
        if result is not None:
            results.append(result)
    return results


def note_result(
    title: str,
    text: str,
    *,
    description: str = "",
    result_id: str = "inl-note",
    buttons: Optional[Sequence[Sequence[InlineKeyboardButton]]] = None,
) -> InlineQueryResultArticle:
    """One informational result (empty / rate-limited / error states)."""
    rows = [list(row) for row in (buttons or []) if row]
    return InlineQueryResultArticle(
        title=title[:120],
        description=description[:180],
        id=result_id[:64],
        input_message_content=InputTextMessageContent(
            text[:1000], parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True
        ),
        reply_markup=InlineKeyboardMarkup(rows) if rows else None,
    )


async def _answer(query: InlineQuery, results: List[InlineQueryResult], **kwargs) -> None:
    """Answer defensively – a failed inline answer must never crash the bot."""
    try:
        await query.answer(
            results[:50],
            cache_time=max(0, int(INLINE_SEARCH_CACHE_TTL or 0)),
            is_personal=True,
            **kwargs,
        )
    except Exception as exc:  # pragma: no cover - Telegram/network hiccup
        logger.warning("Inline answer failed: %s", exc)


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #
@Client.on_inline_query()
async def inline_search(client: Client, query: InlineQuery):
    """``@bot <movie>`` → poster cards of the movies in the bot's index."""
    user_id = int(getattr(getattr(query, "from_user", None), "id", 0) or 0)
    username = _bot_username(client)
    name = _bot_name(client)
    open_bot = f"https://t.me/{username}" if username else "https://t.me"
    open_button = InlineKeyboardButton("🤖 Open the bot", url=open_bot)

    if not INLINE_SEARCH:
        return await _answer(
            query,
            [
                note_result(
                    "Inline search is disabled",
                    f"<b>ℹ️ Inline search is off.</b>\n\nSend the movie name to @{username or name} directly.",
                    description="The owner turned inline mode off",
                    buttons=[[open_button]],
                )
            ],
        )

    chat_type = str(getattr(getattr(query, "chat_type", None), "value", "") or "")
    if not INLINE_SEARCH_GROUPS and chat_type not in ("private", "bot"):
        return await _answer(
            query,
            [
                note_result(
                    "Search in private chat",
                    f"<b>🔒 Inline search works in private chat.</b>\n\nOpen @{username or name} and search there.",
                    description="This chat is not allowed",
                    buttons=[[open_button]],
                )
            ],
            switch_pm_text="Open the bot",
            switch_pm_parameter="inline",
        )

    if rate_limited(user_id):
        return await _answer(
            query,
            [
                note_result(
                    "Slow down a little 🐢",
                    "<b>⏳ Too many inline searches in the last hour.</b>\n\nTry again in a bit, or search inside the bot.",
                    description="Rate limit reached",
                    buttons=[[open_button]],
                )
            ],
        )

    text = sanitize_query(getattr(query, "query", ""))
    if not text:
        trends = await top_queries(INLINE_SEARCH_TOP)
        movies: List[Dict[str, Any]] = []
        for trend in trends:
            for movie in await search_movies(trend, 2):
                if all(movie["movie_id"] != other["movie_id"] for other in movies):
                    movies.append(movie)
        if movies:
            posters = await posters_for([movie["movie_id"] for movie in movies])
            results = build_results(
                movies, query="", bot_username=username, bot_name=name, posters=posters
            )
            results.append(
                note_result(
                    "🔍 Search a movie",
                    f"<b>@{username or name} · inline search</b>\n\n"
                    f"Type a movie name after the bot's username, e.g. <code>@{username or name} jawan</code>.",
                    description="Top searches of the week",
                    result_id="inl-hint",
                    buttons=[[open_button]],
                )
            )
            return await _answer(query, results, switch_pm_text=f"Open {name}", switch_pm_parameter="inline")
        return await _answer(
            query,
            [
                note_result(
                    "🔍 Search a movie",
                    f"<b>Type a movie name</b>, e.g. <code>@{username or name} jawan</code> — "
                    "I will show every file I have for it.",
                    description="Start typing a title",
                    buttons=[[open_button]],
                )
            ],
            switch_pm_text=f"Open {name}",
            switch_pm_parameter="inline",
        )

    limit = max(1, int(INLINE_SEARCH_LIMIT or 20))
    movies = await search_movies(text, limit)
    if not movies:
        site = site_search_url(text)
        buttons: List[List[InlineKeyboardButton]] = []
        deep = start_search_link(username, text)
        if deep:
            buttons.append([InlineKeyboardButton("🤖 Search in the bot", url=deep)])
        if site:
            buttons.append([InlineKeyboardButton("🌐 Search on the website", url=site)])
        buttons.append([InlineKeyboardButton("🔗 Share", switch_inline_query=text)])
        return await _answer(
            query,
            [
                note_result(
                    f"😕 No file for “{text}”",
                    f"<b>😕 I don't have <i>{text}</i> yet.</b>\n\n"
                    "<blockquote>Try a shorter title, drop the year, or ask for it in the group — "
                    "new uploads are indexed automatically.</blockquote>",
                    description="Nothing matched in the file index",
                    result_id="inl-empty",
                    buttons=buttons,
                )
            ],
            switch_pm_text=f"Search “{text}” in {name}",
            switch_pm_parameter="inline",
        )

    posters = await posters_for([movie["movie_id"] for movie in movies])
    results = build_results(
        movies, query=text, bot_username=username, bot_name=name, posters=posters
    )
    deep = start_search_link(username, text)
    more_buttons: List[List[InlineKeyboardButton]] = []
    if deep:
        more_buttons.append([InlineKeyboardButton("🤖 Open the full list", url=deep)])
    more_buttons.append([InlineKeyboardButton("➕ Add me to a group", url=f"{open_bot}?startgroup=true")])
    results.append(
        note_result(
            f"🔎 All results for “{text}”",
            f"<b>🔎 {len(movies)} title(s) for <i>{text}</i></b>\n\n"
            f"Indexed from the MinatoVerse library · {time.strftime('%d %b')}\n"
            "Tap a card's <b>▶️ Play</b> button to open that exact movie in the bot.",
            description="Open the bot for the complete list",
            result_id="inl-more",
            buttons=more_buttons,
        )
    )
    await _answer(query, results, switch_pm_text=f"Search “{text}” in {name}", switch_pm_parameter="inline")
    logger.info("Inline search by %s: %r → %s title(s)", user_id, text, len(movies))


@Client.on_message(filters.command(["inline", "inlinehelp"]) & filters.incoming)
async def inline_help(client: Client, message):
    """``/inline`` – how to use the bot in any chat."""
    username = _bot_username(client) or _bot_name(client)
    if not INLINE_SEARCH:
        return await message.reply_text(
            "<b>ℹ️ Inline search is currently disabled by the owner.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    text = (
        "<b>⚡ ɪɴʟɪɴᴇ sᴇᴀʀᴄʜ</b>\n\n"
        "ᴜsᴇ ᴍᴇ ɪɴ <b>ᴀɴʏ ᴄʜᴀᴛ</b> ᴡɪᴛʜᴏᴜᴛ ᴏᴘᴇɴɪɴɢ ᴀ ᴘʀɪᴠᴀᴛᴇ ᴄʜᴀᴛ:\n\n"
        f"<blockquote>1️⃣ ᴛʏᴘᴇ <code>@{username}</code> ɪɴ ᴛʜᴇ ᴍᴇssᴀɢᴇ ʙᴏx\n"
        "2️⃣ ᴛʏᴘᴇ ᴛʜᴇ ᴍᴏᴠɪᴇ ɴᴀᴍᴇ, ᴇ.ɢ. <code>jawan</code>\n"
        "3️⃣ ᴛᴀᴘ ᴀ ᴘᴏsᴛᴇʀ ᴄᴀʀᴅ ᴛᴏ sᴇɴᴅ ɪᴛ, ᴛʜᴇɴ <b>▶️ ᴘʟᴀʏ</b></blockquote>\n"
        "ᴇᴠᴇʀʏ ᴄᴀʀᴅ ᴏᴘᴇɴs ᴛʜᴀᴛ ᴇxᴀᴄᴛ ᴍᴏᴠɪᴇ ʜᴇʀᴇ ɪɴ ᴛʜᴇ ʙᴏᴛ — ᴘʀᴇᴍɪᴜᴍ ᴀɴᴅ ɢʀᴏᴜᴘ ʀᴜʟᴇs sᴛɪʟʟ ᴀᴘᴘʟʏ."
    )
    buttons = [[InlineKeyboardButton("🔍 Try it now", switch_inline_query_current_chat="")]]
    if username:
        buttons.append([InlineKeyboardButton("🤖 Open the bot", url=f"https://t.me/{username}")])
    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )
