import asyncio
from typing import Any

import musicbrainzngs

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.search.base import IdentData, SearchMixin
from salmon.sources import MusicBrainzBase


class Searcher(MusicBrainzBase, SearchMixin):
    async def search_releases(self, searchstr: str, limit: int, **kwargs) -> tuple[str, dict[str, Any]]:
        artist = kwargs.get("artist")
        album = kwargs.get("album")
        year = kwargs.get("year")
        label = kwargs.get("label")
        catno = kwargs.get("catno")
        release_type = kwargs.get("release_type")
        is_va = kwargs.get("is_va", False)

        releases = {}
        result, fallback_level = await self._structured_search(
            searchstr,
            limit,
            artist=artist,
            album=album,
            year=year,
            label=label,
            catno=catno,
            release_type=release_type,
            is_va=is_va,
        )
        for rls in result.get("release-list", []):
            try:
                artists = rls["artist-credit-phrase"]
                try:
                    track_count = rls["medium-track-count"]
                except KeyError:
                    track_count = None
                rls_label = rls_catno = ""
                if (
                    "label-info-list" in rls
                    and rls["label-info-list"]
                    and "label" in rls["label-info-list"][0]
                    and "name" in rls["label-info-list"][0]["label"]
                ):
                    rls_label = rls["label-info-list"][0]["label"]["name"]
                    if "catalog_number" in rls["label-info-list"][0]:
                        rls_catno = rls["label-info-list"][0]["catalog_number"]

                try:
                    source = rls["medium-list"][0]["format"]
                except KeyError:
                    source = None

                edition = ""
                if rls_label:
                    edition += rls_label
                if rls_catno:
                    edition += " " + rls_catno

                if rls_label.lower() not in cfg.upload.search.excluded_labels:
                    releases[rls["id"]] = (
                        IdentData(
                            artists,
                            rls["title"],
                            None,
                            track_count,
                            source or "",
                        ),
                        self.format_result(
                            artists,
                            rls["title"],
                            edition,
                            ed_title=source,
                            track_count=track_count,
                        ),
                        fallback_level,
                    )
            except (TypeError, IndexError) as e:
                raise ScrapeError("Failed to parse scraped search results.") from e
            if len(releases) == limit:
                break
        return "MusicBrainz", releases

    async def _structured_search(
        self,
        searchstr,
        limit,
        *,
        artist,
        album,
        year,
        label,
        catno,
        release_type,
        is_va,
    ):
        """Try structured params with fallback chain."""
        chains = self._build_fallback_chain(
            searchstr,
            artist=artist,
            album=album,
            year=year,
            label=label,
            catno=catno,
            release_type=release_type,
            is_va=is_va,
        )
        for level, search_kwargs in enumerate(chains):
            result = await asyncio.to_thread(musicbrainzngs.search_releases, limit=limit, **search_kwargs)
            if result.get("release-list"):
                return result, level
        return {"release-list": []}, len(chains) - 1

    @staticmethod
    def _build_fallback_chain(searchstr, *, artist, album, year, label, catno, release_type, is_va):
        chains = []
        if is_va:
            if album and label and catno:
                chains.append({"release": album, "label": label, "catno": catno})
            if album and label:
                chains.append({"release": album, "label": label})
            if album:
                chains.append({"release": album})
        else:
            if artist and album and year and label and catno:
                chains.append(
                    {
                        "artist": artist,
                        "release": album,
                        "date": str(year),
                        "label": label,
                        "catno": catno,
                    }
                )
            if artist and album and year:
                chains.append({"artist": artist, "release": album, "date": str(year)})
            if artist and album:
                chains.append({"artist": artist, "release": album})
        chains.append({"query": searchstr})
        return chains
