"""Bandcamp collection management commands."""

import asyncio

import click

import salmon.trackers
from salmon import cfg
from salmon.common import commandgroup
from salmon.sources.bandcamp_checker import check_items_on_trackers
from salmon.sources.bandcamp_collection import BandcampCollection
from salmon.sources.bandcamp_db import (
    get_all_collection_items,
    get_items_needing_check,
    get_known_urls,
    get_tracker_statuses,
    insert_collection_item,
)
from salmon.sources.bandcamp_display import (
    _parse_selection,
    display_collection,
    select_items,
    verify_item_results,
)
from salmon.sources.bandcamp_downloader import download_and_extract
from salmon.sources.bandcamp_types import CollectionItem


def _scrape_and_insert(bc, items, delay=None):
    """Scrape metadata for items and insert each into the DB immediately."""

    async def _run():
        count = 0
        async for item in bc.scrape_and_yield(items):
            insert_collection_item(item)
            count += 1
            await asyncio.sleep(delay)
        return count

    return asyncio.run(_run())


def _get_cookies():
    """Resolve Bandcamp cookies from config."""
    cookies = cfg.bandcamp.cookies if cfg.bandcamp else None
    if not cookies:
        click.secho(
            "Bandcamp cookies are required.\nSet them in config.toml under [bandcamp].",
            fg="red",
        )
        raise click.Abort()
    return cookies


@commandgroup.group(invoke_without_command=True)
@click.pass_context
def bandcamp(ctx):
    """Manage your Bandcamp collection.

    When run without a subcommand, displays the cached collection.
    """
    if ctx.invoked_subcommand is not None:
        return

    all_items = get_all_collection_items()
    if not all_items:
        click.secho("No items cached. Run 'salmon bandcamp import' first.", fg="yellow")
        return

    tracker_list = salmon.trackers.tracker_list or []
    tracker_statuses = get_tracker_statuses() if tracker_list else {}
    display_collection(all_items, tracker_statuses, tracker_list)


@bandcamp.command(name="import")
@click.option(
    "--delay",
    "-d",
    type=float,
    default=2.0,
    show_default=True,
    help="Seconds between scrape requests",
)
def import_collection(delay):
    """Fetch your Bandcamp collection and cache it locally."""
    cookies = _get_cookies()
    bc = BandcampCollection(cookies)

    click.secho("\nVerifying Bandcamp authentication...", fg="cyan")
    if not bc.verify_auth():
        click.secho("Bandcamp authentication failed. Check your cookies.", fg="red")
        raise click.Abort()
    click.secho("Authenticated.", fg="green")

    click.secho("\nFetching Bandcamp collection...", fg="cyan", bold=True)
    known_urls = get_known_urls()
    new_items = list(bc.fetch_new_items(known_urls))

    if new_items:
        click.secho(f"\nFound {len(new_items)} new items. Scraping metadata...", fg="cyan")
        count = _scrape_and_insert(bc, new_items, delay=delay)
        click.secho(f"Cached {count} new items.", fg="green")
    else:
        click.secho("No new items found.", fg="green")


@bandcamp.command()
@click.option(
    "--tracker",
    "-t",
    default=None,
    help="Only check against this tracker (default: all configured)",
)
@click.option(
    "--recheck-days",
    "-d",
    default=7,
    help="Re-check 'found' items older than N days (default: 7)",
)
def match(tracker, recheck_days):
    """Match cached collection items against trackers."""
    all_trackers = salmon.trackers.tracker_list
    if not all_trackers:
        click.secho("No trackers configured.", fg="red")
        raise click.Abort()

    if tracker:
        if tracker not in all_trackers:
            click.secho(f"Unknown tracker '{tracker}'. Available: {', '.join(all_trackers)}", fg="red")
            raise click.Abort()
        tracker_list = [tracker]
    else:
        tracker_list = all_trackers

    all_items = get_all_collection_items()
    if not all_items:
        click.secho("No items in collection cache. Run 'bandcamp import' first.", fg="red")
        return

    # Build per-tracker lists of items needing check
    items_by_tracker: dict[str, list[CollectionItem]] = {}
    for tracker_code in tracker_list:
        needs_check = get_items_needing_check(tracker_code, recheck_days)
        if needs_check:
            items_by_tracker[tracker_code] = needs_check

    if items_by_tracker:
        total = sum(len(v) for v in items_by_tracker.values())
        click.secho(f"\n{total} item/tracker checks needed...", fg="cyan")
        check_items_on_trackers(items_by_tracker)
    else:
        click.secho("\nAll items are up to date.", fg="green")

    # Display results
    all_items = get_all_collection_items()
    tracker_statuses = get_tracker_statuses()
    display_collection(all_items, tracker_statuses, all_trackers)


