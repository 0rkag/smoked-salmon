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

    def test_various_in_mixed_list_triggers_va(self):
        # A placeholder alongside real artists still means VA.
        assert _detect_va(["Artist A", "Various"]) is True

    def test_empty_is_va(self):
        assert _detect_va([]) is True

    def test_unknown_artist_is_va(self):
        # "Unknown Artist" is a placeholder equivalent to VA for release typing
        assert _detect_va(["Unknown Artist"]) is True

    def test_various_production_is_not_va(self):
        # "Various Production" is a real UK dubstep act — the old substring
        # check matched because "various" ⊂ "various production", but the
        # new sentinel-based check rejects it.
        assert _detect_va(["Various Production"]) is False

    def test_variations_is_not_va(self):
        assert _detect_va(["Variations"]) is False
