from salmon.search.base import IdentData
from salmon.search.scoring import (
    TagData,
    _fuzzy_album,
    _fuzzy_artist,
    _match_catno,
    _match_track_count,
    _match_year,
    score_result,
    strip_album_noise,
)


def _score(**overrides):
    """Helper: perfect match with selective overrides.

    Overrides may use the old `result_*`/`tag_*` keyword style for back-compat
    with existing tests.
    """
    defaults = dict(
        result_artist="The Artist", result_album="The Album",
        result_year=2020, result_track_count=10, result_source="WEB",
        result_label="Cool Label", result_catno="CL001",
        tag_artist="The Artist", tag_album="The Album",
        tag_year=2020, tag_track_count=10, tag_source="WEB",
        tag_label="Cool Label", tag_catno="CL001",
        is_va=False,
    )
    # Discard unsupported overrides (fallback_level was removed)
    overrides.pop("fallback_level", None)
    defaults.update(overrides)
    result = IdentData(
        artist=defaults["result_artist"],
        album=defaults["result_album"],
        year=defaults["result_year"],
        track_count=defaults["result_track_count"],
        source=defaults["result_source"] or "",
        label=defaults["result_label"],
        catno=defaults["result_catno"],
    )
    tag = TagData(
        artist=defaults["tag_artist"],
        album=defaults["tag_album"],
        year=defaults["tag_year"],
        track_count=defaults["tag_track_count"],
        source=defaults["tag_source"],
        label=defaults["tag_label"],
        catno=defaults["tag_catno"],
        is_va=defaults["is_va"],
    )
    return score_result(result, tag)


