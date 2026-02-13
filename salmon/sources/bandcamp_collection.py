"""Bandcamp collection fetcher using bandcampsync library."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

import click
from bandcampsync.bandcamp import Bandcamp

from salmon.sources.bandcamp_types import AlbumMetadata, CollectionItem
from salmon.tagger.sources.bandcamp import Scraper as BandcampScraper


class BandcampCollection:
    DEFAULT_SCRAPE_DELAY = 2.0  # seconds between album page requests

    def __init__(self, cookies):
        self.cookies = cookies
        self.bc = Bandcamp(cookies=cookies)
        self.scraper = BandcampScraper()

    def verify_auth(self):
        """Verify authentication with Bandcamp. Returns True if valid."""
        try:
            self.bc.verify_authentication()
            return True
        except Exception:
            return False

    def fetch_new_items(self, known_urls: set[str] | None = None) -> Iterator[CollectionItem]:
        """Yield new purchases from Bandcamp collection one at a time.

        Uses bandcampsync's Bandcamp.load_purchases() to get the full list,
        then yields items not already in the database (by URL).
        """
        known_urls = known_urls or set()
        click.echo("  Loading purchases from Bandcamp...")
        self.bc.load_purchases()
        purchases = self.bc.purchases
        click.echo(f"  Found {len(purchases)} total purchases.")

        for item in purchases:
            parsed = self._parse_bandcampsync_item(item)
            if not parsed:
                continue
            if parsed["bandcamp_url"] in known_urls:
                continue
            yield parsed

    def _parse_bandcampsync_item(self, item) -> CollectionItem | None:
        """Parse a bandcampsync BandcampItem into our format."""
        if not item.download_available:
            click.echo(f"  Skipping {item.band_name} - {item.item_title} (no download available)")
            return None

        # tralbum_type is the reliable field: 'a' = album, 't' = track
        item_type = "album" if item.tralbum_type == "a" else "track"

        bandcamp_url = self._extract_url_from_item(item)
        if not bandcamp_url:
            click.echo(f"  Skipping {item.band_name} - {item.item_title} (no URL found)")
            return None

        return CollectionItem(
            bandcamp_url=bandcamp_url,
            bandcamp_item_id=item.item_id,
            artist=item.band_name,
            title=item.item_title,
            item_type=item_type,
            purchase_date=item.purchased or "",
            cover_url=item.item_art_url,
        )

    def _extract_url_from_item(self, item):
        """Extract the Bandcamp album/track page URL from a BandcampItem.

        BandcampItem uses __getattr__ on raw API data, so item_url should
        be available from the collection API response. Falls back to
        constructing from url_hints if item_url is not present.
        """
        # Try the direct item_url field from the raw API response
        try:
            url = item.item_url
            if url:
                return url
        except (AttributeError, KeyError):
            pass

        # Fallback: construct from url_hints slug
        try:
            hints = item.url_hints
            if hints and isinstance(hints, dict):
                slug = hints.get("slug")
                custom_domain = hints.get("custom_domain")
                subdomain = hints.get("subdomain")
                if slug:
                    item_type = "album" if item.tralbum_type == "a" else "track"
                    if custom_domain:
                        return f"https://{custom_domain}/{item_type}/{slug}"
                    if subdomain:
                        return f"https://{subdomain}.bandcamp.com/{item_type}/{slug}"
        except (AttributeError, KeyError):
            pass

        return None

    def get_download_url(self, item_ref, encoding="flac"):
        """Get the download URL for a purchased item using bandcampsync.

        Args:
            item_ref: A bandcampsync BandcampItem object
            encoding: Download format (default: "flac")

        Returns:
            Download URL string, or None if unavailable.
        """
        try:
            url = self.bc.get_download_file_url(item_ref, encoding=encoding)
            if url:
                checked = self.bc.check_download_stat(item_ref, url)
                if checked:
                    return checked
                return url
        except Exception as e:
            click.secho(f"  Failed to get download URL: {e}", fg="red")
        return None

    async def scrape_album_metadata(self, bandcamp_url: str) -> AlbumMetadata | None:
        """Scrape full metadata from an album page using the existing scraper."""
        try:
            soup = await self.scraper.create_soup(bandcamp_url)
        except Exception as e:
            click.secho(f"  Failed to scrape {bandcamp_url}: {e}", fg="red")
            return None

        metadata = {}
        try:
            metadata["release_date"] = self.scraper.parse_release_date(soup)
        except Exception:
            metadata["release_date"] = None

        try:
            metadata["label"] = self.scraper.parse_release_label(soup)
        except Exception:
            metadata["label"] = None

        try:
            raw_tags = [a.string.strip() for a in soup.select(".tralbumData.tralbum-tags a.tag") if a.string]
            metadata["tags"] = raw_tags
        except Exception:
            metadata["tags"] = []

        try:
            genres = self.scraper.parse_genres(soup)
            metadata["genres"] = list(genres) if genres else []
        except Exception:
            metadata["genres"] = []

        try:
            metadata["cover_url"] = self.scraper.parse_cover_url(soup)
        except Exception:
            metadata["cover_url"] = None

        try:
            tracks = self.scraper.parse_tracks(soup)
            metadata["tracks"] = tracks
            metadata["track_count"] = sum(len(disc) for disc in tracks.values())
        except Exception:
            metadata["tracks"] = {}
            metadata["track_count"] = 0

        try:
            about_el = soup.select_one(".tralbumData.tralbum-about")
            metadata["description"] = about_el.text.strip() if about_el else None
        except Exception:
            metadata["description"] = None

        try:
            credits_el = soup.select_one(".tralbumData.tralbum-credits")
            metadata["credits"] = credits_el.text.strip() if credits_el else None
        except Exception:
            metadata["credits"] = None

        metadata["barcode"] = self._parse_barcode(soup)
        return metadata

    async def scrape_and_yield(self, items: list[CollectionItem]) -> AsyncIterator[CollectionItem]:
        """Scrape metadata for each item and yield it immediately.

        Yields items one at a time with metadata populated, suitable for
        incremental DB insertion.
        """
        for i, item in enumerate(items):
            url = item.get("bandcamp_url")
            if not url:
                click.secho(f"  Skipping [{i + 1}]: {item['artist']} - {item['title']} (no URL)", fg="yellow")
                continue
            click.echo(f"  Scraping metadata [{i + 1}]: {item['artist']} - {item['title']}")
            metadata = await self.scrape_album_metadata(url)
            if not metadata:
                click.secho(f"  Skipping [{i + 1}]: {item['artist']} - {item['title']} (scrape failed)", fg="red")
                continue
            item.update(metadata)
            yield item

    @staticmethod
    def _parse_barcode(soup):
        """Extract catalog number from ld+json albumRelease identifier."""
        try:
            for script in soup.select('script[type="application/ld+json"]'):
                data = json.loads(script.string)
                for release in data.get("albumRelease", []):
                    identifier = release.get("identifier")
                    if identifier:
                        return identifier
        except Exception:
            pass
        return None
