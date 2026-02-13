"""Display and interactive selection for bandcamp collection scan results."""

from __future__ import annotations

import json
import re
from unicodedata import normalize

import click

import salmon.trackers
from salmon.sources.bandcamp_types import CollectionItem, ResultInfo, TrackerStatus


def display_collection(
    items: list[CollectionItem],
    tracker_statuses: dict[int, dict[str, TrackerStatus]],
    tracker_list: list[str],
) -> None:
    """Display collection items with tracker status indicators."""
    uploadable = 0
    found_statuses = {"found", "verified", "false_positive"}
    for item in items:
        statuses = tracker_statuses.get(item["id"], {})
        if any(statuses.get(t, {}).get("status") not in found_statuses for t in tracker_list):
            uploadable += 1

    click.secho(
        f"\nBandcamp Collection Scan — {len(items)} items ({uploadable} uploadable)\n",
        fg="cyan",
        bold=True,
    )

    tracker_chars = {t: t[0] for t in tracker_list}

    width = len(str(len(items)))

    for idx, item in enumerate(items):
        statuses = tracker_statuses.get(item["id"], {})

        # Build status string
        status_parts = []
        for tracker in tracker_list:
            ts = statuses.get(tracker, {}).get("status", "unknown")
            char = tracker_chars[tracker]
            if ts == "verified":
                status_parts.append(click.style(char.upper(), fg="green", bold=True))
            elif ts == "found":
                status_parts.append(click.style(char, fg="green"))
            elif ts == "false_positive":
                status_parts.append(click.style("x", fg="red"))
            elif ts == "not_found":
                status_parts.append(click.style("·", fg="red"))
            else:
                status_parts.append(click.style("?", fg="yellow"))
        status_str = "".join(status_parts)

        # First line: number, status, artist — title (year)
        year = ""
        if item.get("release_date"):
            year_match = re.search(r"(\d{4})", item["release_date"])
            if year_match:
                year = f" ({year_match[1]})"

        num = f"{idx + 1:>{width}}"
        click.echo(f" {num} {status_str} " + click.style(f"{item['artist']} — {item['title']}{year}", bold=True))

        # Second line: label, track count, genres
        details = []
        if item.get("label"):
            details.append(item["label"])

        track_count = item.get("track_count", 0)
        if item.get("item_type") == "track" or track_count == 1:
            details.append("single")
        elif track_count > 0:
            details.append(f"{track_count} tracks")

        genres = item.get("genres")
        if genres:
            if isinstance(genres, str):
                genres = json.loads(genres)
            if genres:
                details.append(", ".join(genres[:3]))

        if details:
            click.echo(f"       {' · '.join(details)}")

    legend_parts = [f"{tracker_chars[t]} = {t}" for t in tracker_list]
    click.echo(
        f"\nTrackers: {', '.join(legend_parts)}  |  BOLD = verified, letter = found, x = false positive, · = missing\n"
    )


