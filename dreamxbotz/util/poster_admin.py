"""Admin helpers behind ``/posters`` and ``/setposter`` (Stream Mode posters).

"The poster is not showing – what do I do?"  This module answers that with:

* :func:`status_report` – what the poster pipeline needs and what it is
  missing (``TMDB_API_KEY``, switches, queue, the movies without artwork),
* :func:`retry_missing` – forget failed lookups and queue them again
  (typically right after the TMDB key was added),
* :func:`resolve_target` / :func:`set_manual_poster` / :func:`clear_manual_poster`
  – attach a poster by hand (an ``https://`` image URL or a Telegram photo the
  admin replied to) or remove it again.

Everything is free of Telegram imports so it can be unit-tested; the thin
pyrogram wrapper lives in ``plugins/poster_admin.py``.
"""
import html
import logging
import os
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from dreamxbotz.util.movie_titles import as_utc, clean_title_text, relative_time_label, utcnow

logger = logging.getLogger(__name__)

#: How many of the newest movies the report and the retry look at.
REPORT_WINDOW = 60
MAX_LISTED = 12


def esc(value) -> str:
    """HTML-escape user/database text before it goes into a Telegram message."""
    return html.escape(str(value if value is not None else ""), quote=False)


def _cfg(name: str, default):
    try:
        import info  # type: ignore

        return getattr(info, name, default)
    except Exception:
        return default


def _store():
    from database.recent_movies_db import recent_movies

    return recent_movies


def _web_base() -> str:
    """Public base URL of the bot's web server (``URL`` in info.py)."""
    return str(_cfg("URL", "") or "").strip().rstrip("/")


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
#: Variables whose name mentions TMDB but which are *not* the key.
_TMDB_TOGGLES = {"TMDB_POSTER"}


def key_state() -> Tuple[bool, str]:
    """``(usable, description)`` of the configured ``TMDB_API_KEY``.

    A wrong key is described precisely ("24 characters instead of 32",
    "contains 'r'") so a copy/paste slip is obvious from the ``/posters`` reply.
    """
    from dreamxbotz.util.tmdb_direct import clean_key, key_problem

    key = clean_key(_cfg("TMDB_API_KEY", ""))
    if not key:
        return False, "missing"
    problem = key_problem(key)
    if problem:
        return False, f"set, but it does not look like a TMDB key: {problem}"
    kind = "v4 token" if key.count(".") >= 2 else "v3 key"
    return True, f"set ({kind}, …{key[-4:]})"


def misnamed_key_variables(env=None) -> List[str]:
    """Environment variables that *mention* TMDB but are not one of the accepted key names.

    ``/posters`` lists them when the key is missing – the usual cause is a
    typo in the config panel (``TMDBAPI``, ``TMDB`` …).
    """
    env = os.environ if env is None else env
    try:
        import info  # type: ignore

        accepted = set(getattr(info, "TMDB_KEY_VARIABLES", ("TMDB_API_KEY",)))
    except Exception:
        accepted = {"TMDB_API_KEY"}
    found = []
    for name, value in env.items():
        upper = str(name).upper()
        if "TMDB" not in upper and "MOVIEDB" not in upper:
            continue
        normalised = re.sub(r"[^A-Z0-9]+", "_", upper).strip("_")
        if normalised in accepted or normalised in _TMDB_TOGGLES:
            continue
        if str(value or "").strip():
            found.append(str(name))
    return sorted(found)


async def collect_status(window: int = REPORT_WINDOW) -> Dict[str, Any]:
    """Raw facts for the report (also handy for tests and ``/stats``)."""
    from dreamxbotz.util import new_uploaded

    store = _store()
    rows = await store.list_recent(window, hard_limit=max(window, 20))
    missing = [row for row in rows if not row.get("poster_url")]
    manual = sum(1 for row in rows if row.get("poster_source") == new_uploaded.MANUAL_SOURCE)
    usable, key_text = key_state()
    return {
        "enabled": new_uploaded.is_enabled(),
        "poster_fetch": bool(_cfg("NEW_UPLOADED_POSTER_FETCH", True)),
        "tmdb_helper": bool(_cfg("TMDB_POSTER", True)),
        "tmdb_key_ok": usable,
        "tmdb_key": key_text,
        "tmdb_key_set": key_text != "missing",
        "misnamed_vars": misnamed_key_variables() if key_text == "missing" else [],
        "retry_hours": int(_cfg("NEW_UPLOADED_POSTER_RETRY_HOURS", 48) or 48),
        "window": len(rows),
        "with_poster": len(rows) - len(missing),
        "missing": missing,
        "manual": manual,
        "queue": new_uploaded.stats(),
        "total": await store.count(),
    }


