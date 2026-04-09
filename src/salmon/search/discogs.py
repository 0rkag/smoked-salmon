import re

import asyncclick as click
from unidecode import unidecode

from salmon import cfg
from salmon.search.base import IdentData, SearchMixin, SearchResult
from salmon.search.scoring import FallbackLevel, is_sentinel_artist, strip_album_noise
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

        results, fallback_level = await self._structured_search(
            searchstr,
            limit,
            artist=artist,
            album=album,
            year=year,
            label=label,
            catno=catno,
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

    async def _structured_search(self, searchstr, limit, *, artist, album, year, label, catno):
        """Try structured params with fallback chain."""
        chains = self._build_fallback_chain(
            searchstr,
            artist=artist,
            album=album,
            year=year,
            label=label,
            catno=catno,
        )
        for params, level in chains:
            resp = await self.get_json(
                "/database/search",
                params={**params, "type": "release", "perpage": 50},
            )
            if resp.get("results"):
                return resp["results"][: limit * 2], level
        return [], FallbackLevel.LOOSE

    @staticmethod
    def _build_fallback_chain(searchstr, *, artist, album, year, label, catno):
        """Build (params, FallbackLevel) pairs from most structured to loosest.

        Tier 1 - artist-anchored: only when the artist identifies someone
        specific (not a sentinel like "Unknown Artist" or "Various").
        Tier 2 - label-anchored: runs whenever a label is known; works for
        both anonymous releases and releases with real artists as a fallback.
        Tier 2b - bare album: when neither artist nor label help.
        Tier 3 - free text: final catch-all.
        Tier 3b - accent-normalized free text: last resort for non-ASCII titles.
        """
        artist = _clean_artist(artist) if artist else None
        album = _clean_album(album) if album else None
        has_real_artist = bool(artist) and not is_sentinel_artist(artist)

        chains: list[tuple[dict, FallbackLevel]] = []

        # --- Tier 1: artist-anchored ---
        if has_real_artist and album:
            if year and label and catno:
                chains.append((
                    {
                        "artist": artist,
                        "release_title": album,
                        "year": str(year),
                        "label": label,
                        "catno": catno,
                    },
                    FallbackLevel.STRUCTURED,
                ))
            if year:
                chains.append((
                    {"artist": artist, "release_title": album, "year": str(year)},
                    FallbackLevel.STRUCTURED,
                ))
            chains.append((
                {"artist": artist, "release_title": album},
                FallbackLevel.PARTIAL_STRUCTURED,
            ))

        # --- Tier 2: label-anchored (no artist required) ---
        if album and label:
            if year and catno:
                chains.append((
                    {
                        "release_title": album,
                        "label": label,
                        "year": str(year),
                        "catno": catno,
                    },
                    FallbackLevel.STRUCTURED,
                ))
            if year:
                chains.append((
                    {"release_title": album, "label": label, "year": str(year)},
                    FallbackLevel.STRUCTURED,
                ))
            if catno:
                chains.append((
                    {"release_title": album, "label": label, "catno": catno},
                    FallbackLevel.STRUCTURED,
                ))
            chains.append((
                {"release_title": album, "label": label},
                FallbackLevel.PARTIAL_STRUCTURED,
            ))

        # --- Tier 2b: bare album (only when no label anchor) ---
        if album and not label:
            chains.append((
                {"release_title": album},
                FallbackLevel.PARTIAL_STRUCTURED,
            ))

        # --- Tier 3: free text ---
        chains.append(({"q": searchstr}, FallbackLevel.FREE_TEXT))

        # --- Tier 3b: accent-normalized free text ---
        normalized = _normalize_accents(searchstr)
        if normalized != searchstr:
            chains.append(({"q": normalized}, FallbackLevel.LOOSE))

        return chains


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
