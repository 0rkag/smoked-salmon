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


def _similarity_ratio(a: str, b: str) -> float:
    """Return similarity ratio (0.0-1.0) of two strings after normalization."""
    from difflib import SequenceMatcher

    na, nb = normalize_str(a), normalize_str(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _strings_similar(a: str, b: str) -> bool:
    """Check if two strings are similar after normalization."""
    return _similarity_ratio(a, b) > 0.7


def _color_for_similarity(a: str, b: str) -> str:
    """Return click color based on string similarity: green/yellow/red."""
    ratio = _similarity_ratio(a, b)
    if ratio == 1.0:
        return "green"
    if ratio > 0.7:
        return "yellow"
    return "red"


def _color_for_year(bc_year: str | None, result_year: int | None) -> str:
    """Return click color for year comparison."""
    if not bc_year or not result_year:
        return "yellow"
    return "green" if str(result_year) in bc_year else "yellow"


def _color_for_tags(bc_genres: list[str], result_tags: list[str]) -> str:
    """Return click color based on genre/tag overlap."""
    if not bc_genres or not result_tags:
        return "yellow"
    bc_set = {g.lower() for g in bc_genres}
    tag_set = {t.lower() for t in result_tags}
    return "green" if bc_set & tag_set else "red"


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
    """Display condensed Bandcamp item info header (2 lines)."""
    # Line 1: [label] artist — title (year, track/album [N tracks])
    parts = []
    if item.get("label"):
        parts.append(click.style(f"[{item['label']}]", fg="blue"))

    year = ""
    if item.get("release_date"):
        year_match = re.search(r"(\d{4})", item["release_date"])
        if year_match:
            year = year_match[1]

    track_count = item.get("track_count", 0)
    item_type = item.get("item_type", "album")
    if item_type == "track" or track_count == 1:
        type_info = "track"
    elif track_count > 0:
        type_info = f"album [{track_count} tracks]"
    else:
        type_info = item_type or "album"

    meta = ", ".join(filter(None, [year, type_info]))
    title_str = click.style(f"{item['artist']} — {item['title']}", fg="white", bold=True)
    parts.append(f"{title_str} ({meta})")

    click.echo("\n" + " ".join(parts))

    # Line 2: [Genre1, Genre2, ...]
    genres = item.get("genres", [])
    if genres:
        click.echo(click.style(f"[{', '.join(genres)}]", fg="blue"))


def _display_tracker_results(
    item: CollectionItem,
    tracker: str,
    results: dict[str, ResultInfo],
    group_id: int | None = None,
    show_fp: bool = False,
    show_all: bool = False,
    old_results: dict[str, ResultInfo] | None = None,
) -> tuple[list[str], int, bool]:
    """Display match results for a single tracker with color-coded differences.

    Returns ``(gids_in_display_order, hidden_fp_count, was_truncated)``.
    *was_truncated* is True when >5 results were hidden (needs ``[a]ll``).
    *old_results* is used to determine which results are NEW (not in previous set).
    """
    non_fp = [(gid, info) for gid, info in results.items() if not info.get("false_positive")]
    fp = [(gid, info) for gid, info in results.items() if info.get("false_positive")]
    hidden_fp = len(fp) if not show_fp else 0

    # Pin verified result to top
    if group_id is not None:
        gid_str = str(group_id)
        verified_item = [(gid, info) for gid, info in non_fp if gid == gid_str]
        rest = [(gid, info) for gid, info in non_fp if gid != gid_str]
        non_fp = verified_item + rest

    display_pairs = non_fp + (fp if show_fp else [])
    gids = [gid for gid, _ in display_pairs]

    # Check if truncated (>5 non-FP and not showing all)
    was_truncated = False
    if len(non_fp) > 5 and not show_all:
        click.secho(f"    {len(non_fp)} matches (too many to display, press [a] to show all)", fg="red")
        return [], hidden_fp, True

    # Determine which gids are new since last inspection
    old_gids = set((old_results or {}).keys()) if old_results else set()

    # Extract BC item fields for comparison
    bc_year = ""
    if item.get("release_date"):
        year_match = re.search(r"(\d{4})", item["release_date"])
        if year_match:
            bc_year = year_match[1]
    bc_genres = item.get("genres", [])

    for num, (gid, info) in enumerate(display_pairs, 1):
        artist = info.get("artist", "?")
        name = info.get("groupName", "?")
        year = info.get("groupYear")
        rtype = info.get("releaseType", "?")
        is_fp = info.get("false_positive")

        # Color-code based on similarity to BC item
        artist_color = _color_for_similarity(item["artist"], artist)
        title_color = _color_for_similarity(item["title"], name)
        year_color = _color_for_year(bc_year, year)
        tag_color = _color_for_tags(bc_genres, info.get("tags", []))

        artist_styled = click.style(artist, fg=artist_color, dim=bool(is_fp))
        title_styled = click.style(name, fg=title_color, bold=not is_fp, dim=bool(is_fp))
        year_styled = click.style(str(year or "?"), fg=year_color)

        # Markers
        markers = ""
        if group_id is not None and str(group_id) == str(gid):
            markers += click.style(" [verified]", fg="green")
        if is_fp:
            markers += click.style(" [FP]", fg="bright_black")
        if old_results is not None and gid not in old_gids and not is_fp:
            markers += click.style(" NEW", fg="yellow", bold=True)

        # Tag indicator dot
        tag_dot = click.style(" *", fg=tag_color) if info.get("tags") else ""

        click.echo(
            f"    {num}) [{gid}] {artist_styled} — {title_styled}"
            f" ({year_styled}, {rtype}){tag_dot}{markers}"
        )

        # VA artist list (kept, no torrents)
        artists = info.get("artists", [])
        if artists and artist == "Various Artists":
            click.echo(f"           {', '.join(artists[:10])}" + ("..." if len(artists) > 10 else ""))

    if hidden_fp > 0:
        fp_label = f"    ── {hidden_fp} FP result{'s' if hidden_fp != 1 else ''} hidden [h to show] ──"
        click.echo(click.style(fp_label, fg="red"))

    return gids, hidden_fp, was_truncated


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
        non_fp_count = sum(1 for r in results.values() if not r.get("false_positive"))
        if status in ("found", "verified") and results:
            label = f"{non_fp_count} match(es)"
        elif status == "false_positive" and results:
            fp_count = sum(1 for r in results.values() if r.get("false_positive"))
            label = f"false_positive ({fp_count} FP result{'s' if fp_count != 1 else ''})"
        else:
            label = status

        click.echo(f"\n  {tracker}: " + click.style(label, fg=color))

        if results:
            _display_tracker_results(item, tracker, results, group_id, show_fp=True)


def verify_item_results(
    item: CollectionItem,
    tracker_statuses: dict[int, dict[str, TrackerStatus]],
    tracker_list: list[str],
) -> None:
    """Display results per tracker and prompt user to verify, mark FP, or skip.

    Handles previously-matched items:
    - Verified with lost group: prominent warning
    - Verified with changes: verified pinned to top, NEW badges
    - FP with new results: new results shown first
    """
    from salmon.bandcamp.db import mark_false_positive, verify_tracker_match

    statuses = tracker_statuses.get(item["id"], {})
    _display_item_header(item)

    verified_info = None

    for tracker in tracker_list:
        ts = statuses.get(tracker, {})
        status = ts.get("status", "unknown")
        results = ts.get("results", {})
        group_id = ts.get("group_id")
        inspected_at = ts.get("inspected_at")
        results_changed_at = ts.get("results_changed_at")

        # Determine if this is a re-review (results changed since last inspection)
        is_rereview = (
            inspected_at is not None
            and results_changed_at is not None
            and results_changed_at > inspected_at
        )

        # Handle verified status
        if status == "verified":
            if is_rereview:
                gid_str = str(group_id) if group_id else None
                if gid_str and gid_str not in results:
                    # Verified result disappeared!
                    click.echo(f"\n  {tracker}: " + click.style("RESULTS CHANGED", fg="yellow", bold=True))
                    click.secho(
                        f"    ⚠ Verified match [{gid_str}] no longer found!",
                        fg="red",
                        bold=True,
                    )
                else:
                    click.echo(
                        f"\n  {tracker}: "
                        + click.style("RESULTS CHANGED", fg="yellow", bold=True)
                    )
                # Capture verified_info for subsequent trackers
                if verified_info is None and group_id and str(group_id) in results:
                    verified_info = results[str(group_id)]
                # Fall through to interactive prompt below
            else:
                click.echo(f"\n  {tracker}: " + click.style("already verified", fg="green"))
                _display_tracker_results(item, tracker, results, group_id)
                if verified_info is None and group_id and str(group_id) in results:
                    verified_info = results[str(group_id)]
                continue

        # Handle not_found / unknown
        if status not in ("found", "false_positive", "verified") or not results:
            status_colors = {"not_found": "red"}
            color = status_colors.get(status, "yellow")
            click.echo(f"\n  {tracker}: " + click.style(status, fg=color))
            continue

        # Interactive prompt for found / false_positive / verified-rereview
        show_fp = False
        show_all = False
        while True:
            non_fp_count = sum(1 for r in results.values() if not r.get("false_positive"))
            fp_count = sum(1 for r in results.values() if r.get("false_positive"))

            if status == "false_positive" and not is_rereview:
                click.echo(
                    f"\n  {tracker}: "
                    + click.style(f"false_positive ({fp_count} FP result{'s' if fp_count != 1 else ''})", fg="red")
                )
                break

            if status == "false_positive" and is_rereview:
                new_count = sum(1 for r in results.values() if not r.get("false_positive"))
                click.echo(
                    f"\n  {tracker}: "
                    + click.style(f"{new_count} new result(s) since last inspection", fg="bright_red", bold=True)
                )
            elif not is_rereview:
                click.echo(f"\n  {tracker}: " + click.style(f"{non_fp_count} match(es)", fg="green"))

            gids, hidden_fp, was_truncated = _display_tracker_results(
                item, tracker, results, group_id,
                show_fp=show_fp, show_all=show_all,
            )

            # Suggest match: prefer this tracker's verified result, then fuzzy-match from prior
            suggested_num = None
            gid_str = str(group_id) if group_id else None
            if status == "false_positive" and is_rereview:
                suggested_num = "f"
            elif gid_str and gid_str in gids:
                suggested_num = gids.index(gid_str) + 1
            elif verified_info and gids:
                suggested_gid = _find_matching_result(verified_info, results)
                if suggested_gid and suggested_gid in gids:
                    suggested_num = gids.index(suggested_gid) + 1

            # Build prompt
            prompt_parts = []
            if gids:
                if len(gids) == 1:
                    prompt_parts.append("1 to confirm")
                else:
                    prompt_parts.append(f"1-{len(gids)} to confirm")
            prompt_parts.append("[f]alse positive")
            prompt_parts.append("[s]kip")
            prompt_parts.append("[u]rl")
            if was_truncated:
                prompt_parts.append("[a]ll results")
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
            if choice == "u":
                url = item.get("bandcamp_url", "")
                click.echo(f"    {url}" if url else "    No URL available")
                continue
            if choice == "a":
                show_all = True
                continue
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
