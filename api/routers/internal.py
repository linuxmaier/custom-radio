import logging
from datetime import UTC, datetime

from database import db
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from scheduler import get_next_track

logger = logging.getLogger(__name__)
router = APIRouter()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _build_annotate_uri(track: dict) -> str:
    """Build a Liquidsoap annotate URI embedding title and artist from the DB."""

    def esc(s: str) -> str:
        return (s or "").replace("\\", "\\\\").replace('"', '\\"')

    return f'annotate:title="{esc(track["title"])}",artist="{esc(track["artist"])}":{track["file_path"]}'


@router.get("/internal/next-track", response_class=PlainTextResponse)
def next_track():
    """Called by Liquidsoap to get the path of the next track to play."""
    track = get_next_track()
    if not track:
        logger.info("next-track returning: '' (no track available)")
        return ""
    uri = _build_annotate_uri(track)
    logger.info(f"next-track returning: {uri!r}")
    return uri


@router.post("/internal/track-started/{track_id}")
def track_started(track_id: str):
    """Called by Liquidsoap when a track begins playing. Logs to play_log."""
    with db() as conn:
        # Verify track exists
        row = conn.execute("SELECT id FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not row:
            logger.warning(f"track-started called with unknown track_id: {track_id}")
            return {"ok": False, "error": "unknown track"}

        conn.execute(
            "INSERT INTO play_log (track_id, played_at) VALUES (?, ?)",
            (track_id, _now()),
        )

    logger.info(f"track-started logged: {track_id}")
    return {"ok": True}
