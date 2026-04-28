import logging
import math
import os
import random
from datetime import UTC, datetime, timedelta

from audio import AudioFeatures, euclidean_distance, normalize_features
from database import db, get_config, set_config
from dj import DJ_SENTINEL

logger = logging.getLogger(__name__)

COOLDOWN_THRESHOLD_S = 3600  # activate when total library runtime exceeds 60 min
COOLDOWN_WINDOW_S = 3600  # don't replay a track within 60 min


def _total_ready_runtime_s() -> float:
    with db() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(duration_s), 0) FROM tracks WHERE status='ready' AND duration_s IS NOT NULL"
        ).fetchone()[0]


def _cooldown_is_active() -> bool:
    return _total_ready_runtime_s() >= COOLDOWN_THRESHOLD_S


def _pick_global_fallback() -> dict | None:
    """Pick the globally least-recently-played ready track, ignoring cooldown."""
    last_returned_id = get_config("last_returned_track_id") or ""
    with db() as conn:
        last_played = conn.execute("SELECT track_id FROM play_log ORDER BY played_at DESC LIMIT 1").fetchone()
        last_played_id = last_played["track_id"] if last_played else ""

        row = conn.execute(
            """
            SELECT t.id, t.title, t.artist, t.submitter, t.file_path FROM tracks t
            WHERE t.status='ready' AND t.id != ? AND t.id != ?
            ORDER BY COALESCE(
                (SELECT MAX(pl.played_at) FROM play_log pl WHERE pl.track_id=t.id), ''
            ) ASC, t.submitted_at ASC
            LIMIT 1
        """,
            (last_played_id, last_returned_id),
        ).fetchone()

        if not row:  # truly last resort — allow any ready track
            row = conn.execute("""
                SELECT t.id, t.title, t.artist, t.submitter, t.file_path FROM tracks t
                WHERE t.status='ready'
                ORDER BY COALESCE(
                    (SELECT MAX(pl.played_at) FROM play_log pl WHERE pl.track_id=t.id), ''
                ) ASC, t.submitted_at ASC
                LIMIT 1
            """).fetchone()

    if not row:
        return None
    set_config("last_returned_track_id", row["id"])
    logger.info("Global cooldown fallback: returning least-recently-played track")
    return {
        "id": row["id"],
        "title": row["title"],
        "artist": row["artist"],
        "submitter": row["submitter"],
        "file_path": row["file_path"],
    }


def _advance_for_dj():
    """Advance rotation state when the DJ-reserved track is consumed (after an interlude).

    Mirrors the rotation-advance part of _advance() without touching the DJ cycle counter
    (caller resets dj_submitters_since_last_interlude directly).
    """
    with db() as conn:
        rows = conn.execute("SELECT DISTINCT submitter FROM tracks WHERE status='ready' ORDER BY submitter").fetchall()
        submitters = [r["submitter"] for r in rows]

    if not submitters:
        return

    idx = int(get_config("rotation_current_submitter_idx")) % len(submitters)
    next_idx = (idx + 1) % len(submitters)
    set_config("rotation_current_submitter_idx", str(next_idx))

    with db() as conn:
        latest = conn.execute("SELECT COALESCE(MAX(id), 0) as n FROM play_log").fetchone()["n"]
    set_config("rotation_block_start_log_id", str(latest))


