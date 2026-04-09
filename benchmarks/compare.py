#!/usr/bin/env python3
"""Validate ground-truth URLs in the benchmark corpus.

For every ``(slug, provider)`` pair where a corpus entry has a
ground-truth URL, ``compare.py`` scrapes the provider, diffs
release-level and tracklist fields against the local file tags, and
emits a PASS / WARN / FAIL verdict plus a human-readable diff. It does
not mutate the corpus.

Pairs with the capture + suggest flow:

1. ``capture.py`` / ``capture_tree.py`` write corpus entries.
2. ``suggest.py`` fills in ground-truth URLs.
3. ``compare.py`` verifies those URLs actually point at the same
   release as the files on disk.

Benchmark tool. Does not modify production code. The ``benchmarks/``
directory is gitignored.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import string
import sys
import traceback
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path hacks: import from src/ and sibling benchmark scripts
# ---------------------------------------------------------------------------

_BENCH_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BENCH_DIR.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCH_DIR))

import run as _run  # noqa: E402
import suggest as _suggest  # noqa: E402
from rapidfuzz import fuzz  # noqa: E402
from unidecode import unidecode  # noqa: E402

from salmon.search import SEARCHSOURCES  # noqa: E402
from salmon.tagger.tags import gather_tags  # noqa: E402

DEFAULT_CORPUS_DIR = _REPO_ROOT / "benchmarks" / "corpus"
DEFAULT_CACHE_DIR = _REPO_ROOT / "benchmarks" / "cache"
DEFAULT_ORACLE_CACHE_DIR = _REPO_ROOT / "benchmarks" / "cache_oracle"
DEFAULT_REPORT_PATH = _REPO_ROOT / "benchmarks" / "compare.json"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TrackView:
    disc: int
    position: str  # keep as string to handle vinyl (A1, A2, Α, Β, ...)
    title: str
    artists: list[str] = field(default_factory=list)


@dataclass
class ReleaseView:
    title: str | None
    main_artists: list[str]
    year: int | None
    label: str | None
    catno: str | None
    track_count: int | None
    tracks: list[TrackView]


@dataclass
class FieldDiff:
    status: str  # "ok" | "mismatch" | "missing"
    local: Any
    provider: Any


@dataclass
class TrackMismatch:
    index: int  # 0-based position in the combined ordered list
    kind: str  # "local_only" | "provider_only" | "title_mismatch"
    local: str | None = None
    provider: str | None = None
    score: int | None = None


@dataclass
class TrackDiff:
    status: str  # "ok" | "mismatch"
    local_count: int
    provider_count: int
    mismatches: list[TrackMismatch]


@dataclass
class ReleaseDiff:
    slug: str
    provider: str
    verdict: str  # "PASS" | "WARN" | "FAIL" | "ERROR"
    fields: dict[str, FieldDiff]
    tracks: TrackDiff | None
    error: str | None = None


# ---------------------------------------------------------------------------
# Normalization + fuzzy match
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(rf"[{re.escape(string.punctuation)}]+")


def _normalize(text: str | None) -> str:
    """Lowercase, strip diacritics, drop punctuation, collapse whitespace."""
    if not text:
        return ""
    ascii_text = unidecode(str(text))
    ascii_text = unicodedata.normalize("NFKD", ascii_text)
    ascii_text = _PUNCT_RE.sub(" ", ascii_text.lower())
    return " ".join(ascii_text.split())


def _fuzzy_match(a: str | None, b: str | None, threshold: int) -> tuple[bool, int]:
    """Return (match, score) using rapidfuzz token_set_ratio on normalized strings."""
    na, nb = _normalize(a), _normalize(b)
    if not na and not nb:
        return True, 100
    if not na or not nb:
        return False, 0
    score = int(fuzz.token_set_ratio(na, nb))
    return score >= threshold, score


# ---------------------------------------------------------------------------
# Local tracklist from on-disk tags
# ---------------------------------------------------------------------------


def _as_int(value: Any, default: int = 1) -> int:
    try:
        # tracknumber may come as "3/12" or "A1" — take the leading digits.
        s = str(value).strip()
        m = re.match(r"(\d+)", s)
        if m:
            return int(m.group(1))
    except (TypeError, ValueError):
        pass
    return default


def _load_local_view(entry: _run.CorpusEntry) -> ReleaseView:
    """Read the local tracklist from disk via gather_tags()."""
    source_path = entry.tag_data.get("source_path")
    if not source_path:
        raise SystemExit(
            f"error: entry '{entry.slug}' has no tag_data.source_path — "
            "re-capture with the updated capture.py / capture_tree.py"
        )

    folder = Path(source_path)
    if not folder.is_dir():
        raise SystemExit(
            f"error: entry '{entry.slug}' source_path does not exist: {folder}"
        )

    tag_files = gather_tags(str(folder))
    tracks: list[TrackView] = []
    for filename, tag_file in tag_files.items():
        title = getattr(tag_file, "title", None) or filename
        disc = _as_int(getattr(tag_file, "discnumber", 1), default=1)
        position_raw = getattr(tag_file, "tracknumber", None)
        position = str(position_raw).strip() if position_raw else str(len(tracks) + 1)
        artist = getattr(tag_file, "artist", None)
        if isinstance(artist, list):
            artists = [str(a) for a in artist if a]
        elif artist:
            artists = [str(artist)]
        else:
            artists = []
        tracks.append(
            TrackView(disc=disc, position=position, title=str(title), artists=artists)
        )

    tracks.sort(key=lambda t: (t.disc, _as_int(t.position, default=0), t.position))

    td = entry.tag_data
    main_artists = [name for name, role in (td.get("artists") or []) if role == "main"]
    year = td.get("year")
    try:
        year_int = int(str(year)[:4]) if year is not None else None
    except (ValueError, TypeError):
        year_int = None

    return ReleaseView(
        title=td.get("title"),
        main_artists=main_artists,
        year=year_int,
        label=td.get("label"),
        catno=td.get("catno"),
        track_count=td.get("track_count") or len(tracks),
        tracks=tracks,
    )


# ---------------------------------------------------------------------------
# Provider tracklist from scraped dict
# ---------------------------------------------------------------------------


def _load_provider_view(data: dict[str, Any]) -> ReleaseView:
    """Build a ReleaseView from a scraper's scrape_release() dict."""
    raw_tracks = data.get("tracks") or {}
    tracks: list[TrackView] = []
    if isinstance(raw_tracks, dict):
        # Sort discs numerically when possible, fall back to string order.
        disc_keys = list(raw_tracks.keys())
        disc_keys.sort(key=lambda k: _as_int(k, default=0))
        for disc_key in disc_keys:
            disc_num = _as_int(disc_key, default=1)
            disc_tracks = raw_tracks.get(disc_key) or {}
            if not isinstance(disc_tracks, dict):
                continue
            for position, track in disc_tracks.items():
                if not isinstance(track, dict):
                    continue
                title = track.get("title") or ""
                raw_artists = track.get("artists") or []
                artists: list[str] = []
                for a in raw_artists:
                    if isinstance(a, (list, tuple)) and a:
                        artists.append(str(a[0]))
                    elif isinstance(a, str):
                        artists.append(a)
                tracks.append(
                    TrackView(
                        disc=disc_num,
                        position=str(position),
                        title=str(title),
                        artists=artists,
                    )
                )

    main_artists: list[str] = []
    for entry_pair in data.get("artists") or []:
        if isinstance(entry_pair, (list, tuple)) and len(entry_pair) >= 2:
            name, role = entry_pair[0], entry_pair[1]
            if role == "main" and name:
                main_artists.append(str(name))

    year_val: int | None = None
    for y_field in ("year", "group_year"):
        y = data.get(y_field)
        if y is None:
            continue
        try:
            year_val = int(str(y)[:4])
            break
        except (ValueError, TypeError):
            continue

    return ReleaseView(
        title=data.get("title"),
        main_artists=main_artists,
        year=year_val,
        label=data.get("label"),
        catno=data.get("catno"),
        track_count=len(tracks) or None,
        tracks=tracks,
    )


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


