# 🎬 OTT Homepage — a JioHotstar-style storefront for the Stream Mode site

A full storefront on top of the bot's own file index: a **trailer hero**, genre
rails, a real **search page** with filters, a **detail sheet**, **My List** and
**Continue Watching** — served from the same small aiohttp app that already
serves `/watch/<path>`, with the same deep links back into Telegram.

```
                 MongoDB (DATABASE_URI)
   recent_movies ──────────┐
   ott_meta     ──────────┤   dreamxbotz/util/ott_catalog.py
   upcoming     ──────────┘            │  card_for()  (public whitelist)
                                       ▼
                     Catalog: cards · genres · hero · rails · search
                                       │
        ┌──────────────────────────────┼───────────────────────────────┐
        ▼                              ▼                               ▼
  GET /home  (HTML,               GET /search (HTML,            GET /api/ott/*
  hero + first rails)             filters + results)            JSON (live rails,
        │                              │                          search, suggest)
        └──────────► dreamxbotz/static/ott_home.js ◄──── #ott-boot JSON
                     My List · Continue Watching · trailer · sheet · live refresh
                                       │
                     ▶️ deep link  https://t.me/<BOT_USERNAME>?start=movie_<MOVIE_ID>
```

The point of the design: the **page renders on the server first** (hero + the
first rails are in the HTML, so it is meaningful before a single script runs),
and the browser only adds the interactive layer — the trailer hero, filters,
My List, Continue Watching and a quiet live refresh.

---

## What the visitor sees

| | |
|---|---|
| **Hero** | up to `OTT_HERO_LIMIT` featured titles, Ken-Burns backdrop, muted auto-playing trailer after a short hover/scroll pause, dots + arrows, keyboard `←`/`→`, respects `prefers-reduced-motion` |
| **Rails** | *New on MinatoVerse*, one rail per genre, *Top rated*, *Premium (4K)*, *Web Series*, *Trending now*, plus the **Coming Soon** rail the bot already keeps |
| **Card** | poster (through our own proxy, never hotlinked), quality badge, rating, "added 2 hours ago", ❤️ My List, ⓘ details, ▶️ trailer |
| **Detail sheet** | backdrop, facts, genres, overview, *Play in Telegram*, *My List*, *Play trailer*, *More like this* |
| **Search page** | instant suggestions, chips for **genre / quality / year / sort**, live result count, "Load more", empty state with a *Search in the bot* button |
| **My List** | stored in the browser (`dx:mylist`) — shared with the watch page's Netflix Pack, so the list is the same on every page |
| **Continue Watching** | also shared (`dx:cw:list`, `dx:resume:<user>`), injected as the first rail when the visitor has history |
| **Mobile** | sticky bottom tab bar (Home · Search · My List · Bot), horizontally scrollable rails, sheet opens as a bottom drawer |

Nothing on the page requires an account, and nothing on the page can download a
file: **every action is a Telegram deep link**, so premium/FSub/verification
rules in the bot stay in charge.

---

## URLs

| Route | What it does |
|---|---|
| `GET /home` | the storefront (`OTT_HOME_PATH`) |
| `GET /` | the same page when `OTT_HOME_AT_ROOT=True` (the old JSON probe moved to `/health`) |
| `GET /search` | search + filters (`OTT_SEARCH_PATH`) |
| `GET /api/ott/home` | hero + rails — `?rail=18&rails=12&hero=6&light=1` |
| `GET /api/ott/search` | `?q=&genre=&year=&quality=&sort=&page=&limit=` |
| `GET /api/ott/suggest` | `?q=&limit=` type-ahead |
| `GET /api/ott/genres` | genre names + counts (nav dropdown, filter chips) |
| `GET /api/ott/movie/<MOVIE_ID>` | one movie (detail sheet) |
| `GET /health` | `{"ok": true, "service": "dreamxbotz", "ott": true}` |

All of them are registered in `plugins/__init__.py::web_server()` **before**
`plugins/route.py`'s catch-all `/{path:\S+}`, exactly like `movie_api`,
otherwise the stream handler would swallow them. All of them are a **no-op when
`OTT_HOME=False`** — the bot behaves exactly as before.

### Caching

| Response | `Cache-Control` |
|---|---|
| `/home` HTML | `public, max-age=min(30, OTT_BROWSER_CACHE_TTL / 2)` — short, so a fresh upload shows up |
| `/search` HTML | `no-store` (it reflects the query) |
| `/api/ott/*` | `max-age=OTT_BROWSER_CACHE_TTL` (default 60) |
| `/api/ott/search?page>1`, any failure | `no-store` |

