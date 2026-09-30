#!/usr/bin/env python3
"""Backfill the "Newly Uploaded Movies" rail from the existing file library.

The bot fills ``recent_movies`` only for files indexed *after* the feature was
installed, so on a long-lived library ``/api/movies/new`` answers 0 while the
auto-filter already serves those movies for years.  This one-off tool walks
the auto-filter library (``ia_filterdb``'s ``Media``/``Media2`` collections),
parses every file name with the very same ``parse_release_name()`` the
indexing hook uses and upserts the movies into ``recent_movies`` through the
real ``RecentMoviesStore.register_upload()`` – so the homepage rails, search
and suggest pick them up at once.

Re-running is always safe: titles, file ids and qualities are de-duplicated on
every level of the store.

Run from the project environment:

    python tools/backfill_recent_movies.py              # whole library
    python tools/backfill_recent_movies.py --limit 500  # newest 500 files
    python tools/backfill_recent_movies.py --dry-run    # preview, no writes

Posters are not fetched here – once the movies are in the store the bot's
regular poster worker (``/posters`` / section 9b of
docs/NEWLY_UPLOADED_MOVIES.md) fills the artwork automatically.
"""
import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FileSource = Tuple[str, Any]  # (label, motor collection)


def _newest_first(collection):
    """The library cursor: newest-indexed files first (like ``dreamxbotz_fetch_media``)."""
    return collection.find(
        {}, {"file_name": 1, "mime_type": 1, "file_type": 1}
    ).sort("$natural", -1)


def default_sources() -> List[FileSource]:
    """The auto-filter file collections (primary, plus the secondary when on)."""
    from database.ia_filterdb import db, db2
    from info import COLLECTION_NAME, MULTIPLE_DB

    sources: List[FileSource] = [("primary", db[COLLECTION_NAME])]
    if MULTIPLE_DB:
        sources.append(("secondary", db2[COLLECTION_NAME]))
    return sources


async def backfill(
    sources: Optional[Iterable[FileSource]] = None,
    *,
    store=None,
    limit: int = 0,
    dry_run: bool = False,
    include_series: bool = False,
) -> Dict[str, int]:
    """Copy library movies into ``recent_movies``; returns the run summary.

    ``sources``/``store`` are injectable for tooling and tests; by default the
    real filter-DB collections and the real ``recent_movies`` store are used.
    """
    from dreamxbotz.util.movie_titles import looks_like_video, parse_release_name
    from info import NEW_UPLOADED_ONLY_MOVIES

    if store is None:
        from database.recent_movies_db import recent_movies as store

    only_movies = NEW_UPLOADED_ONLY_MOVIES and not include_series
    source_list = list(sources) if sources is not None else default_sources()
    base = datetime.utcnow()
    #: (title, year) -> the newest file's slot, so ``last_upload_at`` keeps the
    #: same "newest file wins" meaning the live indexing hook gives it.
    newest_slot: Dict[Tuple[str, Optional[int]], datetime] = {}
    seen_files: set = set()
    summary = {
        "scanned": 0,
        "files_registered": 0,
        "movies": 0,
        "skipped_non_video": 0,
        "skipped_series": 0,
        "skipped_unparsed": 0,
        "errors": 0,
    }

    for label, collection in source_list:
        async for doc in _newest_first(collection):
            if limit and summary["scanned"] >= limit:
                break
            summary["scanned"] += 1
            if summary["scanned"] % 500 == 0:
                print(f"  … scanned {summary['scanned']} file(s)")

            file_id = str(doc.get("_id") or "")
            if file_id in seen_files:
                continue
            seen_files.add(file_id)

            name = str(doc.get("file_name") or "")
            if not looks_like_video(name, str(doc.get("mime_type") or ""), str(doc.get("file_type") or "")):
                summary["skipped_non_video"] += 1
                continue

            parsed = parse_release_name(name)
            if only_movies and parsed.get("is_series"):
                summary["skipped_series"] += 1
                continue
            title = parsed.get("title")
            # Punctuation-only names ("...", "!!!") normalise to junk titles.
            if not title or not any(ch.isalnum() for ch in str(title)):
                summary["skipped_unparsed"] += 1
                continue

            if dry_run:
                summary["files_registered"] += 1
                newest_slot.setdefault((title, parsed.get("year")), base)
                continue

            key = (title, parsed.get("year"))
            moment = newest_slot.setdefault(key, base - timedelta(seconds=summary["scanned"]))
            try:
                movie_id = await store.register_upload(
                    title=title,
                    year=parsed.get("year"),
                    quality=parsed.get("quality"),
                    file_id=file_id or None,
                    file_name=name,
                    now=moment,
                )
            except Exception as exc:  # keep going, one bad file must not stop the run
                summary["errors"] += 1
                print(f"  ! {label}: could not register {name!r}: {exc}")
                continue
            if movie_id:
                summary["files_registered"] += 1
        if limit and summary["scanned"] >= limit:
            break

    summary["movies"] = len(newest_slot)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill recent_movies (Newly Uploaded rail) from the ia_filterdb file library.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="scan at most N files (newest-indexed first); 0 = whole library",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="only report what would be registered, never write",
    )
    parser.add_argument(
        "--include-series", action="store_true",
        help="also register S01E02-style series files (respects NEW_UPLOADED_ONLY_MOVIES)",
    )
    args = parser.parse_args()

    print("Backfill: ia_filterdb library → recent_movies")
    print(f"  mode: {'dry run' if args.dry_run else 'write'}; limit: {args.limit or 'all'}; "
          f"series: {'included' if args.include_series else 'skipped'}")

    async def run() -> Dict[str, int]:
        if not args.dry_run:
            from database.recent_movies_db import recent_movies

            await recent_movies.ensure_indexes()
        return await backfill(
            limit=max(0, args.limit),
            dry_run=args.dry_run,
            include_series=args.include_series,
        )

    summary = asyncio.run(run())
    print(
        "Done: {scanned} file(s) scanned → {files_registered} file(s) registered "
        "across {movies} movie(s); skipped: {skipped_non_video} non-video, "
        "{skipped_series} series, {skipped_unparsed} unparsed; errors: {errors}".format(**summary)
    )
    if summary["movies"] and not args.dry_run:
        print("Movies are live on the homepage now; posters fill in via the bot's poster worker.")
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
