"""Download and extract Bandcamp purchases using bandcampsync."""

import os
import re

import click
from bandcampsync.download import download_file, is_zip_file, unzip_file

from salmon import cfg


def download_and_extract(bc, bandcampsync_item, dest_base_dir=None):
    """Download a Bandcamp purchase and extract it.

    Args:
        bc: BandcampCollection instance with auth
        bandcampsync_item: The original bandcampsync BandcampItem object
        dest_base_dir: Base directory for extraction. Falls back to
                       cfg.directory.tmp_dir or cfg.directory.download_directory.

    Returns:
        Path to extracted folder, or None on failure.
    """
    if dest_base_dir is None:
        dest_base_dir = cfg.directory.tmp_dir or cfg.directory.download_directory

    artist = bandcampsync_item.band_name
    title = bandcampsync_item.item_title
    click.secho(f"\nDownloading: {artist} — {title}", fg="cyan", bold=True)

    # Get download URL via bandcampsync
    download_format = cfg.bandcamp.download_format if cfg.bandcamp else "flac"
    download_url = bc.get_download_url(bandcampsync_item, encoding=download_format)
    if not download_url:
        click.secho("  Could not find download URL. Item may be streaming-only.", fg="red")
        return None

    # Prepare directories
    tmp_dir = os.path.join(dest_base_dir, ".salmon_bc_tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    extract_dir = os.path.join(
        dest_base_dir,
        _sanitize_dirname(f"{artist} - {title}"),
    )

    # Download using bandcampsync's download_file
    tmp_file = os.path.join(tmp_dir, f"{bandcampsync_item.item_id}.download")
    try:
        download_file(download_url, tmp_file)
    except Exception as e:
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
            ext = f".{download_format}"
            dest_file = os.path.join(extract_dir, f"{_sanitize_dirname(title)}{ext}")
            os.rename(tmp_file, dest_file)
            click.secho(f"  Saved to {extract_dir}", fg="green")
    except Exception as e:
        click.secho(f"  Extraction failed: {e}", fg="red")
        return None
    finally:
        if os.path.exists(tmp_file):
            os.remove(tmp_file)

    return extract_dir


def _sanitize_dirname(name):
    """Remove characters that are invalid in directory names."""
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name.strip(". ")
