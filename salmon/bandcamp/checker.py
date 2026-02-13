"""Cross-reference bandcamp collection items against configured trackers."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import click
from ratelimit import RateLimitException

import salmon.trackers
from salmon.bandcamp.db import get_item_tracker_status, upsert_tracker_status
from salmon.bandcamp.types import CollectionItem, ResultInfo, TorrentSummary
from salmon.uploader.dupe_checker import generate_dupe_check_searchstrs

MAX_RETRIES = 5


def _search_tracker(gazelle_site, searchstrs: list[str], loop: asyncio.AbstractEventLoop) -> list[dict]:
    """Run browse searches on a tracker, reusing the given event loop."""
    async def _search():
        tasks = [gazelle_site.request("browse", searchstr=s) for s in searchstrs]
        return await asyncio.gather(*tasks)

    results = []
    for releases in loop.run_until_complete(_search()):
        for release in releases["results"]:
            if release not in results:
                results.append(release)
    return results


def _summarize_results(results: list[dict]) -> dict[str, ResultInfo]:
    """Extract relevant fields from search results, keyed by groupId."""
    summary: dict[str, ResultInfo] = {}
    for r in results:
        gid = r.get("groupId")
        if not gid:
            continue
        # Collect unique artist names from first torrent's artist list
        artists: list[str] = []
        if r.get("torrents"):
            seen: set[str] = set()
            for a in r["torrents"][0].get("artists", []):
                name = a.get("name")
                if name and name not in seen:
                    seen.add(name)
                    artists.append(name)
        summary[str(gid)] = ResultInfo(
            groupName=r.get("groupName"),
            artist=r.get("artist"),
            artists=artists,
            groupYear=r.get("groupYear"),
            releaseType=r.get("releaseType"),
            tags=r.get("tags", []),
            torrents=[
                TorrentSummary(
                    format=t.get("format"),
                    encoding=t.get("encoding"),
                    media=t.get("media"),
                    seeders=t.get("seeders", 0),
                    snatches=t.get("snatches", 0),
                )
                for t in r.get("torrents", [])
            ],
        )
    return summary


def check_item_on_tracker(
    gazelle_site, item: CollectionItem, loop: asyncio.AbstractEventLoop,
) -> tuple[str, dict[str, ResultInfo]]:
    """Check if a collection item exists on a tracker.

    Returns (status, results) tuple where results is a dict keyed by groupId.
    Retries on rate limit errors with backoff. Uses the provided event loop
    to avoid creating/destroying a loop per item.
    """
    artists = [[item["artist"], "main"]]
    searchstrs = generate_dupe_check_searchstrs(artists, item["title"])
    if not searchstrs:
        return "unknown", {}

    for attempt in range(MAX_RETRIES):
        try:
            results = _search_tracker(gazelle_site, searchstrs, loop)
            if results:
                return "found", _summarize_results(results)
            return "not_found", {}
        except RateLimitException as e:
            wait = getattr(e, "period_remaining", 10)
            click.secho(
                f"    Rate limited, waiting {wait:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})...",
                fg="yellow",
            )
            time.sleep(wait)

    raise RateLimitException("Rate limit exceeded after max retries", period_remaining=0)


def check_items_on_trackers(items_by_tracker: dict[str, list[CollectionItem]]) -> None:
    """Check items against their respective trackers in parallel.

    Each tracker runs in its own thread. DB writes are serialized via a
    module-level lock in bandcamp_db, so per-item results are persisted
    immediately (surviving crashes mid-check).
    """
    # Initialize tracker sites up front
    sites = {}
    for tracker_code in items_by_tracker:
        try:
            sites[tracker_code] = salmon.trackers.get_class(tracker_code)()
        except Exception as e:
            click.secho(f"  Failed to connect to {tracker_code}: {e}", fg="red")

    if not sites:
        click.secho("No trackers available.", fg="red")
        return

    with ThreadPoolExecutor(max_workers=len(sites)) as pool:
        futures = {
            pool.submit(_check_all_items, code, site, items_by_tracker[code]): code
            for code, site in sites.items()
        }
        for future in as_completed(futures):
            tracker_code = futures[future]
            try:
                future.result()
            except Exception as e:
                click.secho(f"  {tracker_code}: fatal error - {e}", fg="red")


def _merge_fp_results(
    old_results: dict[str, ResultInfo],
    new_results: dict[str, ResultInfo],
) -> tuple[bool, dict[str, ResultInfo]]:
    """Merge new search results with previously-FP'd results.

    Old group IDs are kept with ``false_positive: True`` (torrent data updated
    from the new search if available).  New group IDs not in old get
    ``false_positive: False``.

    Returns ``(has_new_results, merged)`` where *has_new_results* is True
    if genuinely new (non-FP) results were found.
    """
    old_gids = set(old_results)
    merged: dict[str, ResultInfo] = {}

    for gid, info in old_results.items():
        merged[gid] = ResultInfo(
            groupName=info.get("groupName"),
            artist=info.get("artist"),
            artists=info.get("artists", []),
            groupYear=info.get("groupYear"),
            releaseType=info.get("releaseType"),
            tags=info.get("tags", []),
            torrents=new_results[gid]["torrents"] if gid in new_results else [],
            false_positive=True,
        )

    for gid, info in new_results.items():
        if gid not in old_gids:
            merged[gid] = ResultInfo(
                groupName=info.get("groupName"),
                artist=info.get("artist"),
                artists=info.get("artists", []),
                groupYear=info.get("groupYear"),
                releaseType=info.get("releaseType"),
                tags=info.get("tags", []),
                torrents=info.get("torrents", []),
                false_positive=False,
            )

    has_new = any(not r.get("false_positive") for r in merged.values())
    return has_new, merged


def _check_all_items(tracker_code: str, gazelle_site, items: list[CollectionItem]) -> None:
    """Check all items against a single tracker (runs in its own thread).

    Creates a single event loop for the thread's lifetime to avoid the
    overhead of creating/destroying one per item via asyncio.run().
    DB writes go through bandcamp_db which serializes them via a write lock.
    """
    loop = asyncio.new_event_loop()
    try:
        _check_all_items_with_loop(tracker_code, gazelle_site, items, loop)
    finally:
        loop.close()


def _check_all_items_with_loop(
    tracker_code: str, gazelle_site, items: list[CollectionItem], loop: asyncio.AbstractEventLoop,
) -> None:
    click.secho(f"\n  Checking against {tracker_code}...", fg="cyan", bold=True)
    for i, item in enumerate(items):
        click.echo(f"    {tracker_code} [{i + 1}/{len(items)}] {item['artist']} - {item['title']}")
        try:
            status, results = check_item_on_tracker(gazelle_site, item, loop)
            existing = get_item_tracker_status(item["id"], tracker_code)

            results_changed = False

            if existing is None:
                # First check ever — results_changed if we found something
                results_changed = bool(results)
            elif existing["status"] in ("verified", "false_positive"):
                # User-set status: never change it, only detect result changes
                if existing["status"] == "false_positive" and existing["results"] and results:
                    has_new, results = _merge_fp_results(existing["results"], results)
                    results_changed = has_new
                elif existing["status"] == "verified":
                    old_gids = set(existing["results"].keys())
                    new_gids = set(results.keys())
                    verified_gid = str(existing["group_id"]) if existing["group_id"] else None
                    if (verified_gid and verified_gid not in new_gids) or old_gids != new_gids:
                        results_changed = True
                # Preserve the user-set status
                status = existing["status"]
            else:
                # System status (found/not_found): auto-transition allowed
                if existing["status"] == "not_found" and status == "found":
                    results_changed = True
                elif existing["status"] == "found" and results:
                    old_gids = set(existing["results"].keys())
                    new_gids = set(results.keys())
                    if old_gids != new_gids:
                        results_changed = True

            upsert_tracker_status(
                item["id"], tracker_code, status, results,
                results_changed=results_changed,
            )

            if results_changed and existing:
                click.echo(
                    f"      {tracker_code}: "
                    + click.style("results changed", fg="yellow")
                )
            elif status == "found":
                n = sum(1 for r in results.values() if not r.get("false_positive"))
                click.echo(
                    f"      {tracker_code}: "
                    + click.style(f"found ({n} match{'es' if n != 1 else ''})", fg="green")
                )
            elif status in ("verified", "false_positive"):
                click.echo(
                    f"      {tracker_code}: "
                    + click.style(f"rechecked ({status})", fg="yellow")
                )
            else:
                click.echo(f"      {tracker_code}: " + click.style("not found", fg="red"))
        except Exception as e:
            click.secho(f"      {tracker_code}: error - {e}", fg="red")
            try:
                upsert_tracker_status(item["id"], tracker_code, "unknown")
            except Exception:
                click.secho(f"      {tracker_code}: failed to persist error status", fg="red")
