# Metadata Search and Scoring

This document describes how smoked-salmon searches for and ranks releases across metadata providers, and how to measure changes to that pipeline with the local benchmark harness.

## Overview

When you upload a release, salmon queries several metadata providers in parallel, scores each result against the release's existing tag data, and presents the ranked list for you to pick from. The goal is that the correct release ranks first (or at worst in the top few) so you don't have to scroll or search manually.

There are two halves:

1. **Search** — building provider queries and parsing responses. Lives under `src/salmon/search/`.
2. **Scoring** — ranking each returned result against known tag data. Lives in `src/salmon/search/scoring.py`.

Both halves are independent: you can change one without affecting the other, and the benchmark measures each in isolation.

## Providers

All providers are registered in `src/salmon/search/__init__.py::SEARCHSOURCES`:

| Provider | Query style | Notes |
|---|---|---|
| Discogs | Structured (`artist=`, `label=`, `catno=`, `release_title=`, `year=`) | Full fallback chain; master URLs supported via version-set resolution |
| MusicBrainz | Structured (Lucene-backed via `musicbrainzngs`) | Full fallback chain |
| Deezer | Structured advanced-query syntax (`artist:"X" album:"Y"`) | Inner quotes stripped to avoid breaking the parser |
| Apple Music | Free-text `term=` via iTunes Search API | Multi-storefront support |
| Bandcamp | Free-text HTML scrape | Label-scoped via subdomain |
| Beatport | Free-text HTML scrape | Anti-bot flakiness observed |
| Qobuz | Free-text JSON search | Requires API token |
| Tidal | Free-text JSON search | Requires token; multi-region |

Each provider implements `SearchMixin.search_releases(searchstr, limit, **kwargs)` and returns `(provider_name, {release_id: SearchResult})`. The `SearchResult` struct (defined in `src/salmon/search/base.py`) has three fields: `ident: IdentData`, `formatted: str`, `fallback_level: FallbackLevel`.

## Scoring

`score_result(result: IdentData, tag: TagData) -> float` in `src/salmon/search/scoring.py` produces a 0-100 score. Higher is better. Default threshold for "show this result" is 40 (configurable via `upload.search.min_score_threshold`).

### Weights (non-VA)

| Field | Weight |
|---|---|
| album | 25 |
| artist | 20 |
| year | 10 |
| label | 10 |
| catno | 10 |
| track_count | 15 |
| source | 10 |

VA releases have reduced artist weight (5) and increased label/catno/track_count weights — the idea being that a compilation is best identified by its release metadata, not its (often missing or "Various") artist field.

Weights live in `_get_weights(is_va: bool)`.

### Key scoring decisions

**Sparse result penalty.** When the tag has a value for a field but the result doesn't, the field's weight still counts toward the denominator. This deliberately penalizes providers returning sparse metadata — the intuition is that a provider returning `None` for a known-good label is less trustworthy than one returning the matching label.

**Label-as-artist cross-field credit.** When the result's `artist` field fuzzy-matches the tag's `label` (common for anonymous techno/dub releases where metadata sources list the label in the artist position), the artist field is credited at 60% of the label match instead of treating it as a hard mismatch. See `score_result` in `scoring.py`.

**Fuzzy matching via rapidfuzz.** `_fuzzy_album` and `_fuzzy_artist` use `rapidfuzz.fuzz.token_sort_ratio` (reorder-tolerant, but — unlike `token_set_ratio` — does NOT treat a token-set subset as a perfect match). This avoids the bug where "Chronic" would match "Chronic Girl" at 100%.

**Normalization before matching.** Both sides are passed through `_normalize` which:

- Strips diacritics (`Café` → `Cafe`)
- Expands abbreviations (`Pt.` → `part`, `Vol.` → `volume`, `&` → `and`, `feat.` → `featuring`)
- Normalizes multi-character roman numerals II-X (`Part II` → `Part 2`). Single-character romans (`I`, `V`, `X`) are intentionally NOT normalized because they false-positive on real words and on `V.A.` and similar abbreviations.
- Drops stopwords (`the`, `a`, `an`) so `The Wall` matches `Wall`
- Lowercases and collapses whitespace

