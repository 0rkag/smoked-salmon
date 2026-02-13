"""Bandcamp collection management commands."""

import asyncio
import os
import shutil

import click

import salmon.trackers
from salmon import cfg
from salmon.bandcamp.checker import check_items_on_trackers
from salmon.bandcamp.collection import BandcampCollection
from salmon.bandcamp.db import (
    get_all_collection_items,
    get_inspectable_items,
    get_items_needing_check,
    get_known_item_ids,
    get_known_urls,
    get_tracker_statuses,
    insert_collection_item,
    purge_bandcamp_data,
)
from salmon.bandcamp.display import (
    display_collection,
    normalize_str,
    parse_selection,
    select_items,
    verify_item_results,
)
from salmon.bandcamp.downloader import download_and_extract
from salmon.bandcamp.types import CollectionItem
from salmon.common import commandgroup
from salmon.constants import TAG_ENCODINGS
from salmon.uploader import print_preassumptions, upload


def _scrape_and_insert(bc, items, delay=None):
    """Scrape metadata for items and insert each into the DB immediately.

    Returns the number of successfully inserted items. Logs per-item
    failures without aborting the entire import.
    """
    async def _run():
        inserted = 0
        failed = 0
        async for item in bc.scrape_and_yield(items):
            try:
                insert_collection_item(item)
                inserted += 1
            except Exception as e:
                failed += 1
                click.secho(
                    f"  DB insert failed for {item.get('artist', '?')} - {item.get('title', '?')}: {e}",
                    fg="red",
                )
            if delay:
                await asyncio.sleep(delay)
        if failed:
            click.secho(f"  {failed} item(s) failed to insert.", fg="yellow")
        return inserted

    return asyncio.run(_run())


def _get_cookies():
    """Resolve Bandcamp cookies from config."""
    cookies = cfg.bandcamp.cookies
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
    known_item_ids = get_known_item_ids()
    try:
        new_items = list(bc.fetch_new_items(known_urls, known_item_ids))
    except (OSError, ValueError, RuntimeError, KeyError) as e:
        click.secho(f"Failed to load purchases from Bandcamp: {e}", fg="red")
        raise click.Abort() from e

    if new_items:
        click.secho(f"\nFound {len(new_items)} new items. Scraping metadata...", fg="cyan")
        count = _scrape_and_insert(bc, new_items, delay=delay)
        click.secho(f"Cached {count} new items.", fg="green")
    else:
        click.secho("No new items found.", fg="green")


@bandcamp.command()
@click.option("--statuses-only", is_flag=True, help="Only purge tracker statuses, keep collection items")
def purge(statuses_only):
    """Purge cached Bandcamp collection data from the database."""
    if statuses_only:
        msg = "This will delete all tracker match results (collection items are kept)."
    else:
        msg = "This will delete ALL Bandcamp data (collection items and tracker results)."
    click.secho(msg, fg="yellow")
    click.confirm(click.style("Are you sure?", fg="red", bold=True), abort=True)

    col, stat = purge_bandcamp_data(collection=not statuses_only, statuses=True)
    if not statuses_only:
        click.secho(f"Deleted {col} collection items.", fg="green")
    click.secho(f"Deleted {stat} tracker status rows.", fg="green")


@bandcamp.command()
@click.option(
    "--tracker",
    "-t",
    type=click.Choice(salmon.trackers.tracker_list, case_sensitive=False),
    default=None,
    help="Only check against this tracker (default: all configured)",
)
@click.option(
    "--recheck",
    "-r",
    multiple=True,
    type=click.Choice(["not-found", "found", "false-positive", "verified", "all"], case_sensitive=False),
    help="Force re-check items with these statuses regardless of age",
)
def match(tracker, recheck):
    """Match cached collection items against trackers."""
    all_trackers = salmon.trackers.tracker_list
    if not all_trackers:
        click.secho("No trackers configured.", fg="red")
        raise click.Abort()

    tracker_list = [tracker] if tracker else all_trackers

    # Build force_recheck set from --recheck flags
    force_recheck: set[str] | None = None
    if recheck:
        raw = {r.lower() for r in recheck}
        if "all" in raw:
            force_recheck = {"not_found", "found", "false_positive", "verified"}
        else:
            force_recheck = {r.replace("-", "_") for r in raw}

    all_items = get_all_collection_items()
    if not all_items:
        click.secho("No items in collection cache. Run 'bandcamp import' first.", fg="red")
        return

    # Build per-tracker lists of items needing check
    items_by_tracker: dict[str, list[CollectionItem]] = {}
    for tracker_code in tracker_list:
        needs_check = get_items_needing_check(
            tracker_code,
            recheck_not_found_days=cfg.bandcamp.recheck_not_found_days,
            recheck_false_positive_days=cfg.bandcamp.recheck_false_positive_days,
            recheck_found_days=cfg.bandcamp.recheck_found_days,
            force_recheck=force_recheck,
        )
        if needs_check:
            items_by_tracker[tracker_code] = needs_check

    if items_by_tracker:
        total = sum(len(v) for v in items_by_tracker.values())
        click.secho(f"\n{total} item/tracker checks needed...", fg="cyan")
        check_items_on_trackers(items_by_tracker)
    else:
        click.secho("\nAll items are up to date.", fg="green")

    # Display results (only for the trackers that were checked)
    all_items = get_all_collection_items()
    tracker_statuses = get_tracker_statuses()
    display_collection(all_items, tracker_statuses, tracker_list)


