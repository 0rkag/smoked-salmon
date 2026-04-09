#!/usr/bin/env python3
"""Metadata search benchmark harness.

Loads labeled corpus entries, queries each metadata provider (cached), scores
results against ground-truth URLs, reports recall@k and MRR per provider.

This is a benchmark tool. It does not modify production code, only imports
from it. The ``benchmarks/`` directory is gitignored.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import msgspec

# Make salmon importable when running from repo root.
_BENCH_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BENCH_DIR.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Make local benchmark modules importable (e.g. `noise`).
if str(_BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCH_DIR))

from salmon.common import make_searchstrs  # noqa: E402
from salmon.search import SEARCHSOURCES, _derive_artist_str  # noqa: E402
from salmon.search.base import SearchResult  # noqa: E402
from salmon.search.scoring import TagData, score_result  # noqa: E402
from salmon.tagger.metadata import _detect_va  # noqa: E402

DEFAULT_CORPUS_DIR = _REPO_ROOT / "benchmarks" / "corpus"
DEFAULT_CACHE_DIR = _REPO_ROOT / "benchmarks" / "cache"
DEFAULT_REPORT_PATH = _REPO_ROOT / "benchmarks" / "report.json"


class CorpusEntry(msgspec.Struct, frozen=True):
    slug: str
    captured_at: str
    category: str
    notes: str
    tag_data: dict[str, Any]
    ground_truth: dict[str, str]


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


def load_corpus(corpus_dir: Path) -> list[CorpusEntry]:
    """Decode every ``*.json`` file in ``corpus_dir`` as a CorpusEntry."""
    if not corpus_dir.is_dir():
        return []
    decoder = msgspec.json.Decoder(CorpusEntry)
    entries: list[CorpusEntry] = []
    for path in sorted(corpus_dir.glob("*.json")):
        try:
            entries.append(decoder.decode(path.read_bytes()))
        except msgspec.DecodeError as exc:
            print(f"warning: failed to decode {path}: {exc}", file=sys.stderr)
    return entries


# ---------------------------------------------------------------------------
# TagData / searchstr construction (mirrors get_metadata)
# ---------------------------------------------------------------------------


def build_tag_data(entry: CorpusEntry) -> TagData:
    """Build a TagData from a corpus entry's tag_data dict."""
    td = entry.tag_data
    main_artists = [name for name, role in td.get("artists") or [] if role == "main"]
    is_va = _detect_va(main_artists)
    artist_str = _derive_artist_str(main_artists, is_va=is_va)

    year = td.get("year")
    try:
        year_int = int(str(year)[:4]) if year is not None else None
    except (ValueError, TypeError):
        year_int = None

    return TagData(
        artist=artist_str,
        album=td.get("title"),
        year=year_int,
        track_count=td.get("track_count"),
        source=td.get("source"),
        label=td.get("label"),
        catno=td.get("catno"),
        is_va=is_va,
    )


def build_searchstr(entry: CorpusEntry) -> str:
    """Return the first searchstr for this entry (matches run_metasearch)."""
    artists_pairs = [(name, role) for name, role in entry.tag_data.get("artists") or []]
    title = entry.tag_data.get("title") or ""
    strs = make_searchstrs(artists_pairs, title)
    if strs:
        return strs[0]
    return title.strip()


def build_structured_kwargs(tag: TagData) -> dict[str, Any]:
    """The dict of kwargs passed to ``provider.search_releases``."""
    return {
        "artist": tag.artist,
        "album": tag.album,
        "year": int(tag.year) if tag.year else None,
        "label": tag.label,
        "catno": tag.catno,
        "is_va": tag.is_va,
    }


# ---------------------------------------------------------------------------
# Cache format — list of (key_repr, SearchResult) to sidestep tuple-key JSON
# ---------------------------------------------------------------------------


_RESULT_DECODER = msgspec.json.Decoder(SearchResult)
_RESULT_ENCODER = msgspec.json.Encoder()


