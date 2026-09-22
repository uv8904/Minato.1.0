# Stream Mode · “Newly Uploaded Movies”

A responsive, dark + gold movie rail that is filled **automatically** from the
bot’s own database. Every card deep-links to the Telegram bot
(`https://t.me/BOT_USERNAME?start=movie_MOVIE_ID`), and the bot opens that exact
movie — the visitor never has to search again.

The **newest upload** additionally gets a Prime-Video-style **“Just added”
spotlight** banner above the rail (16:9 backdrop, poster card, title, year,
quality chips, gold *Open in Telegram* button), and the page **refreshes
itself**: a movie uploaded to the bot while somebody is watching the page
slides into the spotlight and the rail without a reload. See section 0 for a
step-by-step walkthrough (“I upload *hmm* – where does it show up?”).

* Frontend: `dreamxbotz/static/newly_uploaded.css`, `dreamxbotz/static/newly_uploaded.js`
* Page markup: injected into `dreamxbotz/template/req.html` (stream page) and
  `dreamxbotz/template/dl.html` (download page)
* API: `dreamxbotz/server/movie_api.py` → `GET /api/movies/new`, `GET /api/movies/poster/<MOVIE_ID>`
* Storage: `database/recent_movies_db.py` → collection `recent_movies`
* Indexing hook: `dreamxbotz/util/new_uploaded.py` (called from `database/ia_filterdb.save_file`)
* Bot deep link: `dreamxbotz/util/movie_deeplink.py` + `plugins/commands.py` (`/start movie_…`)
* Posters: `dreamxbotz/util/tmdb_direct.py` (official TMDB fallback), `plugins/poster_admin.py`
  + `dreamxbotz/util/poster_admin.py` (`/posters`, `/setposter`, `/delposter`) — section 9b
* Local preview + **upload simulator**: `python tools/preview_section.py`

The same database also powers the **movie hero** above the video player on
`/watch/…` (poster card, 16:9 backdrop, chips, deep link) — see section 9.

---

## 0. Walkthrough — “I upload a movie called *hmm*, where does it show up?”

Nothing has to be clicked on the website. The whole chain is automatic:

| # | What happens | Where (code) | What you see |
| --- | --- | --- | --- |
| 1 | You post `Hmm (2024) 1080p WEB-DL Hindi.mkv` in your file channel (or run `/index`) | Telegram → `plugins/channel.py` | the usual index log line |
| 2 | The bot stores the file in the filter DB — exactly as before | `database/ia_filterdb.save_file()` | — |
| 3 | Right after the commit the file name is queued for the website (non-blocking, never breaks indexing) | `new_uploaded.notify_new_file()` | — |
| 4 | The worker parses the release name → title **Hmm**, year **2024**, quality **1080p** | `movie_titles.parse_release_name()` | — |
| 5 | It upserts one document in **`recent_movies`** with `_id = MOVIE_ID = "hmm-2024"` — deterministic, so a 720p upload of the same movie merges into the same card instead of duplicating it | `recent_movies.register_upload()` | — |
| 6 | The poster worker looks the artwork up (TMDB → IMDb) and stores the poster URL on that document | `new_uploaded._poster_worker()` | until it finishes, a branded placeholder poster |
| 7 | `GET /api/movies/new` returns **Hmm** as `movies[0]` (newest first, sanitized: no file ids, no links, no token) | `dreamxbotz/server/movie_api.py` | `curl …/api/movies/new` |
| 8 | Every open Stream Mode / download page re-checks the feed every `NEW_UPLOADED_POLL` seconds (default 60) — **Hmm** becomes the **“Just added” spotlight** and the first card, with the *new* ribbon, poster and “added just now” | `static/newly_uploaded.js` | the Prime-Video-style banner at the top of the section |
| 9 | A visitor taps the poster → `https://t.me/BOT_USERNAME?start=movie_hmm-2024` → the bot sends **that** movie | `plugins/commands.py` + `movie_deeplink.py` | the files in Telegram |

**Try it without the bot** (no Telegram, no MongoDB needed):

```bash
python tools/preview_section.py          # open http://127.0.0.1:8080/
```

