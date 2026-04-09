import asyncio
from typing import Any

import musicbrainzngs

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.search.base import IdentData, SearchMixin, SearchResult
from salmon.search.scoring import FallbackLevel, is_sentinel_artist
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
                    releases[rls["id"]] = SearchResult(
                        ident=IdentData(
                            artists,
                            rls["title"],
                            None,
                            track_count,
                            source or "",
                            label=rls_label or None,
                            catno=rls_catno or None,
                        ),
                        formatted=self.format_result(
                            artists,
                            rls["title"],
                            edition,
                            ed_title=source,
                            track_count=track_count,
                        ),
                        fallback_level=fallback_level,
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
        for search_kwargs, level in chains:
            result = await asyncio.to_thread(musicbrainzngs.search_releases, limit=limit, **search_kwargs)
            if result.get("release-list"):
                return result, level
        return {"release-list": []}, FallbackLevel.FREE_TEXT

    @staticmethod
    def _build_fallback_chain(searchstr, *, artist, album, year, label, catno, release_type, is_va):
        """Build (kwargs, FallbackLevel) pairs from most structured to loosest.

        Tier 1 - artist-anchored: only when the artist identifies someone
        specific (not a sentinel like "Unknown Artist" or "Various").
        Tier 2 - label-anchored: runs whenever a label is known; works for
        both anonymous releases and releases with real artists as a fallback.
        Tier 2b - bare album: when neither artist nor label help.
        Tier 3 - free text: final catch-all.
        """
        # is_va and release_type kept for API compat
        del is_va, release_type
        has_real_artist = bool(artist) and not is_sentinel_artist(artist)

        chains: list[tuple[dict, FallbackLevel]] = []

        # --- Tier 1: artist-anchored ---
        if has_real_artist and album:
            if year and label and catno:
                chains.append((
                    {
                        "artist": artist,
                        "release": album,
                        "date": str(year),
                        "label": label,
                        "catno": catno,
                    },
                    FallbackLevel.STRUCTURED,
                ))
            if year:
                chains.append((
                    {"artist": artist, "release": album, "date": str(year)},
                    FallbackLevel.STRUCTURED,
                ))
            chains.append((
                {"artist": artist, "release": album},
                FallbackLevel.PARTIAL_STRUCTURED,
            ))

        # --- Tier 2: label-anchored (no artist required) ---
        if album and label:
            if year and catno:
                chains.append((
                    {
                        "release": album,
                        "label": label,
                        "date": str(year),
                        "catno": catno,
                    },
                    FallbackLevel.STRUCTURED,
                ))
            if year:
                chains.append((
                    {"release": album, "label": label, "date": str(year)},
                    FallbackLevel.STRUCTURED,
                ))
            if catno:
                chains.append((
                    {"release": album, "label": label, "catno": catno},
                    FallbackLevel.STRUCTURED,
                ))
            chains.append((
                {"release": album, "label": label},
                FallbackLevel.PARTIAL_STRUCTURED,
            ))

        # --- Tier 2b: bare album (only when no label anchor) ---
        if album and not label:
            chains.append((
                {"release": album},
                FallbackLevel.PARTIAL_STRUCTURED,
            ))

        # --- Tier 3: free text ---
        chains.append(({"query": searchstr}, FallbackLevel.FREE_TEXT))
        return chains
