# ⏳ Coming Soon — upcoming releases with a live countdown

Movies that are **not out yet**, shown on the Stream Mode pages with their
release day and a countdown that ticks in the browser, plus a
`/comingsoon` command in the bot that turns each card into a
**🔔 Notify me** alert.

```
TMDB /3/movie/upcoming ──► coming_soon.refresh_if_stale()
                                    │  (only when the cache is stale)
                                    ▼
                      upcoming_movies  (Mongo, same DATABASE_URI)
                                    │
          GET /api/movies/upcoming ─►  "Coming Soon" rail + countdown
                                    │
          /comingsoon (bot) ─► 🔔 Notify me ─► title_notify
                                    │
          file indexed ─► check_new_file() ─► the user is PM'd
```

The point of the design: the countdown is what makes the section alive, and a
countdown needs no network at all. The card carries the release day as an ISO
timestamp and a single shared 1-second timer repaints the numbers locally. The
*list* only changes when the bot re-reads TMDB, so the rail does not poll by
default.

---

## What the visitor sees

| | |
|---|---|
| **Card** | poster, title, year, release day, ticking `DD HH MM SS` |
| **Gold chip** | `releasing soon` when the release is within `COMING_SOON_SOON_DAYS` |
| **Released chip** | kept for `COMING_SOON_GRACE_DAYS` after the release day — that is the movie about to appear in *Newly Uploaded*, so it is the most useful card on the page |
| **🔥 N waiting** | how many people asked to be notified |
| **Button** | `Notify me in bot` → `https://t.me/<BOT_USERNAME>?start=movie_<MOVIE_ID>` |
| **Zero reached** | the card flips to *"Releases today — check back for the upload"* on its own, no reload |

## What the bot does

```
/comingsoon            next COMING_SOON_LIMIT releases + a 🔔 button per title
/comingsoon refresh    (admins) re-read TMDB right now
/upcoming              alias
```

A 🔔 tap calls the same `register_notify_request()` the no-results keyboard
uses, so when a file of that movie is finally indexed,
`title_notify.check_new_file()` PMs everyone who asked. The tap also raises
that card's `🔥 N waiting` counter.

The `MOVIE_ID` is derived from title + year exactly like the *Newly Uploaded*
rail derives it, so the deep link built from a Coming Soon card still resolves
after the movie is uploaded. The two sections can never disagree about an id.

---

## Endpoints

### `GET /api/movies/upcoming?limit=12`

```json
{
  "ok": true,
  "count": 12,
  "limit": 12,
  "updated_at": "2026-12-18T00:00:00Z",
  "bot_username": "MyMovieBot",
  "movies": [
    {
      "id": "avatar-3-2026",
      "title": "Avatar 3",
      "year": 2026,
      "release_date": "2026-12-18T00:00:00Z",
      "release_label": "18 Dec 2026",
      "days_left": 85,
      "seconds_left": 7344000,
      "countdown": "in 85 days",
      "state": "upcoming",
      "has_poster": true,
      "poster": "/api/movies/upcoming/poster/avatar-3-2026",
      "waiting": 37,
      "deeplink": "https://t.me/MyMovieBot?start=movie_avatar-3-2026"
    }
  ]
}
```

`state` is `released` / `soon` / `upcoming` — that is what paints the chip.
Disabled feature → `{"ok": true, "count": 0, "movies": [], "disabled": true}`.
Mongo down → `503 {"ok": false, "error": "database_unavailable"}`.

### `GET /api/movies/upcoming/poster/<MOVIE_ID>?w=320`

Poster **bytes** from our own origin, or the shared SVG placeholder.
Deliberately separate from `/api/movies/poster/…`: that route reads the
uploaded-movies rail, this one the `upcoming_movies` cache, so a Coming Soon
card can never be mistaken for an available movie.

---

## Storage

`upcoming_movies` — one document per release, `_id` is the `MOVIE_ID`:

| field | notes |
|---|---|
| `title`, `title_key`, `year` | clean title, de-dup key, release year |
| `release_date` | datetime, 00:00 UTC — the countdown sorts on this |
| `release_date_raw` | `"YYYY-MM-DD"` exactly as upstream sent it |
| `poster_url`, `poster_source` | upstream artwork, **never** sent to the browser |
| `overview`, `tmdb_id`, `popularity` | blurb, upstream id, tie-breaker |
| `notify_total` | users waiting → the `🔥 N waiting` badge |
| `fetched_at`, `updated_at` | timestamps |

`upcoming_meta` — a single `{"_id": "refresh", "fetched_at": …}` document.
Keeping the timestamp in its own collection means a refresh can never show up
as a movie card, and `list_upcoming()` stays a plain query.

> `INTERNAL_FIELDS` only strips `overview`. **`poster_url` must stay in the
> projection** — `public_upcoming_movie()` reads it to compute `has_poster`.
> Keeping the upstream URL away from the browser is the whitelist's job, not
> the query's; stripping it there silently reports every movie as poster-less.

---

## Configuration

| env | default | what it does |
|---|---|---|
| `COMING_SOON` | `True` | master switch: rail + API + `/comingsoon` |
| `COMING_SOON_LIMIT` | `12` | cards shown (hard cap 24) |
| `COMING_SOON_REFRESH_HOURS` | `6` | re-read TMDB every N hours |
| `COMING_SOON_GRACE_DAYS` | `3` | keep a released movie for N days |
| `COMING_SOON_SOON_DAYS` | `7` | gold chip within N days |
| `COMING_SOON_CACHE_TTL` | `600` | browser cache for the API (seconds) |
| `COMING_SOON_POLL` | `0` | feed re-check while visible (0 = off) |
| `COMING_SOON_COLLECTION` | `upcoming_movies` | collection name |
| `COMING_SOON_META_COLLECTION` | `upcoming_meta` | refresh-timestamp document |
| `COMING_SOON_API_PATH` | `/api/movies/upcoming` | `API_URL` + this path |

It needs `TMDB_API_KEY` (any of the spellings `info.py` already accepts). With
no key the refresh is skipped and the rail simply stays empty — nothing errors.

---

## Failure behaviour

Every step is best effort:

* no TMDB key / 401 / timeout / empty body → keep serving the cached rail
* Mongo down → `503`, and the rail shows its error state with a retry button
* a broken upstream date (`""`, `0000-00-00`) → that row is dropped, not fatal
* a stale cache is refreshed **after** the response is built, so no visitor
  ever waits on TMDB, and one in-flight refresh task caps a burst of visitors
* housekeeping drops releases 30 days past the grace window

## Try it without a bot, Telegram or a database

```bash
python tools/preview_section.py            # http://127.0.0.1:8080
```

`/` (player page) and `/download` both render the rail from the **real**
templates and the **real** `UpcomingMoviesStore`, only with an in-memory
collection. `?cs=empty`, `?cs=error`, `?cs=loading` and `?cs=hostile` push just
this rail into its empty / error / skeleton / hostile-feed states
(`?state=` moves both rails at once).

## Tests

```bash
pytest tests/test_coming_soon.py -q
```

Covers the date/countdown maths, the TMDB payload mapping, the `/comingsoon`
list text, the store (de-duplication, staleness, the notify counter,
housekeeping), the HTTP contract (whitelisted shape, ETag/304, disabled, 503,
poster proxy), the static-asset whitelist and both templates.