The page is the real `req.html` (player + movie hero + section) plus a floating
**Upload simulator** panel. Type `hmm.mkv` (or any release name) and press
**Upload to bot**: the tool runs the *same* functions the bot runs after
`save_file()` — `parse_release_name()`, `RecentMoviesStore.register_upload()`
on an in-memory collection, a (simulated, 2.5 s) poster worker — and lists every
step. Watch the section: *Hmm* appears in the spotlight and as the first card
with the placeholder, and a couple of seconds later the poster fills in.
The same thing from a shell:

```bash
curl -s -X POST http://127.0.0.1:8080/demo/upload \
     -H 'Content-Type: application/json' \
     -d '{"file_name": "Hmm (2024) 1080p WEB-DL Hindi.mkv"}' | python -m json.tool
curl -s "http://127.0.0.1:8080/api/movies/new?limit=1"    # → "id": "hmm-2024" first
```

In production the only differences are: step 1 is the real channel post, the
poster comes from TMDB/IMDb, and the data lives in your `DATABASE_URI`.

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
                    │   (fetched on page load + every NEW_UPLOADED_POLL seconds while visible)
                    ▼
   “Newly Uploaded Movies” section on req.html & dl.html
     ├─ “Just added” spotlight  ← movies[0]: backdrop, poster, title, chips, deep link
     └─ poster rail             ← all movies, newest first, “new” ribbons
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
>
> The watch-page movie hero (section 9) uses exactly the same placeholders:
> it derives `MOVIE_ID` from the title, builds the `BOT_USERNAME` deep link,
> stores its artwork cache in the same `DATABASE_CONNECTION` and never touches
> `TELEGRAM_BOT_TOKEN` in the browser.

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
| `NEW_UPLOADED_POLL` | `60` | live refresh: open pages re-check the feed every N seconds while the tab is visible (`0` = off, minimum 15). Cheap — the API answers `304` via its `ETag` when nothing changed |
   | `NEW_UPLOADED_COLLECTION` | `recent_movies` | Mongo collection name |
   | `NEW_UPLOADED_MAX_MOVIES` | `500` | housekeeping: keep only the newest N entries |
   | `NEW_UPLOADED_CORS_ORIGIN` | *(empty)* | set only if the website is on another origin |
   | `API_URL` | *(empty)* | see the placeholder table above |

4. **Upload a movie** (channel post, `/index`, bulk index) — within
   `NEW_UPLOADED_POLL` seconds (default 60, or immediately after a reload) the
   movie becomes the **“Just added” spotlight** and the first card of the rail;
   the poster is resolved in the background within a few seconds and fills in
   on the next refresh.

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
    data-api="/api/movies/new" data-limit="20" data-bot="BOT_USERNAME" data-poll="60">
    <div class="nu-spotlight" id="nuSpotlight" data-nu-spotlight hidden></div>   <!-- newest upload -->
    <div class="nu-head">…</div>
    <div class="nu-grid" id="nuGrid" data-nu-grid aria-busy="true"></div>          <!-- poster rail -->
</section>
```

| Requirement | Implementation |
| --- | --- |
| Dark/black background + gold accent | scoped `--nu-gold: #f5c518`, `--nu-gold-2: #ffdd7a`; every selector is namespaced `.nu-*` so the existing theme cannot break |
| **“Just added” spotlight** (Prime-Video style) | `movies[0]` is rendered into `#nuSpotlight`: 16:9 artwork from `backdrop` (`/api/movies/backdrop/<MOVIE_ID>`) with a slow zoom and dark left/bottom gradients, a 2:3 poster card with quality badge + *new* ribbon, kicker “Just added · 2 min ago”, big title, year / quality / Telegram chips, gold **Open in Telegram** + ghost **All new uploads** buttons. If the wide artwork turns out to be portrait (no TMDB backdrop yet) it is blurred into an ambient background instead of being cropped |
| **Live refresh** | `data-poll` seconds (default 60, `0` = off): the feed is re-fetched with `cache: "no-cache"` (→ `304` on the ETag) only while the tab is visible; unchanged cards are **reused** (keyed by id + artwork version + upload time, so posters never flicker), a new newest movie slides in with `.nu-spot--fresh` and the chip reads “new upload · Title” for a few seconds |
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

