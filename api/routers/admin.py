import logging
import os
import socket
from datetime import UTC, datetime

from database import db, get_config, set_config
from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from pydantic import BaseModel

COOKIES_PATH = "/app/cookies/youtube.txt"

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
    dj_enabled: bool | None = None
    dj_submitters_per_interlude: int | None = None
    active_program_id: int | None = None


def _resolved_active_program_id(update: ConfigUpdate) -> int | None:
    if update.active_program_id is not None:
        return update.active_program_id
    raw = get_config("active_program_id")
    return int(raw) if raw else None


def _clear_dj_pending_state():
    """Clear any in-flight DJ generation state. Used when entering programmed mode."""
    pending_file = get_config("dj_pending_file")
    if pending_file and os.path.exists(pending_file):
        try:
            os.unlink(pending_file)
        except OSError:
            pass
    set_config("dj_pending_file", "")
    set_config("dj_reserved_track_id", "")
    set_config("dj_interlude_just_played", "false")
    set_config("dj_generation_needed", "false")


@router.get("/admin/config")
def get_admin_config(auth=Depends(require_admin)):
    active_program_id_raw = get_config("active_program_id")
    active_program_id = int(active_program_id_raw) if active_program_id_raw else None
    active_program_name = None
    active_program_item_count = 0
    if active_program_id:
        with db() as conn:
            row = conn.execute("SELECT name FROM programs WHERE id=?", (active_program_id,)).fetchone()
            if row:
                active_program_name = row["name"]
                active_program_item_count = conn.execute(
                    "SELECT COUNT(*) FROM program_items WHERE program_id=?", (active_program_id,)
                ).fetchone()[0]
            else:
                active_program_id = None  # stale id; surface as cleared
    return {
        "programming_mode": get_config("programming_mode"),
        "rotation_tracks_per_block": int(get_config("rotation_tracks_per_block")),
        "rotation_current_submitter_idx": int(get_config("rotation_current_submitter_idx")),
        "dj_enabled": get_config("dj_enabled") == "true",
        "dj_submitters_per_interlude": int(get_config("dj_submitters_per_interlude") or "2"),
        "active_program_id": active_program_id,
        "active_program_name": active_program_name,
        "active_program_item_count": active_program_item_count,
        "program_current_position": int(get_config("program_current_position") or "0"),
    }


@router.post("/admin/config")
def update_admin_config(update: ConfigUpdate, auth=Depends(require_admin)):
    if update.programming_mode is not None:
        if update.programming_mode not in ("rotation", "mood", "programmed"):
            raise HTTPException(400, "programming_mode must be 'rotation', 'mood', or 'programmed'")

        if update.programming_mode == "programmed":
            pid = _resolved_active_program_id(update)
            if not pid:
                raise HTTPException(400, "active_program_id is required to switch to programmed mode")
            with db() as conn:
                program = conn.execute("SELECT id FROM programs WHERE id=?", (pid,)).fetchone()
                if not program:
                    raise HTTPException(400, f"Program {pid} not found")
                item_count = conn.execute("SELECT COUNT(*) FROM program_items WHERE program_id=?", (pid,)).fetchone()[0]
            if item_count == 0:
                raise HTTPException(400, "Cannot activate an empty program")
            set_config("active_program_id", str(pid))
            set_config("program_current_position", "0")
            set_config("program_pending_position", "")
            _clear_dj_pending_state()
        else:
            # Switching away from programmed — clear program state.
            if get_config("programming_mode") == "programmed":
                set_config("active_program_id", "")
                set_config("program_current_position", "0")
                set_config("program_pending_position", "")

        set_config("programming_mode", update.programming_mode)
        logger.info(f"Programming mode set to: {update.programming_mode}")

    if update.rotation_tracks_per_block is not None:
        if not (1 <= update.rotation_tracks_per_block <= 20):
            raise HTTPException(400, "rotation_tracks_per_block must be 1-20")
        set_config("rotation_tracks_per_block", str(update.rotation_tracks_per_block))
        logger.info(f"Tracks per block set to: {update.rotation_tracks_per_block}")

    if update.dj_enabled is not None:
        set_config("dj_enabled", "true" if update.dj_enabled else "false")
        logger.info(f"DJ enabled set to: {update.dj_enabled}")

    if update.dj_submitters_per_interlude is not None:
        if not (1 <= update.dj_submitters_per_interlude <= 20):
            raise HTTPException(400, "dj_submitters_per_interlude must be 1-20")
        set_config("dj_submitters_per_interlude", str(update.dj_submitters_per_interlude))
        logger.info(f"DJ submitters per interlude set to: {update.dj_submitters_per_interlude}")

    return {"ok": True}


def _liquidsoap_skip():
    """Send a skip command to the Liquidsoap telnet server."""
    # Clear last_returned before flushing so the flushed prefetch track doesn't
    # count as an exclusion in the next get_next_track call. Without this, a
    # submitter with 2 songs would have both excluded simultaneously (last_returned
    # = flushed prefetch, last_played = the skipped track), triggering an early
    # submitter advance.
    set_config("last_returned_track_id", "")
    # In programmed mode, the prefetched-but-not-yet-started item is about to be
    # flushed by Liquidsoap. Roll the position back so the next pick returns it
    # again — otherwise the program silently skips an item it never played.
    if get_config("programming_mode") == "programmed":
        pending = get_config("program_pending_position")
        if pending:
            set_config("program_current_position", pending)
            set_config("program_pending_position", "")
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
        raise HTTPException(503, "Could not reach Liquidsoap") from None
    return {"ok": True}


@router.get("/admin/youtube-cookies/status")
def youtube_cookies_status(auth=Depends(require_admin)):
    """Check whether a YouTube cookies file is present."""
    exists = os.path.exists(COOKIES_PATH)
    updated_at = get_config("youtube_cookies_uploaded_at") if exists else None
    return {"present": exists, "updated_at": updated_at}


@router.post("/admin/youtube-cookies")
async def upload_youtube_cookies(file: UploadFile = File(...), auth=Depends(require_admin)):
    """Upload a YouTube cookies.txt file (Netscape format) to enable downloads from AWS IPs."""
    os.makedirs(os.path.dirname(COOKIES_PATH), exist_ok=True)
    content = await file.read()
    with open(COOKIES_PATH, "wb") as f:
        f.write(content)
    set_config("youtube_cookies_uploaded_at", datetime.now(UTC).isoformat())
    logger.info("YouTube cookies updated")
    return {"ok": True}


class TrackUpdate(BaseModel):
    in_rotation: bool | None = None


@router.patch("/admin/track/{track_id}")
def update_track(track_id: str, update: TrackUpdate, auth=Depends(require_admin)):
    """Update mutable per-track admin fields."""
    with db() as conn:
        row = conn.execute("SELECT id FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Track not found")
        if update.in_rotation is not None:
            conn.execute(
                "UPDATE tracks SET in_rotation=? WHERE id=?",
                (1 if update.in_rotation else 0, track_id),
            )
            logger.info(f"Track {track_id} in_rotation set to: {update.in_rotation}")
    return {"ok": True}


@router.delete("/admin/track/{track_id}")
def delete_track(track_id: str, auth=Depends(require_admin)):
    """Remove a track from the library and delete its file."""
    with db() as conn:
        row = conn.execute("SELECT file_path FROM tracks WHERE id=?", (track_id,)).fetchone()
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
