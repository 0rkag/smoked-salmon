"""Database helpers for bandcamp collection caching."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from salmon.database import DB_PATH
from salmon.sources.bandcamp_types import CollectionItem, ResultInfo, TrackerStatus


def get_connection():
    return sqlite3.connect(DB_PATH)


def get_known_urls() -> set[str]:
    """Return set of all bandcamp_url values in the collection table."""
    with get_connection() as conn:
        cursor = conn.execute("SELECT bandcamp_url FROM bandcamp_collection")
        return {row[0] for row in cursor.fetchall()}


def get_known_item_ids() -> set[int]:
    """Return set of all bandcamp_item_id values in the collection table."""
    with get_connection() as conn:
        cursor = conn.execute("SELECT bandcamp_item_id FROM bandcamp_collection WHERE bandcamp_item_id IS NOT NULL")
        return {row[0] for row in cursor.fetchall()}


def insert_collection_item(item: CollectionItem) -> None:
    """Insert or update a single collection item in the database."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO bandcamp_collection
               (bandcamp_url, bandcamp_item_id, artist, title, item_type,
                purchase_date, release_date, label, barcode, genres, tags,
                cover_url, tracks, description, credits, track_count, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        return [CollectionItem(**row) for row in cursor.fetchall()]


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
                                     checked_at
                              FROM bandcamp_collection_tracker_status""")
        for row in cursor.fetchall():
            cid = row["collection_id"]
            if cid not in statuses:
                statuses[cid] = {}
            raw = row["results"]
            results = json.loads(raw) if raw else {}
            statuses[cid][row["tracker"]] = TrackerStatus(
                status=row["status"],
                results=results,
                group_id=row["group_id"],
                checked_at=row["checked_at"],
            )
    return statuses


def upsert_tracker_status(
    collection_id: int,
    tracker: str,
    status: str,
    results: dict[str, ResultInfo] | None = None,
) -> None:
    """Insert or update tracker status for a collection item."""
    now = datetime.now().isoformat()
    results_json = json.dumps(results or {})
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO bandcamp_collection_tracker_status
                   (collection_id, tracker, status, results, checked_at)
               VALUES (?, ?, ?, ?, ?) ON CONFLICT(collection_id, tracker) DO
            UPDATE SET
                status=excluded.status, results=excluded.results, checked_at=excluded.checked_at""",
            (collection_id, tracker, status, results_json, now),
        )


def verify_tracker_match(collection_id: int, tracker: str, group_id: int) -> None:
    """Mark a specific group_id as the verified match."""
    with get_connection() as conn:
        conn.execute(
            """UPDATE bandcamp_collection_tracker_status
               SET status   = 'verified',
                   group_id = ?
               WHERE collection_id = ?
                 AND tracker = ?""",
            (group_id, collection_id, tracker),
        )


def mark_false_positive(collection_id: int, tracker: str) -> None:
    """Mark all results for this item+tracker as false positives."""
    with get_connection() as conn:
        conn.execute(
            """UPDATE bandcamp_collection_tracker_status
               SET status   = 'false_positive',
                   group_id = NULL
               WHERE collection_id = ?
                 AND tracker = ?""",
            (collection_id, tracker),
        )


def get_items_needing_check(tracker: str, recheck_days: int = 7) -> list[CollectionItem]:
    """Return collection items that need tracker checking.

    Items are returned if:
    - They have no status for this tracker, OR
    - Their status is 'not_found' (always recheck), OR
    - Their status is 'found' but checked_at is older than recheck_days

    Items with 'verified' or 'false_positive' status are never rechecked.
    """
    cutoff = (datetime.now() - timedelta(days=recheck_days)).isoformat()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
                              SELECT bc.id,
                                     bc.bandcamp_url,
                                     bc.bandcamp_item_id,
                                     bc.artist,
                                     bc.title,
                                     bc.item_type,
                                     bc.release_date,
                                     bc.label,
                                     bc.barcode,
                                     bc.genres,
                                     bc.tags,
                                     bc.cover_url,
                                     bc.track_count,
                                     bc.tracks,
                                     bc.description,
                                     bc.credits,
                                     bc.fetched_at
                              FROM bandcamp_collection bc
                                       LEFT JOIN bandcamp_collection_tracker_status ts
                                                 ON bc.id = ts.collection_id AND ts.tracker = ?
                              WHERE ts.collection_id IS NULL
                                 OR ts.status = 'not_found'
                                 OR (ts.status = 'found'
                                  AND ts.checked_at
                                         < ?)
                              ORDER BY bc.purchase_date DESC""",
            (tracker, cutoff),
        )
        return [CollectionItem(**row) for row in cursor.fetchall()]
