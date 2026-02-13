"""Database helpers for bandcamp collection caching."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import click

from salmon.bandcamp.types import CheckStatus, CollectionItem, ResultInfo, TrackerStatus
from salmon.database import DB_PATH

_write_lock = threading.Lock()


def _safe_json_loads(raw: str | None, default: Any = None) -> Any:
    """Parse a JSON string, returning *default* on failure or None input."""
    if not raw:
        return default if default is not None else {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        click.secho(f"  Warning: corrupt JSON in DB column: {e}", fg="yellow")
        return default if default is not None else {}


def _row_to_item(row: sqlite3.Row) -> CollectionItem:
    """Convert a sqlite3.Row to a CollectionItem, deserializing JSON columns."""
    d = dict(row)
    d["genres"] = _safe_json_loads(d.get("genres"), [])
    d["tags"] = _safe_json_loads(d.get("tags"), [])
    d["tracks"] = _safe_json_loads(d.get("tracks"), {})
    return CollectionItem(**d)


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        with conn:  # auto-commit/rollback
            yield conn
    finally:
        conn.close()


def get_known_urls() -> set[str]:
    """Return set of all bandcamp_url values in the collection table."""
    with get_connection() as conn:
        cursor = conn.execute("SELECT bandcamp_url FROM bandcamp_collection")
        return {row[0] for row in cursor.fetchall()}


def get_known_item_ids() -> set[int]:
    """Return set of all bandcamp_item_id values in the collection table."""
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT bandcamp_item_id FROM bandcamp_collection WHERE bandcamp_item_id IS NOT NULL"
        )
        return {row[0] for row in cursor.fetchall()}



def insert_collection_item(item: CollectionItem) -> None:
    """Insert or update a single collection item in the database.

    Uses ON CONFLICT to preserve the row id (and FK references in
    bandcamp_collection_tracker_status) when re-importing an item.
    """
    now = datetime.now().isoformat()
    with _write_lock, get_connection() as conn:
        conn.execute(
            """INSERT INTO bandcamp_collection
               (bandcamp_url, bandcamp_item_id, artist, title, item_type,
                purchase_date, release_date, label, barcode, genres, tags,
                cover_url, tracks, description, credits, track_count, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(bandcamp_url) DO UPDATE SET
                bandcamp_item_id=excluded.bandcamp_item_id,
                artist=excluded.artist,
                title=excluded.title,
                item_type=excluded.item_type,
                purchase_date=excluded.purchase_date,
                release_date=excluded.release_date,
                label=excluded.label,
                barcode=excluded.barcode,
                genres=excluded.genres,
                tags=excluded.tags,
                cover_url=excluded.cover_url,
                tracks=excluded.tracks,
                description=excluded.description,
                credits=excluded.credits,
                track_count=excluded.track_count,
                fetched_at=excluded.fetched_at""",
            (
                item["bandcamp_url"],
                item.get("bandcamp_item_id"),
                item["artist"],
                item["title"],
                item["item_type"],
                item.get("purchase_date"),
                item.get("release_date"),
                item.get("label"),
                item.get("barcode"),
                json.dumps(item.get("genres", [])),
                json.dumps(item.get("tags", [])),
                item.get("cover_url"),
                json.dumps(item.get("tracks", {})),
                item.get("description"),
                item.get("credits"),
                item.get("track_count", 0),
                now,
            ),
        )


def get_all_collection_items() -> list[CollectionItem]:
    """Return all collection items as list of dicts."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("""
                              SELECT id,
                                     bandcamp_url,
                                     bandcamp_item_id,
                                     artist,
                                     title,
                                     item_type,
                                     purchase_date,
                                     release_date,
                                     label,
                                     barcode,
                                     genres,
                                     tags,
                                     cover_url,
                                     track_count,
                                     tracks,
                                     description,
                                     credits,
                                     fetched_at
                              FROM bandcamp_collection
                              ORDER BY purchase_date DESC
                              """)
        return [_row_to_item(row) for row in cursor.fetchall()]


def get_tracker_statuses() -> dict[int, dict[str, TrackerStatus]]:
    """Return dict mapping collection_id -> {tracker: TrackerStatus}."""
    statuses: dict[int, dict[str, TrackerStatus]] = {}
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("""
                              SELECT collection_id,
                                     tracker,
                                     status,
                                     results,
                                     group_id,
                                     matched_at,
                                     inspected_at,
                                     results_changed_at,
                                     uploaded_at
                              FROM bandcamp_collection_tracker_status""")
        for row in cursor.fetchall():
            cid = row["collection_id"]
            if cid not in statuses:
                statuses[cid] = {}
            results = _safe_json_loads(row["results"], {})
            statuses[cid][row["tracker"]] = TrackerStatus(
                status=row["status"],
                results=results,
                group_id=row["group_id"],
                matched_at=row["matched_at"],
                inspected_at=row["inspected_at"],
                results_changed_at=row["results_changed_at"],
                uploaded_at=row["uploaded_at"],
            )
    return statuses


def upsert_tracker_status(
    collection_id: int,
    tracker: str,
    status: CheckStatus,
    results: dict[str, ResultInfo] | None = None,
    results_changed: bool = False,
) -> None:
    """Insert or update tracker status for a collection item.

    When *results_changed* is True, also updates ``results_changed_at``.
    Always updates ``matched_at``.
    """
    now = datetime.now().isoformat()
    results_json = json.dumps(results or {})
    with _write_lock, get_connection() as conn:
        if results_changed:
            conn.execute(
                """INSERT INTO bandcamp_collection_tracker_status
                       (collection_id, tracker, status, results, matched_at, results_changed_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(collection_id, tracker) DO UPDATE SET
                       status=excluded.status,
                       results=excluded.results,
                       matched_at=excluded.matched_at,
                       results_changed_at=excluded.results_changed_at""",
                (collection_id, tracker, status, results_json, now, now),
            )
        else:
            conn.execute(
                """INSERT INTO bandcamp_collection_tracker_status
                       (collection_id, tracker, status, results, matched_at, results_changed_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(collection_id, tracker) DO UPDATE SET
                       status=excluded.status,
                       results=excluded.results,
                       matched_at=excluded.matched_at""",
                (collection_id, tracker, status, results_json, now, now),
            )


def verify_tracker_match(collection_id: int, tracker: str, group_id: int) -> None:
    """Mark a specific group_id as the verified match."""
    now = datetime.now().isoformat()
    with _write_lock, get_connection() as conn:
        conn.execute(
            """UPDATE bandcamp_collection_tracker_status
               SET status       = 'verified',
                   group_id     = ?,
                   inspected_at = ?
               WHERE collection_id = ?
                 AND tracker = ?""",
            (group_id, now, collection_id, tracker),
        )


def mark_false_positive(collection_id: int, tracker: str) -> None:
    """Mark all results for this item+tracker as false positives.

    Sets ``false_positive: true`` on every result entry and updates
    ``inspected_at`` to record the user action.
    """
    now = datetime.now().isoformat()
    with _write_lock, get_connection() as conn:
        row = conn.execute(
            "SELECT results FROM bandcamp_collection_tracker_status WHERE collection_id = ? AND tracker = ?",
            (collection_id, tracker),
        ).fetchone()
        if row and row[0]:
            results = _safe_json_loads(row[0], {})
            for info in results.values():
                info["false_positive"] = True
            results_json = json.dumps(results)
        else:
            results_json = "{}"
        conn.execute(
            """UPDATE bandcamp_collection_tracker_status
               SET status       = 'false_positive',
                   group_id     = NULL,
                   results      = ?,
                   inspected_at = ?
               WHERE collection_id = ?
                 AND tracker = ?""",
            (results_json, now, collection_id, tracker),
        )


def get_item_tracker_status(collection_id: int, tracker: str) -> TrackerStatus | None:
    """Fetch a single item's tracker status, or None if not yet checked."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT status, results, group_id, matched_at, inspected_at,
                      results_changed_at, uploaded_at
               FROM bandcamp_collection_tracker_status
               WHERE collection_id = ? AND tracker = ?""",
            (collection_id, tracker),
        ).fetchone()
        if not row:
            return None
        return TrackerStatus(
            status=row["status"],
            results=_safe_json_loads(row["results"], {}),
            group_id=row["group_id"],
            matched_at=row["matched_at"],
            inspected_at=row["inspected_at"],
            results_changed_at=row["results_changed_at"],
            uploaded_at=row["uploaded_at"],
        )


def mark_uploaded(collection_id: int, tracker: str) -> None:
    """Record when an item was uploaded to a tracker."""
    now = datetime.now().isoformat()
    with _write_lock, get_connection() as conn:
        conn.execute(
            """UPDATE bandcamp_collection_tracker_status
               SET uploaded_at = ?
               WHERE collection_id = ?
                 AND tracker = ?""",
            (now, collection_id, tracker),
        )


def get_items_needing_check(
    tracker: str,
    *,
    recheck_not_found_days: int = 1,
    recheck_false_positive_days: int = 3,
    recheck_found_days: int = 7,
    force_recheck: set[str] | None = None,
) -> list[CollectionItem]:
    """Return collection items that need tracker checking.

    Each status has its own recheck delay. ``force_recheck`` is a set of
    status strings (e.g. ``{"found", "verified"}``) that bypass age checks.
    """
    now = datetime.now()
    cutoff_not_found = (now - timedelta(days=recheck_not_found_days)).isoformat()
    cutoff_fp = (now - timedelta(days=recheck_false_positive_days)).isoformat()
    cutoff_found = (now - timedelta(days=recheck_found_days)).isoformat()
    force = force_recheck or set()

    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """SELECT bc.id, bc.bandcamp_url, bc.bandcamp_item_id,
                      bc.artist, bc.title, bc.item_type, bc.purchase_date,
                      bc.release_date, bc.label, bc.barcode, bc.genres,
                      bc.tags, bc.cover_url, bc.track_count, bc.tracks,
                      bc.description, bc.credits, bc.fetched_at
               FROM bandcamp_collection bc
               LEFT JOIN bandcamp_collection_tracker_status ts
                      ON bc.id = ts.collection_id AND ts.tracker = ?
               WHERE ts.collection_id IS NULL
                  OR (ts.status = 'not_found'      AND (? OR ts.matched_at < ?))
                  OR (ts.status = 'found'           AND (? OR ts.matched_at < ?))
                  OR (ts.status = 'false_positive'  AND (? OR ts.matched_at < ?))
                  OR (ts.status = 'verified'        AND ?)
                  OR ts.status NOT IN ('not_found', 'found', 'false_positive', 'verified')
               ORDER BY bc.purchase_date DESC""",
            (
                tracker,
                "not_found" in force, cutoff_not_found,
                "found" in force, cutoff_found,
                "false_positive" in force, cutoff_fp,
                "verified" in force,
            ),
        )
        return [_row_to_item(row) for row in cursor.fetchall()]


def get_inspectable_items(tracker_list: list[str]) -> list[CollectionItem]:
    """Return collection items that need inspection.

    An item needs inspection if ANY of its tracker statuses satisfy:
    - inspected_at IS NULL (never reviewed) and has results
    - results_changed_at > inspected_at (results changed since last review)
    """
    placeholders = ",".join("?" for _ in tracker_list)
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"""SELECT DISTINCT bc.id, bc.bandcamp_url, bc.bandcamp_item_id,
                       bc.artist, bc.title, bc.item_type, bc.purchase_date,
                       bc.release_date, bc.label, bc.barcode, bc.genres,
                       bc.tags, bc.cover_url, bc.track_count, bc.tracks,
                       bc.description, bc.credits, bc.fetched_at
                FROM bandcamp_collection bc
                JOIN bandcamp_collection_tracker_status ts
                     ON bc.id = ts.collection_id
                WHERE ts.tracker IN ({placeholders})
                  AND ts.results != '{{}}'
                  AND (
                      ts.inspected_at IS NULL
                      OR (ts.results_changed_at IS NOT NULL
                          AND ts.results_changed_at > ts.inspected_at)
                  )
                ORDER BY bc.purchase_date DESC""",
            tracker_list,
        )
        return [_row_to_item(row) for row in cursor.fetchall()]


def purge_bandcamp_data(*, collection: bool = True, statuses: bool = True) -> tuple[int, int]:
    """Delete rows from bandcamp tables.

    Returns (collection_deleted, statuses_deleted) counts.
    """
    col_count = 0
    stat_count = 0
    with _write_lock, get_connection() as conn:
        if statuses:
            stat_count = conn.execute("DELETE FROM bandcamp_collection_tracker_status").rowcount
        if collection:
            col_count = conn.execute("DELETE FROM bandcamp_collection").rowcount
    return col_count, stat_count
