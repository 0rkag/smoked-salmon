import asyncio
from typing import Any

import asyncclick as click

from salmon import cfg
from salmon.common import (
    commandgroup,
    handle_scrape_errors,
)
from salmon.search import (
    apple_music,
    bandcamp,
    beatport,
    deezer,
    discogs,
    musicbrainz,
    qobuz,
    tidal,
)
from salmon.search.scoring import score_result

SEARCHSOURCES = {
    "Bandcamp": bandcamp,
    "MusicBrainz": musicbrainz,
    "Apple Music": apple_music,
    "Discogs": discogs,
    "Beatport": beatport,
    "Qobuz": qobuz,
    "Tidal": tidal,
    "Deezer": deezer,
}


@commandgroup.command()
@click.argument("searchstr", nargs=-1, required=True)
@click.option("--track-count", "-t", type=click.INT)
@click.option("--limit", "-l", type=click.INT, default=cfg.upload.search.limit)
async def metas(searchstr: tuple[str, ...], track_count: int | None, limit: int) -> None:
    """Search for releases from metadata providers."""
    search_query = " ".join(searchstr)
    click.secho(f"Searching {', '.join(SEARCHSOURCES)} (searchstrs: {search_query})", fg="cyan", bold=True)

    results = await run_metasearch([search_query], limit=limit, track_count=track_count, filter=False)
    not_found: list[str] = []
    inactive_sources: list[str] = []
    source_errors = set(SEARCHSOURCES.keys()) - set(results)
    for source, releases in results.items():
        if releases:
            click.secho(f"\nResults from {source}:", fg="yellow", bold=True)
            for rls_id, release in releases.items():
                rls_name = release[0].album
                url = SEARCHSOURCES[source].Searcher.format_url(rls_id, rls_name)
                click.echo(f"> {release[1]} {url}")
        elif source:
            if releases is None:
                inactive_sources.append(source)
            else:
                not_found.append(source)

    click.echo()
    for source in not_found:
        click.secho(f"No results found from {source}.", fg="red")
    for source in inactive_sources:
        click.secho(
            f"{source} is inactive. Update your config.py with the necessary tokens if you want to enable it.",
            fg="red",
        )
    if source_errors:
        click.secho(f"Failed to scrape {', '.join(source_errors)}.", fg="red")


async def run_metasearch(
    searchstrs: list[str],
    limit: int = cfg.upload.search.limit,
    sources: dict[str, Any] | None = None,
    track_count: int | None = None,
    artists: list[str] | None = None,
    album: str | None = None,
    filter: bool = True,
    *,
    year: int | None = None,
    label: str | None = None,
    catno: str | None = None,
    source_medium: str | None = None,
    is_va: bool = False,
) -> dict[str, Any]:
    """Run a search for releases matching the searchstr.

    Args:
        searchstrs: List of search strings.
        limit: Maximum number of results per source.
        sources: Dict of sources to search, defaults to all.
        track_count: Filter by track count if specified.
        artists: Filter by artists if specified.
        album: Filter by album name if specified.
        filter: Whether to apply scoring/filtering.
        year: Release year for scoring.
        label: Label name for scoring.
        catno: Catalogue number for scoring.
        source_medium: Source medium (WEB/CD/Vinyl) for scoring.
        is_va: Whether this is a VA release.

    Returns:
        Dict mapping source names to search results.
    """
    sources = SEARCHSOURCES if not sources else {k: m for k, m in SEARCHSOURCES.items() if k in sources}

    # Build artist string for structured search
    artist_str = None
    if artists and not is_va:
        artist_str = artists[0] if len(artists) == 1 else ", ".join(artists[:3])

    structured_kwargs = {
        "artist": artist_str,
        "album": album,
        "year": int(year) if year else None,
        "label": label,
        "catno": catno,
        "is_va": is_va,
    }

    results: dict[str, Any] = {}
    tasks = [
        handle_scrape_errors(s.Searcher().search_releases(search, limit, **structured_kwargs))
        for search in searchstrs
        for s in sources.values()
    ]
    task_responses = await asyncio.gather(*tasks)

    for source, result in [r or (None, None) for r in task_responses]:
        if result and filter:
            result = _score_and_filter_results(
                result,
                tag_artist=artist_str,
                tag_album=album,
                tag_year=year,
                tag_track_count=track_count,
                tag_source=source_medium,
                tag_label=label,
                tag_catno=catno,
                is_va=is_va,
            )
        if source:
            results[source] = result
    return results


def _score_and_filter_results(
    results: dict[str, Any],
    *,
    tag_artist: str | None,
    tag_album: str | None,
    tag_year: int | str | None,
    tag_track_count: int | None,
    tag_source: str | None,
    tag_label: str | None,
    tag_catno: str | None,
    is_va: bool,
) -> dict[str, Any]:
    """Score results against tag metadata and filter by threshold."""
    scored: list[tuple[Any, Any, float]] = []

    for rls_id, result_tuple in results.items():
        ident_data = result_tuple[0]
        formatted_str = result_tuple[1]
        fallback_level = result_tuple[2] if len(result_tuple) > 2 else 1

        s = score_result(
            result_artist=ident_data.artist,
            result_album=ident_data.album,
            result_year=ident_data.year,
            result_track_count=ident_data.track_count,
            result_source=ident_data.source,
            result_label=None,
            result_catno=None,
            tag_artist=tag_artist,
            tag_album=tag_album,
            tag_year=tag_year,
            tag_track_count=tag_track_count,
            tag_source=tag_source,
            tag_label=tag_label,
            tag_catno=tag_catno,
            is_va=is_va,
            fallback_level=fallback_level,
        )
        scored.append((rls_id, (ident_data, formatted_str), s))

    # Sort by score descending
    scored.sort(key=lambda x: x[2], reverse=True)

    # Filter by threshold unless show_all_results is set
    threshold = cfg.upload.search.min_score_threshold
    if not cfg.upload.search.show_all_results:
        scored = [(rid, data, s) for rid, data, s in scored if s >= threshold]

    return {rid: data for rid, data, _ in scored}
