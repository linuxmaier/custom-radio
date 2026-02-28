import difflib
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from database import db
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()

MEDIA_DIR = os.environ.get("MEDIA_DIR", "/media")
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus"}
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"}
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB
MAX_PENDING_PER_SUBMITTER = 5


DUPLICATE_SIMILARITY_THRESHOLD = 0.75
DUPLICATE_MAX_RESULTS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        return parsed.path.lstrip("/").split("?")[0] or None
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        qs = parse_qs(parsed.query)
        return qs.get("v", [None])[0]
    return None


def _normalize_title(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[\(\[][^\)\]]*[\)\]]", "", text)
    return " ".join(text.split())


def _create_track_and_job(
    conn,
    track_id: str,
    title: str,
    artist: str,
    submitter: str,
    source_type: str,
    source_url: str | None = None,
    comment: str | None = None,
    youtube_video_id: str | None = None,
):
    conn.execute(
        """
        INSERT INTO tracks (id, title, artist, submitter, source_type, source_url,
                            status, submitted_at, comment, youtube_video_id)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        (track_id, title, artist, submitter, source_type, source_url, _now(), comment, youtube_video_id),
    )
    conn.execute(
        "INSERT INTO jobs (track_id, status, created_at) VALUES (?, 'pending', ?)",
        (track_id, _now()),
    )


@router.get("/check-duplicate")
def check_duplicate(
    video_id: str | None = None,
    title: str | None = None,
    artist: str | None = None,
):
    matches = []

    with db() as conn:
        if video_id:
            row = conn.execute(
                "SELECT id, title, artist, submitter FROM tracks WHERE youtube_video_id = ? AND status != 'failed'",
                (video_id,),
            ).fetchone()
            if row:
                matches.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "artist": row["artist"],
                        "submitter": row["submitter"],
                        "similarity": 1.0,
                        "match_type": "video_id",
                    }
                )

        if not matches and title:
            norm_query_title = _normalize_title(title)
            norm_query_artist = _normalize_title(artist) if artist else None
            rows = conn.execute(
                "SELECT id, title, artist, submitter FROM tracks WHERE status != 'failed' AND title != ''"
            ).fetchall()
            candidates = []
            for row in rows:
                norm_title = _normalize_title(row["title"])
                title_sim = difflib.SequenceMatcher(None, norm_query_title, norm_title).ratio()
                if norm_query_artist and row["artist"]:
                    artist_sim = difflib.SequenceMatcher(
                        None, norm_query_artist, _normalize_title(row["artist"])
                    ).ratio()
                    sim = title_sim * 0.8 + artist_sim * 0.2
                else:
                    sim = title_sim
                if sim >= DUPLICATE_SIMILARITY_THRESHOLD:
                    candidates.append(
                        {
                            "id": row["id"],
                            "title": row["title"],
                            "artist": row["artist"],
                            "submitter": row["submitter"],
                            "similarity": round(sim, 3),
                            "match_type": "fuzzy",
                        }
                    )
            candidates.sort(key=lambda x: x["similarity"], reverse=True)
            matches = candidates[:DUPLICATE_MAX_RESULTS]

    return {"matches": matches}


@router.post("/submit")
async def submit_track(
    submitter: str = Form(...),
    youtube_url: str = Form(None),
    title: str = Form(None),
    artist: str = Form(None),
    file: UploadFile = File(None),
    comment: str | None = Form(None),
):
    if not submitter or not submitter.strip():
        raise HTTPException(400, "submitter is required")

    submitter = submitter.strip()[:50]
    comment = comment.strip()[:280] if comment and comment.strip() else None

    with db() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE submitter=? AND status IN ('pending', 'processing')",
            (submitter,),
        ).fetchone()[0]
    if pending >= MAX_PENDING_PER_SUBMITTER:
        raise HTTPException(
            429,
            f"You already have {pending} songs being processed. Please wait for them to finish before adding more.",
        )

    # Determine source
    has_file = bool(file is not None and file.filename)
    has_youtube = youtube_url and youtube_url.strip()

    if sum([has_file, bool(has_youtube)]) != 1:
        raise HTTPException(400, "Provide exactly one of: file or youtube_url")

    track_id = str(uuid.uuid4())

    if has_file:
        assert file is not None and file.filename is not None
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
                conn,
                track_id,
                track_title,
                track_artist,
                submitter,
                "upload",
                comment=comment,
            )

        logger.info(f"Upload submission: track_id={track_id} file={dest}")
        return JSONResponse({"track_id": track_id, "status": "pending"})

    elif has_youtube:
        url = youtube_url.strip()
        if urlparse(url).netloc.lower() not in YOUTUBE_HOSTS:
            raise HTTPException(400, "Only YouTube URLs are supported (youtube.com, youtu.be)")

        video_id = _extract_youtube_video_id(url)

        with db() as conn:
            _create_track_and_job(
                conn,
                track_id,
                title or "Pending...",
                artist or "Pending...",
                submitter,
                "youtube",
                url,
                comment=comment,
                youtube_video_id=video_id,
            )

        logger.info(f"YouTube submission: track_id={track_id} url={url}")
        return JSONResponse({"track_id": track_id, "status": "pending"})