Server-side the whole catalog is computed once per `OTT_CACHE_TTL` seconds
(default 120 s) per distinct parameter combination; a new upload calls
`ott_catalog.invalidate_cache()` so the rail updates immediately. The browser
additionally refreshes the rails every `OTT_POLL` seconds while the tab is
visible (`0` turns the polling off). Every response is `noindex, nofollow` and
gzipped when the client asks for it.

---

## Where the data comes from

```
recent_movies  (one document per movie — the upload pipeline already writes it)
   │  _id · title · year · qualities · poster_url · updated_at …
   ▼
ott_meta  (this feature's small cache: genres, rating, runtime, trailer key)
   ▲
   └── TMDB  /movie/{id}?append_to_response=videos   (tmdb_direct.movie_details)
       one background pass = at most OTT_META_LOOKUPS lookups,
       failures retried after OTT_META_RETRY_HOURS
```

* **Genres / rating / runtime / trailer** are looked up in the background by
  `ott_catalog.enrich_pending()`. A page render never waits for TMDB: it shows
  what is known and quietly schedules the rest.
* A failed lookup is still recorded (`checked_at`), so the next pass does not
  hammer TMDB; it is retried after `OTT_META_RETRY_HOURS`.
* **Trailers** come from TMDB's `videos` (YouTube only, official trailer first,
  then teaser/clip). `tmdb_direct.pick_trailer()` is a pure function, so the
  ranking is unit-testable.
* The **"Trending now"** rail reuses the bot's own `/topsearch` counters
  (`mdb.get_top_messages`), matched against the catalog by title.
* The **Coming Soon** rail reuses `upcoming_movies` — the same collection the
  watch page's countdown rail reads.

### The only card shape the browser ever sees

```json
{
  "id": "jawan-2023", "title": "Jawan", "year": 2023,
  "quality": "1080p", "quality_label": "1080p, 720p", "bucket": "1080p",
  "poster": "/api/movies/poster/jawan-2023?v=0001234",
  "backdrop": "/api/movies/backdrop/jawan-2023?v=0001234",
  "genres": ["Action", "Thriller"], "rating": 7.0, "runtime": 169,
  "trailer": "MwoUr5wPz9o", "is_series": false, "files": 3,
  "added": "2 hours ago",
  "deeplink": "https://t.me/MyMovieBot?start=movie_jawan-2023",
  "search_url": "/search?q=Jawan"
}
```

No file id, no download URL, no token — `card_for()` builds the card from a
whitelist, so leaking one is impossible by construction. Posters/backdrops are
served by our own `/api/movies/*` proxy (with its host allow-list and LRU
cache), the action is the public deep link, and the sheet's overview is capped
at 400 characters.

---

## Configuration (`info.py` / environment)

Copy these into your `.env` — every one has a sane default, so an empty `.env`
runs the full storefront.

| Variable | Default | Meaning |
|---|---|---|
| `OTT_HOME` | `True` | master switch (pages + APIs) |
| `OTT_HOME_AT_ROOT` | `True` | also answer `/` with the storefront; the old JSON probe stays on `/health` |
| `OTT_HOME_PATH` / `OTT_SEARCH_PATH` | `/home` / `/search` | page paths |
| `OTT_HOME_API_PATH`, `OTT_SEARCH_API_PATH`, `OTT_GENRES_API_PATH`, `OTT_MOVIE_API_PATH`, `OTT_SUGGEST_API_PATH` | `/api/ott/…` | JSON paths |
| `OTT_RAIL_LIMIT` | `18` | cards per rail (4–40) |
| `OTT_RAILS_MAX` | `12` | rails on the home page (1–20) |
| `OTT_RAIL_MIN_ITEMS` | `4` | a rail with fewer cards is not rendered |
| `OTT_SERVER_RAILS` | `4` | rails rendered into the HTML; the rest arrive via the JSON bootstrap |
| `OTT_HERO_LIMIT` | `6` | hero rotation size (1–12) |
| `OTT_GENRES` | *(empty)* | preferred rail order, e.g. `Action,Thriller,Comedy` |
| `OTT_CACHE_TTL` | `120` | server-side catalog cache in seconds (`0` = off) |
| `OTT_BROWSER_CACHE_TTL` | `60` | browser cache for the JSON APIs |
| `OTT_POLL` | `90` | live rails refresh while the tab is visible (`0` = off) |
| `OTT_SEARCH_LIMIT` / `OTT_SEARCH_MAX_LIMIT` | `24` / `60` | page size / hard cap for `?limit=` |
| `OTT_SUGGEST_LIMIT` | `8` | suggestions per keystroke |
| `OTT_TRAILER_VOLUME` | `True` | hero trailers start muted (browser autoplay rules) |
| `OTT_TRENDING_RAIL`, `OTT_TRENDING_LIMIT` | `True`, `12` | "Trending now" rail |
| `OTT_META_FETCH` | `True` | automatic TMDB enrichment (genres/rating/runtime) |
| `OTT_TRAILER_FETCH` | `True` | trailer lookup (needs `TMDB_API_KEY`/`TMDB_ACCESS_TOKEN`) |
| `OTT_META_LOOKUPS` | `40` | movies enriched per background pass |
| `OTT_META_RETRY_HOURS` | `72` | retry a failed lookup after N hours |
| `OTT_META_TIMEOUT` | `8` | per-lookup timeout in seconds |
| `OTT_META_COLLECTION` | `ott_meta` | collection holding the metadata cache |