Optional extras: `?limit=` on the page sets the card count (max 20),
`window.MinatoNewlyUploaded.refresh()` re-fetches on demand (skeletons shown)
and `window.MinatoNewlyUploaded.refresh({ silent: true })` re-checks quietly,
only redrawing what changed.

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
      "backdrop": "/api/movies/backdrop/jawan-2023?v=2bf27895",
      "has_poster": true,
      "uploaded_at": "2026-09-20T10:11:12Z",
      "added": "2 days ago",
      "deeplink": "https://t.me/BOT_USERNAME?start=movie_jawan-2023"
    }
  ]
}
```

* `backdrop` is the 16:9 artwork used by the “Just added” spotlight (same
  origin; the TMDB backdrop when known, otherwise the poster, otherwise a
  branded placeholder — see the backdrop endpoint below).
* `503` + `{"ok": false, "error": "database_unavailable"}` when Mongo is unreachable
  (the page shows the error state).
* `Cache-Control: public, max-age=<NEW_UPLOADED_CACHE_TTL>` + `ETag` (304 supported).
* CORS headers are only sent when `NEW_UPLOADED_CORS_ORIGIN` is configured.

### `GET /api/movies/poster/<MOVIE_ID>?w=320`

Poster bytes (JPEG, resized + re-encoded — EXIF stripped) served from **your**
origin so the browser never talks to a third-party CDN.
`w` ∈ `200/320/480/640/800/1600` (snapped to the nearest allowed value).

* unknown id → `404` (the rail/hero then keeps its own placeholder),
* movie known but without artwork, disallowed host, or upstream failure → a
  branded SVG placeholder (`image/svg+xml`) carrying the movie title.

This route never starts an upstream lookup by itself: the rail’s posters come
from `recent_movies` and the hero’s from the `movie_art` cache (filled by the
artwork endpoint below), so a page view can never stall on TMDB.

### `GET /api/movies/art/<MOVIE_ID>?q=<title>&y=<year>`

Artwork URLs for the watch-page hero (section 9).

```json
{
  "ok": true,
  "id": "marco-2024",
  "title": "Marco",
  "year": 2024,
  "poster": "/api/movies/poster/marco-2024?v=8f2c1d3a",
  "backdrop": "/api/movies/backdrop/marco-2024?v=8f2c1d3a",
  "has_poster": true,
  "has_backdrop": true,
  "source": "tmdb"
}
```

Resolution order: `recent_movies` → `movie_art` → TMDB (`get_movie_detailsx`)
→ IMDb (`get_movie_details`). A miss is remembered in `movie_art`
(`checked_at`) and only retried after `WATCH_HERO_ART_RETRY_HOURS`, so the
upstream APIs are never hammered. `400` for an unusable id; the on-demand
lookup can be switched off with `WATCH_HERO_ART_FETCH=False`.

### `GET /api/movies/backdrop/<MOVIE_ID>?w=1280`

Wide (16:9) artwork for the hero band: the TMDB backdrop, else the poster, else
a branded 16:9 SVG placeholder. `w` ∈ `480/720/960/1280/1920`. Unknown id →
`404`.

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

## 6b. Database schema (`movie_art`)

Artwork cache behind the watch-page hero — same database (`DATABASE_CONNECTION`),
collection name `MOVIE_ART_COLLECTION` (default `movie_art`).

| field | type | notes |
| --- | --- | --- |
| `_id` | str | **`MOVIE_ID`** (same deterministic id as everywhere) |
| `title` | str | clean title (used as the lookup hint) |
| `year` | int \| null | release year |
| `poster_url` | str \| null | upstream 2:3 artwork — **internal**, never sent to the browser |
| `backdrop_url` | str \| null | upstream 16:9 artwork — **internal** |
| `poster_source` | str \| null | `tmdb` / `imdb` |
| `checked_at` | datetime | when the lookup was last attempted (hit **or** miss) |
| `updated_at` | datetime | last write |

Documents are created lazily (first time a streamed movie is opened), so the
collection stays small; it is never trimmed automatically and a hero lookup
never bumps a movie into the “Newly Uploaded” rail.

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
# Local preview + upload simulator (real pages + real assets + the real
# recent_movies store on an in-memory collection – no bot, no Mongo)
python tools/preview_section.py            # http://127.0.0.1:8080
#   /                        Stream Mode page: player + movie hero + spotlight + rail
#                            + floating "Upload simulator" panel
#   /download                the download page (same rail, same panel)
#   /?state=empty | /?state=error | /?state=loading | /?state=hostile | /?limit=6
#   /watch/demo?state=error  artwork API down    ?state=noart  no poster yet
#   /watch/demo?state=hostile hostile artwork API (hardening demo)
#   POST /demo/upload {"file_name": "..."}   run the pipeline for one file name
#   POST /demo/reset                         back to the sample data

# Test suite
pytest tests/test_newly_uploaded.py tests/test_newly_uploaded_ui.py -q
pytest tests/test_preview_upload_simulator.py -q   # upload → spotlight/rail flow
pytest tests/test_watch_hero.py -q         # the watch-page hero
pytest tests/ -q                           # whole suite
```