The same normalization helpers live in `src/salmon/common/strings.py` (`normalize_abbreviations`, `normalize_romans`, `strip_stopwords`, `normalize_searchstr`) so both the scoring layer AND the searchstr-building layer (`make_searchstrs`) apply consistent transformations.

### Searchstr normalization

`make_searchstrs()` in `src/salmon/common/strings.py` builds the free-text search query used by providers that don't have structured APIs (Bandcamp, Apple Music, Beatport, Qobuz, Tidal). It applies `normalize_searchstr()` unconditionally — a lighter version of scoring's `_normalize` that keeps punctuation like hyphens (needed for `Jay-Z` type names) and doesn't NFKD-normalize (so CJK titles survive). The important bits — abbreviation expansion, roman numerals, stopwords, whitespace collapse — are applied everywhere.

## Fallback chains

Discogs and MusicBrainz build a tiered fallback chain per search:

- **Tier 1 — artist-anchored.** Only when `tag.artist` is a real artist (not a sentinel like "Unknown Artist" or "Various"). Tries progressively looser structured queries: `{artist, release, year, label, catno}` → `{artist, release, year}` → `{artist, release}`.
- **Tier 2 — label-anchored.** Runs whenever a label is known, regardless of artist. Tries: `{release, label, year, catno}` → `{release, label, year}` → `{release, label, catno}` → `{release, label}`. This is the tier that finds anonymous releases (e.g. Hostom vinyl records where the "artist" is just "Unknown Artist" and the useful identifier is the label + catno).
- **Tier 2b — bare release title.** Only when there's no label anchor either.
- **Tier 3 — free-text.** The `q=searchstr` fallback.
- **Tier 3b — accent-normalized free-text.** Discogs only, for non-ASCII titles.

Chain construction is in `src/salmon/search/discogs.py::_build_fallback_chain` and `src/salmon/search/musicbrainz.py::_build_fallback_chain`. Each chain entry is a `(params_dict, FallbackLevel)` tuple, so each entry self-describes its match quality rather than inferring it from position.

`FallbackLevel` (in `scoring.py`) has four values: `STRUCTURED`, `PARTIAL_STRUCTURED`, `FREE_TEXT`, `LOOSE`. It's an `IntEnum` so comparisons work and it interoperates with code expecting ints.

## Sentinel artist detection

`is_sentinel_artist(artist)` (in `scoring.py`) returns `True` for `"Unknown Artist"`, `"Unknown"`, `"Various"`, `"Various Artists"`, `"VA"`, `"V.A."`, `"Anonymous"`, `"No Artist"`, and empty strings. Used by fallback chain builders to decide whether to emit Tier 1 chains — sentinel artists go straight to Tier 2 because passing `artist="Unknown Artist"` as a provider filter returns zero results. Also used by `_detect_va` in `src/salmon/tagger/metadata.py` to identify VA releases without false-matching real artists like "Various Production" (a UK dubstep act).

Note that `is_sentinel_artist` and `_detect_va` (in `src/salmon/tagger/metadata.py`) are intentionally separate:

- `_detect_va` is a release-level judgment ("is this a compilation?"), used during scoring to adjust artist weight. Triggers at 6+ main artists or any artist containing "various".
- `is_sentinel_artist` is a tag-quality judgment ("does the artist tag identify anyone?"), used for query construction.

A release can be both, just VA, just sentinel-artist, or neither.

## Configuration

Relevant keys in `config.toml` under `[upload.search]`:

```toml
[upload.search]
limit = 3                              # max results per provider to show
min_score_threshold = 40               # below this, results are hidden by default
show_all_results = false               # override threshold filtering
excluded_labels = ["edm comps"]        # labels whose results are dropped entirely
blacklisted_genres = ["Soundtrack", "Asian Music"]
```

Under `[upload.formatting]`:

```toml
various_artist_threshold = 4           # used for display formatting — NOT the is_va detection threshold
```

The VA **display** threshold (formatting only — when to collapse many artists into "Various Artists" in filenames) is 4. The VA **detection** threshold (for metadata search behavior) is hardcoded at 6 in `_detect_va` to avoid misclassifying 4-artist collabs (posse cuts, chamber quartets). See the inline comment in `src/salmon/tagger/metadata.py::_detect_va` for rationale.