@bandcamp.command()
@click.option(
    "--tracker",
    "-t",
    default=None,
    help="Only show results for this tracker",
)
def inspect(tracker):
    """Review and verify tracker match results."""
    all_trackers = salmon.trackers.tracker_list or []
    if not all_trackers:
        click.secho("No trackers configured.", fg="red")
        raise click.Abort()

    if tracker:
        if tracker not in all_trackers:
            click.secho(f"Unknown tracker '{tracker}'. Available: {', '.join(all_trackers)}", fg="red")
            raise click.Abort()
        tracker_list = [tracker]
    else:
        tracker_list = all_trackers

    all_items = get_all_collection_items()
    if not all_items:
        click.secho("No items in collection cache. Run 'bandcamp import' first.", fg="red")
        return

    tracker_statuses = get_tracker_statuses()

    # Filter to items that have at least one "found" result on the selected trackers
    found_items = []
    for item in all_items:
        statuses = tracker_statuses.get(item["id"], {})
        if any(statuses.get(t, {}).get("status") == "found" for t in tracker_list):
            found_items.append(item)

    if not found_items:
        click.secho("No items with tracker matches found.", fg="yellow")
        return

    display_collection(found_items, tracker_statuses, tracker_list)

    while True:
        selection = click.prompt(
            click.style("Select items to inspect (e.g. 3, 1-5, 2,4,6, * for all) or [q]uit", fg="magenta"),
            default="q",
        )
        sel = selection.strip().lower()
        if sel.startswith("q"):
            return

        indices = list(range(len(found_items))) if sel == "*" else _parse_selection(selection, len(found_items))

        if indices is None:
            click.secho("Invalid selection.", fg="red")
            continue

        for i, idx in enumerate(indices):
            verify_item_results(found_items[idx], tracker_statuses, tracker_list)
            if i < len(indices) - 1 and not click.confirm("\nNext?", default=True):
                break


@bandcamp.command(name="up")
@click.option("--group-id", "-g", default=None, help="Group ID to upload torrent to")
@click.option(
    "--lossy/--not-lossy",
    "-l/-L",
    default=None,
    help="Whether or not the files are lossy mastered",
)
@click.option(
    "--spectrals",
    "-sp",
    type=click.INT,
    multiple=True,
    help="Track numbers of spectrals to include in torrent description",
)
@click.option("--overwrite", "-ow", is_flag=True, help="Whether or not to use the original metadata.")
@click.option("--encoding", "-e", type=click.STRING, default=None, help="Encoding if files aren't lossless")
@click.option("--compress", "-c", is_flag=True, help="Recompress flacs before uploading.")
@click.option("--request", "-r", default=None, help="Pass a request URL or ID")
@click.option("--spectrals-after", "-a", is_flag=True, help="Assess spectrals after torrent upload")
@click.option("--auto-rename", "-n", is_flag=True, help="Rename files and folders automatically")
@click.option("--skip-up", is_flag=True, help="Skip check for 24 bit upconversion")
@click.option("--scene", is_flag=True, help="Is this a scene release")
@click.option("-yyy", is_flag=True, help="Automatically pick the default answer for prompt")
@click.option("--skip-mqa", is_flag=True, help="Skip check for MQA marker")
@click.option("--skip-log-check", is_flag=True, help="Skip checking CD logs")
@click.option("--skip-integrity-check", is_flag=True, help="Skip integrity check of audio files")
def bandcamp_up(
    group_id,
    lossy,
    spectrals,
    overwrite,
    encoding,
    compress,
    request,
    spectrals_after,
    auto_rename,
    skip_up,
    scene,
    yyy,
    skip_mqa,
    skip_log_check,
    skip_integrity_check,
):
    """Upload collection items to trackers."""
    tracker_list = salmon.trackers.tracker_list or []
    all_items = get_all_collection_items()
    if not all_items:
        click.secho("No items in collection cache. Run 'bandcamp import' first.", fg="red")
        return

    if tracker_list:
        tracker_statuses = get_tracker_statuses()
        display_collection(all_items, tracker_statuses, tracker_list)

    selections = select_items(all_items, tracker_list)
    if not selections:
        click.secho("No items selected. Done.", fg="green")
        return

    cookies = _get_cookies()
    bc = BandcampCollection(cookies)

    click.secho("\nLoading purchases for download...", fg="cyan")
    bc.bc.load_purchases()
    bc_purchases = bc.bc.purchases

    for item, tracker in selections:
        bc_item = None
        for p in bc_purchases:
            if p.item_id == item.get("bandcamp_item_id"):
                bc_item = p
                break
            if p.band_name == item["artist"] and p.item_title == item["title"]:
                bc_item = p
                break

        if not bc_item:
            click.secho(
                f"Skipping {item['artist']} — {item['title']} (could not find in purchases)",
                fg="red",
            )
            continue

        extract_dir = download_and_extract(bc, bc_item)
        if not extract_dir:
            click.secho(
                f"Skipping {item['artist']} — {item['title']} (download failed)",
                fg="red",
            )
            continue

        click.secho(
            f"\nLaunching upload for: {item['artist']} — {item['title']} → {tracker}",
            fg="cyan",
            bold=True,
        )

        from salmon.uploader import up

        ctx = click.Context(up, info_name="up")
        ctx.invoke(
            up,
            path=extract_dir,
            group_id=group_id,
            source="WEB",
            lossy=lossy,
            spectrals=spectrals,
            overwrite=overwrite,
            encoding=encoding,
            compress=compress,
            tracker=tracker,
            request=request,
            spectrals_after=spectrals_after,
            auto_rename=auto_rename,
            skip_up=skip_up,
            scene=scene,
            source_url=item.get("bandcamp_url") or None,
            yyy=yyy,
            skip_mqa=skip_mqa,
            skip_log_check=skip_log_check,
            skip_integrity_check=skip_integrity_check,
        )