`tests/test_newly_uploaded.py` covers id/slug determinism, parsing, quality
badges, the store’s de-duplication, the JSON/poster API (including 30-odd
sanitization and security cases) and the `/start movie_…` handler.
`tests/test_newly_uploaded_ui.py` renders both real templates and checks the
markup (including the spotlight container and the poll interval), the assets’
states and that no secret can leak into the page.
`tests/test_preview_upload_simulator.py` drives the simulator end to end:
upload *hmm* → first in the feed with the placeholder → poster after the
worker → a second quality merges instead of duplicating → series/junk are
skipped → reset.
`tests/test_watch_hero.py` covers the hero: the server-rendered context
(title/year/quality/deep link), the artwork + backdrop endpoints (resolution
order, caching, misses, hostile ids), the `movie_art` store and the markup
(above the player, single `<h1>`, asset links, no secrets).

---

## 9. Movie hero on the watch page

`/watch/<id>/<file>?hash=…` opens with a full-width **movie hero** above the
player: a 16:9 backdrop band, the 2:3 poster card of the movie that is being
streamed, its year/quality/language chips, the upload date, and the same
Telegram deep link as the rail (`https://t.me/BOT_USERNAME?start=movie_MOVIE_ID`)
— plus a *Copy search link* button and a *Play here* jump back to the player.

* Frontend: `dreamxbotz/static/watch_hero.css`, `dreamxbotz/static/watch_hero.js`
* Page markup: `dreamxbotz/template/req.html` → `<section id="movieHero">`
* Server-rendered context: `dreamxbotz/util/watch_hero.py` (`build_context`)
* API: `/api/movies/art/<MOVIE_ID>` + `/api/movies/backdrop/<MOVIE_ID>` (§5)
* Artwork cache: `database/movie_art_db.py` → collection `movie_art` (§6b)
* Preview: `python tools/preview_section.py` → `/watch/demo`

### How the artwork is resolved

```
  /watch/... page view
        │  render_template.render_page()   ← title / year / quality / upload date /
        │  deep link are derived from the file name + its Telegram message, so the
        │  strip is complete before any JavaScript runs
        ▼
  <section id="movieHero" data-movie-id="marco-2024" data-art="/api/movies/art">
        │  watch_hero.js
        ▼
  GET /api/movies/art/marco-2024?q=Marco&y=2024
        │
        ├─ 1. recent_movies document    (poster already known → no network call)
        ├─ 2. movie_art document        (resolved on an earlier visit)
        └─ 3. TMDB (poster + backdrop)  →  IMDb fallback (poster)
                 │  8 s timeout, https-only, allow-listed image hosts
                 ▼
           movie_art (cached)  →  bytes proxied from our own origin:
           /api/movies/poster/<id>?w=…  ·  /api/movies/backdrop/<id>?w=…
```

The browser never talks to TMDB/IMDb, never receives an upstream URL and never
sees the bot token: the strip carries only the public `BOT_USERNAME`.

### Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `WATCH_HERO` | `True` | master switch for the strip (the player page is untouched when off) |
| `WATCH_HERO_ART_FETCH` | `True` | allow on-demand poster/backdrop lookups |
| `WATCH_HERO_ART_TIMEOUT` | `8.0` | seconds per lookup — a page view must stay snappy |
| `WATCH_HERO_ART_RETRY_HOURS` | `24` | how long a failed lookup is remembered before retrying |
| `WATCH_HERO_API_PATH` | `/api/movies/art` | artwork endpoint (`API_URL` + this path) |
| `MOVIE_ART_COLLECTION` | `movie_art` | Mongo collection of the artwork cache |

### Behaviour

| Situation | What the visitor sees |
| --- | --- |
| Movie uploaded moments ago (already in the rail) | poster immediately, 16:9 backdrop fills in right after |
| Older movie, first visit | skeleton shimmer, then poster + backdrop |
| No artwork found anywhere | branded placeholder card + “Poster coming soon” note |
| TMDB/IMDb unreachable | placeholder stays, the note says so, the deep link still works |
| JavaScript disabled | title, chips, upload date and the deep-link button still render |
| Repeat visit / cached artwork | served from the process cache + `movie_art`, no upstream call |
| Hostile API answer | every URL is re-validated in the browser: only our own `/api/movies/` paths are ever loaded |

Tapping the poster card or *Open in Telegram* opens that exact movie inside the
bot (`?start=movie_<MOVIE_ID>`, section 7) — no searching, no download link on
the page.

---

## 9b. Posters — what is needed, and what to do when one is missing

**Requirements for automatic posters** (nothing else has to be installed):

| Need | Why |
| --- | --- |
| `TMDB_API_KEY` | the poster/backdrop source. Free: themoviedb.org → *Settings → API* (the v3 “API Key” **or** the v4 “Read Access Token” both work). Paste the whole value as one word — a v3 key is exactly 32 characters of `0-9a-f`; `/posters` tells you when it is cut off or mistyped. The variable should be called `TMDB_API_KEY`, but `TMDB_KEY`, `TMDB_TOKEN`, `tmdb api key` and other close spellings are accepted too (`info.TMDB_KEY_VARIABLES`). Without it only the slow IMDb scrape is left and no 16:9 backdrops exist |
| outbound internet from the host | `api.themoviedb.org`, `image.tmdb.org`, `m.media-amazon.com` (IMDb images) must be reachable; the browser never talks to them — the bot proxies and resizes every image |
| a **real, clean release name** | `Jawan (2023) 1080p WEB-DL.mkv` → title *Jawan*, 2023. A made-up name such as `hmm.mkv` has no poster anywhere — use `/setposter` for those |
| switches at their defaults | `NEW_UPLOADED_POSTER_FETCH=True`, `TMDB_POSTER=True`, `WATCH_HERO_ART_FETCH=True` |

Lookup order of the poster worker: **TMDB via the bot’s poster helper → TMDB
directly with your key** (`dreamxbotz/util/tmdb_direct.py`, so a hosted
helper outage cannot leave the rail without posters) **→ IMDb**. A miss is
remembered for `NEW_UPLOADED_POSTER_RETRY_HOURS` (48 h) — `/posters retry`
forgets it.

**Admin commands (private chat, admins only):**

| Command | What it does |
| --- | --- |
| `/posters` | status: key present?, switches, worker queue, the newest movies **without** a poster (with their `MOVIE_ID`), and the concrete fix list |
| `/posters retry` | resets the failed lookups and queues them again — send it right after adding `TMDB_API_KEY` |
| *(inside `/posters`)* `TMDB says: …` | the bot asks TMDB itself whether the key is accepted: ✅ accepted, ❌ REJECTED (one wrong digit — copy the key again), ⚠️ could not check (no internet from the host; the key itself is not judged) |
| `/setposter MOVIE_ID` *(as a reply to a photo)* | that photo becomes the poster. It is stored as `tg://file/<file_id>` and served through the bot itself — no third-party image host, works even if TMDB knows nothing about the movie |
| `/setposter MOVIE_ID https://image.tmdb.org/…jpg` | poster from an allow-listed image host (TMDB, IMDb/Amazon, graph.org, telegra.ph, imgur, `NEW_UPLOADED_POSTER_HOSTS`) |
| `/delposter MOVIE_ID` | removes it; the placeholder is back and the worker may look it up again |

