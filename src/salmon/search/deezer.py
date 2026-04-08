import asyncio
import re
from itertools import chain

from salmon.search.base import (
    ArtistRlsData,
    IdentData,
    LabelRlsData,
    SearchMixin,
    SearchResult,
)
from salmon.search.scoring import FallbackLevel
from salmon.sources import DeezerBase


def _q(value: str) -> str:
    """Escape a value for Deezer's `field:"value"` advanced query syntax.

    Deezer does not document an escape mechanism for inner quotes, so we
    strip them rather than attempting to escape.
    """
    return value.replace('"', "").strip()


class Searcher(DeezerBase, SearchMixin):
    async def search_releases(self, searchstr, limit, **kwargs):
        artist = kwargs.get("artist")
        album = kwargs.get("album")
        label = kwargs.get("label")
        is_va = kwargs.get("is_va", False)

        releases = {}
        query, fallback_level = self._build_query_with_fallback(
            searchstr,
            artist=artist,
            album=album,
            label=label,
            is_va=is_va,
        )
        resp = await self.get_json("/search/album", params={"q": query})
        for rls in resp.get("data", []):
            releases[rls["id"]] = SearchResult(
                ident=IdentData(
                    rls["artist"]["name"],
                    rls["title"],
                    None,
                    rls["nb_tracks"],
                    "WEB",
                ),
                formatted=self.format_result(
                    rls["artist"]["name"],
                    rls["title"],
                    None,
                    track_count=rls["nb_tracks"],
                ),
                fallback_level=fallback_level,
            )
            if len(releases) == limit:
                break

        # If structured query returned nothing and we haven't tried free-text yet
        if not releases and fallback_level == FallbackLevel.STRUCTURED:
            resp = await self.get_json("/search/album", params={"q": searchstr})
            for rls in resp.get("data", []):
                releases[rls["id"]] = SearchResult(
                    ident=IdentData(
                        rls["artist"]["name"],
                        rls["title"],
                        None,
                        rls["nb_tracks"],
                        "WEB",
                    ),
                    formatted=self.format_result(
                        rls["artist"]["name"],
                        rls["title"],
                        None,
                        track_count=rls["nb_tracks"],
                    ),
                    fallback_level=FallbackLevel.FREE_TEXT,
                )
                if len(releases) == limit:
                    break

        return "Deezer", releases

    @staticmethod
    def _build_query_with_fallback(searchstr, *, artist, album, label, is_va):
        """Build advanced query syntax. Returns (query, FallbackLevel)."""
        parts = []
        if not is_va and artist:
            parts.append(f'artist:"{_q(artist)}"')
        if album:
            parts.append(f'album:"{_q(album)}"')
        if label:
            parts.append(f'label:"{_q(label)}"')
        if parts:
            return " ".join(parts), FallbackLevel.STRUCTURED
        return searchstr, FallbackLevel.FREE_TEXT

    async def get_artist_releases(self, artiststr):
        """
        Get the releases of an artist on Deezer. Find their artist page and request
        all their releases.
        """
        artist_ids = await self._get_artist_ids(artiststr)
        tasks = [self._get_artist_albums(artist_id, artiststr) for artist_id in artist_ids]
        return "Deezer", list(chain.from_iterable(await asyncio.gather(*tasks)))

    async def _get_artist_ids(self, artiststr):
        resp = await self.get_json("/search/artist", params={"q": artiststr})
        return [a["id"] for a in resp["data"] if a["name"].lower() == artiststr.lower()]

    async def _get_artist_albums(self, artist_id, artist_name):
        resp = await self.get_json(f"/artist/{artist_id}/albums")
        return [
            ArtistRlsData(
                url=rls["link"],
                quality="LOSSLESS",  # Cannot determine.
                year=self._parse_year(rls["release_date"]),
                artist=artist_name,
                album=rls["title"],
                label="",
                explicit=rls["explicit_lyrics"],
            )
            for rls in resp["data"]
        ]

    async def get_label_releases(self, labelstr, maximum=0, year=None):
        """Gets all the albums released by a label up to a total number.
        Year filtering doesn't actually work."""
        yearstr = "year='" + year + "'" if year else ""
        url_str = f"/search/album&q=label:'{labelstr}' {yearstr}/albums"
        resp = await self.get_json(url_str)
        albums = []
        i = 0
        while i < maximum or maximum == 0:
            print(i)
            i += 25
            for rls in resp["data"]:
                album = await self.get_json(f"/album/{rls['id']}")
                albums.append(
                    LabelRlsData(
                        url=rls["link"],
                        quality="LOSSLESS",  # Cannot determine.
                        year=str(self._parse_year(album["release_date"])),
                        artist=rls["artist"]["name"],
                        album=rls["title"],
                        type=album["record_type"],
                        explicit=rls["explicit_lyrics"],
                    )
                )
                if maximum > 0 and len(albums) >= maximum:
                    return "Deezer", albums
            if "next" in resp:
                resp = await self.get_json(url_str, params={"index": i})
            else:
                return "Deezer", albums
        return "Deezer", albums

    @staticmethod
    def _parse_year(date):
        try:
            match = re.search(r"(\d{4})", date)
            return int(match[0]) if match else None
        except (ValueError, IndexError, TypeError):
            return None
