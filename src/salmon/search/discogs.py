import re

import asyncclick as click
from unidecode import unidecode

from salmon import cfg
from salmon.search.base import IdentData, SearchMixin, SearchResult
from salmon.search.scoring import FallbackLevel, strip_album_noise
from salmon.sources import DiscogsBase

SOURCES = {
    "Vinyl": "Vinyl",
    "File": "WEB",
    "CD": "CD",
}


class Searcher(DiscogsBase, SearchMixin):
    @staticmethod
    def is_active() -> bool:
        return bool(cfg.metadata.discogs_token)

    async def search_releases(self, searchstr, limit, **kwargs):
        releases = {}
        artist = kwargs.get("artist")
        album = kwargs.get("album")
        year = kwargs.get("year")
        label = kwargs.get("label")
        catno = kwargs.get("catno")
        is_va = kwargs.get("is_va", False)

        results, fallback_level = await self._structured_search(
            searchstr,
            limit,
            artist=artist,
            album=album,
            year=year,
            label=label,
            catno=catno,
            is_va=is_va,
        )

        for rls in results:
            artists, title = rls["title"].split(" - ", 1)
            rls_year = rls.get("year", None)
            source = parse_source(rls["format"])
            ed_title = ", ".join(set(rls["format"]))

            rls_label = None
            rls_catno = rls.get("catno") or None
            edition = f"{rls_year} {source}"
            if rls["label"] and rls["label"][0] != "Not On Label":
                rls_label = rls["label"][0]
                edition += f" {rls_label} {rls['catno']}"
            else:
                edition += " Not On Label"

            release_in_user_collection = rls["user_data"]["in_collection"]
            collection_text = click.style("IN COLLECTION", bg="red", bold=True) if release_in_user_collection else None

            releases[rls["id"]] = SearchResult(
                ident=IdentData(artists, title, rls_year, None, source or "", label=rls_label, catno=rls_catno),
                formatted=self.format_result(
                    artists,
                    title,
                    edition,
                    ed_title=ed_title,
                    additional_info=collection_text,
                ),
                fallback_level=fallback_level,
            )
            if len(releases) == limit:
                break
        return "Discogs", releases

    async def _structured_search(self, searchstr, limit, *, artist, album, year, label, catno, is_va):
        """Try structured params with fallback chain."""
        chains = self._build_fallback_chain(
            searchstr,
            artist=artist,
            album=album,
            year=year,
            label=label,
            catno=catno,
            is_va=is_va,
        )
        last_idx = len(chains) - 1
        for level, params in enumerate(chains):
            resp = await self.get_json(
                "/database/search",
                params={**params, "type": "release", "perpage": 50},
            )
            if resp.get("results"):
                return resp["results"][: limit * 2], _map_fallback_level(level, last_idx)
        return [], FallbackLevel.LOOSE

    @staticmethod
    def _build_fallback_chain(searchstr, *, artist, album, year, label, catno, is_va):
        """Build a list of param dicts from most specific to least."""
        # Clean inputs for structured search
        artist = _clean_artist(artist) if artist else None
        album = _clean_album(album) if album else None

        chains = []
        if is_va:
            if album and label and catno:
                chains.append({"release_title": album, "label": label, "catno": catno})
            if album and label:
                chains.append({"release_title": album, "label": label})
            if album:
                chains.append({"release_title": album})
        else:
            if artist and album and year and label and catno:
                chains.append(
                    {
                        "artist": artist,
                        "release_title": album,
                        "year": str(year),
                        "label": label,
                        "catno": catno,
                    }
                )
            if artist and album and year:
                chains.append(
                    {
                        "artist": artist,
                        "release_title": album,
                        "year": str(year),
                    }
                )
            if artist and album:
                chains.append({"artist": artist, "release_title": album})
        chains.append({"q": searchstr})

        # Also try with normalized accents as a final structured attempt
        normalized = _normalize_accents(searchstr)
        if normalized != searchstr:
            chains.append({"q": normalized})

        return chains


def _map_fallback_level(idx: int, last_idx: int) -> FallbackLevel:
    """Map chain index to FallbackLevel enum."""
    if idx == 0:
        return FallbackLevel.STRUCTURED
    if idx == 1:
        return FallbackLevel.PARTIAL_STRUCTURED
    if idx == last_idx:
        return FallbackLevel.LOOSE
    return FallbackLevel.FREE_TEXT


def sanitize_artist_name(name):
    """
    Remove parenthentical number disambiguation bullshit from artist names,
    as well as the asterisk stuff.
    """
    name = re.sub(r" \(\d+\)$", "", name)
    return re.sub(r"\*+$", "", name)


def _clean_artist(artist):
    """Strip disambiguation suffixes and normalize for search.

    Only strips parentheticals that look like disambiguators: numeric
    (e.g. "(2)") or short all-caps country codes (e.g. "(UK)", "(USA)").
    Preserves legitimate parentheticals like "(Live)", "(Acoustic)".
    """
    artist = re.sub(r"\s*\((?:\d+|[A-Z]{2,3})\)\s*$", "", artist)
    return _normalize_accents(artist).strip()


def _clean_album(album):
    """Normalize album title for Discogs search.

    Strips EP/Single markers in trailing `- EP`, ` EP`, `(EP)`, `[Single]`,
    etc. forms, shares the Remastered/Deluxe/feat. noise stripper with
    the scoring module, and transliterates accents.
    """
    # Strip bracketed/parenthetical EP/Single markers anywhere
    album = re.sub(r"\s*[\[\(]\s*(EP|Single)\s*[\]\)]\s*", " ", album, flags=re.IGNORECASE)
    # Strip trailing dash/space EP/Single
    album = re.sub(r"\s*[-–—]\s*(EP|Single)\s*$", "", album, flags=re.IGNORECASE)
    album = re.sub(r"\s+(EP|Single)\s*$", "", album, flags=re.IGNORECASE)
    # Shared noise stripping (Remastered, Deluxe, feat., etc.)
    album = strip_album_noise(album)
    return _normalize_accents(album).strip()


def _normalize_accents(s):
    """Transliterate non-ASCII characters to ASCII equivalents."""
    return unidecode(s)


def parse_source(formats):
    """
    Take the list of format strings provided by Discogs and iterate over them
    to find a possible source for the release.
    """
    for format_s, source in SOURCES.items():
        if any(format_s in f for f in formats):
            return source
