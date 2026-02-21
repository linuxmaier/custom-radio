import os
import socket
import logging

from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from database import db, get_config, set_config

logger = logging.getLogger(__name__)
router = APIRouter()

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


def require_admin(x_admin_token: str = Header(None)):
    if not ADMIN_TOKEN:
        raise HTTPException(500, "ADMIN_TOKEN not configured")
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")


class ConfigUpdate(BaseModel):
    programming_mode: str | None = None
    rotation_tracks_per_block: int | None = None


@router.get("/admin/config")
def get_admin_config(auth=Depends(require_admin)):
    return {
        "programming_mode": get_config("programming_mode"),
        "rotation_tracks_per_block": int(get_config("rotation_tracks_per_block")),
        "rotation_current_submitter_idx": int(get_config("rotation_current_submitter_idx")),
        "rotation_current_block_count": int(get_config("rotation_current_block_count")),
    }


@router.post("/admin/config")
def update_admin_config(update: ConfigUpdate, auth=Depends(require_admin)):
    if update.programming_mode is not None:
        if update.programming_mode not in ("rotation", "mood"):
            raise HTTPException(400, "programming_mode must be 'rotation' or 'mood'")
        set_config("programming_mode", update.programming_mode)
        logger.info(f"Programming mode set to: {update.programming_mode}")

    if update.rotation_tracks_per_block is not None:
        if not (1 <= update.rotation_tracks_per_block <= 20):
            raise HTTPException(400, "rotation_tracks_per_block must be 1-20")
        set_config("rotation_tracks_per_block", str(update.rotation_tracks_per_block))
        logger.info(f"Tracks per block set to: {update.rotation_tracks_per_block}")

    return {"ok": True}


def _liquidsoap_skip():
    """Send a skip command to the Liquidsoap telnet server."""
    with socket.create_connection(("liquidsoap", 1234), timeout=5) as sock:
        sock.sendall(b"dynamic.flush_and_skip\nquit\n")
        sock.recv(1024)  # drain response


@router.post("/admin/skip")
def request_skip(auth=Depends(require_admin)):
    """Signal Liquidsoap to skip to the next track."""
    try:
        _liquidsoap_skip()
        logger.info("Skip sent to Liquidsoap")
    except Exception as e:
        logger.error(f"Skip failed: {e}")
        raise HTTPException(503, "Could not reach Liquidsoap")
    return {"ok": True}


@router.delete("/admin/track/{track_id}")
def delete_track(track_id: str, auth=Depends(require_admin)):
    """Remove a track from the library and delete its file."""
    with db() as conn:
        row = conn.execute(
            "SELECT file_path FROM tracks WHERE id=?", (track_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Track not found")

        file_path = row["file_path"]

        conn.execute("DELETE FROM play_log WHERE track_id=?", (track_id,))
        conn.execute("DELETE FROM jobs WHERE track_id=?", (track_id,))
        conn.execute("DELETE FROM tracks WHERE id=?", (track_id,))

    if file_path and os.path.exists(file_path):
        os.unlink(file_path)
        logger.info(f"Deleted file: {file_path}")

    logger.info(f"Deleted track: {track_id}")
    return {"ok": True}
