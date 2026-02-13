"""TypedDicts for bandcamp collection data structures."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

ItemType = Literal["album", "track"]
CheckStatus = Literal["found", "not_found", "false_positive", "verified", "unknown"]


class AlbumMetadata(TypedDict):
    """Scraped metadata from a Bandcamp album page."""

    release_date: str | None
    label: str | None
    barcode: str | None
    genres: list[str]
    tags: list[str]
    cover_url: str | None
    tracks: dict[str, list[dict]]
    description: str | None
    credits: str | None
    track_count: int


class CollectionItem(TypedDict):
    """A Bandcamp collection item (DB row or pre-insert)."""

    id: NotRequired[int]
    bandcamp_url: str
    bandcamp_item_id: int | None
    artist: str
    title: str
    item_type: ItemType
    purchase_date: str | None
    release_date: NotRequired[str | None]
    label: NotRequired[str | None]
    barcode: NotRequired[str | None]
    genres: NotRequired[list[str]]
    tags: NotRequired[list[str]]
    cover_url: NotRequired[str | None]
    track_count: NotRequired[int]
    tracks: NotRequired[dict]
    description: NotRequired[str | None]
    credits: NotRequired[str | None]
    fetched_at: NotRequired[str]


class TorrentSummary(TypedDict):
    """Summary of a single torrent in a search result."""

    format: str | None
    encoding: str | None
    media: str | None
    seeders: int
    snatches: int


class ResultInfo(TypedDict):
    """Information about a single search result group."""

    groupName: str | None
    artist: str | None
    artists: list[str]
    groupYear: int | None
    releaseType: str | None
    tags: list[str]
    torrents: list[TorrentSummary]
    false_positive: NotRequired[bool]


class TrackerStatus(TypedDict):
    """Tracker check status for a collection item."""

    status: CheckStatus
    results: dict[str, ResultInfo]
    group_id: int | None
    matched_at: str
    inspected_at: str | None
    results_changed_at: str | None
    uploaded_at: str | None
