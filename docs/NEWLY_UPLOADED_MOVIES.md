# Stream Mode · “Newly Uploaded Movies”

A responsive, dark + gold movie rail that is filled **automatically** from the
bot’s own database. Every card deep-links to the Telegram bot
(`https://t.me/BOT_USERNAME?start=movie_MOVIE_ID`), and the bot opens that exact
movie — the visitor never has to search again.

* Frontend: `dreamxbotz/static/newly_uploaded.css`, `dreamxbotz/static/newly_uploaded.js`
* Page markup: injected into `dreamxbotz/template/req.html` (stream page) and
  `dreamxbotz/template/dl.html` (download page)
* API: `dreamxbotz/server/movie_api.py` → `GET /api/movies/new`, `GET /api/movies/poster/<MOVIE_ID>`
* Storage: `database/recent_movies_db.py` → collection `recent_movies`
* Indexing hook: `dreamxbotz/util/new_uploaded.py` (called from `database/ia_filterdb.save_file`)
* Bot deep link: `dreamxbotz/util/movie_deeplink.py` + `plugins/commands.py` (`/start movie_…`)
* Local preview: `python tools/preview_section.py`

---

## 1. How it works

```
 channel post / bulk index / admin index request
                    │
                    ▼
        database.ia_filterdb.save_file()          ← unchanged, still de-duplicates
                    │  (fire-and-forget, never blocks or breaks indexing)
                    ▼
   dreamxbotz/util/new_uploaded.py  ─ queue ─▶ worker
                    │                            │
                    │                            ├─▶ recent_movies.register_upload()
                    │                            │      title, year, quality, upload date
                    │                            │      (unique MOVIE_ID ⇒ no duplicates)
                    │                            ▼
                    │                        poster worker
                    │                        (TMDB → IMDb, rate-limited, cached)
                    ▼
   plugins/channel.py  ─ register_movie_update()  → curated title/year/quality/poster
                    │
                    ▼
   MongoDB collection  recent_movies   (newest first, newest 20 served)
                    │
                    ▼
   GET /api/movies/new            ← sanitized JSON, <abbr>no</abbr> file ids / links / token
                    │
                    ▼
   “Newly Uploaded Movies” section on req.html & dl.html
                    │  click
                    ▼
   https://t.me/BOT_USERNAME?start=movie_MOVIE_ID
                    │
                    ▼
   /start handler → resolve MOVIE_ID → run the normal auto-filter search
```

---

## 2. Placeholders you must fill in

| Placeholder | Where it is used | What to put there |
| --- | --- | --- |
| **`BOT_USERNAME`** | deep links (`t.me/BOT_USERNAME`), the `<section data-bot="…">` attribute | your bot’s public username **without** `@`, e.g. `dreamxbotz_bot`. It is filled automatically at runtime from the running client (`temp.U_NAME`), so normally you change nothing. |
| **`API_URL`** | `info.py` → the endpoint the web pages call | leave **empty** when the pages are served by the bot itself (`/api/movies/new`, same origin). Set a base URL only if the website is hosted elsewhere, e.g. `https://my-minato-api.onrender.com`. |
| **`MOVIE_ID`** | the `?start=movie_<MOVIE_ID>` payload | generated for you: a slug of the movie title plus the release year, e.g. `jawan-2023`, `pushpa-2-the-rule-2024`. Deterministic, deep-link safe, ≤ 64 chars. Never invent it by hand. |
| **`DATABASE_CONNECTION`** | `DATABASE_URI` in `info.py` (env `DATABASE_URI`) | `mongodb+srv://<user>:<password>@<cluster>/…`. The new `recent_movies` collection lives in the **same** database — nothing extra to provision. |
| **`TELEGRAM_BOT_TOKEN`** | `BOT_TOKEN` in `info.py` (env `BOT_TOKEN`) | your BotFather token. It is used **only** by the Python process; it is never sent to the browser (the section only ever receives the public username). |

