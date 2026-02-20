import logging
import os
import threading
import time
from datetime import datetime, timezone

from database import db, get_connection
from downloader import download_youtube, download_spotify, convert_to_standard_mp3
from audio import extract_features
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
                "SELECT source_type, source_url, submitter FROM tracks WHERE id=?",
                (track_id,),
            ).fetchone()

        if not row:
            raise RuntimeError(f"Track {track_id} not found")

        source_type = row["source_type"]
        source_url = row["source_url"]
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
                t = conn.execute(
                    "SELECT title, artist FROM tracks WHERE id=?", (track_id,)
                ).fetchone()
            title = t["title"]
            artist = t["artist"]

        elif source_type == "youtube":
            title, artist, raw_path = download_youtube(source_url, track_id)

        elif source_type == "spotify":
            title, artist, raw_path = download_spotify(source_url, track_id)

        else:
            raise RuntimeError(f"Unknown source_type: {source_type}")

        # Convert to standard MP3 with track_id embedded as comment tag
        final_path = convert_to_standard_mp3(raw_path, track_id, track_id)

        # Extract audio features
        features = extract_features(final_path)

        # Update feature normalization bounds
        update_feature_bounds(features)

        # Get duration via ffprobe
        import subprocess
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", final_path,
            ],
            capture_output=True, text=True,
        )
        duration_s = None
        if result.returncode == 0:
            import json
            info = json.load(result.stdout if hasattr(result.stdout, "read") else __import__("io").StringIO(result.stdout))
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
                    title, artist, final_path, duration_s,
                    features.tempo_bpm, features.rms_energy,
                    features.spectral_centroid, features.zero_crossing_rate,
                    _now(), track_id,
                ),
            )
            conn.execute(
                "UPDATE jobs SET status='done', finished_at=? WHERE id=?",
                (_now(), job_id),
            )

        logger.info(f"Job {job_id} completed: track {track_id} ready at {final_path}")

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
