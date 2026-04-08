from salmon.search.discogs import _clean_album, _clean_artist


class TestCleanArtist:
    def test_strips_country_disambiguation(self):
        assert _clean_artist("Artist (UK)") == "Artist"

    def test_strips_numeric_disambiguation(self):
        assert _clean_artist("Artist (2)") == "Artist"

    def test_preserves_live_parenthetical(self):
        # "(Live)" is not a disambiguator; don't strip it.
        assert _clean_artist("Artist (Live)") == "Artist (Live)"

    def test_preserves_mi_a(self):
        # Real-world example of a legitimate 3-char parenthetical-free name
        assert _clean_artist("M.I.A.") == "M.I.A."

    def test_accent_normalization(self):
        assert _clean_artist("Beyoncé") == "Beyonce"


class TestCleanAlbum:
    def test_strips_trailing_dash_ep(self):
        assert _clean_album("Great Album - EP") == "Great Album"

    def test_strips_trailing_space_ep(self):
        assert _clean_album("Great Album EP") == "Great Album"

    def test_strips_parenthetical_ep(self):
        assert _clean_album("Great Album (EP)") == "Great Album"

    def test_strips_bracketed_single(self):
        assert _clean_album("Great Song [Single]") == "Great Song"

    def test_preserves_normal_title(self):
        assert _clean_album("Normal Album") == "Normal Album"

    def test_strips_remastered_via_shared_helper(self):
        assert _clean_album("Old Album (Remastered)") == "Old Album"

    def test_strips_feat_via_shared_helper(self):
        # Shared strip_album_noise should remove "feat." parentheticals
        result = _clean_album("Song (feat. Other)")
        assert "feat" not in result.lower()