def is_penultimate_submitter_track(submitter: str, track_id: str) -> bool:
    """Return True if generation should be triggered now to allow the interlude to land
    cleanly between this submitter's block and the next.

    Liquidsoap prefetches one track ahead: next-track fires when a track STARTS, not when
    it ends. So for the interlude to be returned before Molly1, dj_pending_file must be set
    before the next-track call that fires when Joe's last track starts. That means generation
    must be triggered one track earlier — at Joe's penultimate track.

    Returns True at the penultimate position, defined as:
      - played_this_block >= max(1, tracks_per_block - 1)  — second-to-last in a full block
      - OR remaining eligible tracks <= 1                   — abbreviated block: only one left
        (remaining == 1 means this IS second-to-last; remaining == 0 is a 1-track fallback)
    """
    tracks_per_block = int(get_config("rotation_tracks_per_block"))
    block_start_log_id = int(get_config("rotation_block_start_log_id") or "0")

    with db() as conn:
        played_this_block = conn.execute(
            """
            SELECT COUNT(*) FROM play_log pl
            JOIN tracks t ON pl.track_id = t.id
            WHERE t.submitter = ? AND pl.id > ?
            """,
            (submitter, block_start_log_id),
        ).fetchone()[0]

    if played_this_block >= max(1, tracks_per_block - 1):
        logger.info(
            "DJ: penultimate check for %s — played_this_block=%d/%d → trigger",
            submitter,
            played_this_block,
            tracks_per_block,
        )
        return True

    # Block isn't full yet — check remaining eligible tracks.
    last_returned_id = get_config("last_returned_track_id") or ""
    cooldown_active = _cooldown_is_active()

    with db() as conn:
        if cooldown_active:
            cutoff = (datetime.now(UTC) - timedelta(seconds=COOLDOWN_WINDOW_S)).isoformat()
            remaining = conn.execute(
                """
                SELECT COUNT(*) FROM tracks
                WHERE submitter = ? AND status = 'ready'
                  AND id != ?
                  AND id != ?
                  AND id NOT IN (SELECT track_id FROM play_log WHERE played_at > ?)
                """,
                (submitter, track_id, last_returned_id, cutoff),
            ).fetchone()[0]
        else:
            remaining = conn.execute(
                """
                SELECT COUNT(*) FROM tracks
                WHERE submitter = ? AND status = 'ready'
                  AND id != ?
                  AND id != ?
                """,
                (submitter, track_id, last_returned_id),
            ).fetchone()[0]

    # remaining == 1: one track left after this — this is the penultimate, trigger generation
    # remaining == 0: 1-track block — no penultimate exists; treat like a 0-track submitter
    #                 (dj_generation_needed persists into the next real submitter's block)
    if remaining == 1:
        logger.info(
            "DJ: penultimate check for %s — played_this_block=%d, remaining_eligible=1 → trigger",
            submitter,
            played_this_block,
        )
        return True
    return False


def peek_next_submitter_track() -> dict | None:
    """Read-only: return what the scheduler would pick as the first track of the next submitter block.

    Used by the DJ trigger to pre-select the post-interlude track before generation starts.
    Does not modify any config keys.

    Mirrors _pick_rotation_track's skip logic: if the immediately next submitter has no
    eligible tracks (e.g. all on cooldown), advances further until a submitter with tracks
    is found, rather than returning None and silently suppressing DJ generation.
    """
    with db() as conn:
        rows = conn.execute("SELECT DISTINCT submitter FROM tracks WHERE status='ready' ORDER BY submitter").fetchall()
        submitters = [r["submitter"] for r in rows]

    if not submitters:
        return None

    idx = int(get_config("rotation_current_submitter_idx")) % len(submitters)
    last_returned_id = get_config("last_returned_track_id") or ""
    cooldown_active = _cooldown_is_active()

    with db() as conn:
        last_played = conn.execute("SELECT track_id FROM play_log ORDER BY played_at DESC LIMIT 1").fetchone()
        last_played_id = last_played["track_id"] if last_played else ""

    for offset in range(1, len(submitters) + 1):
        next_submitter = submitters[(idx + offset) % len(submitters)]

        with db() as conn:
            if cooldown_active:
                cutoff = (datetime.now(UTC) - timedelta(seconds=COOLDOWN_WINDOW_S)).isoformat()
                candidate_rows = conn.execute(
                    """
                    SELECT t.id, t.title, t.artist, t.submitter, t.file_path,
                           COUNT(pl.id) as play_count
                    FROM tracks t
                    LEFT JOIN play_log pl ON pl.track_id = t.id
                    WHERE t.submitter=? AND t.status='ready'
                      AND t.id != ?
                      AND t.id != ?
                      AND t.id NOT IN (SELECT track_id FROM play_log WHERE played_at > ?)
                    GROUP BY t.id
                    """,
                    (next_submitter, last_played_id, last_returned_id, cutoff),
                ).fetchall()
            else:
                candidate_rows = conn.execute(
                    """
                    SELECT t.id, t.title, t.artist, t.submitter, t.file_path,
                           COUNT(pl.id) as play_count
                    FROM tracks t
                    LEFT JOIN play_log pl ON pl.track_id = t.id
                    WHERE t.submitter=? AND t.status='ready'
                      AND t.id != ?
                      AND t.id != ?
                    GROUP BY t.id
                    """,
                    (next_submitter, last_played_id, last_returned_id),
                ).fetchall()

        if not candidate_rows:
            continue

        new_tracks = [r for r in candidate_rows if r["play_count"] == 0]
        existing_tracks = [r for r in candidate_rows if r["play_count"] > 0]

        if new_tracks:
            row = random.choice(new_tracks)
        else:
            weights = [1.0 / math.sqrt(r["play_count"] + 1) for r in existing_tracks]
            row = random.choices(existing_tracks, weights=weights, k=1)[0]

        return {
            "id": row["id"],
            "title": row["title"],
            "artist": row["artist"],
            "submitter": row["submitter"],
            "file_path": row["file_path"],
        }

    return None


