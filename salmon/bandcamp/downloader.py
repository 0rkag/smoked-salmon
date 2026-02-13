"""Download and extract Bandcamp purchases using bandcampsync."""

from __future__ import annotations

import os
import re
import shutil
from typing import TYPE_CHECKING

import click
from bandcampsync.bandcamp import BandcampItem
from bandcampsync.download import download_file, is_zip_file, unzip_file

from salmon import cfg

if TYPE_CHECKING:
    from salmon.bandcamp.collection import BandcampCollection

_FORMAT_EXTENSIONS = {
    "flac": ".flac",
    "mp3-v0": ".mp3",
    "mp3-320": ".mp3",
    "mp3-128": ".mp3",
    "aac-hi": ".m4a",
    "vorbis": ".ogg",
    "alac": ".m4a",
    "wav": ".wav",
    "aiff-lossless": ".aiff",
    "aiff": ".aiff",
}


def download_and_extract(bc: BandcampCollection, bc_item: BandcampItem, dest_base_dir: str | None = None) -> str | None:
    """Download a Bandcamp purchase and extract it.

    Args:
        bc: BandcampCollection instance with auth
        bc_item: The original bandcampsync BandcampItem object
        dest_base_dir: Base directory for extraction. Falls back to
                       cfg.directory.tmp_dir or cfg.directory.download_directory.

    Returns:
        Path to extracted folder, or None on failure.
    """
    if dest_base_dir is None:
        dest_base_dir = cfg.directory.tmp_dir or cfg.directory.download_directory

    artist = bc_item.band_name
    title = bc_item.item_title
    click.secho(f"\nDownloading: {artist} — {title}", fg="cyan", bold=True)

    # Get download URL via bandcampsync
    download_format = cfg.bandcamp.download_format
    download_url = bc.get_download_url(bc_item, encoding=download_format)
    if not download_url:
        click.secho("  Could not find download URL. Item may be streaming-only.", fg="red")
        return None

    # Prepare directories
    tmp_dir = os.path.join(dest_base_dir, ".salmon_bc_tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    extract_dir = os.path.join(
        dest_base_dir,
        _sanitize_dirname(f"{artist} - {title} [{bc_item.item_id}]"),
    )

    # Download using bandcampsync's download_file (expects an open file handle)
    tmp_file = os.path.join(tmp_dir, f"{bc_item.item_id}.download")
    try:
        with open(tmp_file, "wb") as fh:
            download_file(download_url, fh)
    except (OSError, ValueError) as e:
        click.secho(f"  Download failed: {e}", fg="red")
        return None

    # Extract if ZIP, otherwise move directly
    try:
        if is_zip_file(tmp_file):
            os.makedirs(extract_dir, exist_ok=True)
            unzip_file(tmp_file, extract_dir)
            click.secho(f"  Extracted to {extract_dir}", fg="green")
        else:
            # Single file (track purchase)
            os.makedirs(extract_dir, exist_ok=True)
            ext = _FORMAT_EXTENSIONS.get(download_format, f".{download_format}")
            dest_file = os.path.join(extract_dir, f"{_sanitize_dirname(title)}{ext}")
            shutil.move(tmp_file, dest_file)
            click.secho(f"  Saved to {extract_dir}", fg="green")
    except (OSError, ValueError) as e:
        click.secho(f"  Extraction failed: {e}", fg="red")
        if os.path.isdir(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
        return None
    finally:
        # Clean up temp download dir
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return extract_dir


def _sanitize_dirname(name: str) -> str:
    """Remove characters that are invalid in directory names."""
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name.strip(". ")
