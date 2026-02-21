import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from database import db, get_config
from scheduler import get_next_track_path

logger = logging.getLogger(__name__)
router = APIRouter()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/internal/next-track", response_class=PlainTextResponse)
def next_track():
    """Called by Liquidsoap to get the path of the next track to play."""
    path = get_next_track_path()
    logger.info(f"next-track returning: {path!r}")
    return path


@router.post("/internal/track-started/{track_id}")
def track_started(track_id: str):
    """Called by Liquidsoap when a track begins playing. Logs to play_log."""
    with db() as conn:
        # Verify track exists
        row = conn.execute(
            "SELECT id FROM tracks WHERE id=?", (track_id,)
        ).fetchone()
        if not row:
            logger.warning(f"track-started called with unknown track_id: {track_id}")
            return {"ok": False, "error": "unknown track"}

        conn.execute(
            "INSERT INTO play_log (track_id, played_at) VALUES (?, ?)",
            (track_id, _now()),
        )

    logger.info(f"track-started logged: {track_id}")
    return {"ok": True}
