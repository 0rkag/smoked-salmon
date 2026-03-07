import re

import asyncclick as click

from salmon.search.base import IdentData, SearchMixin
from salmon.sources import DiscogsBase

SOURCES = {
    "Vinyl": "Vinyl",
    "File": "WEB",
    "CD": "CD",
}


class Searcher(DiscogsBase, SearchMixin):
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

            edition = f"{rls_year} {source}"
            if rls["label"] and rls["label"][0] != "Not On Label":
                edition += f" {rls['label'][0]} {rls['catno']}"
            else:
                edition += " Not On Label"

            release_in_user_collection = rls["user_data"]["in_collection"]
            collection_text = click.style("IN COLLECTION", bg="red", bold=True) if release_in_user_collection else None

            releases[rls["id"]] = (
                IdentData(artists, title, rls_year, None, source or ""),
                self.format_result(
                    artists,
                    title,
                    edition,
                    ed_title=ed_title,
                    additional_info=collection_text,
                ),
                fallback_level,
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
        for level, params in enumerate(chains):
            resp = await self.get_json(
                "/database/search",
                params={**params, "type": "release", "perpage": 50},
            )
            if resp.get("results"):
                return resp["results"][: limit * 2], level
        return [], len(chains) - 1

    @staticmethod
    def _build_fallback_chain(searchstr, *, artist, album, year, label, catno, is_va):
        """Build a list of param dicts from most specific to least."""
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
        return chains


def sanitize_artist_name(name):
    """
    Remove parenthentical number disambiguation bullshit from artist names,
    as well as the asterisk stuff.
    """
    name = re.sub(r" \(\d+\)$", "", name)
    return re.sub(r"\*+$", "", name)


def parse_source(formats):
    """
    Take the list of format strings provided by Discogs and iterate over them
    to find a possible source for the release.
    """
    for format_s, source in SOURCES.items():
        if any(format_s in f for f in formats):
            return source