> Replace the placeholders above with your own values and keep them in the
> environment (Koyeb/Heroku/Docker), never hard-coded in the repository.

---

## 3. Setup (5 minutes)

1. **Nothing to install.** The feature uses what the bot already has
   (aiohttp, motor, Pillow, jinja2) — no new dependency.

2. **Publish the web pages** as usual (`/watch/<hash><id>` or `/<hash><id>`) —
   the section is already part of `req.html` and `dl.html`.

3. **Optional environment variables** (all have safe defaults):

   | Variable | Default | Meaning |
   | --- | --- | --- |
   | `NEW_UPLOADED_MOVIES` | `True` | master switch: tracking + API + section |
   | `NEW_UPLOADED_LIMIT` | `20` | cards shown (hard cap `20`) |
   | `NEW_UPLOADED_ONLY_MOVIES` | `True` | skip `S01E02`-style series for this rail |
   | `NEW_UPLOADED_POSTER_FETCH` | `True` | look missing posters up automatically (TMDB → IMDb) |
   | `NEW_UPLOADED_POSTER_LOOKUPS` | `60` | how many of the newest movies to backfill per run |
   | `NEW_UPLOADED_POSTER_RETRY_HOURS` | `48` | retry a failed poster lookup after N hours |
   | `NEW_UPLOADED_POSTER_TIMEOUT` | `25` | per-lookup timeout (seconds) |
   | `NEW_UPLOADED_POSTER_HOSTS` | *(empty)* | extra allow-listed poster hosts (space separated) |
   | `NEW_UPLOADED_POSTER_ANY_HOST` | `False` | allow **any** https poster host (trusted sources only) |
   | `NEW_UPLOADED_CACHE_TTL` | `60` | browser cache for `/api/movies/new` (seconds) |
   | `NEW_UPLOADED_COLLECTION` | `recent_movies` | Mongo collection name |
   | `NEW_UPLOADED_MAX_MOVIES` | `500` | housekeeping: keep only the newest N entries |
   | `NEW_UPLOADED_CORS_ORIGIN` | *(empty)* | set only if the website is on another origin |
   | `API_URL` | *(empty)* | see the placeholder table above |

4. **Upload a movie** (channel post, `/index`, bulk index) and reload the page —
   the movie appears at the front of the rail, the poster is resolved in the
   background within a few seconds.

5. **Check the API**:

   ```bash
   curl -s "https://YOUR-WEB-URL/api/movies/new?limit=3" | head -c 800
   ```

---

## 4. The section (frontend)

Markup injected into both pages (inside `<main class="wrap">`, **before**
“Ranked trending” — nothing else in the page is touched):

```html
<section class="nu-section" id="newlyUploaded" aria-labelledby="nuTitle"
    data-api="/api/movies/new" data-limit="20" data-bot="BOT_USERNAME"> … </section>
```

| Requirement | Implementation |
| --- | --- |
| Dark/black background + gold accent | scoped `--nu-gold: #f5c518`, `--nu-gold-2: #ffdd7a`; every selector is namespaced `.nu-*` so the existing theme cannot break |
| Responsive grid **and** slider | `grid-template-columns: repeat(auto-fill, minmax(158px, 1fr))` on desktop; below `640px` it becomes a horizontal snap slider (`scroll-snap-type: x mandatory`) |
| Poster aspect ratio | `aspect-ratio: 2 / 3` + `object-fit: cover` |
| Hover zoom / glow | card lifts (`translateY(-6px)`), gold glow shadow, poster zooms (`scale(1.08)`), “Open in Telegram” pill appears |
| Loading state | shimmer **skeleton cards** (`#nuGrid[aria-busy="true"]`) |
| Empty state | “No new movies uploaded yet” + explanation |
| Error state | “Couldn’t load new movies” + reason + **Try again** button (re-fetches with `no-store`) |
| Poster fallback | broken/absent poster → generated placeholder artwork with the movie title (server-generated SVG **and** an inline data-URI fallback in JS) |
| Lazy loading | `loading="lazy"`, `decoding="async"`, `width`/`height` set, `srcset` with `?w=200/320/480` |
| Sanitized data | whitelisted JSON fields + `textContent`-only DOM building (`innerHTML` is never used), poster/deep-link URLs re-validated in the browser |
| No direct links | the section has no download/file URLs at all; the only action is the Telegram deep link |
| Accessibility | `aria-live`, `aria-busy`, `role="alert"`, focus-visible gold ring, `prefers-reduced-motion` support |