def _normalize_str(s):
    """Normalize a string for fuzzy comparison."""
    s = normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _strings_similar(a, b):
    """Check if two strings are similar after normalization."""
    na, nb = _normalize_str(a), _normalize_str(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # One contains the other (handles subtitle differences)
    return na in nb or nb in na


def _tag_overlap(tags_a, tags_b):
    """Return Jaccard similarity of two tag lists."""
    sa = {t.lower() for t in (tags_a or [])}
    sb = {t.lower() for t in (tags_b or [])}
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _find_matching_result(verified_info: ResultInfo, results: dict[str, ResultInfo]) -> str | None:
    """Find the best matching result in results that is similar to verified_info.

    Returns the group ID string if a good match is found, None otherwise.
    """
    best_gid = None
    best_score = 0

    for gid, info in results.items():
        score = 0

        # Group name similarity (most important)
        if _strings_similar(info.get("groupName", ""), verified_info.get("groupName", "")):
            score += 3

        # Artist similarity
        if _strings_similar(info.get("artist", ""), verified_info.get("artist", "")):
            score += 2

        # Year match
        if info.get("groupYear") and info["groupYear"] == verified_info.get("groupYear"):
            score += 2

        # Release type match
        if info.get("releaseType") and info["releaseType"] == verified_info.get("releaseType"):
            score += 1

        # Tag overlap
        score += _tag_overlap(info.get("tags"), verified_info.get("tags"))

        if score > best_score:
            best_score = score
            best_gid = gid

    # Require at least name + one other strong signal
    return best_gid if best_score >= 5 else None


def _display_item_header(item: CollectionItem) -> None:
    """Display the Bandcamp item info header."""
    click.secho(
        f"\n{item['artist']} — {item['title']}",
        fg="cyan",
        bold=True,
    )

    details = []
    if item.get("item_type"):
        details.append(item["item_type"].capitalize())
    if item.get("release_date"):
        year_match = re.search(r"(\d{4})", item["release_date"])
        if year_match:
            details.append(year_match[1])
    if item.get("label"):
        details.append(item["label"])
    if details:
        click.echo(f"  {' · '.join(details)}")

    track_count = item.get("track_count", 0)
    if track_count > 0:
        click.echo(f"  {track_count} track{'s' if track_count != 1 else ''}")

    genres = item.get("genres")
    if genres:
        if isinstance(genres, str):
            genres = json.loads(genres)
        if genres:
            click.echo(f"  Genres: {', '.join(genres)}")

    tags = item.get("tags")
    if tags:
        if isinstance(tags, str):
            tags = json.loads(tags)
        if tags:
            click.echo(f"  Tags: {', '.join(tags)}")

    if item.get("barcode"):
        click.echo(f"  Barcode: {item['barcode']}")

    if item.get("bandcamp_url"):
        click.echo(f"  {item['bandcamp_url']}")


def _display_tracker_results(tracker: str, results: dict[str, ResultInfo], group_id: int | None = None) -> list[str]:
    """Display match results for a single tracker, numbering each match.

    Returns the list of group ID strings in display order.
    """
    gids = list(results.keys())

    for num, gid in enumerate(gids, 1):
        info = results[gid]
        artist = info.get("artist", "?")
        name = info.get("groupName", "?")
        year = info.get("groupYear", "?")
        rtype = info.get("releaseType", "?")

        verified_marker = ""
        if group_id is not None and str(group_id) == str(gid):
            verified_marker = click.style(" [verified]", fg="green")

        click.echo(
            f"    {num}) [{gid}] "
            + click.style(f"{artist} — {name}", bold=True)
            + f" ({year}, {rtype})"
            + verified_marker
        )

        # Show detailed artists if different from summary
        artists = info.get("artists", [])
        if artists and artist == "Various Artists":
            click.echo(f"           {', '.join(artists[:10])}" + ("..." if len(artists) > 10 else ""))

        tags = info.get("tags", [])
        if tags:
            click.echo(f"           tags: {', '.join(tags)}")

        for t in info.get("torrents", []):
            fmt = t.get("format", "?")
            enc = t.get("encoding", "?")
            media = t.get("media", "?")
            seeders = t.get("seeders", 0)
            snatches = t.get("snatches", 0)
            seed_color = "green" if seeders > 0 else "red"
            click.echo(
                f"           {fmt} / {enc} / {media}"
                f"  — " + click.style(f"{seeders}s", fg=seed_color) + f" / {snatches}sn"
            )

    return gids


def display_item_results(
    item: CollectionItem,
    tracker_statuses: dict[int, dict[str, TrackerStatus]],
    tracker_list: list[str],
) -> None:
    """Display detailed tracker match results for a single collection item."""
    statuses = tracker_statuses.get(item["id"], {})
    _display_item_header(item)

    for tracker in tracker_list:
        ts = statuses.get(tracker, {})
        status = ts.get("status", "unknown")
        results = ts.get("results", {})
        group_id = ts.get("group_id")

        status_colors = {
            "found": "green",
            "verified": "green",
            "not_found": "red",
            "false_positive": "red",
        }
        color = status_colors.get(status, "yellow")
        label = f"{len(results)} match(es)" if status in ("found", "verified") and results else status

        click.echo(f"\n  {tracker}: " + click.style(label, fg=color))

        if results:
            _display_tracker_results(tracker, results, group_id)


def verify_item_results(
    item: CollectionItem,
    tracker_statuses: dict[int, dict[str, TrackerStatus]],
    tracker_list: list[str],
) -> None:
    """Display results per tracker and prompt user to verify, mark false positive, or skip.

    When a match is verified on one tracker, subsequent trackers with a similar
    result will have that match preselected as the default.
    """
    from salmon.sources.bandcamp_db import mark_false_positive, verify_tracker_match

    statuses = tracker_statuses.get(item["id"], {})
    _display_item_header(item)

    # Track the verified result info to suggest matches on other trackers
    verified_info = None

    for tracker in tracker_list:
        ts = statuses.get(tracker, {})
        status = ts.get("status", "unknown")
        results = ts.get("results", {})
        group_id = ts.get("group_id")

        if status == "verified":
            click.echo(f"\n  {tracker}: " + click.style("already verified", fg="green"))
            _display_tracker_results(tracker, results, group_id)
            # Use existing verified result as reference for other trackers
            if verified_info is None and group_id and str(group_id) in results:
                verified_info = results[str(group_id)]
            continue

        if status != "found" or not results:
            status_colors = {"not_found": "red", "false_positive": "red"}
            color = status_colors.get(status, "yellow")
            click.echo(f"\n  {tracker}: " + click.style(status, fg=color))
            continue

        click.echo(f"\n  {tracker}: " + click.style(f"{len(results)} match(es)", fg="green"))
        gids = _display_tracker_results(tracker, results)

        # Check if a previous verification suggests a match here
        suggested_num = None
        if verified_info:
            suggested_gid = _find_matching_result(verified_info, results)
            if suggested_gid:
                suggested_num = gids.index(suggested_gid) + 1

        while True:
            prompt_parts = []
            if len(gids) == 1:
                prompt_parts.append("1 to confirm")
            else:
                prompt_parts.append(f"1-{len(gids)} to confirm")
            prompt_parts.append("[f]alse positive")
            prompt_parts.append("[s]kip")

            default = str(suggested_num) if suggested_num else "s"

            choice = (
                click.prompt(
                    click.style(f"    {', '.join(prompt_parts)}", fg="magenta"),
                    default=default,
                )
                .strip()
                .lower()
            )

            if choice == "s":
                break
            if choice == "f":
                mark_false_positive(item["id"], tracker)
                click.secho(f"    Marked as false positive on {tracker}.", fg="yellow")
                break
            try:
                num = int(choice)
                if 1 <= num <= len(gids):
                    gid_str = gids[num - 1]
                    verify_tracker_match(item["id"], tracker, int(gid_str))
                    click.secho(f"    Verified match [{gid_str}] on {tracker}.", fg="green")
                    # Use this as reference for remaining trackers
                    if verified_info is None:
                        verified_info = results[gid_str]
                    break
                click.secho("    Invalid number.", fg="red")
            except ValueError:
                click.secho("    Invalid input.", fg="red")


def select_items(items: list[CollectionItem], tracker_list: list[str]) -> list[tuple[CollectionItem, str]]:
    """Interactive selection of items to upload.

    Returns list of (item, tracker) tuples.
    """
    while True:
        selection = click.prompt(
            click.style(
                "Select items to upload (e.g. 3, 1-5, 2,4,6) or [q]uit",
                fg="magenta",
            ),
            default="q",
        )

        if selection.strip().lower().startswith("q"):
            return []

        indices = _parse_selection(selection, len(items))
        if not indices:
            click.secho("Invalid selection. Try again.", fg="red")
            continue

        selected_items = [items[i] for i in indices]

        click.secho("\nSelected:", fg="yellow", bold=True)
        for item in selected_items:
            click.echo(f"  {item['artist']} — {item['title']}")

        if len(tracker_list) == 1:
            tracker = tracker_list[0]
            click.secho(f"\nUsing tracker: {tracker}", fg="green")
        else:
            tracker = salmon.trackers.choose_tracker(tracker_list)
            if not tracker:
                continue

        if click.confirm(
            click.style(f"\nUpload {len(selected_items)} item(s) to {tracker}?", fg="magenta"),
            default=True,
        ):
            return [(item, tracker) for item in selected_items]


def _parse_selection(selection_str: str, max_items: int) -> list[int] | None:
    """Parse a selection string like '3', '1-5', '2,4,6' into 0-based indices."""
    indices = set()
    parts = selection_str.replace(" ", "").split(",")
    for part in parts:
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                start, end = int(start), int(end)
                if 1 <= start <= end <= max_items:
                    indices.update(range(start - 1, end))
                else:
                    return None
            except ValueError:
                return None
        else:
            try:
                num = int(part)
                if 1 <= num <= max_items:
                    indices.add(num - 1)
                else:
                    return None
            except ValueError:
                return None
    return sorted(indices) if indices else None