def _diff_string(local: str | None, provider: str | None, threshold: int) -> FieldDiff:
    if local is None and provider is None:
        return FieldDiff(status="missing", local=local, provider=provider)
    if local is None or provider is None:
        return FieldDiff(status="missing", local=local, provider=provider)
    match, _score = _fuzzy_match(local, provider, threshold)
    return FieldDiff(
        status="ok" if match else "mismatch", local=local, provider=provider
    )


def _diff_artist(
    local: list[str], provider: list[str], threshold: int
) -> FieldDiff:
    local_str = " / ".join(local) if local else None
    provider_str = " / ".join(provider) if provider else None
    if not local and not provider:
        return FieldDiff(status="missing", local=local_str, provider=provider_str)
    if not local or not provider:
        return FieldDiff(status="missing", local=local_str, provider=provider_str)
    # Match if the first main artist on each side fuzzy-matches, OR if
    # the concatenated strings match. Covers "Various" vs full VA lists.
    match, _ = _fuzzy_match(local[0], provider[0], threshold)
    if not match:
        match, _ = _fuzzy_match(local_str, provider_str, threshold)
    return FieldDiff(
        status="ok" if match else "mismatch", local=local_str, provider=provider_str
    )


def _diff_year(local: int | None, provider: int | None) -> FieldDiff:
    if local is None or provider is None:
        return FieldDiff(status="missing", local=local, provider=provider)
    return FieldDiff(
        status="ok" if local == provider else "mismatch",
        local=local,
        provider=provider,
    )


