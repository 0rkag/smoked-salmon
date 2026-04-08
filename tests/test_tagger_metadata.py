from salmon.tagger.metadata import _detect_va


class TestDetectVA:
    def test_single_artist_not_va(self):
        assert _detect_va(["Solo Artist"]) is False

    def test_duo_not_va(self):
        assert _detect_va(["A", "B"]) is False

    def test_trio_not_va(self):
        assert _detect_va(["A", "B", "C"]) is False

    def test_quartet_not_va(self):
        # 4-artist collab should NOT be VA
        assert _detect_va(["A", "B", "C", "D"]) is False

    def test_quintet_not_va(self):
        assert _detect_va(["A", "B", "C", "D", "E"]) is False

    def test_sextet_is_va(self):
        assert _detect_va(["A", "B", "C", "D", "E", "F"]) is True

    def test_various_artists_keyword(self):
        assert _detect_va(["Various Artists"]) is True

    def test_various_keyword(self):
        assert _detect_va(["Various"]) is True

    def test_various_substring_in_mixed_list(self):
        # "various" anywhere triggers VA
        assert _detect_va(["Artist A", "Various"]) is True

    def test_empty_is_va(self):
        assert _detect_va([]) is True
