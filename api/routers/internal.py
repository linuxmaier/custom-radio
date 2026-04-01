import logging
from datetime import UTC, datetime, timedelta

from database import db, get_config, set_config
from dj import DJ_SENTINEL, trigger_generation
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from scheduler import get_next_track, is_penultimate_submitter_track, peek_next_submitter_track

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
    # DJ interludes are not in the tracks table — handle them cleanly without a warning.
    if track_id == DJ_SENTINEL:
        logger.info("DJ interlude started playing")
        return {"ok": True}

    with db() as conn:
        row = conn.execute("SELECT id, duration_s, submitter FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not row:
            logger.warning(f"track-started called with unknown track_id: {track_id}")
            return {"ok": False, "error": "unknown track"}

        conn.execute(
            "INSERT INTO play_log (track_id, played_at) VALUES (?, ?)",
            (track_id, _now()),
        )
        duration_s = row["duration_s"]
        submitter = row["submitter"]

    logger.info(f"track-started logged: {track_id}")

    # DJ interlude generation trigger. dj_generation_needed is set by _advance() when
    # dj_submitters_since_last_interlude reaches threshold-1, meaning we've entered the
    # last submitter block before an interlude is due. We wait until the *penultimate*
    # track of that block to trigger generation. This ensures dj_pending_file is set
    # before the last track starts — so the simultaneous next-track call during the last
    # track's start returns the interlude rather than the first track of the next block.
    #
    # is_penultimate_submitter_track() returns True at the second-to-last position, or
    # earlier for abbreviated blocks (only 1 remaining eligible track = this is penultimate).
    # For 0-track submitters, dj_generation_needed persists into the next submitter's
    # block and fires at their penultimate track — the empty submitter is invisible.
    if (
        get_config("dj_enabled") == "true"
        and get_config("dj_generation_needed") == "true"
        and not get_config("dj_pending_file")
        and is_penultimate_submitter_track(submitter, track_id)
    ):
        set_config("dj_generation_needed", "false")
        peeked = peek_next_submitter_track()
        if peeked:
            set_config("dj_reserved_track_id", peeked["id"])
            last_track_id = get_config("last_returned_track_id")
            with db() as conn:
                recent_rows = conn.execute(
                    """
                    SELECT t.title, t.artist, t.submitter
                    FROM play_log pl
                    JOIN tracks t ON pl.track_id = t.id
                    ORDER BY pl.played_at DESC
                    LIMIT 3
                    """,
                ).fetchall()
                last_track_row = (
                    conn.execute(
                        "SELECT title, artist, submitter FROM tracks WHERE id=?",
                        (last_track_id,),
                    ).fetchone()
                    if last_track_id
                    else None
                )
            recent_tracks = [
                {"title": r["title"], "artist": r["artist"], "submitter": r["submitter"]} for r in reversed(recent_rows)
            ]
            if last_track_row:
                recent_tracks.append(
                    {
                        "title": last_track_row["title"],
                        "artist": last_track_row["artist"],
                        "submitter": last_track_row["submitter"],
                    }
                )
                recent_tracks = recent_tracks[-3:]
            last_track_duration_s = 0.0
            if last_track_id:
                with db() as conn:
                    last_row = conn.execute("SELECT duration_s FROM tracks WHERE id=?", (last_track_id,)).fetchone()
                if last_row and last_row["duration_s"]:
                    last_track_duration_s = float(last_row["duration_s"])
            estimated_play_time = datetime.now(UTC) + timedelta(
                seconds=float(duration_s or 180) + last_track_duration_s
            )
            # Look up pronouns for all submitters involved in this interlude.
            unique_submitters = {t["submitter"] for t in recent_tracks} | {peeked["submitter"]}
            with db() as conn:
                placeholders = ",".join("?" * len(unique_submitters))
                pronoun_rows = conn.execute(
                    f"SELECT name, pronouns FROM users WHERE name IN ({placeholders}) AND pronouns IS NOT NULL",  # noqa: S608
                    list(unique_submitters),
                ).fetchall()
            submitter_pronouns = {r["name"]: r["pronouns"] for r in pronoun_rows}
            logger.info(
                "DJ: triggering generation at penultimate track (submitter=%s); reserved next: %s ('%s' by %s)",
                submitter,
                peeked["id"],
                peeked["title"],
                peeked["artist"],
            )
            logger.info(
                "DJ: recent_tracks: %s",
                [(t["title"][:35], t["submitter"]) for t in recent_tracks],
            )
            trigger_generation(recent_tracks, peeked, estimated_play_time, submitter_pronouns)

    return {"ok": True}
