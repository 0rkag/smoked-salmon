"""Cross-reference bandcamp collection items against configured trackers."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import click
from ratelimit import RateLimitException

import salmon.trackers
from salmon.sources.bandcamp_db import upsert_tracker_status
from salmon.sources.bandcamp_types import CollectionItem, ResultInfo, TorrentSummary
from salmon.uploader.dupe_checker import generate_dupe_check_searchstrs, get_search_results

MAX_RETRIES = 5


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
        summary[gid] = ResultInfo(
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


def check_item_on_tracker(gazelle_site, item: CollectionItem) -> tuple[str, dict[str, ResultInfo]]:
    """Check if a collection item exists on a tracker.

    Returns (status, results) tuple where results is a dict keyed by groupId.
    Retries on rate limit errors with backoff.
    """
    artists = [[item["artist"], "main"]]
    searchstrs = generate_dupe_check_searchstrs(artists, item["title"])
    if not searchstrs:
        return "unknown", {}

    for attempt in range(MAX_RETRIES):
        try:
            results = get_search_results(gazelle_site, searchstrs)
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


def check_items_on_trackers(
    items: list[CollectionItem], tracker_list: list[str] | None = None, recheck_days: int = 7
) -> None:
    """Check all items against the given trackers in parallel.

    For each item, all trackers are queried concurrently.
    """
    if tracker_list is None:
        tracker_list = salmon.trackers.tracker_list
    if not tracker_list:
        click.secho("No trackers configured.", fg="red")
        return

    # Initialize tracker sites up front
    sites = {}
    for tracker_code in tracker_list:
        try:
            sites[tracker_code] = salmon.trackers.get_class(tracker_code)()
        except Exception as e:
            click.secho(f"  Failed to connect to {tracker_code}: {e}", fg="red")

    if not sites:
        click.secho("No trackers available.", fg="red")
        return

    with ThreadPoolExecutor(max_workers=len(sites)) as pool:
        futures = {pool.submit(_check_all_items, code, site, items): code for code, site in sites.items()}
        for future in as_completed(futures):
            tracker_code = futures[future]
            try:
                future.result()
            except Exception as e:
                click.secho(f"  {tracker_code}: fatal error - {e}", fg="red")


def _check_all_items(tracker_code: str, gazelle_site, items: list[CollectionItem]) -> None:
    """Check all items against a single tracker (runs in its own thread)."""
    click.secho(f"\n  Checking against {tracker_code}...", fg="cyan", bold=True)
    for i, item in enumerate(items):
        click.echo(f"    {tracker_code} [{i + 1}/{len(items)}] {item['artist']} - {item['title']}")
        try:
            status, results = check_item_on_tracker(gazelle_site, item)
            upsert_tracker_status(item["id"], tracker_code, status, results)
            if status == "found":
                n = len(results)
                click.echo(
                    f"      {tracker_code}: " + click.style(f"found ({n} match{'es' if n != 1 else ''})", fg="green")
                )
            else:
                click.echo(f"      {tracker_code}: " + click.style("not found", fg="red"))
        except Exception as e:
            click.secho(f"      {tracker_code}: error - {e}", fg="red")
            upsert_tracker_status(item["id"], tracker_code, "unknown")