class TestScoreResult:
    def test_perfect_match_scores_100(self):
        assert _score() == 100.0

    def test_no_tag_data_returns_neutral_50(self):
        s = score_result(
            IdentData(artist="x", album="y", year=None, track_count=None, source=""),
            TagData(),
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
            IdentData(artist="A", album="B", year=None, track_count=None, source=""),
            TagData(artist="", album=""),
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
        assert "feat" not in strip_album_noise("Song (feat. Other)").lower()

    def test_strips_remastered(self):
        assert "remastered" not in strip_album_noise("Album (Remastered)").lower()

    def test_preserves_clean_title(self):
        assert strip_album_noise("Clean Title") == "Clean Title"


class TestScoreWeightSemantics:
    def test_sparse_result_penalized(self):
        """Tag has year + label, result has neither → weight counted, score 0."""
        s = score_result(
            IdentData(
                artist="A", album="B", year=None, track_count=None,
                source="", label=None, catno=None,
            ),
            TagData(artist="A", album="B", year=2020, label="Foo"),
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


class TestLabelAsArtistCredit:
    """When a release lists the LABEL in the artist field (common for
    anonymous techno/dub), score_result should give partial credit instead
    of treating it as a hard mismatch.
    """

    def test_unknown_artist_with_matching_label_gets_credit(self):
        # The driving real-world case: hostom-004
        # tag has Unknown Artist + label Hostom; result has Hostom as artist
        score_with = _score(
            tag_artist="Unknown Artist",
            tag_album="HOSTOM - 004",
            tag_label="Hostom",
            tag_catno="HOSTOM004",
            tag_year=2017,
            tag_track_count=2,
            tag_source=None,  # benchmark capture default
            result_artist="Hostom",
            result_album="HOSTOM - 004",
            result_label="Hostom",
            result_catno=None,
            result_year=None,
            result_track_count=2,
            result_source="12\" Vinyl",
        )
        # Without cross-field credit: ~55 (artist contributes 0).
        # With cross-field credit: ~69 (artist contributes 12 of 20).
        # The remaining gap to 80 is the year/catno sparse-result penalty,
        # which is a separate concern and not addressed by this fix.
        assert score_with >= 65.0, f"expected >= 65 (with credit), got {score_with}"

    def test_no_credit_when_artist_already_matches(self):
        # When the artist legitimately matches, label-as-artist must NOT
        # double-bump the score. Score should be the standard perfect match.
        s = _score()  # everything matches
        assert s == 100.0

    def test_no_credit_when_no_label_in_tag(self):
        # If the tag has no label, the cross-field credit can't apply.
        s = _score(
            tag_artist="Unknown Artist",
            tag_label=None,
            result_artist="Hostom",
        )
        # Standard mismatch behavior — artist scores partial (rapidfuzz
        # WRatio gives ~0.45 for "Unknown Artist" vs "Hostom" via partial
        # token overlap). The point of this test is that no cross-field
        # label credit is added; the artist still fails to reach a
        # credited match score of 100. Bound relaxed from 80 -> 90 after
        # migrating from token-Jaccard to rapidfuzz, which is more
        # generous on short-token overlaps.
        assert s < 90.0

    def test_no_credit_when_result_artist_unrelated_to_label(self):
        # If result.artist doesn't match tag.label, no credit applies.
        s = _score(
            tag_artist="Unknown Artist",
            tag_label="Hostom",
            result_artist="Some Other Artist",
            result_label="Hostom",
        )
        # Standard mismatch behavior — no cross-field credit applies.
        # "Some Other Artist" vs "Unknown Artist" has token overlap on
        # "Artist" plus partial-ratio boost from rapidfuzz WRatio
        # (~0.57), and the cross-field check against "Hostom" yields 0,
        # so the artist field only gets its natural weak score — still
        # well below the 100 a credited match would produce. Bound
        # relaxed from 90 -> 95 after migrating from token-Jaccard to
        # rapidfuzz, which is more generous on short-token overlaps.
        assert s < 95.0

    def test_credit_does_not_apply_when_artist_match_strong(self):
        # When the standard artist match is already > 0.5, cross-field
        # check is skipped (this case: tag.artist == "Hostom Records" and
        # result.artist == "Hostom" — they match strongly enough on their own).
        s_normal = _score(
            tag_artist="Hostom Records",
            tag_label="Hostom",
            result_artist="Hostom",
        )
        # The standard fuzzy artist match should kick in here
        assert s_normal > 50.0


class TestIsSentinelArtist:
    def test_none(self):
        from salmon.search.scoring import is_sentinel_artist
        assert is_sentinel_artist(None) is True

    def test_empty(self):
        from salmon.search.scoring import is_sentinel_artist
        assert is_sentinel_artist("") is True

    def test_unknown_artist(self):
        from salmon.search.scoring import is_sentinel_artist
        assert is_sentinel_artist("Unknown Artist") is True

    def test_various_artists(self):
        from salmon.search.scoring import is_sentinel_artist
        assert is_sentinel_artist("Various Artists") is True

    def test_case_insensitive(self):
        from salmon.search.scoring import is_sentinel_artist
        assert is_sentinel_artist("VARIOUS") is True
        assert is_sentinel_artist("unknown") is True

    def test_whitespace_stripped(self):
        from salmon.search.scoring import is_sentinel_artist
        assert is_sentinel_artist("  Unknown Artist  ") is True

    def test_real_artist(self):
        from salmon.search.scoring import is_sentinel_artist
        assert is_sentinel_artist("Burial") is False
        assert is_sentinel_artist("The Beatles") is False

    def test_real_artist_containing_various(self):
        # "Variously" is NOT a sentinel
        from salmon.search.scoring import is_sentinel_artist
        assert is_sentinel_artist("Variously") is False


class TestFuzzyImprovements:
    """Regression tests for the rapidfuzz + normalization upgrade."""

    def test_roman_numeral_equivalence(self):
        from salmon.search.scoring import _fuzzy_album
        assert _fuzzy_album("Part II", "Pt. 2") >= 0.85

    def test_volume_abbreviation_equivalence(self):
        from salmon.search.scoring import _fuzzy_album
        assert _fuzzy_album("Vol. 1", "Volume 1") >= 0.85

    def test_the_stopword_handling(self):
        from salmon.search.scoring import _fuzzy_album
        assert _fuzzy_album("The Wall", "Wall") >= 0.85

    def test_ampersand_equivalence(self):
        from salmon.search.scoring import _fuzzy_artist
        assert _fuzzy_artist("Jay-Z & Kanye West", "Jay-Z and Kanye West") >= 0.85

    def test_volume_with_numeral(self):
        from salmon.search.scoring import _fuzzy_album
        assert _fuzzy_album("Greatest Hits, Vol. 2", "Greatest Hits, Volume 2") >= 0.95

    def test_no_spurious_matches(self):
        """Unrelated titles should still score low."""
        from salmon.search.scoring import _fuzzy_album
        assert _fuzzy_album("Burial", "Taylor Swift") < 0.5

    def test_normalize_abbreviations(self):
        from salmon.search.scoring import _normalize_abbreviations
        assert "part" in _normalize_abbreviations("Pt. 2")
        assert "volume" in _normalize_abbreviations("Vol. 1")
        assert "and" in _normalize_abbreviations("Jay & Beyoncé")

    def test_normalize_romans(self):
        from salmon.search.scoring import _normalize_romans
        assert _normalize_romans("Part II") == "Part 2"
        assert _normalize_romans("Vol III") == "Vol 3"
        # Do not mangle words that look like roman numerals
        assert _normalize_romans("Paradise City") == "Paradise City"  # no change, no roman
