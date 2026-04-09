from unittest.mock import patch

import pytest

from salmon.search import _derive_artist_str, run_metasearch
from salmon.search.base import IdentData, SearchResult
from salmon.search.scoring import FallbackLevel


class TestDeriveArtistStr:
    def test_none_artists(self):
        assert _derive_artist_str(None, is_va=False) is None

    def test_empty_artists(self):
        assert _derive_artist_str([], is_va=False) is None

    def test_single_artist(self):
        assert _derive_artist_str(["Solo"], is_va=False) == "Solo"

    def test_two_artists_uses_primary(self):
        assert _derive_artist_str(["A", "B"], is_va=False) == "A"

    def test_three_artists_uses_primary(self):
        assert _derive_artist_str(["A", "B", "C"], is_va=False) == "A"

    def test_va_returns_none(self):
        assert _derive_artist_str(["A", "B"], is_va=True) is None

    def test_va_with_single_artist_still_none(self):
        # Edge case: is_va flag dominates
        assert _derive_artist_str(["Solo"], is_va=True) is None


# ---------------------------------------------------------------------------
# End-to-end characterization tests for run_metasearch
# ---------------------------------------------------------------------------


class FakeActiveSearcher:
    """Stub searcher that records kwargs and returns a canned high-scoring result."""
    last_kwargs: dict | None = None

    @staticmethod
    def is_active() -> bool:
        return True

    async def search_releases(self, searchstr, limit, **kwargs):
        FakeActiveSearcher.last_kwargs = kwargs
        return "FakeActive", {
            "id1": SearchResult(
                ident=IdentData(
                    artist="The Artist",
                    album="The Album",
                    year=2020,
                    track_count=10,
                    source="WEB",
                    label="Cool Label",
                    catno="CL001",
                ),
                formatted="formatted-string",
                fallback_level=FallbackLevel.STRUCTURED,
            ),
        }


class FakeInactiveSearcher:
    @staticmethod
    def is_active() -> bool:
        return False

    async def search_releases(self, searchstr, limit, **kwargs):
        raise AssertionError("inactive source should never be called")


class FakeErroringSearcher:
    @staticmethod
    def is_active() -> bool:
        return True

    async def search_releases(self, searchstr, limit, **kwargs):
        from salmon.errors import ScrapeError
        raise ScrapeError("boom")


class FakeModule:
    """Shim so `.Searcher` attribute lookup works when SEARCHSOURCES is patched."""
    def __init__(self, cls):
        self.Searcher = cls


@pytest.fixture(autouse=True)
def _reset_fake_state():
    """Reset the class-level capture attribute before each test so tests
    don't leak kwarg state into each other (especially under parallel
    runners like pytest-xdist).
    """
    FakeActiveSearcher.last_kwargs = None
    yield


@pytest.fixture
def fake_sources():
    return {
        "FakeActive": FakeModule(FakeActiveSearcher),
        "FakeInactive": FakeModule(FakeInactiveSearcher),
        "FakeError": FakeModule(FakeErroringSearcher),
    }


