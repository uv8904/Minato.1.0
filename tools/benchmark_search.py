#!/usr/bin/env python3
"""Read-only cold/warm search benchmark using the deployment's configured MongoDB.

Run from the project environment: python tools/benchmark_search.py 'Movie Title'
This measures the database search layer, NOT Telegram end-to-end response time.
"""
import argparse
import asyncio
import statistics
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def benchmark(query, runs, page_size):
    from database.ia_filterdb import (
        client, client2, get_search_results, invalidate_search_cache,
    )
    from info import SEARCH_CACHE_SIZE, SEARCH_CACHE_TTL

    async def timed():
        start = perf_counter()
        files, next_offset, total = await get_search_results(
            None, query, max_results=page_size
        )
        return (perf_counter() - start) * 1000, len(files), total

    try:
        invalidate_search_cache()
        cold_ms, returned, total = await timed()
        timings = [(await timed())[0] for _ in range(runs)]
        ordered = sorted(timings)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        print(f"Search cache: {SEARCH_CACHE_SIZE} entries, TTL {SEARCH_CACHE_TTL}s")
        print(f"Matches: {total}; returned: {returned}")
        print(f"Cold (includes first connection): {cold_ms:.2f} ms")
        print(f"Repeated ({runs} runs): median {statistics.median(timings):.2f} ms; p95 {p95:.2f} ms")
        print("Search layer only. Telegram, access checks, posters and hosting queue time are excluded.")
    finally:
        client.close()
        client2.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="A title that exists in your file database")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--page-size", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.runs <= 1000 or not 1 <= args.page_size <= 100:
        parser.error("--runs must be 1..1000 and --page-size must be 1..100")
    asyncio.run(benchmark(args.query, args.runs, args.page_size))


if __name__ == "__main__":
    main()