## Benchmark harness

`benchmarks/` (gitignored) contains a local-only benchmark harness for measuring changes to the search and scoring pipeline. The goal is to turn scoring tweaks from guesswork into measurable improvements by running the algorithm against a corpus of real releases with known-correct ground-truth URLs.

### Directory layout

```
benchmarks/
├── capture.py               # capture a single album folder as a corpus entry
├── capture_tree.py          # bulk capture by walking a directory tree
├── suggest.py               # cross-validation: scrape labeled providers, suggest ground truth for unlabeled ones
├── run.py                   # the main benchmark harness
├── noise.py                 # probabilistic noise pipeline for resilience testing
├── noise-profiles/*.toml    # preset noise configurations
├── corpus/*.json            # labeled corpus entries (tag_data + ground_truth URLs)
├── cache/                   # per-slug provider response cache (no-noise runs)
└── cache_noise/             # query-hash cache (noise runs)
```

### Corpus entry schema

```json
{
  "slug": "burial-subtemple",
  "captured_at": "2026-04-08T21:30:00Z",
  "category": "representative",
  "notes": "",
  "tag_data": {
    "artists": [["Burial", "main"]],
    "title": "Subtemple",
    "year": 2017,
    "label": null,
    "catno": null,
    "track_count": 2,
    "source": null
  },
  "ground_truth": {
    "Discogs": "https://www.discogs.com/master/1182270-Burial-Subtemple",
    "Bandcamp": "https://burial.bandcamp.com/album/subtemple"
  }
}
```

### Capturing corpus entries

Single folder:

```bash
uv run python benchmarks/capture.py /path/to/album \
    --discogs https://www.discogs.com/release/1234567 \
    --bandcamp https://artist.bandcamp.com/album/name \
    --source Vinyl \
    --force
```

Bulk from a music library:

```bash
uv run python benchmarks/capture_tree.py ~/music \
    --only-missing \
    --source-from-folder
```

`capture_tree.py` walks the directory, captures every leaf album folder, auto-detects source from bracketed folder tokens (`[Vinyl]`, `[WEB]`, `[CD]`, `[Cassette]`), and skips folders already in the corpus. Ground-truth URLs are left empty — add them via manual edit or via the suggest tool.

### Running the benchmark

```bash
# Basic run
uv run python benchmarks/run.py

# Refresh all cached provider responses (use when provider data might have changed)
uv run python benchmarks/run.py --refresh

# Save a baseline for regression comparison
uv run python benchmarks/run.py --save-baseline benchmarks/baseline-$(date +%F).json

# Compare against a saved baseline — exits 2 if any provider's recall@1 regressed
uv run python benchmarks/run.py --compare benchmarks/baseline-2026-04-09-post-rapidfuzz.json \
    --max-regression 0.03
```

The output reports per-provider recall@1, recall@3, recall@5, MRR, and `n` (entries with ground truth). Worst-performing entries are listed at the bottom so you can investigate specific failures.

### Noise testing

The noise pipeline lets you measure how the algorithm degrades when tag data is corrupted. Presets live in `benchmarks/noise-profiles/`:

| Preset | What it tests |
|---|---|
| `pristine` | No noise — baseline |
| `minimal_tags` | Field dropping only (label/catno/year/etc.) — simulates minimally-tagged files |
| `typos_only` | Character-level typos only — measures rapidfuzz's typo tolerance |
| `realistic` | Mixed bag of light-to-medium noise — simulates a real-world library distribution |
| `aggressive` | High probabilities across all buckets — stress test |

```bash
uv run python benchmarks/run.py --noise-preset realistic
uv run python benchmarks/run.py --noise-preset aggressive
```

Each noise run has its own cache keyed by `(slug, provider, seed, noise_config)`, so the results are deterministic and repeatable. Change a probability in the TOML file → cache is correctly invalidated and the run re-queries providers.

Noise types are split into five buckets:

