from salmon.search.discogs import Searcher
from salmon.search.scoring import FallbackLevel


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


class TestDiscogsFallbackChain:
    def test_sentinel_artist_skips_tier_1(self):
        """When artist is 'Unknown Artist', no artist-anchored chains run."""
        chains = _build(
            artist="Unknown Artist",
            album="HOSTOM - 004",
            year=2017,
            label="Hostom",
        )
        for params, _level in chains:
            assert "artist" not in params, f"sentinel artist leaked into chain: {params}"

    def test_label_anchored_chain_for_hostom_case(self):
        """The exact hostom-004 case: label + album + year should produce a
        structured query the user manually verified works on Discogs.
        """
        chains = _build(
            artist="Unknown Artist",
            album="HOSTOM - 004",
            year=2017,
            label="Hostom",
        )
        target = {"release_title": "HOSTOM - 004", "label": "Hostom", "year": "2017"}
        assert any(params == target for params, _ in chains), (
            f"expected label+album+year chain, got: {[c[0] for c in chains]}"
        )

    def test_real_artist_uses_tier_1(self):
        """When artist is real, artist-anchored chains run first."""
        chains = _build(
            artist="Burial",
            album="Subtemple",
            year=2017,
            label="Hyperdub",
        )
        # First structured chain should include artist
        first_params, first_level = chains[0]
        assert "artist" in first_params
        assert first_params["artist"] == "Burial"
        assert first_level == FallbackLevel.STRUCTURED

    def test_real_artist_also_has_label_fallback(self):
        """Real-artist releases ALSO get label-anchored chains as fallback."""
        chains = _build(
            artist="Burial",
            album="Subtemple",
            year=2017,
            label="Hyperdub",
        )
        # Somewhere after the artist chains, label-only chains should appear
        assert any(
            "label" in params and "artist" not in params
            for params, _ in chains
        ), f"expected label-only fallback chain, got: {[c[0] for c in chains]}"

    def test_bare_album_only_when_no_label(self):
        """Bare-album chain runs only when label is missing."""
        chains_no_label = _build(album="Some Album")
        chains_with_label = _build(album="Some Album", label="Some Label")

        has_bare = lambda cs: any(  # noqa: E731
            params == {"release_title": "Some Album"} for params, _ in cs
        )
        assert has_bare(chains_no_label)
        assert not has_bare(chains_with_label)

    def test_free_text_always_included(self):
        """Final fallback is always a free-text q= chain."""
        chains = _build(album="Whatever")
        params, level = chains[-1]
        assert "q" in params
        assert level == FallbackLevel.FREE_TEXT

    def test_various_artists_is_sentinel(self):
        """'Various Artists' also skips Tier 1."""
        chains = _build(
            artist="Various Artists",
            album="Comp 01",
            label="Some Label",
            year=2020,
        )
        for params, _ in chains:
            assert "artist" not in params