def _diff_identifier(local: str | None, provider: str | None) -> FieldDiff:
    if not local and not provider:
        return FieldDiff(status="missing", local=local, provider=provider)
    if not local or not provider:
        return FieldDiff(status="missing", local=local, provider=provider)
    return FieldDiff(
        status="ok" if _normalize(local) == _normalize(provider) else "mismatch",
        local=local,
        provider=provider,
    )


def _diff_tracks(
    local: list[TrackView],
    provider: list[TrackView],
    *,
    fuzzy_threshold: int,
    fail_ratio: float,
) -> TrackDiff:
    n_local, n_provider = len(local), len(provider)
    mismatches: list[TrackMismatch] = []

    length = max(n_local, n_provider)
    for i in range(length):
        local_track = local[i] if i < n_local else None
        provider_track = provider[i] if i < n_provider else None

        if local_track is None and provider_track is not None:
            mismatches.append(
                TrackMismatch(
                    index=i,
                    kind="provider_only",
                    provider=provider_track.title,
                )
            )
            continue
        if provider_track is None and local_track is not None:
            mismatches.append(
                TrackMismatch(
                    index=i,
                    kind="local_only",
                    local=local_track.title,
                )
            )
            continue
        assert local_track is not None and provider_track is not None
        match, score = _fuzzy_match(
            local_track.title, provider_track.title, fuzzy_threshold
        )
        if not match:
            mismatches.append(
                TrackMismatch(
                    index=i,
                    kind="title_mismatch",
                    local=local_track.title,
                    provider=provider_track.title,
                    score=score,
                )
            )

    length_mismatch = n_local != n_provider
    bad_ratio = (len(mismatches) / length) if length else 0.0
    status = (
        "mismatch"
        if length_mismatch or bad_ratio > fail_ratio
        else "ok"
    )
    return TrackDiff(
        status=status,
        local_count=n_local,
        provider_count=n_provider,
        mismatches=mismatches,
    )


