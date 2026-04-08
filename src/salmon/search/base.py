from abc import ABC, abstractmethod
from typing import Any

import asyncclick as click
import msgspec

from salmon.search.scoring import FallbackLevel


class IdentData(msgspec.Struct, frozen=True):
    """Data structure for release identification."""

    artist: str
    album: str
    year: int | str | None
    track_count: int | None
    source: str
    label: str | None = None
    catno: str | None = None


class ArtistRlsData(msgspec.Struct, frozen=True):
    """Data structure for artist release search results."""

    url: str
    quality: str | None
    year: int | str | None
    artist: str
    album: str
    label: str | None
    explicit: bool


class LabelRlsData(msgspec.Struct, frozen=True):
    """Data structure for label release search results."""

    url: str
    quality: str | None
    year: int | str | None
    artist: str
    album: str
    type: str | None
    explicit: bool


class SearchResult(msgspec.Struct, frozen=True):
    """A single search result returned by a metadata provider.

    All `SearchMixin.search_releases` implementations return
    `(provider_name, {release_id: SearchResult})`.
    """

    ident: IdentData
    formatted: str
    fallback_level: FallbackLevel


class SearchMixin(ABC):
    @staticmethod
    def is_active() -> bool:
        """Whether this source has the credentials/config needed to search."""
        return True

    @abstractmethod
    async def search_releases(
        self,
        searchstr: str,
        limit: int,
        **kwargs,
    ) -> tuple[str, dict[Any, SearchResult]]:
        """Search the metadata site for releases.

        Providers that support structured search may use the kwargs to refine
        their queries; providers limited to free-text should use `searchstr`
        and set `fallback_level=FallbackLevel.FREE_TEXT` on results.

        Supported kwargs: artist, album, year, label, catno, is_va

        Returns: `(provider_name, {release_id: SearchResult})`.
        """
        pass

    @staticmethod
    def format_result(
        artists,
        title,
        edition,
        track_count=None,
        ed_title=None,
        country_code=None,
        explicit=False,
        clean=False,
        additional_info=None,
    ):
        """
        Take the attributes of a search result and format them into a
        string with ANSI bells and whistles.
        """
        artists = click.style(artists, fg="yellow")
        title = click.style(title, fg="yellow", bold=True)
        result = f"{artists} - {title}"

        if track_count:
            result += f" {{Tracks: {click.style(str(track_count), fg='green')}}}"
        if ed_title:
            result += f" {{{click.style(ed_title, fg='yellow')}}}"
        if edition:
            result += f" {click.style(edition, fg='green')}"
        if explicit:
            result = click.style("[E] ", fg="red", bold=True) + result
        if clean:
            result = click.style("[C] ", fg="cyan", bold=True) + result
        if country_code:
            result = f"[{country_code}] " + result
        # Add any additional information that might be helpful to identify the release
        if additional_info:
            result += f" {additional_info}"

        return result