@bandcamp.command()
@click.option(
    "--tracker",
    "-t",
    type=click.Choice(salmon.trackers.tracker_list, case_sensitive=False),
    default=None,
    help="Only show results for this tracker",
)
def inspect(tracker):
    """Review and verify tracker match results.

    Shows only items needing attention: new matches that haven't been
    inspected, or items whose results changed since last inspection.
    """
    all_trackers = salmon.trackers.tracker_list or []
    if not all_trackers:
        click.secho("No trackers configured.", fg="red")
        raise click.Abort()

    tracker_list = [tracker] if tracker else all_trackers

    found_items = get_inspectable_items(tracker_list)
    if not found_items:
        click.secho("No items need inspection.", fg="green")
        return

    tracker_statuses = get_tracker_statuses()
    display_collection(found_items, tracker_statuses, tracker_list)

    selection = click.prompt(
        click.style("Select items to inspect (e.g. 3, 1-5, 2,4,6, * for all) or [q]uit", fg="magenta"),
        default="*",
    )
    sel = selection.strip().lower()
    if sel.startswith("q"):
        return

    indices = list(range(len(found_items))) if sel == "*" else parse_selection(selection, len(found_items))

    if indices is None:
        click.secho("Invalid selection.", fg="red")
        return

    for idx in indices:
        verify_item_results(found_items[idx], tracker_statuses, tracker_list)
        # Reload statuses after mutations
        tracker_statuses = get_tracker_statuses()

    click.secho("\nDone inspecting.", fg="green")


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
    if not tracker_list:
        click.secho("No trackers configured.", fg="red")
        raise click.Abort()

    all_items = get_all_collection_items()
    if not all_items:
        click.secho("No items in collection cache. Run 'bandcamp import' first.", fg="red")
        return

    tracker_statuses = get_tracker_statuses()
    display_collection(all_items, tracker_statuses, tracker_list)

    selections = select_items(all_items, tracker_list)
    if not selections:
        click.secho("No items selected. Done.", fg="green")
        return

    # Validate encoding into (name, is_vbr) tuple like the main up command
    if encoding:
        enc_upper = encoding.upper()
        if enc_upper not in TAG_ENCODINGS:
            raise click.BadParameter(
                f"{encoding} is not a valid encoding. Choose from: {', '.join(TAG_ENCODINGS.keys())}",
                param_hint="'--encoding'",
            )
        encoding = TAG_ENCODINGS[enc_upper]
    else:
        encoding = (None, None)

    cookies = _get_cookies()
    bc = BandcampCollection(cookies)

    click.secho("\nVerifying Bandcamp authentication...", fg="cyan")
    if not bc.verify_auth():
        click.secho("Bandcamp authentication failed. Check your cookies.", fg="red")
        raise click.Abort()
    click.secho("Authenticated.", fg="green")

    click.secho("\nLoading purchases for download...", fg="cyan")
    bc.load_purchases()
    bc_purchases = bc.purchases

    original_yes_all = cfg.upload.yes_all
    if yyy:
        cfg.upload.yes_all = True

    # Cache tracker sessions to avoid re-authenticating per item
    tracker_sessions: dict[str, object] = {}

    try:
        for item, tracker_code in selections:
            try:
                bc_item = _find_purchase(bc_purchases, item)
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

                try:
                    click.secho(
                        f"\nLaunching upload for: {item['artist']} — {item['title']} → {tracker_code}",
                        fg="cyan",
                        bold=True,
                    )

                    if tracker_code not in tracker_sessions:
                        tracker_sessions[tracker_code] = salmon.trackers.get_class(tracker_code)()
                    gazelle_site = tracker_sessions[tracker_code]

                    request_id = salmon.trackers.validate_request(gazelle_site, request) if request else None
                    source_url = (item.get("bandcamp_url") or "").strip() or None

                    print_preassumptions(
                        gazelle_site, extract_dir, group_id, "WEB", lossy,
                        spectrals, encoding, spectrals_after,
                    )
                    upload(
                        gazelle_site,
                        extract_dir,
                        group_id,
                        "WEB",
                        lossy,
                        spectrals,
                        encoding,
                        source_url=source_url,
                        scene=scene,
                        overwrite_meta=overwrite,
                        recompress=compress,
                        request_id=request_id,
                        spectrals_after=spectrals_after,
                        auto_rename=auto_rename,
                        skip_up=skip_up,
                        skip_mqa=skip_mqa,
                        skip_log_check=skip_log_check,
                        skip_integrity_check=skip_integrity_check,
                    )
                finally:
                    # Clean up extracted files after upload attempt
                    if extract_dir and os.path.isdir(extract_dir):
                        shutil.rmtree(extract_dir, ignore_errors=True)
            except Exception as e:
                click.secho(
                    f"\nUpload failed for {item['artist']} — {item['title']}: {e}",
                    fg="red",
                )
    finally:
        cfg.upload.yes_all = original_yes_all


def _find_purchase(bc_purchases, item: CollectionItem):
    """Find the matching bandcampsync purchase for a collection item.

    Prefers exact bandcamp_item_id match. Falls back to artist+title+type
    to avoid confusing track and album purchases that share the same name.
    """
    item_type_code = "a" if item.get("item_type") == "album" else "t"
    for p in bc_purchases:
        if p.item_id == item.get("bandcamp_item_id"):
            return p
    for p in bc_purchases:
        if (
            normalize_str(p.band_name) == normalize_str(item["artist"])
            and normalize_str(p.item_title) == normalize_str(item["title"])
            and p.tralbum_type == item_type_code
        ):
            return p
    return None