def diff_release(
    slug: str,
    provider_name: str,
    local: ReleaseView,
    provider: ReleaseView,
    *,
    fuzzy_threshold: int,
    track_fail_ratio: float,
) -> ReleaseDiff:
    fields: dict[str, FieldDiff] = {
        "title": _diff_string(local.title, provider.title, fuzzy_threshold),
        "artist": _diff_artist(
            local.main_artists, provider.main_artists, fuzzy_threshold
        ),
        "year": _diff_year(local.year, provider.year),
        "label": _diff_identifier(local.label, provider.label),
        "catno": _diff_identifier(local.catno, provider.catno),
    }
    tracks = _diff_tracks(
        local.tracks,
        provider.tracks,
        fuzzy_threshold=fuzzy_threshold,
        fail_ratio=track_fail_ratio,
    )

    fail = (
        fields["title"].status == "mismatch"
        or fields["artist"].status == "mismatch"
        or tracks.status == "mismatch"
    )
    warn = any(
        fields[name].status == "mismatch" for name in ("year", "label", "catno")
    )

    if fail:
        verdict = "FAIL"
    elif warn:
        verdict = "WARN"
    else:
        verdict = "PASS"

    return ReleaseDiff(
        slug=slug,
        provider=provider_name,
        verdict=verdict,
        fields=fields,
        tracks=tracks,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


_VERDICT_ORDER = {"FAIL": 0, "ERROR": 1, "WARN": 2, "PASS": 3}


def _format_field_line(name: str, diff: FieldDiff) -> str:
    tag = {
        "ok": "ok",
        "mismatch": "MISMATCH",
        "missing": "missing",
    }[diff.status]
    left = str(diff.local) if diff.local not in (None, "") else "-"
    right = str(diff.provider) if diff.provider not in (None, "") else "-"
    combined = f"{left} / {right}" if left != right else left
    return f"    {name:<8} {combined:<60} {tag}"


def print_report(diffs: list[ReleaseDiff], n_entries: int) -> None:
    by_slug: dict[str, list[ReleaseDiff]] = {}
    for d in diffs:
        by_slug.setdefault(d.slug, []).append(d)

    for slug in sorted(by_slug):
        print(f"\n=== {slug} ===")
        slug_diffs = sorted(
            by_slug[slug], key=lambda d: (_VERDICT_ORDER[d.verdict], d.provider)
        )
        for d in slug_diffs:
            if d.verdict == "ERROR":
                print(f"  {d.provider}: ERROR ({d.error})")
                continue
            print(f"  {d.provider}: {d.verdict}")
            for name in ("title", "artist", "year", "label", "catno"):
                print(_format_field_line(name, d.fields[name]))
            if d.tracks is not None:
                tag = "ok" if d.tracks.status == "ok" else "MISMATCH"
                counts = f"{d.tracks.local_count} / {d.tracks.provider_count}"
                print(f"    {'tracks':<8} {counts:<60} {tag}")
                for m in d.tracks.mismatches[:10]:
                    if m.kind == "provider_only":
                        print(
                            f"      [{m.index + 1}] provider-only: {m.provider!r}"
                        )
                    elif m.kind == "local_only":
                        print(f"      [{m.index + 1}] local-only: {m.local!r}")
                    else:
                        print(
                            f"      [{m.index + 1}] title_mismatch "
                            f"(score={m.score}): {m.local!r} / {m.provider!r}"
                        )
                if len(d.tracks.mismatches) > 10:
                    print(f"      ... {len(d.tracks.mismatches) - 10} more")

    counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "ERROR": 0}
    for d in diffs:
        counts[d.verdict] += 1

    print()
    print("== compare summary ==")
    print(
        f"Corpus: {n_entries} entries   |   Checked: {len(diffs)} "
        f"(slug, provider) pairs"
    )
    print(
        f"PASS: {counts['PASS']}   WARN: {counts['WARN']}   "
        f"FAIL: {counts['FAIL']}   ERROR: {counts['ERROR']}"
    )


