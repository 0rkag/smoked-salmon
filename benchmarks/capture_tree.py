"""Walk a directory tree and auto-capture album folders as corpus entries.

This is a convenience wrapper around ``benchmarks/capture.py`` for bulk
ingestion of a music library. It discovers leaf album folders (directories
that contain at least one audio file directly inside) and writes a corpus
JSON entry for each one, with ``tag_data`` populated from the on-disk tags
and an empty ``ground_truth`` dict to be filled in later via ``suggest.py``
or manual editing.

Benchmark tool. Does not modify production code under ``src/``. The
``benchmarks/`` directory is gitignored.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Make sure we can import salmon and benchmarks/capture.py.
_BENCH_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BENCH_DIR.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCH_DIR))

import capture as _capture  # noqa: E402  benchmarks/capture.py

DEFAULT_CORPUS_DIR = _REPO_ROOT / "benchmarks" / "corpus"

AUDIO_EXTS = {
    ".flac",
    ".mp3",
    ".m4a",
    ".ogg",
    ".opus",
    ".wav",
    ".aiff",
    ".aif",
    ".ape",
    ".wv",
}

# Bracketed source tokens found anywhere in a folder name. Each alternative
# maps to a canonical source string via the group name. Catalog numbers like
# ``[HOSTOM004]`` do NOT match because the inner text must exactly match one
# of the listed tokens.
_SOURCE_PATTERN = re.compile(
    r"\[(?:"
    r"(?P<vinyl>VINYL|LP)"
    r"|(?P<web>WEB(?:[\s._-]?FLAC(?:[\s._-]?24(?:BIT)?)?)?|DIGITAL|FLAC24)"
    r"|(?P<cd>CD(?:[-\s]?R)?)"
    r"|(?P<cassette>CASSETTE|CASS|TAPE)"
    r"|(?P<sacd>SACD)"
    r"|(?P<dvd>DVD(?:[-\s]?A)?)"
    r")\]",
    re.IGNORECASE,
)

_SOURCE_CANONICAL = {
    "vinyl": "Vinyl",
    "web": "WEB",
    "cd": "CD",
    "cassette": "Cassette",
    "sacd": "SACD",
    "dvd": "DVD",
}


def _detect_source_from_folder(folder_name: str) -> str | None:
    """Return a canonical source string parsed from the folder name, or None."""
    match = _SOURCE_PATTERN.search(folder_name)
    if not match:
        return None
    for key, canonical in _SOURCE_CANONICAL.items():
        if match.group(key) is not None:
            return canonical
    return None


def _has_audio_child(directory: Path) -> bool:
    """Return True if ``directory`` contains at least one audio file directly."""
    try:
        for child in directory.iterdir():
            if child.is_file() and child.suffix.lower() in AUDIO_EXTS:
                return True
    except OSError:
        return False
    return False


def _iter_album_folders(root: Path, corpus_dir: Path) -> list[Path]:
    """Walk ``root`` and yield leaf album folders.

    A leaf album folder is a directory containing at least one audio file as
    a direct child. Once a folder is classified as an album, we do not recurse
    into it. Hidden directories (``.``-prefixed) are skipped, as is
    ``corpus_dir`` itself.
    """
    results: list[Path] = []
    corpus_dir_resolved = corpus_dir.resolve()

    def walk(directory: Path) -> None:
        try:
            resolved = directory.resolve()
        except OSError:
            return
        if resolved == corpus_dir_resolved:
            return
        if directory.name.startswith(".") and directory != root:
            return
        if _has_audio_child(directory):
            results.append(directory)
            return
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for child in entries:
            if child.is_dir():
                walk(child)

    walk(root)
    return results


def _matches_any(patterns: list[str], name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmarks/capture_tree.py",
        description=(
            "Walk a directory tree and auto-capture each album folder as a "
            "corpus entry (tag_data only — no ground-truth URLs)."
        ),
    )
    parser.add_argument("root", type=Path, help="Root directory to walk")
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=DEFAULT_CORPUS_DIR,
        help="Corpus output directory (default: benchmarks/corpus)",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Skip folders whose derived slug already exists in corpus",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-capture even if slug exists (overwrites)",
    )
    parser.add_argument(
        "--source-from-folder",
        action="store_true",
        help="Parse [Vinyl]/[WEB]/[CD]/etc. from folder name",
    )
    parser.add_argument(
        "--default-source",
        default=None,
        help="Fallback source if --source-from-folder finds nothing",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be captured, don't write",
    )
    parser.add_argument(
        "--category",
        choices=["representative", "adversarial"],
        default="representative",
        help="Corpus category (default: representative)",
    )
    parser.add_argument(
        "--include-glob",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Only process folders whose name matches PATTERN (repeatable)",
    )
    parser.add_argument(
        "--exclude-glob",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Skip folders whose name matches PATTERN (repeatable)",
    )
    parser.add_argument(
        "--max-entries",
        type=int,
        default=None,
        metavar="N",
        help="Cap at N captures (useful for testing)",
    )
    return parser


def _format_artists(tag_data: dict[str, Any]) -> str:
    main_artists = [name for name, role in tag_data.get("artists") or [] if role == "main"]
    if not main_artists:
        all_artists = [name for name, _ in tag_data.get("artists") or []]
        main_artists = all_artists[:1]
    return ", ".join(main_artists) or "(none)"


def _resolve_source(
    folder_name: str,
    *,
    source_from_folder: bool,
    default_source: str | None,
) -> tuple[str | None, str]:
    """Return (source, origin_label) where origin is 'detected'|'default'|'none'."""
    if source_from_folder:
        detected = _detect_source_from_folder(folder_name)
        if detected:
            return detected, "detected"
    if default_source:
        return default_source, "default"
    return None, "none"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    root: Path = args.root
    if not root.is_dir():
        raise SystemExit(f"error: {root} is not a directory")

    corpus_dir: Path = args.corpus_dir
    if not args.dry_run:
        corpus_dir.mkdir(parents=True, exist_ok=True)

    candidates = _iter_album_folders(root, corpus_dir)

    # Apply include/exclude globs against the folder basename.
    if args.include_glob:
        candidates = [p for p in candidates if _matches_any(args.include_glob, p.name)]
    if args.exclude_glob:
        candidates = [p for p in candidates if not _matches_any(args.exclude_glob, p.name)]

    scanned = len(candidates)
    captured = 0
    skipped = 0
    errors: list[tuple[Path, str]] = []

    for folder in candidates:
        if args.max_entries is not None and captured >= args.max_entries:
            break

        source, origin = _resolve_source(
            folder.name,
            source_from_folder=args.source_from_folder,
            default_source=args.default_source,
        )

        try:
            tag_data = _capture._extract_tag_data(folder, source=source)
        except SystemExit as exc:
            errors.append((folder, f"tag extraction failed: {exc}"))
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append((folder, f"tag extraction failed: {exc}"))
            if not args.dry_run:
                traceback.print_exc(file=sys.stderr)
            continue

        if not tag_data.get("title") or not tag_data.get("artists"):
            errors.append((folder, "missing title or artists in tags"))
            continue

        try:
            slug = _capture._derive_slug(tag_data)
        except SystemExit as exc:
            errors.append((folder, f"slug derivation failed: {exc}"))
            continue

        out_path = corpus_dir / f"{slug}.json"
        if out_path.exists():
            if args.force:
                pass  # re-capture
            elif args.only_missing:
                print(
                    f"skipped: {folder}  (slug '{slug}' already in corpus; "
                    "use --force to overwrite)"
                )
                skipped += 1
                continue
            else:
                errors.append(
                    (
                        folder,
                        f"slug '{slug}' already in corpus at {out_path}; "
                        "use --force or --only-missing",
                    )
                )
                continue

        entry: dict[str, Any] = {
            "slug": slug,
            "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "category": args.category,
            "notes": "",
            "tag_data": tag_data,
            "ground_truth": {},
        }

        try:
            rel_path = folder.relative_to(_REPO_ROOT)
            path_display: str = str(rel_path)
        except ValueError:
            path_display = str(folder)

        print(f"captured: {slug}")
        print(f"  path: {path_display}")
        print(f"  title: {tag_data.get('title')}")
        print(f"  artists: {_format_artists(tag_data)}")
        print(f"  source: {source if source else 'none'} ({origin})")
        print(f"  category: {args.category}")

        if not args.dry_run:
            try:
                out_path.write_bytes(_capture._encode_pretty(entry))
            except OSError as exc:
                errors.append((folder, f"write failed: {exc}"))
                continue

        captured += 1

    print()
    print("== capture_tree summary ==")
    print(f"Scanned: {scanned} album folder(s)")
    print(f"Captured: {captured} {'(dry-run)' if args.dry_run else 'new entries'}")
    print(f"Skipped (already in corpus): {skipped}")
    print(f"Errors: {len(errors)}")
    for path, reason in errors:
        print(f"  - {path}: {reason}")

    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