Optional extras: `?limit=` on the page sets the card count (max 20), and
`window.MinatoNewlyUploaded.refresh()` re-fetches on demand.

---

## 5. API reference

### `GET /api/movies/new?limit=20`

Newest uploads first, max 20, sanitized.

```json
{
  "ok": true,
  "count": 2,
  "limit": 20,
  "updated_at": "2026-09-22T09:12:04Z",
  "bot_username": "BOT_USERNAME",
  "movies": [
    {
      "id": "jawan-2023",
      "title": "Jawan",
      "year": 2023,
      "quality": "1080p",
      "quality_label": "1080p, 720p, 480p",
      "poster": "/api/movies/poster/jawan-2023?v=2bf27895",
      "has_poster": true,
      "uploaded_at": "2026-09-20T10:11:12Z",
      "added": "2 days ago",
      "deeplink": "https://t.me/BOT_USERNAME?start=movie_jawan-2023"
    }
  ]
}
```

* `503` + `{"ok": false, "error": "database_unavailable"}` when Mongo is unreachable
  (the page shows the error state).
* `Cache-Control: public, max-age=<NEW_UPLOADED_CACHE_TTL>` + `ETag` (304 supported).
* CORS headers are only sent when `NEW_UPLOADED_CORS_ORIGIN` is configured.

### `GET /api/movies/poster/<MOVIE_ID>?w=320`

Poster bytes (JPEG, resized + re-encoded — EXIF stripped) served from **your**
origin so the browser never talks to a third-party CDN. `w` ∈ `200/320/480/640`.
Unknown id → `404`. No poster, disallowed host or upstream failure → a branded
SVG placeholder (`image/svg+xml`) with the movie title.

### Never exposed

`file_ids`, `file_names`, Mongo internals, stream/download URLs, `BOT_TOKEN`,
`DATABASE_URI`. Every string is cleaned, angle brackets stripped and
length-capped; the movie id is validated against `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`
before it reaches a query or a URL.

---

## 6. Database schema (`recent_movies`)

| field | type | notes |
| --- | --- | --- |
| `_id` | str | **`MOVIE_ID`** = `slug` or `slug-year` (deterministic ⇒ duplicates collapse into one document) |
| `title` | str | clean title shown on the card |
| `title_key` | str | `"title year"` de-duplication key |
| `year` | int \| null | release year |
| `search_query` | str | what the bot searches for this deep link |
| `qualities` | list[str] | every quality seen (`480p`, `720p`, `1080p`, …) |
| `poster_url` | str \| null | upstream poster (TMDB/IMDb) — **internal**, never given to the browser |
| `poster_source` | str \| null | `tmdb` / `imdb` |
| `poster_checked_at` | datetime | last lookup attempt (avoids hammering the APIs) |
| `file_ids` | list[str] | **internal**, capped at 40, never exposed |
| `file_names` | list[str] | **internal**, capped at 40, never exposed |
| `file_total` | int | number of distinct files registered |
| `uploaded_at` | datetime | first time the movie appeared |
| `last_upload_at` | datetime | newest file of this movie → **sort key (newest first)** |
| `updated_at` | datetime | last write |

Indexes (created at startup): `last_upload_at ↓`, `title_key`, `updated_at`.

**De-duplication in three layers**

1. `_id` is derived from the title (+ year), so the same movie can never create
   a second document — no matter how many qualities/parts are uploaded.
