# Search performance / lite runtime

## What changed

- Search-result and spelling-suggestion deletion use timers rather than sleeping
  inside Telegram handlers for 60–300 seconds. Auto-delete still uses the same
  deadline; a busy group no longer exhausts dispatcher workers just waiting to
  delete messages. One failed deletion does not stop the other message's cleanup.
- Removed unconditional `tracemalloc.start()` from the production search plugin.
  Python allocation tracing was adding CPU and memory overhead to the entire bot.
- Search counts and primary page retrieval run concurrently. With two databases,
  both counts run concurrently too. Counts remain exact at the time of the DB
  read, and primary-newest-first / secondary-newest-first ordering is preserved.
- A bounded process-local TTL/LRU cache reuses pages and counts. Simultaneous
  identical searches share one lookup; cancelling one caller does not cancel the
  others. Cached result lists are copied before being returned to callers.
- Pagination across the primary/secondary boundary no longer skips secondary
  files. A full primary page does not open a secondary cursor with `limit(0)`.
- File saves and every existing administrative/channel deletion path invalidate
  search caches immediately after the write. An older in-flight read cannot
  repopulate the cache after invalidation. Errors are not cached.
- Cinemagoer poster and spell-fallback lookups now run on one dedicated worker
  thread, not the asyncio event loop. A separate 64-entry / 5-minute cache reuses
  metadata. Posters and spell correction have not been disabled.
- The existing searching indicator is sent concurrently with the DB search.
  The caption's elapsed time uses a monotonic clock (safe across midnight).

Search regex/caption matching, quality/language/season filters, result buttons,
Send All, access checks, premium gating, streaming, indexing and notification
features have not intentionally been removed or bypassed. No new production
package, Redis service, index migration or database rewrite is required.

## Configuration

The defaults work without adding environment variables:

```dotenv
SEARCH_CACHE_SIZE=128
SEARCH_CACHE_TTL=30
```

Size limits **each** of the page/count caches. Size is capped at 1024 and TTL at
300 seconds to avoid accidental excessive retention. Set either to `0` to
turn off search caching for troubleshooting. Existing maximum-button settings
still determine the returned page size.

Caches are process-local. This bot's writes invalidate them, but writes from
another bot/process or direct MongoDB edits are seen after expiry. Because a
new page can reuse a still-live cached count, externally changed totals can lag
by up to twice `SEARCH_CACHE_TTL`. Use TTL `0` if immediate cross-process
visibility is required. Regular local indexing prioritizes freshness by clearing
both caches; heavy imports will therefore reduce cache hit rates.

## Verify after deployment

Restart/redeploy the bot to load the changes. With its normal environment and
installed dependencies, run this **read-only** benchmark for an existing title:

```sh
python tools/benchmark_search.py 'Movie Title' --runs 20
```

It reports the first (cold) lookup and median/p95 repeated lookup times. Cold
includes initial MongoDB connection setup. It does not send Telegram messages,
write files to MongoDB or include Telegram/network/poster/access-check latency.
Avoid sharing database URIs or bot credentials in benchmark reports.

Offline regression tests (no live Telegram or MongoDB required):

```sh
python -m pip install pytest
python -m pytest -q
```

New tests cover caching, coalescing, cancellation, expiry/eviction, failure retry,
in-flight invalidation, single/dual-DB pagination, matching options, mutation
hooks, non-blocking deletion, photo fallbacks and off-loop IMDb execution.

## About the 0.2-second target

A warm in-process cache removes MongoDB reads from the search layer. It does
**not** guarantee a 200 ms user-visible reply. Cold unanchored regex/caption
queries can still scan the collection; MongoDB's existing text index cannot
transparently replace those queries without changing matching behavior. Telegram
RPCs, regional network latency, host load/cold starts, and cold IMDb/Groq requests
also affect response time. IMDb-enabled searches still wait for their own poster,
but that lookup no longer freezes unrelated requests.

For a meaningful 200 ms target, measure cold and warm searches separately on the
actual deployment, and measure message arrival to result delivery separately
from the database benchmark. Keeping the bot and MongoDB geographically close
and the host awake can matter more than further local micro-optimizations. No
live-production latency claim is made by the offline tests.
