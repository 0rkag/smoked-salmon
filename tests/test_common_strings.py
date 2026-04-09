from salmon.common.strings import (
    make_searchstrs,
    normalize_abbreviations,
    normalize_romans,
    normalize_searchstr,
    strip_stopwords,
)


class TestNormalizeSearchstr:
    def test_collapses_whitespace(self):
        assert normalize_searchstr("kcik  3") == "kcik 3"

    def test_strips_trailing_whitespace(self):
        assert normalize_searchstr("  foo bar  ") == "foo bar"

    def test_expands_abbreviations(self):
        s = normalize_searchstr("Pt. 2")
        assert "part" in s

    def test_normalizes_romans(self):
        s = normalize_searchstr("Part II")
        assert "2" in s

    def test_strips_the_stopword(self):
        # "the" should be dropped, "wall" should remain
        result = normalize_searchstr("The Wall")
        assert "wall" in result
        assert "the" not in result.split()

    def test_ampersand_becomes_and(self):
        result = normalize_searchstr("Jay-Z & Kanye")
        assert "&" not in result
        assert "and" in result

    def test_empty_input(self):
        assert normalize_searchstr("") == ""
        assert normalize_searchstr(None if False else "") == ""


class TestMakeSearchstrsNormalization:
    def test_double_space_in_album_collapsed(self):
        result = make_searchstrs([("Unknown Artist", "main")], "Kcik  3")
        assert all("  " not in s for s in result)

    def test_abbreviations_expanded_in_searchstrs(self):
        result = make_searchstrs([("Floyd", "main")], "The Wall Pt. 2")
        assert all("pt." not in s.lower() for s in result)
        assert any("part" in s.lower() for s in result)

    def test_romans_normalized_in_searchstrs(self):
        result = make_searchstrs([("Composer", "main")], "Symphony Part III")
        assert any("3" in s for s in result)


class TestHelpers:
    def test_normalize_abbreviations_direct(self):
        assert "part" in normalize_abbreviations("Pt. 2")

    def test_normalize_romans_direct(self):
        assert normalize_romans("Part II") == "Part 2"

    def test_strip_stopwords_direct(self):
        assert strip_stopwords("the wall") == "wall"
