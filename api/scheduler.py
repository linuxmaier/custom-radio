import logging
import numpy as np

from database import db, get_config, set_config
from audio import normalize_features, euclidean_distance, AudioFeatures

logger = logging.getLogger(__name__)


def get_next_track_path() -> str:
    """
    Main scheduling entry point. Returns the file path of the next track to play,
    or empty string if nothing is ready.
    """
    mode = get_config("programming_mode")
    logger.info(f"Scheduling mode: {mode}")

    if mode == "mood":
        path = _pick_mood_track()
    else:
        path = _pick_rotation_track()

    return path or ""


def _pick_rotation_track() -> str:
    """Round-robin through submitters, N tracks per block."""
    with db() as conn:
        # Get all distinct submitters who have ready tracks
        rows = conn.execute(
            "SELECT DISTINCT submitter FROM tracks WHERE status='ready' ORDER BY submitter"
        ).fetchall()
        submitters = [r["submitter"] for r in rows]

    if not submitters:
        return ""

    idx = int(get_config("rotation_current_submitter_idx"))
    block_count = int(get_config("rotation_current_block_count"))
    tracks_per_block = int(get_config("rotation_tracks_per_block"))

    # Clamp index to valid range
    idx = idx % len(submitters)
    current_submitter = submitters[idx]

    # Pick the least-played ready track for this submitter, avoiding immediate repeats.
    # First try excluding the globally last played track; fall back if no alternatives.
    with db() as conn:
        row = conn.execute(
            """
            SELECT t.id, t.file_path FROM tracks t
            WHERE t.submitter=? AND t.status='ready'
              AND t.id != COALESCE(
                  (SELECT track_id FROM play_log ORDER BY played_at DESC LIMIT 1), ''
              )
            ORDER BY (
                SELECT COUNT(*) FROM play_log pl WHERE pl.track_id=t.id
            ) ASC,
            (
                SELECT MAX(pl.played_at) FROM play_log pl WHERE pl.track_id=t.id
            ) ASC NULLS FIRST,
            t.submitted_at ASC
            LIMIT 1
            """,
            (current_submitter,),
        ).fetchone()

        if not row:
            # Submitter's only ready track is the globally last played — allow repeat
            row = conn.execute(
                """
                SELECT t.id, t.file_path FROM tracks t
                WHERE t.submitter=? AND t.status='ready'
                ORDER BY (
                    SELECT COUNT(*) FROM play_log pl WHERE pl.track_id=t.id
                ) ASC,
                (
                    SELECT MAX(pl.played_at) FROM play_log pl WHERE pl.track_id=t.id
                ) ASC NULLS FIRST,
                t.submitted_at ASC
                LIMIT 1
                """,
                (current_submitter,),
            ).fetchone()

    if not row:
        # This submitter has no ready tracks, advance to next
        next_idx = (idx + 1) % len(submitters)
        set_config("rotation_current_submitter_idx", str(next_idx))
        set_config("rotation_current_block_count", "0")
        return _pick_rotation_track()

    # Advance block counter
    new_block_count = block_count + 1
    if new_block_count >= tracks_per_block:
        next_idx = (idx + 1) % len(submitters)
        set_config("rotation_current_submitter_idx", str(next_idx))
        set_config("rotation_current_block_count", "0")
    else:
        set_config("rotation_current_block_count", str(new_block_count))

    logger.info(f"Rotation: submitter={current_submitter} block={block_count}/{tracks_per_block}")
    return row["file_path"]


def _pick_mood_track() -> str:
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
            SELECT t.id, t.file_path, t.tempo_bpm, t.rms_energy,
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
            best_path = row["file_path"]

    logger.info(f"Mood: picked track with distance={best_dist:.4f}")
    return best_path or ""


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
