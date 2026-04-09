from salmon.search.musicbrainz import Searcher


def _build(**kwargs):
    defaults = dict(
        searchstr="Unknown Artist HOSTOM - 004",
        artist=None,
        album=None,
        year=None,
        label=None,
        catno=None,
    )
    defaults.update(kwargs)
    return Searcher._build_fallback_chain(**defaults)


class TestMusicBrainzFallbackChain:
    def test_sentinel_artist_skips_tier_1(self):
        chains = _build(
            artist="Unknown Artist",
            album="HOSTOM - 004",
            year=2017,
            label="Hostom",
        )
        for params, _ in chains:
            assert "artist" not in params

    def test_label_anchored_chain(self):
        chains = _build(
            artist="Unknown Artist",
            album="HOSTOM - 004",
            year=2017,
            label="Hostom",
        )
        # MB uses 'release' and 'date' field names
        target = {"release": "HOSTOM - 004", "label": "Hostom", "date": "2017"}
        assert any(params == target for params, _ in chains)

    def test_real_artist_uses_tier_1(self):
        chains = _build(
            artist="Burial",
            album="Subtemple",
            year=2017,
        )
        first_params, _ = chains[0]
        assert first_params.get("artist") == "Burial"