2. A year-less document is *adopted* as soon as the year becomes known
   (`Jawan` and `Jawan 2023` collapse into one entry).
3. `qualities` / `file_ids` are `$addToSet`-ed, so re-indexing the same file only
   refreshes timestamps.

Housekeeping: every 100 registrations the collection is trimmed to the newest
`NEW_UPLOADED_MAX_MOVIES` (default 500) entries.

---

## 7. Telegram bot deep-link flow

1. Visitor taps a poster → the browser opens
   `https://t.me/BOT_USERNAME?start=movie_<MOVIE_ID>`.
2. Telegram forwards it to the bot as `/start movie_<MOVIE_ID>`.
3. `plugins/commands.py` detects the `movie_` payload and calls
   `dreamxbotz/util/movie_deeplink.py::resolve_movie_deeplink()`.
4. The resolver validates the id (regex), looks it up in `recent_movies`
   (5-minute in-process cache) and returns the movie’s `search_query`, e.g.
   `Jawan 2023`.
5. The handler sets `message.text` to that query and runs the **normal**
   `auto_filter` flow — the user sees that movie’s results immediately, with the
   usual quality/language/season buttons, force-sub checks and premium handling.

Fallbacks:

| Situation | Behaviour |
| --- | --- |
| id not in `recent_movies` (trimmed/older link) | the slug is converted back to a title (`jawan-2023` → “Jawan 2023”) and searched |
| malformed payload (`movie_`, `movie_../../etc`) | friendly “link is not valid anymore” message + a search button |
| database down | the same slug fallback is used, the bot never crashes |

Nothing here duplicates the existing `msrch_` (web search) or `getfile-`
(channel update) deep links — all three coexist.

---

## 8. Preview & tests

```bash
# Local visual preview (real page + real assets + fake feed)
python tools/preview_section.py            # http://127.0.0.1:8080
#   /?state=empty | /?state=error | /?state=loading | /?state=hostile | /?limit=6

# Test suite
pytest tests/test_newly_uploaded.py tests/test_newly_uploaded_ui.py -q
pytest tests/ -q                           # whole suite
```

`tests/test_newly_uploaded.py` covers id/slug determinism, parsing, quality
badges, the store’s de-duplication, the JSON/poster API (including 30-odd
sanitization and security cases) and the `/start movie_…` handler.
`tests/test_newly_uploaded_ui.py` renders both real templates and checks the
markup, the assets’ states and that no secret can leak into the page.

---

## 9. Troubleshooting

| Symptom | Fix |
| --- | --- |
| Empty rail, API returns `{"movies": []}` | upload/index a movie; check `NEW_UPLOADED_MOVIES=True` and that `recent_movies` has documents and indexes |
| Error state on the page | `curl /api/movies/new` — a `503` means Mongo (`DATABASE_URI`) is unreachable; the pages themselves keep working |
| Posters missing | `NEW_UPLOADED_POSTER_FETCH=True`, and `TMDB_API_KEY`/`GROQ…`-style keys are set as usual; a disallowed host also forces the placeholder — add it to `NEW_UPLOADED_POSTER_HOSTS` |
| Posters look blurry/zoomed | lower the `?w=` values or widen the card; the proxy keeps the 2:3 ratio, so use poster (portrait) URLs |
| Cards link to the wrong bot | `BOT_USERNAME` comes from the running client (`temp.U_NAME`); restart the bot so it is set, or let the page’s `data-bot` fill in |
| Section missing on the page | `NEW_UPLOADED_MOVIES=True` (restart required) and the page must be rendered by `render_template.render_page` |
| `404` on `/static/newly_uploaded.js` | the assets must exist in `dreamxbotz/static/` (they are whitelisted by `dreamxbotz/server/static_assets.py`) |
| Website on another domain | set `API_URL` and `NEW_UPLOADED_CORS_ORIGIN`, then restart |
| Want it gone quickly | `NEW_UPLOADED_MOVIES=False` — the section, the API and all writes disappear; existing data is left untouched |
