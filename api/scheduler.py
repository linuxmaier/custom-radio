import logging
import math
import random
from datetime import datetime, timedelta, timezone

from audio import AudioFeatures, euclidean_distance, normalize_features
from database import db, get_config, set_config

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
            SELECT t.id, t.title, t.artist, t.file_path FROM tracks t
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
                SELECT t.id, t.title, t.artist, t.file_path FROM tracks t
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
        "file_path": row["file_path"],
    }


def get_next_track() -> dict | None:
    """
    Main scheduling entry point. Returns a dict with id/title/artist/file_path
    for the next track to play, or None if nothing is ready.
    """
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
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=COOLDOWN_WINDOW_S)).isoformat()
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
            SELECT t.id, t.title, t.artist, t.file_path, t.tempo_bpm, t.rms_energy,
                   t.spectral_centroid, t.zero_crossing_rate
            FROM tracks t
            WHERE t.status='ready' AND t.tempo_bpm IS NOT NULL
              AND t.id NOT IN (
                  SELECT track_id FROM play_log
                  GROUP BY track_id
                  ORDER BY MAX(played_at) DESC
                  LIMIT {exclusion_count}
              )
            """
        ).fetchall()

    if not rows:
        # No candidates with features; try rotation
        return _pick_rotation_track()

    best_id = None
    best_title = None
    best_artist = None
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
            best_path = row["file_path"]

    if not best_id:
        return None
    set_config("last_returned_track_id", best_id)
    logger.info(f"Mood: picked track with distance={best_dist:.4f}")
    return {
        "id": best_id,
        "title": best_title,
        "artist": best_artist,
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