1. **Field dropping** — `drop_label`, `drop_catno`, `drop_year`, `drop_track_count`, `drop_source`
2. **Numeric drift** — `year_off_by_{1,5,10}`, `track_count_off_by_1`
3. **Character-level errors** — `album_swap_adjacent_chars`, `album_drop_char`, `album_case_flip`, `album_double_space`, `artist_trailing_whitespace`
4. **Unicode drift** — `album_strip_accents`, `album_transliterate`, `album_fullwidth_to_halfwidth`, `album_mojibake`
5. **Semantic/formatting drift** — `album_feat_drop`, `album_feat_add`, `album_edition_add`, `album_edition_strip`, `album_volume_abbrev_drift`, `album_roman_numeral_drift`, `label_as_artist`, `catno_as_catno_no_label`, `swap_artist_album`

All 27 functions are in `benchmarks/noise.py::NOISE_FUNCTIONS`. Each takes `(TagData, random.Random)` and returns either a modified `TagData` or `None` if the noise is not applicable (e.g. `album_strip_accents` on an ASCII-only title).

### Cross-validation / corpus expansion

`benchmarks/suggest.py` scrapes the highest-priority labeled provider for each corpus entry to build an "oracle" of enriched metadata, then queries unlabeled providers with the enriched data. Outputs:

- **INPUT_GAP** — providers where `run.py` missed but the enriched query finds it. These are the cases where "the algorithm can find this release, but only when given better input" — the most actionable category.
- **SUGGESTIONS** — providers without ground truth where the enriched query confidently finds a match. Copy the URL into the corpus YAML manually.
- **EXCLUSIONS** — providers in ground truth where the enriched query returns zero results. Suggests the release isn't on that platform and the "miss" should be ignored.
- **WARNINGS** — providers where the existing ground-truth URL may be wrong.

```bash
uv run python benchmarks/suggest.py                      # full corpus
uv run python benchmarks/suggest.py --slug burial-subtemple   # single entry
```

### Workflow for validating a scoring change

1. Save a baseline of the current state: `uv run python benchmarks/run.py --save-baseline benchmarks/before.json`
2. Make the scoring change in `src/salmon/search/scoring.py` and add tests.
3. Run the suite: `uv run pytest tests/ -q`
4. Measure: `uv run python benchmarks/run.py --refresh --compare benchmarks/before.json`
5. If any provider regressed (>3% recall@1 drop by default), the exit code is 2. Diagnose before committing.
6. If the change is a pure improvement, save a new baseline: `uv run python benchmarks/run.py --save-baseline benchmarks/after.json`
7. Commit.

This workflow caught a real regression during the rapidfuzz migration: `token_set_ratio` was the wrong function for album matching because it treats token-set subsets as perfect matches ("Chronic" matched "Chronic Girl" at 100%). The benchmark's `--compare` mode surfaced this as a Beatport regression; switching to `token_sort_ratio` fixed it while preserving the other wins.

## Production test coverage

Key test files under `tests/`:

- `test_search_scoring.py` — scoring primitives, fuzzy matching, label-as-artist credit, sentinel detection, roman/abbreviation normalization
- `test_search_discogs.py` — Discogs `_clean_artist` / `_clean_album`
- `test_search_discogs_chains.py` — fallback chain ordering (sentinel artists skip Tier 1, label-anchored tier works)
- `test_search_musicbrainz_chains.py` — MB fallback chain ordering
- `test_search_deezer.py` — advanced query quote escaping
- `test_run_metasearch.py` — end-to-end characterization of `run_metasearch` with mocked providers
- `test_tagger_metadata.py` — `_detect_va` threshold behavior
- `test_common_strings.py` — searchstr normalization

Run everything:

```bash
uv run pytest tests/ -q
```

## Pointers for common tasks

| I want to... | Look at... |
|---|---|
| Change the scoring weights | `src/salmon/search/scoring.py::_get_weights` |
| Add a new provider | Implement `SearchMixin` in `src/salmon/search/<name>.py`, register in `SEARCHSOURCES` |
| Add a fallback chain entry | `_build_fallback_chain` in `discogs.py` / `musicbrainz.py` |
| Change the VA detection threshold | `_detect_va` in `src/salmon/tagger/metadata.py` |
| Add a new noise type | `NOISE_FUNCTIONS` in `benchmarks/noise.py` |
| Validate a scoring change | Follow the workflow in the benchmark section above |
| Understand why a release doesn't match | Run `benchmarks/suggest.py --slug <slug>` to see enriched-query results |
