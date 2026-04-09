#!/usr/bin/env python3
"""Cross-validation tool for the metadata-search benchmark corpus.

For each corpus entry:
  1. Pick the highest-priority labeled provider (oracle priority).
  2. Scrape its release URL via the production scraper to get an "oracle"
     dict of enriched metadata (title/year/label/catno/track_count/artist).
  3. Build an enriched TagData by overlaying oracle fields on the entry's
     existing tag_data.
  4. Re-query each non-oracle provider with the enriched TagData (cached
     separately from run.py's deterministic cache).
  5. Emit:
       - SUGGESTION ("add"): a high-confidence match for a provider not
         yet in ground_truth — the user can copy the URL into the YAML.
       - EXCLUSION ("exclude"): a provider in ground_truth where the
         enriched query still returns zero results — likely the release
         is not on that platform.
       - WARNING ("warning"): a provider in ground_truth where the
         enriched query returns a high-confidence match that does NOT
         match the recorded ground-truth URL — the existing GT may be
         wrong.

This tool does NOT mutate corpus entries. It writes a JSON report and
prints a human-readable summary.

This is a benchmark tool. It does not modify production code, only imports
from it. The ``benchmarks/`` directory is gitignored.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Path hacks: import from src/ and from benchmarks/run.py
# ---------------------------------------------------------------------------

_BENCH_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BENCH_DIR.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCH_DIR))

import run as _run  # noqa: E402  imports benchmarks/run.py

from salmon.search import SEARCHSOURCES, _derive_artist_str  # noqa: E402
from salmon.search.base import SearchResult  # noqa: E402,TC001
from salmon.search.scoring import TagData, score_result  # noqa: E402
from salmon.tagger.metadata import _detect_va  # noqa: E402
from salmon.tagger.sources import METASOURCES  # noqa: E402

DEFAULT_CORPUS_DIR = _REPO_ROOT / "benchmarks" / "corpus"
DEFAULT_CACHE_DIR = _REPO_ROOT / "benchmarks" / "cache"
DEFAULT_ENRICHED_CACHE_DIR = _REPO_ROOT / "benchmarks" / "cache_enriched"
DEFAULT_ORACLE_CACHE_DIR = _REPO_ROOT / "benchmarks" / "cache_oracle"
DEFAULT_REPORT_PATH = _REPO_ROOT / "benchmarks" / "suggestions.json"
DEFAULT_RUN_REPORT_PATH = _REPO_ROOT / "benchmarks" / "report.json"

_DISCOGS_MASTER_RE = re.compile(r"/master/(\d+)")
_DISCOGS_RELEASE_RE = re.compile(r"/release/(\d+)")

# Oracle priority: prefer the most reliably-labeled providers first.
ORACLE_PRIORITY = [
    "Discogs",
    "MusicBrainz",
    "Bandcamp",
    "Deezer",
    "Apple Music",
    "Beatport",
    "Qobuz",
    "Tidal",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Suggestion:
    slug: str
    provider: str
    kind: str  # "add" | "exclude" | "warning" | "input_gap"
    url: str | None
    score: float | None
    reason: str
    oracle_provider: str | None = None
    gt_url: str | None = None


# ---------------------------------------------------------------------------
# Oracle scraping
# ---------------------------------------------------------------------------


def _discogs_master_fallback_url(
    gt_url: str, master_cache: dict[str, set[str]]
) -> tuple[str, str, str] | None:
    """If gt_url is a Discogs master URL with cached versions, return a release URL.

    Returns (release_url, master_id, version_id) or None if no fallback applies.
    """
    m = _DISCOGS_MASTER_RE.search(urlparse(gt_url).path)
    if not m:
        return None
    master_id = m.group(1)
    versions = master_cache.get(master_id) or set()
    if not versions:
        return None
    version_id = sorted(versions, key=lambda s: int(s) if s.isdigit() else s)[0]
    return f"https://www.discogs.com/release/{version_id}", master_id, version_id


async def scrape_oracle(
    entry: _run.CorpusEntry,
    oracle_cache_dir: Path,
    master_cache: dict[str, set[str]],
    *,
    refresh: bool,
    verbose: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    """Walk ORACLE_PRIORITY, scrape the first available labeled provider.

    Returns (oracle_data, oracle_provider_name). Falls through to the next
    priority on scrape errors. Returns (None, None) if nothing usable.

    For Discogs master URLs that the scraper cannot handle directly, falls
    back to the first known version (release) URL from ``master_cache``.
    """
    for provider in ORACLE_PRIORITY:
        gt_url = entry.ground_truth.get(provider)
        if not gt_url:
            continue
        if provider not in METASOURCES:
            continue

        cache_path = oracle_cache_dir / entry.slug / f"{provider.replace(' ', '_')}.json"
        if cache_path.exists() and not refresh:
            try:
                cached = json.loads(cache_path.read_text())
                print(f"  oracle attempt: {provider} -> cache hit")
                return cached, provider
            except (json.JSONDecodeError, OSError) as exc:
                print(
                    f"  ! {entry.slug}: oracle cache {cache_path} unreadable: {exc}",
                    file=sys.stderr,
                )

        scrape_url = gt_url
        fallback_note: str | None = None

        # Try the GT URL first.
        data: dict[str, Any] | None = None
        try:
            scraper = METASOURCES[provider].Scraper()
            data = await scraper.scrape_release(scrape_url)
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            print(f"  oracle attempt: {provider} -> {err}")
            if verbose:
                traceback.print_exc()

            # Try Discogs master -> release fallback.
            if provider == "Discogs":
                fb = _discogs_master_fallback_url(gt_url, master_cache)
                if fb is not None:
                    scrape_url, master_id, version_id = fb
                    print(
                        f"  oracle attempt: {provider} -> master URL not directly "
                        f"scrapeable, using version {version_id}"
                    )
                    try:
                        scraper = METASOURCES[provider].Scraper()
                        data = await scraper.scrape_release(scrape_url)
                        fallback_note = f"via master {master_id} -> release {version_id}"
                    except Exception as exc2:  # noqa: BLE001
                        err2 = f"{type(exc2).__name__}: {exc2}"
                        print(f"  oracle attempt: {provider} -> fallback failed: {err2}")
                        if verbose:
                            traceback.print_exc()
                        continue
                else:
                    continue
            else:
                continue

        if data is None:
            continue

        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(_jsonable(data), indent=2, sort_keys=True, default=str) + "\n"
            )
        except (TypeError, OSError) as exc:
            print(f"  ! {entry.slug}: failed to cache oracle: {exc}", file=sys.stderr)

        if fallback_note:
            print(f"  oracle: {provider} ({fallback_note})")
        else:
            print(f"  oracle: {provider}")
        return data, provider

    return None, None


def _jsonable(obj: Any) -> Any:
    """Best-effort conversion to JSON-serializable form."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    return str(obj)


