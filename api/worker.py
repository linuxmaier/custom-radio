import logging
import os
import threading
from datetime import datetime, timezone

from alerts import send_alert
from audio import extract_features
from database import db
from downloader import convert_to_standard_mp3, download_youtube
from push import send_push_to_all
from scheduler import update_feature_bounds

logger = logging.getLogger(__name__)

_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _process_job(job_id: int, track_id: str):
    """Process a single job: download, convert, analyze."""
    logger.info(f"Processing job {job_id} for track {track_id}")

    # Mark job as started
    with db() as conn:
        conn.execute(
            "UPDATE jobs SET status='processing', started_at=? WHERE id=?",
            (_now(), job_id),
        )
        conn.execute(
            "UPDATE tracks SET status='processing' WHERE id=?",
            (track_id,),
        )

    try:
        # Get track details
        with db() as conn:
            row = conn.execute(
                "SELECT source_type, source_url, submitter, comment FROM tracks WHERE id=?",
                (track_id,),
            ).fetchone()

        if not row:
            raise RuntimeError(f"Track {track_id} not found")

        source_type = row["source_type"]
        source_url = row["source_url"] or ""
        submitter = row["submitter"] or ""
        comment = row["comment"] or ""
        raw_path = None
        title = None
        artist = None

        if source_type == "upload":
            # File was already uploaded to /media/raw/{track_id}.*
            upload_dir = os.path.join(os.environ.get("MEDIA_DIR", "/media"), "raw")
            for ext in ["mp3", "wav", "flac", "m4a", "ogg", "opus"]:
                candidate = os.path.join(upload_dir, f"{track_id}.{ext}")
                if os.path.exists(candidate):
                    raw_path = candidate
                    break
            if not raw_path:
                raise RuntimeError(f"Uploaded file not found for track {track_id}")
            # Title/artist already set at submission time; just get them
            with db() as conn:
                t = conn.execute("SELECT title, artist FROM tracks WHERE id=?", (track_id,)).fetchone()
            title = t["title"]
            artist = t["artist"]

        elif source_type == "youtube":
            title, artist, raw_path = download_youtube(source_url, track_id)

        else:
            raise RuntimeError(f"Unknown source_type: {source_type}")

        # Convert to standard MP3 with track_id embedded as comment tag
        final_path = convert_to_standard_mp3(raw_path, track_id, track_id, title=title or "", artist=artist or "")

        # Extract audio features
        features = extract_features(final_path)

        # Update feature normalization bounds
        update_feature_bounds(features)

        # Get duration via ffprobe
        import subprocess

        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                final_path,
            ],
            capture_output=True,
            text=True,
        )
        duration_s = None
        if result.returncode == 0:
            import json

            info = json.loads(result.stdout)
            for stream in info.get("streams", []):
                if stream.get("codec_type") == "audio":
                    duration_s = float(stream.get("duration", 0)) or None
                    break

        # Update track in DB
        with db() as conn:
            conn.execute(
                """
                UPDATE tracks SET
                    title=?, artist=?, file_path=?, duration_s=?,
                    tempo_bpm=?, rms_energy=?, spectral_centroid=?,
                    zero_crossing_rate=?, status='ready', ready_at=?, error_msg=NULL
                WHERE id=?
                """,
                (
                    title,
                    artist,
                    final_path,
                    duration_s,
                    features.tempo_bpm,
                    features.rms_energy,
                    features.spectral_centroid,
                    features.zero_crossing_rate,
                    _now(),
                    track_id,
                ),
            )
            conn.execute(
                "UPDATE jobs SET status='done', finished_at=? WHERE id=?",
                (_now(), job_id),
            )

        logger.info(f"Job {job_id} completed: track {track_id} ready at {final_path}")
        if comment:
            signoff = "\nTune in to hear its upcoming debut." if len(comment) <= 50 else ""
            body = f'They said: "{comment}"{signoff}'
        else:
            body = "Tune in to hear its upcoming debut."
        send_push_to_all(
            title=f"{submitter} added {title} to the radio!",
            body=body,
        )

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Job {job_id} failed: {error_msg}", exc_info=True)
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', finished_at=?, error_msg=? WHERE id=?",
                (_now(), error_msg, job_id),
            )
            conn.execute(
                "UPDATE tracks SET status='failed', error_msg=? WHERE id=?",
                (error_msg, track_id),
            )
        if "bot-check failed" in error_msg:
            hostname = os.environ.get("SERVER_HOSTNAME", "")
            admin_url = f"https://{hostname}/admin" if hostname else "(admin panel)"
            send_alert(
                subject="[Family Radio] YouTube bot-check failed",
                body=(
                    f"A YouTube download failed because YouTube is requiring sign-in verification.\n\n"
                    f"Submitted by: {submitter}\n"
                    f"URL: {source_url}\n\n"
                    f"Fix: upload fresh cookies at the admin panel:\n"
                    f"{admin_url}\n\n"
                    f"Error: {error_msg}"
                ),
            )


def reset_stuck_jobs():
    """Reset any jobs left in 'processing' state by a previous crash/restart.

    Called from the main thread during startup, before the worker thread starts,
    so it is guaranteed to run against the correct DB and see committed state.
    """
    with db() as conn:
        stuck = conn.execute("SELECT id, track_id FROM jobs WHERE status='processing'").fetchall()
        for row in stuck:
            conn.execute(
                "UPDATE jobs SET status='pending', started_at=NULL WHERE id=?",
                (row["id"],),
            )
            conn.execute(
                "UPDATE tracks SET status='pending' WHERE id=?",
                (row["track_id"],),
            )
    if stuck:
        logger.warning(f"Reset {len(stuck)} stuck processing job(s) to pending on startup")
    else:
        logger.info("No stuck processing jobs found on startup")


def _worker_loop():
    """Background worker: poll for pending jobs and process them."""
    logger.info("Worker thread started")
    while not _stop_event.is_set():
        try:
            with db() as conn:
                row = conn.execute(
                    "SELECT id, track_id FROM jobs WHERE status='pending' ORDER BY created_at ASC LIMIT 1"
                ).fetchone()

            if row:
                _process_job(row["id"], row["track_id"])
            else:
                # No pending jobs; wait before polling again
                _stop_event.wait(timeout=5.0)

        except Exception as e:
            logger.error(f"Worker loop error: {e}", exc_info=True)
            _stop_event.wait(timeout=10.0)

    logger.info("Worker thread stopped")


def start_worker():
    global _worker_thread
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="radio-worker")
    _worker_thread.start()


def stop_worker():
    _stop_event.set()
    if _worker_thread:
        _worker_thread.join(timeout=30)
