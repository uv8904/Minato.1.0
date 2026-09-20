"""AI movie-title spell correction.

Primary: Groq Chat Completions (OpenAI-compatible HTTP API).
Fallback: IMDb title search + fuzzy match (same idea as the old inline helper).
"""
from __future__ import annotations

import logging
import re

import aiohttp

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM_PROMPT = (
    "You correct misspelled movie and TV series titles. "
    "Reply with ONLY the corrected official English title. "
    "No quotes, no explanation, no extra punctuation. "
    "Keep a year only if the user included one. "
    "If the title is already correct, repeat it unchanged. "
    "If you cannot tell, repeat the original text."
)


def _clean(text: str) -> str:
    text = (text or "").strip().strip("\"'`")
    text = re.sub(r"\s+", " ", text)
    return text


async def groq_correct(query: str) -> str | None:
    """Ask Groq to fix a movie/series title. Returns None if unavailable."""
    from info import GROQ_API_KEY, GROQ_MODEL

    query = _clean(query)
    if not query or not GROQ_API_KEY:
        return None

    payload = {
        "model": GROQ_MODEL,
        "temperature": 0.1,
        "max_tokens": 40,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ],
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(GROQ_URL, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Groq spell HTTP %s: %s", resp.status, body[:300])
                    return None
                data = await resp.json()
        choice = (data.get("choices") or [{}])[0]
        content = ((choice.get("message") or {}).get("content") or "").strip()
        content = _clean(content.splitlines()[0] if content else "")
        if not content or content.lower() in {"none", "n/a", "unknown"}:
            return None
        return content
    except Exception as e:
        logger.warning("Groq spell failed: %s", e)
        return None


async def imdb_fallback(wrong_name: str, chat_id=None) -> str | None:
    """IMDb + fuzzywuzzy fallback. Optionally require the title to exist in the file DB."""
    from fuzzywuzzy import process
    from utils import imdb

    wrong_name = _clean(wrong_name)
    if not wrong_name:
        return None
    try:
        results = imdb.search_movie(wrong_name) or []
    except Exception as e:
        logger.warning("IMDb search failed: %s", e)
        return None
    movie_list = [m.get("title") for m in results if m.get("title")]
    if not movie_list:
        return None

    for _ in range(min(5, len(movie_list))):
        closest = process.extractOne(wrong_name, movie_list)
        if not closest or closest[1] <= 80:
            return None
        movie = closest[0]
        if chat_id is None:
            return movie
        try:
            from database.ia_filterdb import get_search_results
            files, _, _ = await get_search_results(chat_id=chat_id, query=movie)
            if files:
                return movie
        except Exception as e:
            logger.warning("DB check during IMDb fallback failed: %s", e)
            return movie
        movie_list.remove(movie)
    return None


async def correct_title(query: str, chat_id=None) -> str | None:
    """Groq first, IMDb fallback. Returns a better title or None."""
    from info import AI_SPELL_CHECK

    query = _clean(query)
    if not query or not AI_SPELL_CHECK:
        return None

    groq = await groq_correct(query)
    if groq and groq.lower() != query.lower():
        if chat_id is None:
            return groq
        try:
            from database.ia_filterdb import get_search_results
            files, _, _ = await get_search_results(chat_id=chat_id, query=groq)
            if files:
                return groq
        except Exception as e:
            logger.warning("DB check during Groq correction failed: %s", e)
            return groq
        # Title is corrected but not in the file DB — still useful for /aispell,
        # but auto_filter (chat_id set) must not recurse on a miss.

    return await imdb_fallback(query, chat_id=chat_id)
