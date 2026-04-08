from salmon.search.deezer import Searcher
from salmon.search.scoring import FallbackLevel


class TestBuildQueryWithFallback:
    def test_strips_inner_quotes(self):
        query, level = Searcher._build_query_with_fallback(
            "fallback",
            artist='Band "Name"',
            album='The "Alternate" Sessions',
            label=None,
            is_va=False,
        )
        # Balanced quotes (all opening quotes have matching closing quotes)
        assert query.count('"') % 2 == 0
        # No empty quoted strings
        assert '""' not in query
        # Useful content preserved
        assert "Band" in query
        assert "Alternate" in query
        assert level == FallbackLevel.STRUCTURED

    def test_empty_params_returns_searchstr(self):
        query, level = Searcher._build_query_with_fallback(
            "just a searchstr",
            artist=None,
            album=None,
            label=None,
            is_va=False,
        )
        assert query == "just a searchstr"
        assert level == FallbackLevel.FREE_TEXT

    def test_va_skips_artist(self):
        query, _ = Searcher._build_query_with_fallback(
            "fallback",
            artist="Various",
            album="Compilation",
            label=None,
            is_va=True,
        )
        assert "artist:" not in query
        assert "album:" in query

    def test_label_included(self):
        query, _ = Searcher._build_query_with_fallback(
            "fallback",
            artist=None,
            album=None,
            label='Cool "Label"',
            is_va=False,
        )
        assert "label:" in query
        assert query.count('"') % 2 == 0
