import logging

from fastapi import APIRouter, HTTPException

from database import db

logger = logging.getLogger(__name__)
router = APIRouter()


def _track_row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "artist": row["artist"],
        "submitter": row["submitter"],
        "source_type": row["source_type"],
        "source_url": row["source_url"],
        "duration_s": row["duration_s"],
        "status": row["status"],
        "error_msg": row["error_msg"],
        "submitted_at": row["submitted_at"],
        "ready_at": row["ready_at"],
    }


@router.get("/status")
def get_status():
    """Now playing, recent tracks, and pending count."""
    with db() as conn:
        # Currently playing: last entry in play_log
        now_playing_row = conn.execute(
            """
            SELECT t.id, t.title, t.artist, t.submitter, t.duration_s,
                   pl.played_at
            FROM play_log pl
            JOIN tracks t ON pl.track_id = t.id
            ORDER BY pl.played_at DESC
            LIMIT 1
            """
        ).fetchone()

        # Recent 10 tracks (excluding current)
        recent_rows = conn.execute(
            """
            SELECT t.id, t.title, t.artist, t.submitter, t.duration_s,
                   pl.played_at
            FROM play_log pl
            JOIN tracks t ON pl.track_id = t.id
            ORDER BY pl.played_at DESC
            LIMIT 11
            """
        ).fetchall()

        # Pending queue count
        pending_count = conn.execute(
            "SELECT COUNT(*) as n FROM tracks WHERE status IN ('pending', 'processing')"
        ).fetchone()["n"]

    now_playing = None
    if now_playing_row:
        now_playing = {
            "id": now_playing_row["id"],
            "title": now_playing_row["title"],
            "artist": now_playing_row["artist"],
            "submitter": now_playing_row["submitter"],
            "duration_s": now_playing_row["duration_s"],
            "played_at": now_playing_row["played_at"],
        }

    recent = []
    for i, row in enumerate(recent_rows):
        if i == 0:
            continue  # skip now-playing
        recent.append({
            "id": row["id"],
            "title": row["title"],
            "artist": row["artist"],
            "submitter": row["submitter"],
            "played_at": row["played_at"],
        })

    return {
        "now_playing": now_playing,
        "recent": recent,
        "pending_count": pending_count,
    }


@router.get("/library")
def get_library():
    """All tracks with their status."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM tracks ORDER BY submitted_at DESC"
        ).fetchall()
    return {"tracks": [_track_row_to_dict(r) for r in rows]}


@router.get("/submitters")
def list_submitters():
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT submitter FROM tracks ORDER BY submitter"
        ).fetchall()
    return {"submitters": [r["submitter"] for r in rows]}


@router.get("/track/{track_id}")
def get_track(track_id: str):
    """Single track details (for polling submission status)."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM tracks WHERE id=?", (track_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Track not found")
    return _track_row_to_dict(row)