# ---------------------------------------------------------------------------
# Enriched TagData construction
# ---------------------------------------------------------------------------


def _count_oracle_tracks(oracle_data: dict[str, Any]) -> int | None:
    tracks = oracle_data.get("tracks") or {}
    if not isinstance(tracks, dict):
        return None
    total = 0
    for disc in tracks.values():
        if isinstance(disc, dict):
            total += len(disc)
    return total or None


def _oracle_main_artists(oracle_data: dict[str, Any]) -> list[str]:
    artists = oracle_data.get("artists") or []
    out: list[str] = []
    for entry in artists:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            name, role = entry[0], entry[1]
            if role == "main" and name:
                out.append(str(name))
    return out


def build_enriched_tag(entry: _run.CorpusEntry, oracle_data: dict[str, Any]) -> TagData:
    """Overlay oracle fields onto the entry's tag_data and return a TagData."""
    base = _run.build_tag_data(entry)
    td = entry.tag_data

    # Title
    album = base.album or oracle_data.get("title")

    # Year
    year_int = base.year
    if year_int is None:
        for y_field in ("year", "group_year"):
            y = oracle_data.get(y_field)
            if y is None:
                continue
            try:
                year_int = int(str(y)[:4])
                break
            except (ValueError, TypeError):
                continue

    # Label / catno
    label = base.label or oracle_data.get("label")
    catno = base.catno or oracle_data.get("catno")

    # Track count
    track_count = base.track_count or _count_oracle_tracks(oracle_data)

    # Artist: prefer entry's main artists; fall back to oracle's main artists.
    entry_main_artists = [name for name, role in (td.get("artists") or []) if role == "main"]
    main_artists = entry_main_artists or _oracle_main_artists(oracle_data)
    is_va = _detect_va(main_artists)
    artist_str = _derive_artist_str(main_artists, is_va=is_va)

    return TagData(
        artist=artist_str,
        album=album,
        year=year_int,
        track_count=track_count,
        source=base.source,
        label=label,
        catno=catno,
        is_va=is_va,
    )