`MOVIE_ID` is what `/posters` prints (e.g. `hmm-2024`); the title (`Hmm 2024`)
works as well when it is unambiguous. A hand-set poster (`poster_source =
"manual"`) is never replaced by the automatic flows, the artwork caches of the
poster proxy are evicted immediately and the `?v=` token of the poster URL
changes, so the website shows the new image on its next refresh (at most
`NEW_UPLOADED_POLL` seconds).

---

## 10. Troubleshooting

| Symptom | Fix |
| --- | --- |
| Empty rail, API returns `{"movies": []}` | upload/index a movie; check `NEW_UPLOADED_MOVIES=True` and that `recent_movies` has documents and indexes |
| Error state on the page | `curl /api/movies/new` — a `503` means Mongo (`DATABASE_URI`) is unreachable; the pages themselves keep working |
| Posters missing | send `/posters` — it tells you what is missing. Usually: `TMDB_API_KEY` not set (free, see section 9b) → set it, restart, `/posters retry`; `NEW_UPLOADED_POSTER_FETCH=True`; a made-up/unknown title → `/setposter MOVIE_ID` as a reply to a poster photo; a disallowed host forces the placeholder — add it to `NEW_UPLOADED_POSTER_HOSTS` |
| Poster is wrong (other movie) | `/setposter MOVIE_ID` with the right photo/link — manual posters win over the automatic lookup |
| Posters look blurry/zoomed | lower the `?w=` values or widen the card; the proxy keeps the 2:3 ratio, so use poster (portrait) URLs |
| Cards link to the wrong bot | `BOT_USERNAME` comes from the running client (`temp.U_NAME`); restart the bot so it is set, or let the page’s `data-bot` fill in |
| Section missing on the page | `NEW_UPLOADED_MOVIES=True` (restart required) and the page must be rendered by `render_template.render_page` |
| `404` on `/static/newly_uploaded.js` | the assets must exist in `dreamxbotz/static/` (they are whitelisted by `dreamxbotz/server/static_assets.py`) |
| Website on another domain | set `API_URL` and `NEW_UPLOADED_CORS_ORIGIN`, then restart |
| Hero strip missing on `/watch/…` | `WATCH_HERO=True` (restart required) and the page must be rendered by `render_template.render_page` |
| Hero shows “Poster coming soon” | no artwork was found: check `TMDB_API_KEY` / `WATCH_HERO_ART_FETCH`, or allow-list the host with `NEW_UPLOADED_POSTER_HOSTS` |
| Hero shows “Artwork unavailable” | `curl /api/movies/art/<MOVIE_ID>` — a non-200 means the API/Mongo is down; the strip keeps working without artwork |
| Wrong movie name/quality in the hero | the title is parsed from the file name — use clean release names (`Marco (2024) 1080p …`); see `dreamxbotz/util/movie_titles.py` |
| Spotlight missing (rail works) | the banner is `movies[0]` — it only renders when the feed has at least one movie; check that `/static/newly_uploaded.css` is the current version (`?v=` token) and that the template contains `<div … data-nu-spotlight hidden>` |
| Spotlight shows a blurry poster instead of a wide image | no 16:9 backdrop is known yet: TMDB is the only source of backdrops (`TMDB_API_KEY`, `WATCH_HERO_ART_FETCH=True`); IMDb-only artwork stays a blurred poster background by design |
| New upload does not appear until I reload | `NEW_UPLOADED_POLL` is `0` or the tab was hidden (polling pauses); the default re-checks every 60 s. A CDN/proxy that strips `ETag`/`Cache-Control` can also serve stale JSON — check `curl -I /api/movies/new` |
| Want it gone quickly | `NEW_UPLOADED_MOVIES=False` — the section, the API and all writes disappear; existing data is left untouched. `WATCH_HERO=False` removes only the watch-page strip |