def get_next_track() -> dict | None:
    """
    Main scheduling entry point. Returns a dict with id/title/artist/file_path
    for the next track to play, or None if nothing is ready.

    Before normal scheduling, checks for a pending DJ interlude (dj_pending_file).
    When an interlude is returned, the rotation is immediately advanced to the next
    submitter so normal scheduling picks up correctly after the interlude plays.
    """
    if get_config("dj_enabled") == "true":
        # Step 1: interlude just played — return the reserved post-interlude track.
        # This is the only code path that consumes dj_reserved_track_id, so a skip
        # cannot prematurely advance past the reserved track.
        if get_config("dj_interlude_just_played") == "true":
            set_config("dj_interlude_just_played", "false")
            reserved_id = get_config("dj_reserved_track_id")
            set_config("dj_reserved_track_id", "")
            if reserved_id:
                with db() as conn:
                    row = conn.execute(
                        "SELECT id, title, artist, submitter, file_path FROM tracks WHERE id=? AND status='ready'",
                        (reserved_id,),
                    ).fetchone()
                if row:
                    logger.info(
                        "DJ: consuming reserved track %s ('%s' by %s) after interlude",
                        reserved_id,
                        row["title"],
                        row["artist"],
                    )
                    set_config("last_returned_track_id", row["id"])
                    return {
                        "id": row["id"],
                        "title": row["title"],
                        "artist": row["artist"],
                        "submitter": row["submitter"],
                        "file_path": row["file_path"],
                    }
                logger.warning(
                    "DJ: reserved track %s not found in DB — falling through to rotation",
                    reserved_id,
                )

        # Step 2: interlude is ready — dispatch it.
        pending_file = get_config("dj_pending_file")
        if pending_file and os.path.exists(pending_file):
            reserved_id = get_config("dj_reserved_track_id")
            set_config("dj_playing_file", pending_file)
            set_config("dj_pending_file", "")
            set_config("dj_submitters_since_last_interlude", "0")
            set_config("last_returned_track_id", "")
            set_config("dj_interlude_just_played", "true")
            _advance_for_dj()
            logger.info(
                "DJ: dispatching interlude %s (reserved next track: %s)",
                pending_file,
                reserved_id or "none",
            )
            return {
                "id": DJ_SENTINEL,
                "title": "Family Radio",
                "artist": "The AI DJ",
                "submitter": "",
                "file_path": pending_file,
            }

    mode = get_config("programming_mode")
    logger.info(f"Scheduling mode: {mode}")

    if mode == "mood":
        return _pick_mood_track()
    else:
        return _pick_rotation_track()


