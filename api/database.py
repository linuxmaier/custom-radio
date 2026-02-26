import os
import sqlite3
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

DB_PATH = os.environ.get("DB_PATH", "/data/radio.db")

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    submitter TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_url TEXT,
    file_path TEXT,
    duration_s REAL,
    tempo_bpm REAL,
    rms_energy REAL,
    spectral_centroid REAL,
    zero_crossing_rate REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    error_msg TEXT,
    submitted_at TEXT NOT NULL,
    ready_at TEXT,
    comment TEXT,
    youtube_video_id TEXT
);

CREATE TABLE IF NOT EXISTS play_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id TEXT NOT NULL REFERENCES tracks(id),
    played_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id TEXT NOT NULL REFERENCES tracks(id),
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_msg TEXT
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

CONFIG_DEFAULTS = {
    "programming_mode": "rotation",
    "rotation_tracks_per_block": "3",
    "rotation_current_submitter_idx": "0",
    "rotation_block_start_log_id": "0",
    "skip_requested": "false",
    "last_returned_track_id": "",
    "feature_min_tempo_bpm": "0",
    "feature_max_tempo_bpm": "1",
    "feature_min_rms_energy": "0",
    "feature_max_rms_energy": "1",
    "feature_min_spectral_centroid": "0",
    "feature_max_spectral_centroid": "1",
    "feature_min_zero_crossing_rate": "0",
    "feature_max_zero_crossing_rate": "1",
}


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        for key, value in CONFIG_DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                (key, value),
            )
        # Migrations for existing databases
        try:
            conn.execute("ALTER TABLE tracks ADD COLUMN comment TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE tracks ADD COLUMN youtube_video_id TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Backfill youtube_video_id from source_url for tracks submitted before this column existed
        rows = conn.execute(
            "SELECT id, source_url FROM tracks"
            " WHERE source_type='youtube' AND youtube_video_id IS NULL AND source_url IS NOT NULL"
        ).fetchall()
        for row in rows:
            parsed = urlparse(row["source_url"])
            host = (parsed.hostname or "").lower()
            vid = None
            if host == "youtu.be":
                vid = parsed.path.lstrip("/").split("?")[0] or None
            elif host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
                qs = parse_qs(parsed.query)
                vid = qs.get("v", [None])[0]
            if vid:
                conn.execute("UPDATE tracks SET youtube_video_id=? WHERE id=?", (vid, row["id"]))
        conn.commit()
    finally:
        conn.close()


def get_config(key: str) -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        if row:
            return row["value"]
        return CONFIG_DEFAULTS.get(key, "")


def set_config(key: str, value: str):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        )