def _checked_label(doc: Dict[str, Any], retry_hours: int) -> str:
    checked = as_utc(doc.get("poster_checked_at"))
    if checked is None:
        return "not looked up yet"
    if (utcnow() - checked) > timedelta(hours=retry_hours):
        return "lookup due again"
    return f"looked up {relative_time_label(checked)}"


def format_report(status: Dict[str, Any]) -> str:
    """Human readable ``/posters`` answer (HTML, Telegram-safe)."""
    def onoff(flag: bool) -> str:
        return "✅ on" if flag else "❌ off"

    lines = ["<b>🖼 Stream Mode · poster status</b>", ""]
    lines.append(f"Section: {onoff(status['enabled'])} · auto poster lookup: {onoff(status['poster_fetch'])}")
    key_icon = "✅" if status["tmdb_key_ok"] else "❌"
    lines.append(f"TMDB_API_KEY: {key_icon} {status['tmdb_key']}")
    lines.append(
        "Sources: TMDB helper ({}) → TMDB direct ({}) → IMDb (no key needed)".format(
            "on" if status["tmdb_helper"] else "off",
            "on" if status["tmdb_key_ok"] else "needs key",
        )
    )
    lines.append("")
    window = status["window"]
    lines.append(
        f"Newest {window} movies: <b>{status['with_poster']}</b> with poster · "
        f"<b>{len(status['missing'])}</b> without"
        + (f" · {status['manual']} set by hand" if status["manual"] else "")
        + f" · {status['total']} in the collection"
    )
    queue = status.get("queue") or {}
    lines.append(
        f"Worker: {queue.get('poster_queue', 0)} lookups waiting · "
        f"{queue.get('upload_queue', 0)} uploads waiting · {queue.get('processed', 0)} processed"
    )

    missing = status["missing"]
    if missing:
        lines.append("")
        lines.append("<b>Without poster:</b>")
        for doc in missing[:MAX_LISTED]:
            lines.append(f"• {describe(doc)} — {_checked_label(doc, status['retry_hours'])}")
        if len(missing) > MAX_LISTED:
            lines.append(f"… and {len(missing) - MAX_LISTED} more")

    lines.append("")
    lines.append("<b>How to get posters:</b>")
    step = 1
    if not status["tmdb_key_ok"]:
        if status.get("tmdb_key_set"):
            lines.append(
                f"{step}. <code>TMDB_API_KEY</code> is set but wrong ({esc(status['tmdb_key'].split(': ', 1)[-1])}). "
                "Open themoviedb.org → Settings → API and paste the <b>API Key</b> again exactly – "
                "32 characters, only 0-9 and a-f, no spaces, quotes or <code>*</code> – "
                "then restart the bot and send <code>/posters retry</code>."
            )
        else:
            hint = ""
            if status.get("misnamed_vars"):
                names = ", ".join(f"<code>{esc(n)}</code>" for n in status["misnamed_vars"][:3])
                hint = f" Found {names} – the variable must be called <code>TMDB_API_KEY</code>."
            lines.append(
                f"{step}. Set <code>TMDB_API_KEY</code> (free: themoviedb.org → Settings → API), "
                f"restart the bot, then send <code>/posters retry</code>.{hint}"
            )
        step += 1
    if not status["poster_fetch"]:
        lines.append(f"{step}. Set <code>NEW_UPLOADED_POSTER_FETCH=True</code> and restart.")
        step += 1
    lines.append(
        f"{step}. Use clean release names – <i>Movie Name (2024) 1080p …</i> – a made-up name has no poster anywhere."
    )
    step += 1
    lines.append(
        f"{step}. Set one by hand: reply to a poster photo with <code>/setposter MOVIE_ID</code>, "
        "or <code>/setposter MOVIE_ID https://image.tmdb.org/…jpg</code>. "
        "<code>/delposter MOVIE_ID</code> removes it again."
    )
    lines.append(f"{step + 1}. <code>/posters retry</code> re-queues every movie above right away.")
    return "\n".join(lines)


async def status_report(window: int = REPORT_WINDOW) -> str:
    return format_report(await collect_status(window))


async def retry_missing(window: int = REPORT_WINDOW) -> Tuple[int, int]:
    """Forget failed lookups + queue them again → ``(reset, queued)``."""
    from dreamxbotz.util import new_uploaded

    reset = await _store().reset_poster_checks(window)
    queued = await new_uploaded.refresh_posters(window)
    return reset, queued


