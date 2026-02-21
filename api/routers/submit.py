import os
import uuid
import shutil
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse

from database import db

logger = logging.getLogger(__name__)
router = APIRouter()

MEDIA_DIR = os.environ.get("MEDIA_DIR", "/media")
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus"}
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _create_track_and_job(
    conn,
    track_id: str,
    title: str,
    artist: str,
    submitter: str,
    source_type: str,
    source_url: str | None = None,
):
    conn.execute(
        """
        INSERT INTO tracks (id, title, artist, submitter, source_type, source_url,
                            status, submitted_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (track_id, title, artist, submitter, source_type, source_url, _now()),
    )
    conn.execute(
        "INSERT INTO jobs (track_id, status, created_at) VALUES (?, 'pending', ?)",
        (track_id, _now()),
    )


@router.post("/submit")
async def submit_track(
    submitter: str = Form(...),
    youtube_url: str = Form(None),
    title: str = Form(None),
    artist: str = Form(None),
    file: UploadFile = File(None),
):
    if not submitter or not submitter.strip():
        raise HTTPException(400, "submitter is required")

    submitter = submitter.strip()[:50]

    # Determine source
    has_file = file is not None and file.filename
    has_youtube = youtube_url and youtube_url.strip()

    if sum([has_file, bool(has_youtube)]) != 1:
        raise HTTPException(400, "Provide exactly one of: file or youtube_url")

    track_id = str(uuid.uuid4())

    if has_file:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(400, f"Unsupported file type: {ext}")

        raw_dir = os.path.join(MEDIA_DIR, "raw")
        os.makedirs(raw_dir, exist_ok=True)
        dest = os.path.join(raw_dir, f"{track_id}{ext}")

        size = 0
        with open(dest, "wb") as f_out:
            while chunk := await file.read(65536):
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    os.unlink(dest)
                    raise HTTPException(413, "File too large (max 200MB)")
                f_out.write(chunk)

        track_title = (title or os.path.splitext(file.filename)[0])[:200]
        track_artist = (artist or submitter)[:200]

        with db() as conn:
            _create_track_and_job(
                conn, track_id, track_title, track_artist,
                submitter, "upload",
            )

        logger.info(f"Upload submission: track_id={track_id} file={dest}")
        return JSONResponse({"track_id": track_id, "status": "pending"})

    elif has_youtube:
        url = youtube_url.strip()
        if "youtu" not in url:
            raise HTTPException(400, "Does not look like a YouTube URL")

        with db() as conn:
            _create_track_and_job(
                conn, track_id,
                title or "Pending...", artist or "Pending...",
                submitter, "youtube", url,
            )

        logger.info(f"YouTube submission: track_id={track_id} url={url}")
        return JSONResponse({"track_id": track_id, "status": "pending"})
