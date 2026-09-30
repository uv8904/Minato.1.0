# ⚡ Inline Search — `@YourBot jawan` in *any* chat

Type the bot's username in **any** Telegram chat (a private chat, a group, even
another bot's chat), keep typing a movie name, and Telegram asks this bot for
results. The bot searches its own file index and answers with poster cards —
each one opens that exact movie *inside* the bot, so premium, FSub and
verification rules still apply.

```
        user types "@MyMovieBot jawan"  (any chat)
                       │
                       ▼
        plugins/inline_search.py::inline_search()
                       │
        ┌──────────────┼───────────────────────────┐
        ▼              ▼                           ▼
   sanitize_query   rate_limited()          search_movies()
   (control chars,  (INLINE_SEARCH_MAX_     ia_filterdb.get_search_results(
    length, "@bot "  REQUESTS / hour)        chat_id=None, filter=True)
    prefix)                                        │
        │                                          ▼
        │                              movie_groups()  → one card per movie,
        │                              qualities merged, series flagged
        ▼                                          │
   build_results() ◄── posters_for() (recent_movies.col, http(s) only)
        │
        ▼
   query.answer(results, cache_time=INLINE_SEARCH_CACHE_TTL, is_personal=True)
```

---

## What a user sees

| | |
|---|---|
| **Poster card** | the movie poster, title + year, quality (`1080p, 720p`), number of files and their total size, `Series` when it is one |
| **Text card** | the same information without a poster — used when the movie has no public poster (e.g. a `tg://file/…` admin poster Telegram cannot fetch) |
| **▶️ Play / Download** | `https://t.me/<BOT_USERNAME>?start=movie_<MOVIE_ID>` — resolves inside the bot |
| **🌐 Watch on website** | the OTT search page (`/search?q=<title>`), when `URL` is configured |
| **🔗 Share** | re-opens inline mode with the same title |
| **Empty query** | the bot's own top searches (`/topsearch`) as suggestions |
| **No result** | a friendly card with *Search in the bot* (`?start=msrch_<query>`) and *Search on the website* |
| **Too many queries** | a rate-limit card with an *Open the bot* button |

Cards can be sent to the chat — tapping one sends the card as a message, and
its buttons keep working there. **No file id, download URL or token is ever
part of an inline result.**

### The `msrch_` deep link

```
https://t.me/<BOT_USERNAME>?start=msrch_<base64url(query)>
```

`plugins/commands.py` understands the `msrch_` payload and immediately runs
`auto_filter` for that query, so "Search in the bot" opens the bot *and* shows
results — one tap, no retyping. (Telegram limits `?start=` payloads to 64
characters, so the query is truncated there; the same convention is used by the
search page's no-result keyboard.)

---

## Configuration (`info.py` / environment)

| Variable | Default | Meaning |
|---|---|---|
| `INLINE_SEARCH` | `True` | master switch |
| `INLINE_SEARCH_LIMIT` | `20` | movie cards per query (1–50) |
| `INLINE_SEARCH_CACHE_TTL` | `60` | seconds Telegram may cache an answer (`is_personal=True`, so it is per user) |
| `INLINE_SEARCH_POSTERS` | `True` | poster cards; `False` answers with text cards |
| `INLINE_SEARCH_GROUPS` | `True` | allow inline search outside private chats |
| `INLINE_SEARCH_TOP` | `6` | results for an empty query (top searches) |
| `INLINE_SEARCH_MAX_QUERY` | `64` | longest accepted query |
| `INLINE_SEARCH_MAX_REQUESTS` | `240` | per-user queries per hour (`0` = unlimited) |

> **BotFather:** inline mode must be enabled once — `/setinline` in
> [@BotFather](https://t.me/BotFather), then pick a placeholder such as
> `Search movies…`. Without it Telegram never sends the bot inline queries.

`/inline` (alias `/inlinehelp`) explains the feature inside the bot and offers
a *🔍 Try it now* button, so users discover it without hunting for settings.

---

## Where the results come from

* **Search** — `database/ia_filterdb.get_search_results(chat_id=None, …,
  filter=True)` — the very same search the group filter uses, so a movie that
  is invisible to a non-premium user stays invisible here too. `chat_id=None`
  means *global index*, `filter=True` keeps the normal clean-up/spell-check
  behaviour.
* **Grouping** — `movie_groups()` merges the qualities of one movie into a
  single card (`Jawan 2023 1080p`, `Jawan 2023 720p` → one *Jawan*), flags
  series episodes and caps a card at `MAX_FILES_PER_MOVIE` (40) files.
* **Posters** — one `recent_movies.col.find({"_id": {"$in": …}})` round trip,
  and only `http(s)` URLs are used, because Telegram must be able to fetch the
  image itself. Anything else falls back to a text card.
* **Rate limiting** — a small in-process deque per user (bounded to
  `MAX_TRACKED_USERS` 5000 users), window of one hour. It is deliberately not
  in the database: an inline query must never wait on Mongo.

---

## Files & tests

| File | Role |
|---|---|
| `plugins/inline_search.py` | the handler, `/inline`, and every helper above |
| `dreamxbotz/util/movie_titles.py` | `parse_release_name`, `movie_id_for`, `build_deeplink`, `sanitize_movie_id` |
| `database/ia_filterdb.py` | `get_search_results` |
| `database/recent_movies_db.py` | poster lookup |
| `tests/test_inline_search.py` | pure helpers + every handler state (fake client & query, no Telegram) |

```bash
pytest tests/test_inline_search.py -q
```

The tests stub the file search and poster lookup, then drive the handler
directly: a normal answer, an empty query, no results, the rate limit, a
disabled bot, a group when `INLINE_SEARCH_GROUPS=False`, and a failing
`answer()` (which must never crash the bot).

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Telegram shows nothing while typing `@bot …` | inline mode enabled in `/setinline`? `INLINE_SEARCH=True`? |
| Text cards instead of posters | the movie has no public poster URL, or `INLINE_SEARCH_POSTERS=False` |
| "No file for …" for a movie that exists | the index search is empty — try a shorter title, or check the upload was indexed |
| "Slow down a little" | `INLINE_SEARCH_MAX_REQUESTS` reached for that user |
| "works in private chat" | `INLINE_SEARCH_GROUPS=False`; set it to `True` to allow groups |
| Tapping ▶️ does nothing | the card was sent before the movie id existed / the deep link resolver needs `BOT_USERNAME` set |
