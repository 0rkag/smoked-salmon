CREATE TABLE IF NOT EXISTS bandcamp_collection (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bandcamp_url TEXT UNIQUE NOT NULL,
    bandcamp_item_id INTEGER,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    item_type TEXT NOT NULL,
    purchase_date TEXT,
    release_date TEXT,
    label TEXT,
    barcode TEXT,
    genres TEXT,
    tags TEXT,
    cover_url TEXT,
    track_count INTEGER,
    tracks TEXT,
    description TEXT,
    credits TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bandcamp_collection_tracker_status (
    collection_id INTEGER NOT NULL,
    tracker TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
    results TEXT NOT NULL DEFAULT '{}',
    group_id INTEGER,
    matched_at TEXT NOT NULL,
    inspected_at TEXT,
    results_changed_at TEXT,
    uploaded_at TEXT,
    PRIMARY KEY (collection_id, tracker),
    FOREIGN KEY (collection_id) REFERENCES bandcamp_collection(id)
);