def build_enriched_searchstr(enriched_tag: TagData) -> str:
    """Compose a free-text searchstr from the enriched tag fields."""
    parts: list[str] = []
    if enriched_tag.artist:
        parts.append(enriched_tag.artist)
    if enriched_tag.album:
        parts.append(enriched_tag.album)
    return " ".join(parts).strip() or (enriched_tag.album or "")


# ---------------------------------------------------------------------------
# Provider querying with enriched cache
# ---------------------------------------------------------------------------


async def query_with_enriched(
    provider_name: str,
    slug: str,
    enriched_tag: TagData,
    limit: int,
    enriched_cache_dir: Path,
    *,
    refresh: bool,
) -> dict[Any, SearchResult]:
    """Query a provider using the enriched TagData. Caches results separately."""
    safe_provider = provider_name.replace(" ", "_")
    cache_path = enriched_cache_dir / slug / f"{safe_provider}.json"

    if cache_path.exists() and not refresh:
        try:
            return _run._deserialize_results(cache_path.read_bytes())
        except Exception as exc:  # noqa: BLE001
            print(
                f"warning: failed to read enriched cache {cache_path}: {exc} (refetching)",
                file=sys.stderr,
            )

    module = SEARCHSOURCES[provider_name]
    searcher = module.Searcher()
    searchstr = build_enriched_searchstr(enriched_tag)
    kwargs = _run.build_structured_kwargs(enriched_tag)

    response = await searcher.search_releases(searchstr, limit, **kwargs)
    if response is None:
        results: dict[Any, SearchResult] = {}
    else:
        _, results = response
        results = results or {}

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(_run._serialize_results(results))
    return results


# ---------------------------------------------------------------------------
# Top-match evaluation
# ---------------------------------------------------------------------------


def evaluate_top_match(
    results: dict[Any, SearchResult],
    enriched_tag: TagData,
) -> tuple[float, float, SearchResult | None, Any | None]:
    """Score results, return (top_score, second_score, top_result, top_rls_id)."""
    if not results:
        return 0.0, 0.0, None, None
    scored: list[tuple[Any, SearchResult, float]] = []
    for rls_id, res in results.items():
        s = score_result(res.ident, enriched_tag)
        scored.append((rls_id, res, s))
    scored.sort(key=lambda t: t[2], reverse=True)

    top_rls_id, top_result, top_score = scored[0]
    second_score = scored[1][2] if len(scored) >= 2 else 0.0
    return top_score, second_score, top_result, top_rls_id


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(
    entry: _run.CorpusEntry,
    provider: str,
    results: dict[Any, SearchResult],
    top_score: float,
    second_score: float,
    top_url: str | None,
    master_cache: dict[str, set[str]],
    oracle_provider: str | None,
    *,
    min_score: float,
    margin: float,
) -> Suggestion | None:
    """Classify a provider's enriched-query result into a suggestion."""
    if oracle_provider is not None and provider == oracle_provider:
        return None

    n_results = len(results)
    gap = top_score - second_score
    high_conf = top_score >= min_score and (n_results == 1 or gap >= margin)
    in_gt = provider in entry.ground_truth

    if high_conf:
        if not in_gt:
            return Suggestion(
                slug=entry.slug,
                provider=provider,
                kind="add",
                url=top_url,
                score=top_score,
                reason=f"enriched query found high-confidence match (score={top_score:.1f}, gap={gap:.1f})",
                oracle_provider=oracle_provider,
            )

        gt_url = entry.ground_truth[provider]
        if _run.url_matches(provider, gt_url, top_url, master_cache):
            return None  # silent: already matches GT

        return Suggestion(
            slug=entry.slug,
            provider=provider,
            kind="warning",
            url=top_url,
            score=top_score,
            reason="enriched query found a different high-confidence URL than ground truth",
            oracle_provider=oracle_provider,
            gt_url=gt_url,
        )

    # Not high confidence
    if in_gt and n_results == 0:
        return Suggestion(
            slug=entry.slug,
            provider=provider,
            kind="exclude",
            url=None,
            score=None,
            reason="enriched query returned 0 results — release likely not on this platform",
            oracle_provider=oracle_provider,
            gt_url=entry.ground_truth[provider],
        )

    return None