# --------------------------------------------------------------------------- #
# Manual posters
# --------------------------------------------------------------------------- #
def split_setposter_args(text: str) -> Tuple[str, str]:
    """``"/setposter hmm-2024 https://x/y.jpg"`` → ``("hmm-2024", "https://x/y.jpg")``."""
    parts = str(text or "").split()
    if parts and parts[0].startswith("/"):
        parts = parts[1:]
    url = ""
    if parts and parts[-1].lower().startswith(("http://", "https://")):
        url = parts.pop()
    return " ".join(parts).strip(), url


def validate_poster_url(url: str) -> Tuple[bool, str]:
    """``(ok, reason)`` – https + allow-listed host (SSRF guard of the proxy)."""
    from dreamxbotz.server.movie_api import _poster_hosts, poster_host_allowed

    url = str(url or "").strip()
    if not url:
        return False, "no URL given"
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "that is not a valid URL"
    if parsed.scheme != "https":
        return False, "only https:// links are accepted"
    if len(url) > 600:
        return False, "the link is too long (max 600 characters)"
    if not poster_host_allowed(url):
        hosts = ", ".join(_poster_hosts()[:6])
        return False, (
            f"host “{esc(parsed.hostname)}” is not allow-listed. Use an image from {hosts} …, "
            "add the host to NEW_UPLOADED_POSTER_HOSTS, or simply reply to the poster photo instead."
        )
    return True, ""


async def resolve_target(text: str) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(movie, candidates)`` for a typed ``MOVIE_ID`` or title.

    ``movie`` is set when the text identifies exactly one movie; otherwise
    ``candidates`` lists what was found (possibly empty).
    """
    rows = await _store().find_by_title(text)
    if len(rows) == 1:
        return rows[0], rows
    safe = str(text or "").strip().lower()
    for row in rows:
        if str(row.get("_id", "")).lower() == safe:
            return row, rows
    return None, rows


def describe(doc: Dict[str, Any]) -> str:
    """``Title (year) — <code>MOVIE_ID</code>`` (escaped for HTML parse mode)."""
    title = esc(clean_title_text(doc.get("title") or "", limit=60) or "?")
    year = f" ({doc.get('year')})" if doc.get("year") else ""
    return f"{title}{year} — <code>{esc(doc.get('_id'))}</code>"


def poster_preview_url(movie_id: str) -> str:
    base = _web_base()
    return f"{base}/api/movies/poster/{movie_id}" if base else f"/api/movies/poster/{movie_id}"


async def set_manual_poster(movie_id: str, poster_ref: str) -> bool:
    """Store ``https://…`` or ``tg://file/<id>`` as the movie's poster."""
    from dreamxbotz.server import movie_api
    from dreamxbotz.util import new_uploaded

    ok = await _store().set_poster(movie_id, poster_ref, new_uploaded.MANUAL_SOURCE)
    if ok:
        movie_api.evict_cached(movie_id)
        try:  # the watch-page hero caches artwork separately – refresh it too
            from database.movie_art_db import movie_art

            doc = await _store().get(movie_id)
            await movie_art.set_art(
                movie_id,
                title=(doc or {}).get("title") or "",
                year=(doc or {}).get("year"),
                poster_url=poster_ref,
                backdrop_url=None,
                source=new_uploaded.MANUAL_SOURCE,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("movie_art refresh after /setposter failed: %s", exc)
    return ok


async def clear_manual_poster(movie_id: str) -> bool:
    from dreamxbotz.server import movie_api

    ok = await _store().clear_poster(movie_id)
    if ok:
        movie_api.evict_cached(movie_id)
    return ok


def telegram_poster_ref(reply) -> Tuple[str, str]:
    """``(tg://file/<id>, "")`` for a replied photo / image document, else ``("", reason)``."""
    if reply is None:
        return "", ""
    photo = getattr(reply, "photo", None)
    if photo is not None and getattr(photo, "file_id", None):
        return f"tg://file/{photo.file_id}", ""
    document = getattr(reply, "document", None)
    if document is not None and getattr(document, "file_id", None):
        mime = str(getattr(document, "mime_type", "") or "")
        size = int(getattr(document, "file_size", 0) or 0)
        if not mime.startswith("image/"):
            return "", "the replied file is not an image"
        if size > 8 * 1024 * 1024:
            return "", "the image is larger than 8 MB"
        return f"tg://file/{document.file_id}", ""
    return "", "reply to a photo (or send an https:// image link)"
