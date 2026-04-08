from salmon.search.scoring import (
    _fuzzy_album,
    _fuzzy_artist,
    _match_catno,
    _match_track_count,
    _match_year,
    _strip_album_noise,
    score_result,
)


def _score(**overrides):
    """Helper: perfect match with selective overrides."""
    defaults = dict(
        result_artist="The Artist",
        result_album="The Album",
        result_year=2020,
        result_track_count=10,
        result_source="WEB",
        result_label="Cool Label",
        result_catno="CL001",
        tag_artist="The Artist",
        tag_album="The Album",
        tag_year=2020,
        tag_track_count=10,
        tag_source="WEB",
        tag_label="Cool Label",
        tag_catno="CL001",
    )
    defaults.update(overrides)
    return score_result(**defaults)


class TestScoreResult:
    def test_perfect_match_scores_100(self):
        assert _score() == 100.0

    def test_no_tag_data_returns_neutral_50(self):
        s = score_result(
            result_artist="x", result_album="y", result_year=None,
            result_track_count=None, result_source=None,
            result_label=None, result_catno=None,
        )
        assert s == 50.0

    def test_album_mismatch_below_threshold(self):
        s = _score(result_album="Totally Different Title")
        assert s < 90

    def test_artist_weight_reduced_for_va(self):
        solo_score = _score(result_artist="Wrong Artist")
        va_score = _score(result_artist="Wrong Artist", is_va=True)
        assert va_score > solo_score

    def test_year_off_by_one_is_partial(self):
        s = _score(result_year=2021)
        assert 90 <= s < 100

    def test_year_off_by_two_is_zero(self):
        s = _score(result_year=2022)
        assert s < 95

    def test_track_count_off_by_one_is_partial(self):
        s = _score(result_track_count=11)
        assert s < 100 and s > 90

    def test_missing_result_label_dings_but_still_high(self):
        s = _score(result_label=None)
        assert s < 100

    def test_fallback_level_bonus_saturates(self):
        base = _score(fallback_level=0)
        deep = _score(fallback_level=5)
        assert base >= deep

    def test_accent_normalization_album(self):
        s = _score(result_album="Café", tag_album="Cafe")
        assert s == 100.0

    def test_feat_stripped_from_album(self):
        s = _score(result_album="Hit Song (feat. Someone)", tag_album="Hit Song")
        assert s >= 95

    def test_remastered_stripped_from_album(self):
        s = _score(
            result_album="Classic Album (Remastered)",
            tag_album="Classic Album",
        )
        assert s >= 95

    def test_catno_ignores_dashes_and_spaces(self):
        s = _score(result_catno="CL-001", tag_catno="CL001")
        assert s == 100.0

    def test_empty_strings_are_treated_as_missing(self):
        s = score_result(
            result_artist="A", result_album="B", result_year=None,
            result_track_count=None, result_source=None,
            result_label=None, result_catno=None,
            tag_artist="", tag_album="",
        )
        assert s == 50.0


class TestFuzzyAlbum:
    def test_identical(self):
        assert _fuzzy_album("Foo", "Foo") == 1.0

    def test_substring(self):
        assert _fuzzy_album("Foo", "Foo (Deluxe)") >= 0.85

    def test_unrelated(self):
        assert _fuzzy_album("Foo", "Bar") == 0.0


class TestFuzzyArtist:
    def test_identical(self):
        assert _fuzzy_artist("The Beatles", "The Beatles") == 1.0

    def test_token_overlap(self):
        assert 0 < _fuzzy_artist("Beatles", "The Beatles") < 1.0


class TestMatchYear:
    def test_exact(self):
        assert _match_year(2020, 2020) == 1.0

    def test_off_by_one(self):
        assert _match_year(2020, 2021) == 0.5
        assert _match_year(2021, 2020) == 0.5

    def test_off_by_two(self):
        assert _match_year(2020, 2022) == 0.0

    def test_string_year(self):
        assert _match_year("2020-01-01", "2020") == 1.0

    def test_invalid(self):
        assert _match_year(None, 2020) == 0.0
        assert _match_year("abc", 2020) == 0.0


class TestMatchCatno:
    def test_exact(self):
        assert _match_catno("ABC123", "ABC123") == 1.0

    def test_hyphen_normalized(self):
        assert _match_catno("ABC-123", "ABC123") == 1.0

    def test_space_normalized(self):
        assert _match_catno("ABC 123", "ABC123") == 1.0

    def test_case_insensitive(self):
        assert _match_catno("abc-123", "ABC123") == 1.0

    def test_mismatch(self):
        assert _match_catno("ABC123", "XYZ999") == 0.0


class TestMatchTrackCount:
    def test_exact(self):
        assert _match_track_count(10, 10) == 1.0

    def test_off_by_one(self):
        assert _match_track_count(10, 11) == 0.5

    def test_off_by_two(self):
        assert _match_track_count(10, 12) == 0.0

    def test_none(self):
        assert _match_track_count(10, None) == 0.0


class TestStripAlbumNoise:
    def test_strips_feat(self):
        assert "feat" not in _strip_album_noise("Song (feat. Other)").lower()

    def test_strips_remastered(self):
        assert "remastered" not in _strip_album_noise("Album (Remastered)").lower()

    def test_preserves_clean_title(self):
        assert _strip_album_noise("Clean Title") == "Clean Title"


class TestScoreWeightSemantics:
    def test_sparse_result_penalized(self):
        """Tag has year + label, result has neither → weight counted, score 0."""
        s = score_result(
            result_artist="A", result_album="B",
            result_year=None, result_track_count=None, result_source=None,
            result_label=None, result_catno=None,
            tag_artist="A", tag_album="B",
            tag_year=2020, tag_label="Foo",
        )
        # album(25) + artist(20) + year(10, 0 score) + label(10, 0 score)
        # = 45/65 * 100 ≈ 69.2
        assert 65 <= s <= 75


class TestFallbackLevelEnum:
    def test_enum_values_ordered(self):
        from salmon.search.scoring import FallbackLevel
        assert FallbackLevel.STRUCTURED < FallbackLevel.PARTIAL_STRUCTURED
        assert FallbackLevel.PARTIAL_STRUCTURED < FallbackLevel.FREE_TEXT
        assert FallbackLevel.FREE_TEXT < FallbackLevel.LOOSE

    def test_enum_is_int_compatible(self):
        from salmon.search.scoring import FallbackLevel
        # Should accept in score_result without error
        s = score_result(
            result_artist="A", result_album="B", result_year=None,
            result_track_count=None, result_source=None,
            result_label=None, result_catno=None,
            tag_artist="A", tag_album="B",
            fallback_level=FallbackLevel.STRUCTURED,
        )
        assert 0 <= s <= 100