class TestRunMetasearch:
    async def test_active_source_returns_results(self, fake_sources):
        with patch.dict("salmon.search.SEARCHSOURCES", fake_sources, clear=True):
            results = await run_metasearch(
                ["searchstr"],
                artists=["The Artist"],
                album="The Album",
                year=2020,
                track_count=10,
                label="Cool Label",
                catno="CL001",
                source_medium="WEB",
                apply_filter=True,
            )
        assert "FakeActive" in results
        assert results["FakeActive"], "expected non-empty results from active source"

    async def test_inactive_source_returns_none(self, fake_sources):
        with patch.dict("salmon.search.SEARCHSOURCES", fake_sources, clear=True):
            results = await run_metasearch(["searchstr"])
        assert "FakeInactive" in results
        assert results["FakeInactive"] is None

    async def test_erroring_source_absent_from_results(self, fake_sources):
        with patch.dict("salmon.search.SEARCHSOURCES", fake_sources, clear=True):
            results = await run_metasearch(["searchstr"])
        # handle_scrape_errors swallows the exception and the error path
        # drops the source from the results dict entirely
        assert "FakeError" not in results

    async def test_structured_kwargs_forwarded_to_provider(self, fake_sources):
        with patch.dict("salmon.search.SEARCHSOURCES", fake_sources, clear=True):
            await run_metasearch(
                ["searchstr"],
                artists=["The Artist"],
                album="The Album",
                year=2020,
                label="Cool Label",
                catno="CL001",
                is_va=False,
            )
        assert FakeActiveSearcher.last_kwargs is not None
        assert FakeActiveSearcher.last_kwargs["artist"] == "The Artist"
        assert FakeActiveSearcher.last_kwargs["album"] == "The Album"
        assert FakeActiveSearcher.last_kwargs["year"] == 2020
        assert FakeActiveSearcher.last_kwargs["label"] == "Cool Label"
        assert FakeActiveSearcher.last_kwargs["catno"] == "CL001"
        assert FakeActiveSearcher.last_kwargs["is_va"] is False

    async def test_apply_filter_true_filters_low_scores(self, fake_sources):
        """With wildly-wrong tag data and apply_filter=True, low-scoring results
        get filtered out by the min_score_threshold."""
        with patch.dict("salmon.search.SEARCHSOURCES", fake_sources, clear=True):
            results = await run_metasearch(
                ["searchstr"],
                artists=["Completely Different"],
                album="Completely Different Album",
                year=1950,
                track_count=100,
                label="Wrong",
                catno="W999",
                source_medium="CD",
                apply_filter=True,
            )
        # The fake returns one result that won't match these tags at all.
        # With default min_score_threshold=40 it should be filtered out.
        active_results = results.get("FakeActive", {})
        assert active_results == {}, f"expected empty after filtering, got {active_results}"

    async def test_apply_filter_false_keeps_all(self, fake_sources):
        """With apply_filter=False, results pass through without scoring."""
        with patch.dict("salmon.search.SEARCHSOURCES", fake_sources, clear=True):
            results = await run_metasearch(
                ["searchstr"],
                artists=["Completely Different"],
                album="Completely Different Album",
                apply_filter=False,
            )
        assert results.get("FakeActive"), "expected results when apply_filter=False"


class TestScoreAndFilterTiebreaker:
    """Direct unit tests for _score_and_filter_results' sort ordering,
    specifically the FallbackLevel tiebreaker when scores are equal.
    """

    def _make_result(self, rls_id: str, fallback_level: FallbackLevel) -> SearchResult:
        return SearchResult(
            ident=IdentData(
                artist="Same Artist",
                album="Same Album",
                year=2020,
                track_count=10,
                source="WEB",
                label="Same Label",
                catno="SAME001",
            ),
            formatted=f"formatted-{rls_id}",
            fallback_level=fallback_level,
        )

    def test_structured_beats_free_text_on_equal_score(self):
        """When two results produce identical scores, the one with a lower
        fallback_level (more structured) must rank first."""
        from salmon.search import _score_and_filter_results
        from salmon.search.scoring import TagData

        # Both results are structurally identical, so they score the same.
        # The only difference is fallback_level.
        free_text = self._make_result("free_id", FallbackLevel.FREE_TEXT)
        structured = self._make_result("structured_id", FallbackLevel.STRUCTURED)

        # Deliberately pass the free-text one first so dict insertion order
        # doesn't accidentally give the correct answer.
        results = {"free_id": free_text, "structured_id": structured}
        tag = TagData(
            artist="Same Artist", album="Same Album", year=2020,
            track_count=10, source="WEB", label="Same Label", catno="SAME001",
        )

        filtered = _score_and_filter_results(results, tag)
        keys = list(filtered.keys())
        assert keys[0] == "structured_id", (
            f"expected structured to beat free-text on tie; got order {keys}"
        )
