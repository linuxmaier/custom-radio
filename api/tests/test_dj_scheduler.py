"""
Tests for DJ interlude scheduling behavior in get_next_track().

Specifically tests the dj_reserved_track_id / dj_interlude_just_played guard
to ensure a skip cannot prematurely consume the reserved post-interlude track.
"""

import sqlite3

import database
import pytest
import scheduler
from database import get_config, init_db, set_config
from dj import DJ_SENTINEL


@pytest.fixture
def fresh_db(tmp_path):
    """Patch DB_PATH to a temp file, initialize schema, restore on teardown."""
    db_file = str(tmp_path / "test_radio.db")
    original = database.DB_PATH
    database.DB_PATH = db_file
    init_db()
    yield db_file
    database.DB_PATH = original


def _seed_tracks(db_path: str, submitter: str, count: int):
    conn = sqlite3.connect(db_path)
    for i in range(count):
        track_id = f"{submitter.lower()}{i + 1}"
        conn.execute(
            "INSERT INTO tracks "
            "(id, title, artist, submitter, source_type, file_path, duration_s, status, submitted_at)"
            " VALUES (?, ?, ?, ?, 'upload', ?, 180, 'ready', datetime('now'))",
            (track_id, f"{submitter} Track {i + 1}", "Artist", submitter, f"/media/{track_id}.mp3"),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Core guard tests
# ---------------------------------------------------------------------------


def test_skip_returns_interlude_not_reserved_track(fresh_db, tmp_path):
    """
    When dj_pending_file is set and dj_reserved_track_id is set,
    calling get_next_track() (as a skip would) must return the interlude —
    NOT the reserved track. The reserved track must remain untouched.
    """
    interlude = str(tmp_path / "interlude.mp3")
    open(interlude, "wb").close()  # create the file so os.path.exists() passes

    _seed_tracks(fresh_db, "Alice", 3)
    _seed_tracks(fresh_db, "Bob", 3)

    set_config("dj_enabled", "true")
    set_config("dj_pending_file", interlude)
    set_config("dj_reserved_track_id", "alice1")
    set_config("dj_interlude_just_played", "false")
    set_config("rotation_current_submitter_idx", "1")  # Bob's block is current

    track = scheduler.get_next_track()

    assert track is not None
    assert track["id"] == DJ_SENTINEL, (
        f"Expected interlude but got track '{track['id']}' — reserved track was consumed prematurely by a skip"
    )
    # dj_pending_file consumed, flag set
    assert get_config("dj_pending_file") == ""
    assert get_config("dj_interlude_just_played") == "true"
    # reserved track NOT consumed yet — interlude hasn't played yet from Liquidsoap's perspective
    assert get_config("dj_reserved_track_id") == "alice1"


def test_reserved_track_consumed_after_interlude(fresh_db):
    """
    After the interlude has played (dj_interlude_just_played=True),
    the very next get_next_track() call must return the reserved track.
    """
    _seed_tracks(fresh_db, "Alice", 3)
    _seed_tracks(fresh_db, "Bob", 3)

    set_config("dj_enabled", "true")
    set_config("dj_pending_file", "")
    set_config("dj_interlude_just_played", "true")
    set_config("dj_reserved_track_id", "alice1")
    set_config("rotation_current_submitter_idx", "0")  # Alice is next after interlude

    track = scheduler.get_next_track()

    assert track is not None
    assert track["id"] == "alice1", f"Expected reserved track 'alice1' but got '{track['id']}'"
    assert get_config("dj_interlude_just_played") == "false"
    assert get_config("dj_reserved_track_id") == ""


def test_reserved_track_not_consumed_by_normal_rotation(fresh_db):
    """
    With dj_reserved_track_id set but dj_interlude_just_played=False
    and no pending interlude file, get_next_track() must NOT return the
    reserved track — normal rotation proceeds as if DJ state doesn't exist.
    """
    _seed_tracks(fresh_db, "Alice", 3)
    _seed_tracks(fresh_db, "Bob", 3)

    set_config("dj_enabled", "true")
    set_config("dj_pending_file", "")
    set_config("dj_reserved_track_id", "alice1")
    set_config("dj_interlude_just_played", "false")
    set_config("rotation_current_submitter_idx", "1")  # Bob's turn

    track = scheduler.get_next_track()

    assert track is not None
    assert track["submitter"] == "Bob", (
        f"Expected a Bob track from normal rotation but got submitter='{track['submitter']}' "
        f"(track id='{track['id']}') — reserved track consumed by a non-interlude call"
    )
    assert get_config("dj_reserved_track_id") == "alice1"  # still reserved


def test_skip_during_interlude_returns_reserved_track(fresh_db):
    """
    If skip fires while the interlude is playing (dj_interlude_just_played=True),
    the reserved track should be returned — correct, since the interlude did play.
    """
    _seed_tracks(fresh_db, "Alice", 3)

    set_config("dj_enabled", "true")
    set_config("dj_pending_file", "")
    set_config("dj_interlude_just_played", "true")
    set_config("dj_reserved_track_id", "alice1")
    set_config("rotation_current_submitter_idx", "0")

    track = scheduler.get_next_track()

    assert track is not None
    assert track["id"] == "alice1"
    assert get_config("dj_interlude_just_played") == "false"
    assert get_config("dj_reserved_track_id") == ""


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


def test_reserved_track_missing_from_db_falls_back_to_rotation(fresh_db):
    """
    If dj_interlude_just_played=True but the reserved track no longer exists
    in the DB (e.g. it was deleted), fall through to normal rotation rather
    than returning None or crashing.
    """
    _seed_tracks(fresh_db, "Alice", 3)

    set_config("dj_enabled", "true")
    set_config("dj_pending_file", "")
    set_config("dj_interlude_just_played", "true")
    set_config("dj_reserved_track_id", "nonexistent-track-id")
    set_config("rotation_current_submitter_idx", "0")

    track = scheduler.get_next_track()

    assert track is not None
    assert track["id"] != "nonexistent-track-id"
    assert get_config("dj_interlude_just_played") == "false"
    assert get_config("dj_reserved_track_id") == ""
