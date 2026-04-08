from salmon.search import _derive_artist_str


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