def _detect_input_gap(
    entry: _run.CorpusEntry,
    provider: str,
    results: dict[Any, SearchResult],
    top_score: float,
    second_score: float,
    top_url: str | None,
    master_cache: dict[str, set[str]],
    run_misses: dict[str, dict[str, bool]] | None,
    *,
    min_score: float,
    margin: float,
) -> Suggestion | None:
    """Detect whether (slug, provider) is an INPUT_GAP case.

    Conditions:
      - run.py report is loaded
      - provider is in entry.ground_truth
      - run.py recorded a miss (rank == None) for this (slug, provider)
      - the enriched query yields a high-confidence match
      - the high-confidence match's URL matches the ground truth
    """
    if run_misses is None:
        return None
    if provider not in entry.ground_truth:
        return None

    slug_misses = run_misses.get(entry.slug)
    if slug_misses is None:
        # Slug not present in report.json -> unknown, do not emit.
        return None
    if not slug_misses.get(provider, False):
        return None

    n_results = len(results)
    gap = top_score - second_score
    high_conf = top_score >= min_score and (n_results == 1 or gap >= margin)
    if not high_conf:
        return None

    gt_url = entry.ground_truth[provider]
    if not _run.url_matches(provider, gt_url, top_url, master_cache):
        return None

    return Suggestion(
        slug=entry.slug,
        provider=provider,
        kind="input_gap",
        url=top_url,
        score=top_score,
        reason="run.py missed this (slug, provider); enriched query found the GT release",
        gt_url=gt_url,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_report(suggestions: list[Suggestion], n_entries: int) -> None:
    adds = [s for s in suggestions if s.kind == "add"]
    excludes = [s for s in suggestions if s.kind == "exclude"]
    warnings = [s for s in suggestions if s.kind == "warning"]
    input_gaps = [s for s in suggestions if s.kind == "input_gap"]

    print()
    print("== Cross-validation suggestions ==")
    print(f"Corpus: {n_entries} entries")
    print(
        f"Input gaps: {len(input_gaps)}  |  Suggestions: {len(adds)} add  "
        f"|  Exclusions: {len(excludes)}  |  Warnings: {len(warnings)}"
    )
    print()

    if input_gaps:
        print(
            "INPUT_GAP (run.py missed these but enriched query DOES find them - "
            "this is the most actionable category):"
        )
        for s in sorted(input_gaps, key=lambda x: (x.slug, x.provider)):
            score_str = f"score={s.score:.1f}" if s.score is not None else ""
            print(
                f"  {s.slug:<40} {s.provider:<14} {score_str:<12} "
                f"(sparse-tag query in run.py failed; enriched succeeded)"
            )
        print()

    if adds:
        print("ADD (provider not yet in ground truth, enriched query found high-confidence match):")
        for s in sorted(adds, key=lambda x: (x.slug, x.provider)):
            score_str = f"score={s.score:.1f}" if s.score is not None else ""
            print(f"  {s.slug:<40} {s.provider:<14} {score_str:<12} {s.url or ''}")
        print()

    if excludes:
        print(
            "EXCLUDE (provider in ground truth, enriched query returned 0 results — "
            "release likely not on this platform):"
        )
        for s in sorted(excludes, key=lambda x: (x.slug, x.provider)):
            print(f"  {s.slug:<40} {s.provider:<14} (gt={s.gt_url})")
        print()

    if warnings:
        print("WARNINGS (existing ground truth may be wrong — enriched query found a different high-confidence URL):")
        for s in sorted(warnings, key=lambda x: (x.slug, x.provider)):
            print(f"  {s.slug:<40} {s.provider:<14} GT={s.gt_url}")
            print(f"  {'':<40} {'':<14} enriched={s.url} (score={s.score:.1f})")
        print()

    if not (adds or excludes or warnings or input_gaps):
        print("(no suggestions)")
        print()


def write_report(suggestions: list[Suggestion], path: Path) -> None:
    payload = {
        "suggestions": [asdict(s) for s in suggestions],
        "summary": {
            "add": sum(1 for s in suggestions if s.kind == "add"),
            "exclude": sum(1 for s in suggestions if s.kind == "exclude"),
            "warning": sum(1 for s in suggestions if s.kind == "warning"),
            "input_gap": sum(1 for s in suggestions if s.kind == "input_gap"),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"Wrote suggestions JSON to {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmarks/suggest.py",
        description="Cross-validation tool: scrape labeled providers, re-query unlabeled ones with enriched tags.",
    )
    parser.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                        help="run.py-compatible cache dir (used for the Discogs master cache).")
    parser.add_argument("--enriched-cache-dir", type=Path, default=DEFAULT_ENRICHED_CACHE_DIR,
                        help="Cache dir for enriched-query results (kept separate from run.py's cache).")
    parser.add_argument("--oracle-cache-dir", type=Path, default=DEFAULT_ORACLE_CACHE_DIR,
                        help="Cache dir for scraped oracle metadata.")
    parser.add_argument("--report", type=str, default=str(DEFAULT_REPORT_PATH),
                        help='Path to write JSON report, or "-" to skip the file write.')
    parser.add_argument("--limit", type=int, default=25, help="Max results per provider query.")
    parser.add_argument(
        "--filter",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Filter corpus entries (e.g. category=adversarial, slug=foo).",
    )
    parser.add_argument("--slug", type=str, default=None, help="Shortcut for --filter slug=<slug>.")
    parser.add_argument("--refresh", action="store_true", help="Invalidate enriched + oracle caches.")
    parser.add_argument("--min-score", type=float, default=80.0,
                        help="Minimum score for a high-confidence match.")
    parser.add_argument("--margin", type=float, default=10.0,
                        help="Required gap between top and second result for high-confidence.")
    parser.add_argument(
        "--report-source",
        type=Path,
        default=DEFAULT_RUN_REPORT_PATH,
        help="Path to run.py's JSON report (used to detect INPUT_GAP cases).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full tracebacks for oracle scrape errors.",
    )
    return parser


def _load_run_report_misses(path: Path) -> dict[str, dict[str, bool]] | None:
    """Load run.py's report.json and build {slug: {provider: missed_bool}}.

    Returns None if the report cannot be loaded (so INPUT_GAP detection
    is disabled). A "miss" is rank == None for a (slug, provider) entry.
    """
    if not path.exists():
        print(
            f"warning: --report-source {path} not found; INPUT_GAP detection disabled",
            file=sys.stderr,
        )
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"warning: failed to read {path}: {exc}; INPUT_GAP detection disabled",
            file=sys.stderr,
        )
        return None

    misses: dict[str, dict[str, bool]] = {}
    providers = raw.get("providers", {}) or {}
    for provider, data in providers.items():
        if not isinstance(data, dict) or data.get("inactive"):
            continue
        for r in data.get("ranks", []) or []:
            slug = r.get("slug")
            if not slug:
                continue
            missed = r.get("rank") is None
            misses.setdefault(slug, {})[provider] = missed
    return misses


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _run_async(args: argparse.Namespace) -> int:
    corpus = _run.load_corpus(args.corpus_dir)
    if not corpus:
        print(f"No corpus entries found at {args.corpus_dir}.")
        return 0

    filters = list(args.filter)
    if args.slug:
        filters.append(f"slug={args.slug}")
    entries = _run.apply_filter(corpus, filters)
    if not entries:
        print("No corpus entries match the supplied filters.")
        return 0

    # Pre-populate Discogs master cache so url_matches works. Loaded once
    # at startup; reused by oracle scrape (for master->release fallback)
    # and url_matches.
    master_cache = _run._load_master_cache(args.cache_dir)
    await _run._prepopulate_discogs_master_cache(entries, master_cache)
    _run._save_master_cache(args.cache_dir, master_cache)

    # Load run.py's report to detect INPUT_GAP cases.
    run_misses = _load_run_report_misses(args.report_source)

    # Detect inactive providers up front.
    inactive: set[str] = set()
    for provider_name, module in SEARCHSOURCES.items():
        try:
            if not module.Searcher.is_active():
                inactive.add(provider_name)
        except Exception:  # noqa: BLE001
            inactive.add(provider_name)
    if inactive:
        print(f"Skipping inactive providers: {', '.join(sorted(inactive))}")

    suggestions: list[Suggestion] = []

    for entry in entries:
        print(f"\n=== {entry.slug} ===")

        # An oracle is only possible when the entry already has at least
        # one ground-truth URL. For un-validated entries (e.g. fresh
        # captures from capture_tree.py with empty ground_truth), fall
        # back to the raw tag_data — any high-confidence match found
        # against an unlabeled provider still becomes an ADD suggestion.
        if entry.ground_truth:
            oracle_data, oracle_provider = await scrape_oracle(
                entry,
                args.oracle_cache_dir,
                master_cache,
                refresh=args.refresh,
                verbose=args.verbose,
            )
        else:
            oracle_data, oracle_provider = None, None

        if oracle_data and oracle_provider:
            enriched_tag = build_enriched_tag(entry, oracle_data)
            print(
                f"  enriched: artist={enriched_tag.artist!r} album={enriched_tag.album!r} "
                f"year={enriched_tag.year} label={enriched_tag.label!r} catno={enriched_tag.catno!r} "
                f"track_count={enriched_tag.track_count}"
            )
        else:
            if entry.ground_truth:
                print(f"  ! {entry.slug}: no oracle available, falling back to raw tag_data")
            else:
                print(f"  {entry.slug}: no ground truth yet, querying with raw tag_data")
            enriched_tag = _run.build_tag_data(entry)
            print(
                f"  raw: artist={enriched_tag.artist!r} album={enriched_tag.album!r} "
                f"year={enriched_tag.year} label={enriched_tag.label!r} catno={enriched_tag.catno!r} "
                f"track_count={enriched_tag.track_count}"
            )

        for provider_name in SEARCHSOURCES:
            if provider_name == oracle_provider:
                continue
            if provider_name in inactive:
                continue

            try:
                results = await query_with_enriched(
                    provider_name,
                    entry.slug,
                    enriched_tag,
                    args.limit,
                    args.enriched_cache_dir,
                    refresh=args.refresh,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {entry.slug}/{provider_name}: query failed: {exc}", file=sys.stderr)
                traceback.print_exc()
                continue

            top_score, second_score, top_result, top_rls_id = evaluate_top_match(
                results, enriched_tag
            )
            top_url = (
                _run._result_url(provider_name, top_rls_id, top_result) if top_result else None
            )

            sug = classify(
                entry,
                provider_name,
                results,
                top_score,
                second_score,
                top_url,
                master_cache,
                oracle_provider,
                min_score=args.min_score,
                margin=args.margin,
            )
            if sug:
                suggestions.append(sug)
                print(f"  {provider_name}: {sug.kind} - {sug.reason}")

            # INPUT_GAP detection: provider in GT, run.py missed it, but
            # the enriched query found a high-confidence match whose URL
            # matches the recorded ground truth. This signals that the
            # algorithm CAN find the release given better input.
            input_gap = _detect_input_gap(
                entry,
                provider_name,
                results,
                top_score,
                second_score,
                top_url,
                master_cache,
                run_misses,
                min_score=args.min_score,
                margin=args.margin,
            )
            if input_gap is not None:
                input_gap.oracle_provider = oracle_provider
                suggestions.append(input_gap)
                print(f"  {provider_name}: input_gap - {input_gap.reason}")

    print_report(suggestions, len(entries))

    if args.report != "-":
        write_report(suggestions, Path(args.report))

    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
