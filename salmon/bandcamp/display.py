"""Display and interactive selection for bandcamp collection scan results."""

from __future__ import annotations

import re
from unicodedata import normalize

import click

import salmon.trackers
from salmon.bandcamp.types import CollectionItem, ResultInfo, TrackerStatus


def display_collection(
    items: list[CollectionItem],
    tracker_statuses: dict[int, dict[str, TrackerStatus]],
    tracker_list: list[str],
) -> None:
    """Display collection items with tracker status indicators."""
    uploadable = 0
    on_tracker = {"found", "verified"}
    for item in items:
        statuses = tracker_statuses.get(item["id"], {})
        if any(statuses.get(t, {}).get("status") not in on_tracker for t in tracker_list):
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

        genres = item.get("genres", [])
        if genres:
            details.append(", ".join(genres[:3]))

        if details:
            click.echo(f"       {' · '.join(details)}")

    legend_parts = [f"{tracker_chars[t]} = {t}" for t in tracker_list]
    click.echo(
        f"\nTrackers: {', '.join(legend_parts)}  |  BOLD = verified, letter = found, x = false positive, · = missing\n"
    )


def normalize_str(s: str) -> str:
    """Normalize a string for fuzzy comparison."""
    s = normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _strings_similar(a: str, b: str) -> bool:
    """Check if two strings are similar after normalization."""
    na, nb = normalize_str(a), normalize_str(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # One contains the other (handles subtitle differences)
    return na in nb or nb in na


def _tag_overlap(tags_a: list[str] | None, tags_b: list[str] | None) -> float:
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

    genres = item.get("genres", [])
    if genres:
        click.echo(f"  Genres: {', '.join(genres)}")

    tags = item.get("tags", [])
    if tags:
        click.echo(f"  Tags: {', '.join(tags)}")

    if item.get("barcode"):
        click.echo(f"  Barcode: {item['barcode']}")

    if item.get("bandcamp_url"):
        click.echo(f"  {item['bandcamp_url']}")


def _display_tracker_results(
    tracker: str,
    results: dict[str, ResultInfo],
    group_id: int | None = None,
    show_fp: bool = False,
) -> tuple[list[str], int]:
    """Display match results for a single tracker, numbering each match.

    Returns ``(gids_in_display_order, hidden_fp_count)``.  When *show_fp* is
    False, false-positive results are hidden; when True they are appended at
    the end with a dimmed ``[FP]`` marker.
    """
    non_fp = [(gid, info) for gid, info in results.items() if not info.get("false_positive")]
    fp = [(gid, info) for gid, info in results.items() if info.get("false_positive")]

    display_pairs = non_fp + (fp if show_fp else [])
    gids = [gid for gid, _ in display_pairs]
    hidden_fp = len(fp) if not show_fp else 0

    for num, (gid, info) in enumerate(display_pairs, 1):
        artist = info.get("artist", "?")
        name = info.get("groupName", "?")
        year = info.get("groupYear", "?")
        rtype = info.get("releaseType", "?")

        markers = ""
        if group_id is not None and str(group_id) == str(gid):
            markers += click.style(" [verified]", fg="green")
        if info.get("false_positive"):
            markers += click.style(" [FP]", fg="bright_black")

        is_fp = info.get("false_positive")
        label = click.style(f"{artist} — {name}", bold=not is_fp, dim=bool(is_fp))

        click.echo(f"    {num}) [{gid}] " + label + f" ({year}, {rtype})" + markers)

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

    return gids, hidden_fp


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
        if status in ("found", "verified") and results:
            label = f"{len(results)} match(es)"
        elif status == "false_positive" and results:
            label = f"false_positive ({len(results)} FP result{'s' if len(results) != 1 else ''})"
        else:
            label = status

        click.echo(f"\n  {tracker}: " + click.style(label, fg=color))

        if results:
            _display_tracker_results(tracker, results, group_id, show_fp=True)


def verify_item_results(
    item: CollectionItem,
    tracker_statuses: dict[int, dict[str, TrackerStatus]],
    tracker_list: list[str],
) -> None:
    """Display results per tracker and prompt user to verify, mark false positive, or skip.

    When a match is verified on one tracker, subsequent trackers with a similar
    result will have that match preselected as the default.
    """
    from salmon.bandcamp.db import mark_false_positive, verify_tracker_match

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

        if status not in ("found", "false_positive") or not results:
            status_colors = {"not_found": "red"}
            color = status_colors.get(status, "yellow")
            click.echo(f"\n  {tracker}: " + click.style(status, fg=color))
            continue

        show_fp = False
        while True:
            # (Re-)display results with current FP visibility
            non_fp_count = sum(1 for r in results.values() if not r.get("false_positive"))
            fp_count = sum(1 for r in results.values() if r.get("false_positive"))

            if status == "false_positive":
                click.echo(
                    f"\n  {tracker}: "
                    + click.style(f"false_positive ({fp_count} FP result{'s' if fp_count != 1 else ''})", fg="red")
                )
            else:
                click.echo(f"\n  {tracker}: " + click.style(f"{non_fp_count} match(es)", fg="green"))

            gids, hidden_fp = _display_tracker_results(tracker, results, show_fp=show_fp)

            # Check if a previous verification suggests a match here
            suggested_num = None
            if verified_info:
                suggested_gid = _find_matching_result(verified_info, results)
                if suggested_gid and suggested_gid in gids:
                    suggested_num = gids.index(suggested_gid) + 1

            prompt_parts = []
            if len(gids) == 1:
                prompt_parts.append("1 to confirm")
            elif len(gids) > 1:
                prompt_parts.append(f"1-{len(gids)} to confirm")
            prompt_parts.append("[f]alse positive")
            prompt_parts.append("[s]kip")
            if hidden_fp > 0:
                prompt_parts.append(f"[h]idden ({hidden_fp})")
            elif show_fp and fp_count > 0:
                prompt_parts.append("[h]ide FPs")

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
            if choice == "h":
                show_fp = not show_fp
                continue
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

        indices = parse_selection(selection, len(items))
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


def parse_selection(selection_str: str, max_items: int) -> list[int] | None:
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
