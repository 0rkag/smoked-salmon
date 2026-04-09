"""Capture a benchmark corpus entry from an album folder.

Reads tags from an album directory using salmon's existing extraction
pipeline, pairs them with ground-truth metadata-provider URLs provided
on the command line, and writes a JSON corpus entry to
``benchmarks/corpus/<slug>.json``.

This is a benchmark tool. It does not modify production code, only
imports from it. The ``benchmarks/`` directory is gitignored.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import msgspec

# Make sure we can import salmon when running from repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Providers, slug: CLI flag name -> canonical SEARCHSOURCES key.
PROVIDERS: dict[str, str] = {
    "discogs": "Discogs",
    "musicbrainz": "MusicBrainz",
    "deezer": "Deezer",
    "apple-music": "Apple Music",
    "bandcamp": "Bandcamp",
    "beatport": "Beatport",
    "qobuz": "Qobuz",
    "tidal": "Tidal",
}

CORPUS_DIR = _REPO_ROOT / "benchmarks" / "corpus"


def _slugify(text: str, max_length: int = 80) -> str:
    """Return a kebab-case slug derived from text.

    Non-ASCII characters (CJK, Cyrillic, etc.) are transliterated to Latin
    via ``unidecode`` before the ASCII-only pipeline, so that Japanese or
    Chinese titles produce readable slugs instead of empty strings.
    """
    from unidecode import unidecode

    transliterated = unidecode(text)
    normalized = unicodedata.normalize("NFKD", transliterated)
    stripped = "".join(c for c in normalized if not unicodedata.combining(c))
    lowered = stripped.lower().replace("_", "-").replace(" ", "-")
    cleaned = re.sub(r"[^a-z0-9\-]+", "", lowered)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned[:max_length].rstrip("-")


def _extract_tag_data(path: Path, source: str | None = None) -> dict[str, Any]:
    """Extract the tag_data subset from an album folder.

    Uses salmon's existing pipeline: ``gather_tags`` + ``gather_audio_info``
    + ``construct_rls_data``. Only the fields needed by
    ``salmon.search.scoring.TagData`` are kept. ``source`` is whatever the
    user supplied via ``--source`` (or None if unknown — never injected).
    """
    from salmon.tagger.audio_info import gather_audio_info
    from salmon.tagger.pre_data import construct_rls_data
    from salmon.tagger.tags import gather_tags

    tags = gather_tags(str(path))
    if not tags:
        raise SystemExit(f"error: no audio files found in {path}")
    audio_info = gather_audio_info(str(path))

    # Pass a non-None supplied_encoding so parse_encoding never prompts.
    # `source` is informational only; for benchmark capture we accept the
    # caller-supplied value (None if unknown — never inject a fake default).
    rls_data = construct_rls_data(
        tags,
        audio_info,
        source=source,
        encoding=("Lossless", False),
        scene=False,
        overwrite=False,
        prompt_encoding=False,
        hybrid=False,
    )

    # Count tracks across all discs.
    track_count = sum(len(disc) for disc in rls_data.get("tracks", {}).values())

    # Keep artists as [name, role] pairs preserving order.
    artists_pairs = [[name, role] for name, role in rls_data.get("artists", [])]

    # Year may be a str in rls_data; coerce to int when possible.
    year_val: int | None = None
    raw_year = rls_data.get("year")
    if raw_year is not None:
        try:
            year_val = int(str(raw_year)[:4])
        except (ValueError, TypeError):
            year_val = None

    return {
        "artists": artists_pairs,
        "title": rls_data.get("title"),
        "year": year_val,
        "label": rls_data.get("label"),
        "catno": rls_data.get("catno"),
        "track_count": track_count,
        "source": rls_data.get("source"),
        # Absolute path to the album folder on disk. Used by
        # benchmarks/compare.py to re-read the local tracklist for
        # diffing against provider metadata.
        "source_path": str(path.resolve()),
    }


def _derive_slug(tag_data: dict[str, Any]) -> str:
    """Build a slug from the first main artist + title."""
    title = tag_data.get("title") or ""
    main_artist = ""
    for name, role in tag_data.get("artists") or []:
        if role == "main":
            main_artist = name
            break
    if not main_artist and tag_data.get("artists"):
        main_artist = tag_data["artists"][0][0]
    base = f"{main_artist} {title}".strip()
    slug = _slugify(base)
    if not slug:
        raise SystemExit("error: could not derive slug from tags; pass --slug")
    return slug


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmarks/capture.py",
        description="Capture a benchmark corpus entry from an album folder.",
    )
    parser.add_argument("path", type=Path, help="Path to the album directory")
    for flag, canonical in PROVIDERS.items():
        parser.add_argument(
            f"--{flag}",
            metavar="URL",
            default=None,
            help=f"Ground-truth {canonical} release URL",
        )
    parser.add_argument("--slug", default=None, help="Override the derived slug")
    parser.add_argument(
        "--category",
        choices=["representative", "adversarial"],
        default="representative",
        help="Corpus category (default: representative)",
    )
    parser.add_argument("--notes", default="", help="Free-text notes for this entry")
    parser.add_argument(
        "--source",
        default=None,
        help='Release source (e.g. "CD", "WEB", "Vinyl", "Cassette"). '
        "Leave unset if unknown — never injected as a default.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing corpus file if present",
    )
    return parser


def _collect_ground_truth(args: argparse.Namespace) -> dict[str, str]:
    ground_truth: dict[str, str] = {}
    for flag, canonical in PROVIDERS.items():
        value = getattr(args, flag.replace("-", "_"))
        if value:
            ground_truth[canonical] = value
    return ground_truth


def _validate(tag_data: dict[str, Any], ground_truth: dict[str, str]) -> None:
    if not ground_truth:
        raise SystemExit(
            "error: at least one provider URL must be supplied "
            "(e.g. --discogs <url>)"
        )
    if not tag_data.get("title"):
        raise SystemExit("error: extracted tag_data.title is empty")
    if not tag_data.get("artists"):
        raise SystemExit("error: extracted tag_data.artists is empty")
    if not any(role == "main" for _, role in tag_data["artists"]):
        raise SystemExit("error: no main artist found in extracted tags")


def _encode_pretty(entry: dict[str, Any]) -> bytes:
    """Encode entry to pretty, sorted JSON bytes.

    msgspec.json.encode has no indent option, so we round-trip through
    ``msgspec.to_builtins`` and ``json.dumps`` for a readable, sorted form.
    """
    return (json.dumps(msgspec.to_builtins(entry), indent=2, sort_keys=True) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    path: Path = args.path
    if not path.is_dir():
        raise SystemExit(f"error: {path} is not a directory")

    ground_truth = _collect_ground_truth(args)
    if not ground_truth:
        raise SystemExit(
            "error: at least one provider URL must be supplied "
            "(e.g. --discogs <url>)"
        )

    tag_data = _extract_tag_data(path, source=args.source)
    _validate(tag_data, ground_truth)

    slug = args.slug or _derive_slug(tag_data)
    slug = _slugify(slug)
    if not slug:
        raise SystemExit("error: slug is empty after normalization")

    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CORPUS_DIR / f"{slug}.json"
    if out_path.exists() and not args.force:
        raise SystemExit(f"error: {out_path} already exists (use --force to overwrite)")

    entry: dict[str, Any] = {
        "slug": slug,
        "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "category": args.category,
        "notes": args.notes,
        "tag_data": tag_data,
        "ground_truth": ground_truth,
    }

    out_path.write_bytes(_encode_pretty(entry))

    main_artists = [name for name, role in tag_data["artists"] if role == "main"]
    artists_display = ", ".join(f"{a} (main)" for a in main_artists) or "(none)"
    rel_out = out_path.relative_to(_REPO_ROOT)
    print(f"Captured: {slug}")
    print(f"  Title: {tag_data['title']}")
    print(f"  Artists: {artists_display}")
    print(f"  Year: {tag_data['year']}")
    print(f"  Ground truth: {', '.join(sorted(ground_truth))}")
    print(f"Wrote: {rel_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