def _pick_rotation_track(depth: int = 0) -> dict | None:
    """Round-robin through submitters, N tracks per block."""
    with db() as conn:
        rows = conn.execute("SELECT DISTINCT submitter FROM tracks WHERE status='ready' ORDER BY submitter").fetchall()
        submitters = [r["submitter"] for r in rows]

    if not submitters:
        return None

    if depth >= len(submitters):
        logger.info("All submitters on cooldown; using global fallback")
        return _pick_global_fallback()

    idx = int(get_config("rotation_current_submitter_idx")) % len(submitters)
    tracks_per_block = int(get_config("rotation_tracks_per_block"))
    block_start_log_id = int(get_config("rotation_block_start_log_id") or "0")
    last_returned_id = get_config("last_returned_track_id") or ""
    current_submitter = submitters[idx]

    # Count songs from this submitter that have actually played since the block started.
    # Add 1 if last_returned_id is also from this submitter — it may not be in
    # play_log yet due to the prefetch/track-started race condition.
    with db() as conn:
        played_this_block = conn.execute(
            """
            SELECT COUNT(*) as n FROM play_log pl
            JOIN tracks t ON pl.track_id = t.id
            WHERE t.submitter = ? AND pl.id > ?
            """,
            (current_submitter, block_start_log_id),
        ).fetchone()["n"]

        if last_returned_id:
            lr = conn.execute("SELECT submitter FROM tracks WHERE id = ?", (last_returned_id,)).fetchone()
            if lr and lr["submitter"] == current_submitter:
                played_this_block += 1

    def _advance():
        next_idx = (idx + 1) % len(submitters)
        set_config("rotation_current_submitter_idx", str(next_idx))
        with db() as conn:
            latest = conn.execute("SELECT COALESCE(MAX(id), 0) as n FROM play_log").fetchone()["n"]
        set_config("rotation_block_start_log_id", str(latest))

        if get_config("dj_enabled") == "true":
            since = int(get_config("dj_submitters_since_last_interlude") or "0") + 1
            set_config("dj_submitters_since_last_interlude", str(since))
            threshold = int(get_config("dj_submitters_per_interlude") or "2")
            logger.info("DJ: submitters_since_last_interlude=%d (threshold=%d)", since, threshold)
            if since >= threshold - 1 and not get_config("dj_pending_file"):
                set_config("dj_generation_needed", "true")
                logger.info("DJ: generation needed — entering last block before interlude")

    if played_this_block >= tracks_per_block:
        logger.info(f"Rotation: block complete for {current_submitter}, advancing")
        _advance()
        return _pick_rotation_track(0)

    # Pick the next track for this submitter:
    #   - Tracks with 0 plays are guaranteed (pick randomly among them).
    #   - Tracks with >0 plays are chosen by weighted random: weight = 1/sqrt(play_count + 1),
    #     so less-played tracks are more likely but well-played tracks still have a real chance.
    # When cooldown is active, exclude tracks played within the cooldown window.
    cooldown_active = _cooldown_is_active()
    with db() as conn:
        last_played = conn.execute("SELECT track_id FROM play_log ORDER BY played_at DESC LIMIT 1").fetchone()
        last_played_id = last_played["track_id"] if last_played else ""

        if cooldown_active:
            cutoff = (datetime.now(UTC) - timedelta(seconds=COOLDOWN_WINDOW_S)).isoformat()
            rows = conn.execute(
                """
                SELECT t.id, t.title, t.artist, t.file_path,
                       COUNT(pl.id) as play_count
                FROM tracks t
                LEFT JOIN play_log pl ON pl.track_id = t.id
                WHERE t.submitter=? AND t.status='ready'
                  AND t.id != ?
                  AND t.id != ?
                  AND t.id NOT IN (SELECT track_id FROM play_log WHERE played_at > ?)
                GROUP BY t.id
                """,
                (current_submitter, last_played_id, last_returned_id, cutoff),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT t.id, t.title, t.artist, t.file_path,
                       COUNT(pl.id) as play_count
                FROM tracks t
                LEFT JOIN play_log pl ON pl.track_id = t.id
                WHERE t.submitter=? AND t.status='ready'
                  AND t.id != ?
                  AND t.id != ?
                GROUP BY t.id
                """,
                (current_submitter, last_played_id, last_returned_id),
            ).fetchall()

    if not rows:
        logger.info(f"Rotation: no eligible track for {current_submitter} (depth={depth}), advancing")
        _advance()
        return _pick_rotation_track(depth + 1)

    new_tracks = [r for r in rows if r["play_count"] == 0]
    existing_tracks = [r for r in rows if r["play_count"] > 0]

    if new_tracks:
        # Guarantee: any unplayed track gets priority; pick randomly among them.
        row = random.choice(new_tracks)
        logger.info(f"Rotation: guaranteeing unplayed track for {current_submitter}")
    else:
        # Weighted random: weight = 1/sqrt(play_count + 1).
        weights = [1.0 / math.sqrt(r["play_count"] + 1) for r in existing_tracks]
        row = random.choices(existing_tracks, weights=weights, k=1)[0]

    set_config("last_returned_track_id", row["id"])
    logger.info(f"Rotation: submitter={current_submitter} played_this_block={played_this_block}/{tracks_per_block}")
    return {
        "id": row["id"],
        "title": row["title"],
        "artist": row["artist"],
        "submitter": current_submitter,
        "file_path": row["file_path"],
    }


def _pick_mood_track() -> dict | None:
    """Pick track with minimum Euclidean distance from the last played track."""
    # Get the last played track's features
    with db() as conn:
        last_row = conn.execute(
            """
            SELECT t.tempo_bpm, t.rms_energy, t.spectral_centroid, t.zero_crossing_rate
            FROM play_log pl
            JOIN tracks t ON pl.track_id = t.id
            WHERE t.tempo_bpm IS NOT NULL
            ORDER BY pl.played_at DESC
            LIMIT 1
            """
        ).fetchone()

    if not last_row:
        # No play history; fall back to rotation
        logger.info("No play history for mood matching, falling back to rotation")
        return _pick_rotation_track()

    last_features = AudioFeatures(
        tempo_bpm=last_row["tempo_bpm"],
        rms_energy=last_row["rms_energy"],
        spectral_centroid=last_row["spectral_centroid"],
        zero_crossing_rate=last_row["zero_crossing_rate"],
    )

    # Load normalization bounds from config
    mins = {
        "tempo_bpm": float(get_config("feature_min_tempo_bpm")),
        "rms_energy": float(get_config("feature_min_rms_energy")),
        "spectral_centroid": float(get_config("feature_min_spectral_centroid")),
        "zero_crossing_rate": float(get_config("feature_min_zero_crossing_rate")),
    }
    maxs = {
        "tempo_bpm": float(get_config("feature_max_tempo_bpm")),
        "rms_energy": float(get_config("feature_max_rms_energy")),
        "spectral_centroid": float(get_config("feature_max_spectral_centroid")),
        "zero_crossing_rate": float(get_config("feature_max_zero_crossing_rate")),
    }

    last_vec = normalize_features(last_features, mins, maxs)

    # Compute how many distinct recently-played tracks to exclude.
    # Scales with library size so small libraries always have at least one candidate.
    with db() as conn:
        library_size = conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE status='ready' AND tempo_bpm IS NOT NULL"
        ).fetchone()[0]

    exclusion_count = min(max(library_size - 1, 0), 3)

    # Get all ready tracks with features, excluding the most recently played distinct tracks
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT t.id, t.title, t.artist, t.submitter, t.file_path, t.tempo_bpm, t.rms_energy,
                   t.spectral_centroid, t.zero_crossing_rate
            FROM tracks t
            WHERE t.status='ready' AND t.tempo_bpm IS NOT NULL
              AND t.id NOT IN (
                  SELECT track_id FROM play_log
                  GROUP BY track_id
                  ORDER BY MAX(played_at) DESC
                  LIMIT {exclusion_count}
              )
            """,  # noqa: S608 — exclusion_count is always an int derived from library_size
        ).fetchall()

    if not rows:
        # No candidates with features; try rotation
        return _pick_rotation_track()

    best_id = None
    best_title = None
    best_artist = None
    best_submitter = None
    best_path = None
    best_dist = float("inf")

    for row in rows:
        features = AudioFeatures(
            tempo_bpm=row["tempo_bpm"],
            rms_energy=row["rms_energy"],
            spectral_centroid=row["spectral_centroid"],
            zero_crossing_rate=row["zero_crossing_rate"],
        )
        vec = normalize_features(features, mins, maxs)
        dist = euclidean_distance(last_vec, vec)
        if dist < best_dist:
            best_dist = dist
            best_id = row["id"]
            best_title = row["title"]
            best_artist = row["artist"]
            best_submitter = row["submitter"]
            best_path = row["file_path"]

    if not best_id:
        return None
    set_config("last_returned_track_id", best_id)
    logger.info(f"Mood: picked track with distance={best_dist:.4f}")
    return {
        "id": best_id,
        "title": best_title,
        "artist": best_artist,
        "submitter": best_submitter,
        "file_path": best_path,
    }


def update_feature_bounds(features: AudioFeatures):
    """Update running min/max for each audio feature in config."""
    fields = {
        "tempo_bpm": features.tempo_bpm,
        "rms_energy": features.rms_energy,
        "spectral_centroid": features.spectral_centroid,
        "zero_crossing_rate": features.zero_crossing_rate,
    }
    for name, val in fields.items():
        current_min = float(get_config(f"feature_min_{name}"))
        current_max = float(get_config(f"feature_max_{name}"))
        # On first real value (still at defaults 0/1), use the actual value as seed
        # but keep expanding from there
        new_min = min(current_min, val)
        new_max = max(current_max, val)
        if new_min != current_min:
            set_config(f"feature_min_{name}", str(new_min))
        if new_max != current_max:
            set_config(f"feature_max_{name}", str(new_max))