def write_report(diffs: list[ReleaseDiff], path: Path) -> None:
    counts = {"pass": 0, "warn": 0, "fail": 0, "errors": 0}
    for d in diffs:
        key = "errors" if d.verdict == "ERROR" else d.verdict.lower()
        counts[key] += 1
    payload = {
        "summary": counts,
        "results": [asdict(d) for d in diffs],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"Wrote compare report to {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmarks/compare.py",
        description=(
            "Validate corpus ground-truth URLs by diffing scraped provider "
            "metadata against local file tags."
        ),
    )
    parser.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                        help="run.py cache dir (for the Discogs master cache).")
    parser.add_argument(
        "--oracle-cache-dir",
        type=Path,
        default=DEFAULT_ORACLE_CACHE_DIR,
        help="Shared scraped-metadata cache (same as suggest.py).",
    )
    parser.add_argument(
        "--report",
        type=str,
        default=str(DEFAULT_REPORT_PATH),
        help='Path to write JSON report, or "-" to skip the file write.',
    )
    parser.add_argument("--slug", type=str, default=None)
    parser.add_argument(
        "--filter",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Filter corpus entries (e.g. category=adversarial).",
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--fuzzy-threshold",
        type=int,
        default=90,
        help="rapidfuzz token_set_ratio cutoff for string match (default: 90).",
    )
    parser.add_argument(
        "--track-fail-ratio",
        type=float,
        default=0.2,
        help="Tracklist FAIL if >ratio of tracks mismatch (default: 0.2).",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Print full scrape tracebacks."
    )
    return parser


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

    master_cache = _run._load_master_cache(args.cache_dir)
    await _run._prepopulate_discogs_master_cache(entries, master_cache)
    _run._save_master_cache(args.cache_dir, master_cache)

    inactive: set[str] = set()
    for provider_name, module in SEARCHSOURCES.items():
        try:
            if not module.Searcher.is_active():
                inactive.add(provider_name)
        except Exception:  # noqa: BLE001
            inactive.add(provider_name)
    if inactive:
        print(f"Skipping inactive providers: {', '.join(sorted(inactive))}")

    diffs: list[ReleaseDiff] = []

    for entry in entries:
        if not entry.ground_truth:
            print(f"skip {entry.slug}: no ground truth to validate")
            continue

        try:
            local_view = _load_local_view(entry)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            print(
                f"  ! {entry.slug}: failed to read local tags: {exc}",
                file=sys.stderr,
            )
            if args.verbose:
                traceback.print_exc()
            continue

        print(f"\n=== {entry.slug} ===")
        for provider_name, _gt_url in entry.ground_truth.items():
            if provider_name in inactive:
                continue
            try:
                data = await _suggest.scrape_provider_cached(
                    entry,
                    provider_name,
                    args.oracle_cache_dir,
                    master_cache,
                    refresh=args.refresh,
                    verbose=args.verbose,
                    log_prefix="compare scrape",
                )
            except Exception as exc:  # noqa: BLE001
                diffs.append(
                    ReleaseDiff(
                        slug=entry.slug,
                        provider=provider_name,
                        verdict="ERROR",
                        fields={},
                        tracks=None,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                if args.verbose:
                    traceback.print_exc()
                continue

            if data is None:
                diffs.append(
                    ReleaseDiff(
                        slug=entry.slug,
                        provider=provider_name,
                        verdict="ERROR",
                        fields={},
                        tracks=None,
                        error="scrape failed or returned no data",
                    )
                )
                continue

            provider_view = _load_provider_view(data)
            diff = diff_release(
                entry.slug,
                provider_name,
                local_view,
                provider_view,
                fuzzy_threshold=args.fuzzy_threshold,
                track_fail_ratio=args.track_fail_ratio,
            )
            diffs.append(diff)
            print(f"  {provider_name}: {diff.verdict}")

    print_report(diffs, len(entries))

    if args.report != "-":
        write_report(diffs, Path(args.report))

    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