def _serialize_results(results: dict[Any, SearchResult]) -> bytes:
    """Serialize {rls_id: SearchResult} to JSON bytes.

    Keys may be tuples (Apple Music, Tidal, Bandcamp), which aren't valid
    JSON keys. We serialize to a list of {"key_repr", "result"} pairs.
    """
    payload = {
        "entries": [
            {
                "key_repr": repr(k),
                "result": json.loads(_RESULT_ENCODER.encode(v).decode("utf-8")),
            }
            for k, v in results.items()
        ]
    }
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def _deserialize_results(data: bytes) -> dict[Any, SearchResult]:
    payload = json.loads(data)
    out: dict[Any, SearchResult] = {}
    for item in payload.get("entries", []):
        key_repr = item["key_repr"]
        try:
            key = ast.literal_eval(key_repr)
        except (ValueError, SyntaxError):
            key = key_repr
        result = _RESULT_DECODER.decode(json.dumps(item["result"]).encode("utf-8"))
        out[key] = result
    return out


# ---------------------------------------------------------------------------
# Provider querying
# ---------------------------------------------------------------------------


def _noise_cache_path(
    cache_dir: Path,
    slug: str,
    provider: str,
    noise_config: Any,
) -> Path:
    """Compute cache path for a noised provider query.

    Key includes (slug, provider, seed, canonical_noise_config) hashed via
    SHA256 for stable cross-platform behavior. Falls under
    ``benchmarks/cache_noise/<hash>.json``.
    """
    payload = (
        f"{slug}|{provider}|{noise_config.seed}|{noise_config.canonical_string()}"
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return cache_dir.parent / "cache_noise" / f"{digest}.json"


async def query_provider(
    provider_name: str,
    entry: CorpusEntry,
    tag: TagData,
    limit: int,
    cache_dir: Path,
    *,
    refresh: bool,
    noise_config: Any = None,
) -> dict[Any, SearchResult]:
    """Return the raw {rls_id: SearchResult} dict for a provider+entry.

    Uses an on-disk cache under ``cache_dir/<slug>/<provider>.json`` for
    determinism and to avoid hammering provider APIs on repeated runs.

    When ``noise_config`` is supplied and is not a no-op, cache reads/writes
    go to a separate query-hash cache at ``cache_dir.parent/cache_noise/``
    keyed by (slug, provider, seed, canonical-noise-config).
    """
    safe_provider = provider_name.replace(" ", "_")
    if noise_config is not None and not noise_config.is_noop():
        cache_path = _noise_cache_path(
            cache_dir, entry.slug, safe_provider, noise_config
        )
    else:
        cache_path = cache_dir / entry.slug / f"{safe_provider}.json"

    if cache_path.exists() and not refresh:
        try:
            return _deserialize_results(cache_path.read_bytes())
        except (msgspec.DecodeError, json.JSONDecodeError, KeyError) as exc:
            print(
                f"warning: failed to read cache {cache_path}: {exc} (refetching)",
                file=sys.stderr,
            )

    module = SEARCHSOURCES[provider_name]
    searcher = module.Searcher()
    searchstr = build_searchstr(entry)
    kwargs = build_structured_kwargs(tag)

    response = await searcher.search_releases(searchstr, limit, **kwargs)
    # search_releases returns (provider_name, {rls_id: SearchResult}) | None
    if response is None:
        results: dict[Any, SearchResult] = {}
    else:
        _, results = response
        results = results or {}

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(_serialize_results(results))
    return results


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _normalize_generic(u: str) -> str:
    """Generic URL normalization: lowercase host, strip www, strip trailing slash."""
    if not u:
        return ""
    try:
        p = urlparse(u)
    except ValueError:
        return u.strip().lower()
    scheme = p.scheme or "https"
    netloc = p.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = p.path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


# Per-provider URL matchers. Each takes (ground_truth_url, candidate_url,
# discogs_master_versions_cache) and returns True if they refer to the same
# release. The cache is only used by the Discogs matcher; others ignore it.

_APPLE_ID_RE = re.compile(r"/(\d+)(?:[/?#]|$)")


def _match_apple_music(gt: str, candidate: str | None, _cache: dict) -> bool:
    """Apple Music URLs vary in storefront and album-slug position; only the
    trailing numeric collection ID is stable.

    e.g. https://music.apple.com/mt/album/subtemple-beachfires-ep/1238656816
         https://music.apple.com/album/-/1238656816
    Both share id=1238656816.
    """
    if not candidate:
        return False
    gt_m = _APPLE_ID_RE.search(urlparse(gt).path)
    cand_m = _APPLE_ID_RE.search(urlparse(candidate).path)
    return bool(gt_m and cand_m and gt_m.group(1) == cand_m.group(1))


_DISCOGS_RELEASE_RE = re.compile(r"/release/(\d+)")
_DISCOGS_MASTER_RE = re.compile(r"/master/(\d+)")

# MusicBrainz release URLs embed a UUID that may be followed by subpath
# suffixes like /details, /cover-art, /discids, etc.
_MB_UUID_RE = re.compile(
    r"/release/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)


def _match_musicbrainz(gt: str, candidate: str | None, _cache: dict) -> bool:
    """MusicBrainz release URLs vary by subpath suffix (/details, /cover-art,
    /discids, ...). Match on the embedded UUID only.

    e.g. https://musicbrainz.org/release/0bfa1f6f-ad6c-40bc-beef-27086a0c31b6/details
         https://musicbrainz.org/release/0bfa1f6f-ad6c-40bc-beef-27086a0c31b6
    Both share the UUID.
    """
    if not candidate:
        return False
    gt_m = _MB_UUID_RE.search(urlparse(gt).path)
    cand_m = _MB_UUID_RE.search(urlparse(candidate).path)
    return bool(gt_m and cand_m and gt_m.group(1).lower() == cand_m.group(1).lower())


async def _fetch_discogs_master_versions(master_id: str) -> set[str]:
    """Fetch the set of release IDs that belong to a Discogs master release.

    Live-fetched via the Discogs API (paginated /masters/<id>/versions) using
    salmon's existing DiscogsBase scraper for auth.
    """
    from salmon.sources.discogs import DiscogsBase

    scraper = DiscogsBase()
    ids: set[str] = set()
    page = 1
    while True:
        resp = await scraper.get_json(
            f"/masters/{master_id}/versions",
            params={"page": page, "per_page": 100},
        )
        for v in resp.get("versions", []):
            if "id" in v:
                ids.add(str(v["id"]))
        pagination = resp.get("pagination", {})
        if page >= pagination.get("pages", 1):
            break
        page += 1
    return ids


def _load_master_cache(cache_dir: Path) -> dict[str, set[str]]:
    """Load the on-disk Discogs master → release-IDs cache."""
    path = cache_dir / "_discogs_masters.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        return {k: set(v) for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        return {}


def _save_master_cache(cache_dir: Path, cache: dict[str, set[str]]) -> None:
    """Persist the Discogs master cache to disk."""
    path = cache_dir / "_discogs_masters.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {k: sorted(v) for k, v in cache.items()}
    path.write_text(json.dumps(serializable, indent=2, sort_keys=True) + "\n")


async def _prepopulate_discogs_master_cache(
    entries: list[CorpusEntry],
    cache: dict[str, set[str]],
) -> None:
    """Walk corpus, find Discogs master URLs, fetch versions for any not cached."""
    needed: set[str] = set()
    for entry in entries:
        gt = entry.ground_truth.get("Discogs")
        if not gt:
            continue
        m = _DISCOGS_MASTER_RE.search(urlparse(gt).path)
        if m:
            needed.add(m.group(1))

    missing = [mid for mid in needed if mid not in cache]
    if not missing:
        return
    print(f"Resolving {len(missing)} Discogs master(s) → release IDs ...")
    for master_id in missing:
        try:
            cache[master_id] = await _fetch_discogs_master_versions(master_id)
            print(f"  master {master_id}: {len(cache[master_id])} versions")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! master {master_id}: fetch failed: {exc}")
            cache[master_id] = set()


def _match_discogs(gt: str, candidate: str | None, master_cache: dict) -> bool:
    """Discogs ground truth may be a /release/<id> or /master/<id> URL.

    Release URLs are matched directly by ID. Master URLs are resolved to their
    set of versions (cached) and the candidate matches if its release ID is in
    that set.
    """
    if not candidate:
        return False
    cand_m = _DISCOGS_RELEASE_RE.search(urlparse(candidate).path)
    if not cand_m:
        return False
    cand_id = cand_m.group(1)

    gt_path = urlparse(gt).path
    rel_m = _DISCOGS_RELEASE_RE.search(gt_path)
    if rel_m:
        return rel_m.group(1) == cand_id

    master_m = _DISCOGS_MASTER_RE.search(gt_path)
    if master_m:
        version_ids = master_cache.get(master_m.group(1), set())
        return cand_id in version_ids

    return False


def _match_default(gt: str, candidate: str | None, _cache: dict) -> bool:
    """Generic exact-match after normalization."""
    if not candidate:
        return False
    return _normalize_generic(gt) == _normalize_generic(candidate)


_BANDCAMP_SLUG_RE = re.compile(r"/(?:album|track)/([^/?#]+)")


def _match_bandcamp(gt: str, candidate: str | None, _cache: dict) -> bool:
    """Bandcamp releases can live at multiple subdomains (label vs artist).
    Match on the album/track slug rather than the full URL.

    Additionally, if both slugs contain a common identifier substring
    (catno-like token), treat them as matching even when the slug shapes
    differ.
    """
    if not candidate:
        return False
    gt_m = _BANDCAMP_SLUG_RE.search(urlparse(gt).path)
    cand_m = _BANDCAMP_SLUG_RE.search(urlparse(candidate).path)
    if not gt_m or not cand_m:
        return False

    gt_slug = gt_m.group(1).lower()
    cand_slug = cand_m.group(1).lower()

    # Exact match after normalization
    if gt_slug == cand_slug:
        return True

    # One is a substring of the other (common when label prepends the
    # artist name: "switchback-ep" vs "albert-zhirnov-switchback-ep-crg028")
    if gt_slug in cand_slug or cand_slug in gt_slug:
        return True

    # Token overlap heuristic: if the two slugs share enough tokens
    # (Jaccard > 0.5), consider them the same release.
    gt_tokens = set(gt_slug.replace("_", "-").split("-"))
    cand_tokens = set(cand_slug.replace("_", "-").split("-"))
    if gt_tokens and cand_tokens:
        intersection = gt_tokens & cand_tokens
        union = gt_tokens | cand_tokens
        if len(intersection) / len(union) >= 0.5:
            return True

    return False


_MATCHERS = {
    "Apple Music": _match_apple_music,
    "Bandcamp": _match_bandcamp,
    "Discogs": _match_discogs,
    "MusicBrainz": _match_musicbrainz,
}


def url_matches(provider_name: str, gt: str, candidate: str | None, master_cache: dict) -> bool:
    matcher = _MATCHERS.get(provider_name, _match_default)
    return matcher(gt, candidate, master_cache)


def _result_url(provider_name: str, rls_id: Any, result: SearchResult) -> str | None:
    """Compute the canonical URL for a SearchResult via the provider's
    ``Searcher.format_url``.
    """
    try:
        searcher_cls = SEARCHSOURCES[provider_name].Searcher
        return searcher_cls.format_url(rls_id, rls_name=result.ident.album)
    except TypeError:
        try:
            return SEARCHSOURCES[provider_name].Searcher.format_url(rls_id)
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None


def find_rank(
    results: dict[Any, SearchResult],
    tag: TagData,
    ground_truth_url: str,
    provider_name: str,
    master_cache: dict,
) -> tuple[int | None, list[tuple[Any, SearchResult, float, str | None]]]:
    """Return (1-indexed rank, sorted_scored_list) for the ground-truth URL."""
    scored: list[tuple[Any, SearchResult, float, str | None]] = []
    for rls_id, res in results.items():
        s = score_result(res.ident, tag)
        url = _result_url(provider_name, rls_id, res)
        scored.append((rls_id, res, s, url))

    # Sort by score descending. No threshold filtering — we want true rank.
    scored.sort(key=lambda t: t[2], reverse=True)

    for idx, (_rid, _res, _score, url) in enumerate(scored, start=1):
        if url_matches(provider_name, ground_truth_url, url, master_cache):
            return idx, scored
    return None, scored


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class ProviderStats:
    ranks: list[int | None] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)  # slug per rank position
    inactive: bool = False


def compute_metrics(ranks: list[int | None]) -> dict[str, float | int]:
    n = len(ranks)
    if n == 0:
        return {"n": 0, "recall@1": 0.0, "recall@3": 0.0, "recall@5": 0.0, "mrr": 0.0}
    recall_at_1 = sum(1 for r in ranks if r is not None and r <= 1) / n
    recall_at_3 = sum(1 for r in ranks if r is not None and r <= 3) / n
    recall_at_5 = sum(1 for r in ranks if r is not None and r <= 5) / n
    mrr = sum((1.0 / r) for r in ranks if r is not None) / n
    return {
        "n": n,
        "recall@1": round(recall_at_1, 4),
        "recall@3": round(recall_at_3, 4),
        "recall@5": round(recall_at_5, 4),
        "mrr": round(mrr, 4),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_report(
    stats_by_provider: dict[str, ProviderStats],
    entries: list[CorpusEntry],
    noise_info: tuple[str, int] | None = None,
) -> str:
    lines: list[str] = []
    if noise_info is not None:
        profile_name, seed = noise_info
        lines.append(
            f"== Metadata Search Benchmark (noise: {profile_name}, seed={seed}) =="
        )
    else:
        lines.append("== Metadata Search Benchmark ==")
    categories: dict[str, int] = {}
    for e in entries:
        categories[e.category] = categories.get(e.category, 0) + 1
    cat_str = ", ".join(f"{v} {k}" for k, v in sorted(categories.items()))
    lines.append(f"Corpus: {len(entries)} entries ({cat_str})" if cat_str else f"Corpus: {len(entries)} entries")
    lines.append("")
    header = f"{'Provider':<16} | {'recall@1':^8} | {'recall@3':^8} | {'recall@5':^8} | {'MRR':^6} | n"
    lines.append(header)
    lines.append("-" * len(header))
    for provider, stats in sorted(stats_by_provider.items()):
        if stats.inactive:
            lines.append(f"{provider:<16} | {'(inactive — skipped)':<50}")
            continue
        m = compute_metrics(stats.ranks)
        lines.append(
            f"{provider:<16} | "
            f"{m['recall@1']:^8.3f} | "
            f"{m['recall@3']:^8.3f} | "
            f"{m['recall@5']:^8.3f} | "
            f"{m['mrr']:^6.3f} | "
            f"{m['n']}"
        )

    # Adversarial-only subset
    adversarial_slugs = {e.slug for e in entries if e.category == "adversarial"}
    if adversarial_slugs:
        lines.append("")
        lines.append("Adversarial subset:")
        lines.append(header)
        lines.append("-" * len(header))
        for provider, stats in sorted(stats_by_provider.items()):
            if stats.inactive:
                continue
            pairs = [
                (slug, rank)
                for slug, rank in zip(stats.entries, stats.ranks, strict=False)
                if slug in adversarial_slugs
            ]
            if not pairs:
                continue
            adv_ranks = [r for _, r in pairs]
            m = compute_metrics(adv_ranks)
            lines.append(
                f"{provider:<16} | "
                f"{m['recall@1']:^8.3f} | "
                f"{m['recall@3']:^8.3f} | "
                f"{m['recall@5']:^8.3f} | "
                f"{m['mrr']:^6.3f} | "
                f"{m['n']}"
            )

    # Worst-performing entries per provider
    lines.append("")
    lines.append("Worst-performing entries:")
    any_worst = False
    for provider, stats in sorted(stats_by_provider.items()):
        if stats.inactive or not stats.ranks:
            continue
        pairs = list(zip(stats.entries, stats.ranks, strict=False))
        # worst = not found, then highest rank numbers
        pairs.sort(key=lambda p: (p[1] is not None, p[1] if p[1] is not None else 10**9), reverse=True)
        worst = [p for p in pairs if p[1] is None or p[1] > 3][:5]
        if not worst:
            continue
        any_worst = True
        lines.append(f"  {provider}:")
        for slug, rank in worst:
            rank_str = "not found" if rank is None else f"rank {rank}"
            lines.append(f"    - {slug}: {rank_str}")
    if not any_worst:
        lines.append("  (all entries within top 3 for every provider)")

    return "\n".join(lines) + "\n"


def build_json_report(
    stats_by_provider: dict[str, ProviderStats],
    entries: list[CorpusEntry],
    noise_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    providers_out: dict[str, Any] = {}
    for provider, stats in stats_by_provider.items():
        if stats.inactive:
            providers_out[provider] = {"inactive": True}
            continue
        metrics = compute_metrics(stats.ranks)
        providers_out[provider] = {
            **metrics,
            "ranks": [
                {"slug": slug, "rank": rank}
                for slug, rank in zip(stats.entries, stats.ranks, strict=False)
            ],
        }
    report: dict[str, Any] = {
        "corpus_size": len(entries),
        "categories": {
            c: sum(1 for e in entries if e.category == c) for c in {e.category for e in entries}
        },
        "providers": providers_out,
    }
    if noise_meta is not None:
        report["noise"] = noise_meta
    return report


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def apply_filter(entries: list[CorpusEntry], filter_exprs: list[str]) -> list[CorpusEntry]:
    """Apply simple ``key=value`` filters. Supported keys: category, slug."""
    result = entries
    for expr in filter_exprs:
        if "=" not in expr:
            print(f"warning: ignoring invalid filter {expr!r}", file=sys.stderr)
            continue
        key, _, value = expr.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "category":
            result = [e for e in result if e.category == value]
        elif key == "slug":
            result = [e for e in result if e.slug == value]
        else:
            print(f"warning: unsupported filter key {key!r}", file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmarks/run.py",
        description="Run the metadata-search benchmark over the labeled corpus.",
    )
    parser.add_argument("--refresh", action="store_true", help="Invalidate all caches before running.")
    parser.add_argument(
        "--refresh-slug",
        action="append",
        default=[],
        metavar="SLUG",
        help="Invalidate cache for a specific slug (can repeat).",
    )
    parser.add_argument(
        "--refresh-provider",
        action="append",
        default=[],
        metavar="PROVIDER",
        help="Invalidate cache for a specific provider (can repeat).",
    )
    parser.add_argument(
        "--filter",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Filter corpus entries (e.g. category=adversarial).",
    )
    parser.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument(
        "--report",
        type=str,
        default=str(DEFAULT_REPORT_PATH),
        help='Path to write JSON report, or "-" to skip the file write.',
    )
    parser.add_argument("--limit", type=int, default=25, help="Max results per provider query.")

    noise_group = parser.add_argument_group("Noise")
    noise_group.add_argument(
        "--noise-config",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to a TOML noise profile (e.g. benchmarks/noise-profiles/realistic.toml).",
    )
    noise_group.add_argument(
        "--noise-preset",
        type=str,
        default=None,
        metavar="NAME",
        help=(
            "Named preset from benchmarks/noise-profiles/<name>.toml "
            "(shorthand for --noise-config)."
        ),
    )

    compare_group = parser.add_argument_group("Comparison")
    compare_group.add_argument(
        "--compare",
        type=str,
        default=None,
        metavar="PATH",
        help="Compare the current report against a previously saved baseline JSON report.",
    )
    compare_group.add_argument(
        "--max-regression",
        type=float,
        default=0.03,
        metavar="FLOAT",
        help="Tolerable recall@1 drop per provider before flagging a regression (default 0.03).",
    )
    compare_group.add_argument(
        "--save-baseline",
        type=str,
        default=None,
        metavar="PATH",
        help="Write the current report to PATH (in addition to --report) and exit.",
    )
    return parser


def _invalidate_cache(
    cache_dir: Path,
    *,
    refresh_all: bool,
    slugs: list[str],
    providers: list[str],
    noise_active: bool = False,
) -> None:
    # Noise runs use a query-hash cache under cache_dir.parent/cache_noise/.
    # For a full refresh during a noise run, wipe that directory too; the
    # hashed filenames don't encode slug/provider so fine-grained removal
    # is not feasible without a sidecar index.
    if noise_active and refresh_all:
        noise_cache_dir = cache_dir.parent / "cache_noise"
        if noise_cache_dir.exists():
            for child in noise_cache_dir.glob("*.json"):
                child.unlink(missing_ok=True)

    if not cache_dir.exists():
        return
    if refresh_all and not slugs and not providers:
        for child in cache_dir.glob("*/*.json"):
            child.unlink(missing_ok=True)
        return
    for slug_dir in cache_dir.iterdir():
        if not slug_dir.is_dir():
            continue
        if slugs and slug_dir.name not in slugs:
            continue
        for cache_file in slug_dir.glob("*.json"):
            if providers:
                provider_name = cache_file.stem.replace("_", " ")
                if provider_name not in providers and cache_file.stem not in providers:
                    continue
            cache_file.unlink(missing_ok=True)


def _print_comparison(
    current_report: dict[str, Any],
    baseline_path: Path,
    max_regression: float,
) -> int:
    """Print provider-by-provider deltas; return 2 if any regression exceeds threshold."""
    if not baseline_path.exists():
        print(f"\nwarning: baseline {baseline_path} does not exist; skipping comparison")
        return 0

    try:
        baseline = json.loads(baseline_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"\nwarning: failed to load baseline {baseline_path}: {exc}")
        return 0

    baseline_providers = baseline.get("providers", {})
    current_providers = current_report.get("providers", {})

    all_providers = set(baseline_providers) | set(current_providers)
    regressions: list[str] = []

    print(f"\n== Comparison vs baseline ({baseline_path}) ==")
    print(
        f"{'Provider':<16} | {'Δ recall@1':>10} | {'Δ recall@3':>10} | {'Δ MRR':>7} | Status"
    )
    print("-" * 68)

    for provider in sorted(all_providers):
        bp = baseline_providers.get(provider)
        cp = current_providers.get(provider)
        if bp is None:
            print(f"{provider:<16} | {'(new)':>10} | {'(new)':>10} | {'(new)':>7} | new provider")
            continue
        if cp is None:
            print(
                f"{provider:<16} | {'(gone)':>10} | {'(gone)':>10} | {'(gone)':>7} | missing from current"
            )
            continue
        if bp.get("inactive") or cp.get("inactive"):
            print(
                f"{provider:<16} | {'(inactive)':>10} | {'(inactive)':>10} | {'(n/a)':>7} | skipped (inactive)"
            )
            continue

        # Metrics live flat on the provider dict (see build_json_report).
        b_r1 = float(bp.get("recall@1", 0.0))
        c_r1 = float(cp.get("recall@1", 0.0))
        b_r3 = float(bp.get("recall@3", 0.0))
        c_r3 = float(cp.get("recall@3", 0.0))
        b_mrr = float(bp.get("mrr", 0.0))
        c_mrr = float(cp.get("mrr", 0.0))

        d_r1 = c_r1 - b_r1
        d_r3 = c_r3 - b_r3
        d_mrr = c_mrr - b_mrr

        status = "ok"
        if d_r1 < -max_regression:
            status = f"REGRESSED ({d_r1:+.3f} > -{max_regression:.3f} threshold)"
            regressions.append(provider)
        elif d_r1 > max_regression:
            status = "improved"

        print(
            f"{provider:<16} | {d_r1:>+10.3f} | {d_r3:>+10.3f} | {d_mrr:>+7.3f} | {status}"
        )

    if regressions:
        print(f"\nREGRESSION DETECTED on: {', '.join(regressions)}")
        return 2
    print("\nNo regressions detected.")
    return 0


async def _run(args: argparse.Namespace) -> int:
    corpus_dir: Path = args.corpus_dir
    cache_dir: Path = args.cache_dir

    entries = load_corpus(corpus_dir)
    if not entries:
        print(f"No corpus entries found at {corpus_dir}. Run capture.py first.")
        return 0

    entries = apply_filter(entries, args.filter)
    if not entries:
        print("No corpus entries match the supplied filters.")
        return 0

    # --- Noise config loading ---------------------------------------------
    import noise as _noise  # local import to avoid polluting top-level

    noise_config = None
    profile_path: Path | None = None
    if args.noise_config or args.noise_preset:
        if args.noise_preset:
            profile_path = _BENCH_DIR / "noise-profiles" / f"{args.noise_preset}.toml"
            if not profile_path.exists():
                print(
                    f"error: noise preset '{args.noise_preset}' not found at {profile_path}",
                    file=sys.stderr,
                )
                return 1
        else:
            profile_path = Path(args.noise_config)
        try:
            noise_config = _noise.NoiseConfig.from_toml(profile_path)
        except Exception as exc:  # noqa: BLE001
            print(f"error: failed to load noise config: {exc}", file=sys.stderr)
            return 1
        print(
            f"Loaded noise profile: {profile_path.name} (seed={noise_config.seed}, "
            f"{len(noise_config.noises)} noise type(s))"
        )

    _invalidate_cache(
        cache_dir,
        refresh_all=args.refresh,
        slugs=args.refresh_slug,
        providers=args.refresh_provider,
        noise_active=noise_config is not None and not noise_config.is_noop(),
    )

    # Pre-populate Discogs master → release-id cache so find_rank can be sync.
    master_cache = _load_master_cache(cache_dir)
    if not args.refresh:
        await _prepopulate_discogs_master_cache(entries, master_cache)
    else:
        master_cache = {}
        await _prepopulate_discogs_master_cache(entries, master_cache)
    _save_master_cache(cache_dir, master_cache)

    # Detect inactive providers up front. Only build stats for providers that
    # appear in at least one entry's ground truth.
    needed_providers: set[str] = set()
    for e in entries:
        needed_providers.update(e.ground_truth.keys())

    stats_by_provider: dict[str, ProviderStats] = {p: ProviderStats() for p in sorted(needed_providers)}
    inactive: set[str] = set()
    for provider in needed_providers:
        module = SEARCHSOURCES.get(provider)
        if module is None:
            print(f"warning: unknown provider {provider!r} in ground truth; skipping")
            stats_by_provider[provider].inactive = True
            inactive.add(provider)
            continue
        if not module.Searcher.is_active():
            print(f"Skipping {provider} (inactive)")
            stats_by_provider[provider].inactive = True
            inactive.add(provider)

    for entry in entries:
        tag = build_tag_data(entry)
        if noise_config is not None:
            tag = _noise.apply_noise(tag, entry.slug, noise_config)
        for provider, gt_url in entry.ground_truth.items():
            if provider in inactive:
                continue
            try:
                results = await query_provider(
                    provider,
                    entry,
                    tag,
                    args.limit,
                    cache_dir,
                    refresh=False,  # pre-invalidated above if requested
                    noise_config=noise_config,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"error: {provider} query failed for {entry.slug}: {exc}", file=sys.stderr)
                traceback.print_exc()
                stats_by_provider[provider].ranks.append(None)
                stats_by_provider[provider].entries.append(entry.slug)
                continue
            rank, _scored = find_rank(results, tag, gt_url, provider, master_cache)
            stats_by_provider[provider].ranks.append(rank)
            stats_by_provider[provider].entries.append(entry.slug)

    noise_info: tuple[str, int] | None = None
    noise_meta: dict[str, Any] | None = None
    if noise_config is not None and profile_path is not None:
        noise_info = (profile_path.name, noise_config.seed)
        noise_meta = {
            "profile": profile_path.name,
            "seed": noise_config.seed,
            "probabilities": noise_config.noises,
        }

    text_report = format_report(stats_by_provider, entries, noise_info=noise_info)
    print(text_report)

    json_report = build_json_report(stats_by_provider, entries, noise_meta=noise_meta)

    if args.report != "-":
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_bytes(
            (json.dumps(json_report, indent=2, sort_keys=True) + "\n").encode("utf-8")
        )
        print(f"Wrote JSON report to {report_path}")

    if args.save_baseline:
        baseline_path = Path(args.save_baseline)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_bytes(
            (json.dumps(json_report, indent=2, sort_keys=True) + "\n").encode("utf-8")
        )
        print(f"Wrote baseline to {baseline_path}")
        return 0

    if args.compare:
        if noise_config is not None:
            print(
                "\nwarning: comparing a noised run against a baseline may show "
                "misleading deltas. Baselines should typically be no-noise.",
                file=sys.stderr,
            )
        return _print_comparison(
            json_report,
            baseline_path=Path(args.compare),
            max_regression=args.max_regression,
        )

    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.compare and args.save_baseline:
        print("error: --compare and --save-baseline are mutually exclusive", file=sys.stderr)
        return 1
    if args.noise_config and args.noise_preset:
        print(
            "error: --noise-config and --noise-preset are mutually exclusive",
            file=sys.stderr,
        )
        return 1
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
