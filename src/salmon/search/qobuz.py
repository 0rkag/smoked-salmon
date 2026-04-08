import re
from typing import Any

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.search.base import (
    IdentData,
    SearchMixin,
)
from salmon.search.scoring import FallbackLevel
from salmon.sources.qobuz import QobuzBase


class Searcher(QobuzBase, SearchMixin):
    @staticmethod
    def is_active() -> bool:
        return bool(cfg.metadata.qobuz.app_id and cfg.metadata.qobuz.user_auth_token)

    async def search_releases(self, searchstr, limit, **kwargs):
        if not self.is_active():
            return "Qobuz", {}

        releases = {}
        try:
            resp = await self.get_json(
                "/catalog/search",
                params={"query": searchstr, "limit": limit, "offset": 0},
                headers=self.headers,
            )

            items = resp.get("albums", {}).get("items")

            if not items:
                return "Qobuz", {}

            for rls in items:
                try:
                    artists = rls["artist"]["name"]
                    title = rls["title"]
                    year = self._parse_year(rls.get("release_date_original"))
                    track_count = rls["tracks_count"]

                    rls_label = (rls.get("label") or {}).get("name")
                    edition = f"{year}"
                    if rls_label:
                        edition += f" {rls_label}"

                    format_details = []
                    if rls.get("hires"):
                        format_details.append("Hi-Res")
                    if rls.get("maximum_bit_depth"):
                        format_details.append(f"{rls['maximum_bit_depth']}bit")

                    ed_title = ", ".join(format_details) if format_details else None

                    releases[rls["id"]] = (
                        IdentData(
                            artists,
                            title,
                            year,
                            track_count,
                            "WEB",
                            label=rls_label,
                        ),
                        self.format_result(
                            artists,
                            title,
                            edition,
                            track_count=track_count,
                            ed_title=ed_title,
                            explicit=rls.get("parental_warning", False),
                        ),
                        FallbackLevel.FREE_TEXT,
                    )
                except (KeyError, TypeError, AttributeError):
                    # Skip individual release if it has missing/malformed data
                    continue

                if len(releases) == limit:
                    break

            return "Qobuz", releases
        except Exception as e:
            raise ScrapeError(f"Failed to retrieve or parse Qobuz search results: {str(e)}") from e

    @staticmethod
    def _parse_year(date):
        try:
            match = re.search(r"(\d{4})", date)
            return int(match[0]) if match else None
        except (ValueError, IndexError, TypeError):
            return None

    @classmethod
    def format_url(cls, rls_id: Any, rls_name: str | None = None, url: str | None = None) -> str:
        """Format a Qobuz URL from a release ID."""
        if url:
            return url
        return f"https://www.qobuz.com/album/-/{rls_id}"