`NEW_UPLOADED_CORS_ORIGIN` additionally enables CORS when the page is hosted on
another origin; relative paths mean it is usually unnecessary.

---

## Files

| File | Role |
|---|---|
| `dreamxbotz/util/ott_catalog.py` | catalog, cards, rails, search/ranking, trending, TMDB enrichment, TTL caches |
| `dreamxbotz/util/ott_pages.py` | renders the two pages, the `#ott-config` / `#ott-boot` bootstraps, fallback page |
| `dreamxbotz/server/ott_api.py` | routes, clamping, cache headers, CORS, `/health` |
| `dreamxbotz/template/ott_home.html`, `ott_search.html` | the markup (server-rendered hero + rails) |
| `dreamxbotz/static/ott_home.css`, `ott_home.js` | storefront design system + interactive layer (`window.MinatoOTT`) |
| `dreamxbotz/static/ott_search.css`, `ott_search.js` | search/filter layout + behaviour |
| `database/ott_meta_db.py` | `ott_meta` store (`upsert`, `get_many`, `mark_checked_many`, `stats`, …) |
| `dreamxbotz/util/tmdb_direct.py` | `movie_details()`, `pick_trailer()` |
| `plugins/__init__.py` | registers the storefront before the stream catch-all |
| `dreamxbotz/server/static_assets.py` | whitelists the four new assets |

---

## Run it locally, without Telegram or MongoDB

```bash
python tools/preview_section.py --port 8080
# → /            Stream Mode page (watch hero, rail, simulator)
# → /home        the OTT storefront      ← Option A
# → /search      the search page
# → /api/ott/*   the JSON APIs
```

The preview harness seeds the real in-memory stores, runs the **real** catalog
code on them and installs fake TMDB metadata, so genre rails, trailers, ratings
and the search filters all work offline.

```bash
pytest tests/test_ott_pages.py tests/test_ott_api.py -q
```

`test_ott_pages.py` renders both pages with fake payloads and pins the DOM
contract (ids, `data-*`, JSON bootstraps, "never link to nothing", the
`rail["cards"]` naming, the search `query` regression); `test_ott_api.py` runs
the real handlers on an aiohttp test app and pins clamping, cache headers, CORS,
error shapes and the registration order.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| `/home` is empty with an error hint | MongoDB reachable? the page stays HTTP 200 by design |
| Rails have no genres | `OTT_META_FETCH=True` and a TMDB key (`TMDB_API_KEY` / `TMDB_ACCESS_TOKEN`) |
| No trailer in the hero | `OTT_TRAILER_FETCH=True`; TMDB needs a YouTube video for that title |
| Posters are placeholders | the poster worker has not finished yet (`/api/movies/poster/<id>` serves a placeholder meanwhile) |
| Storefront missing entirely | `OTT_HOME=True` and `info.py` imported (the routes log `OTT homepage ready at …`) |
| `/` still shows JSON | `OTT_HOME_AT_ROOT=True` |
| Old storefront HTML cached | bump the deploy (assets are versioned with `?v=`) or set `OTT_BROWSER_CACHE_TTL=0` |
