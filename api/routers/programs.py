import json
import logging
import re
from datetime import UTC, datetime

from database import db, get_config, set_config
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from routers.admin import require_admin

logger = logging.getLogger(__name__)
router = APIRouter()

MANIFEST_VERSION = 1


class ProgramCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProgramRename(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProgramItemCreate(BaseModel):
    track_id: str
    position: int | None = None  # None = append


class ProgramItemsReorder(BaseModel):
    item_ids: list[int]


class ProgramManifestItem(BaseModel):
    track_id: str | None = None
    title: str
    artist: str
    submitter: str


class ProgramManifest(BaseModel):
    version: int
    name: str = Field(min_length=1, max_length=200)
    items: list[ProgramManifestItem]


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _active_program_id() -> int | None:
    raw = get_config("active_program_id")
    return int(raw) if raw else None


def _program_summary(conn, row) -> dict:
    counts = conn.execute(
        """
        SELECT COUNT(*) AS item_count, COALESCE(SUM(t.duration_s), 0) AS total_duration_s
        FROM program_items pi
        LEFT JOIN tracks t ON pi.track_id = t.id
        WHERE pi.program_id = ?
        """,
        (row["id"],),
    ).fetchone()
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "item_count": counts["item_count"],
        "total_duration_s": counts["total_duration_s"],
    }


def _program_detail(conn, program_id: int) -> dict | None:
    row = conn.execute("SELECT id, name, created_at, updated_at FROM programs WHERE id=?", (program_id,)).fetchone()
    if not row:
        return None
    items = conn.execute(
        """
        SELECT pi.id AS item_id, pi.position,
               t.id AS track_id, t.title, t.artist, t.submitter, t.duration_s,
               t.status, t.in_rotation
        FROM program_items pi
        JOIN tracks t ON pi.track_id = t.id
        WHERE pi.program_id = ?
        ORDER BY pi.position ASC
        """,
        (program_id,),
    ).fetchall()
    item_dicts = [
        {
            "item_id": item["item_id"],
            "position": item["position"],
            "track_id": item["track_id"],
            "title": item["title"],
            "artist": item["artist"],
            "submitter": item["submitter"],
            "duration_s": item["duration_s"],
            "status": item["status"],
            "in_rotation": bool(item["in_rotation"]),
        }
        for item in items
    ]
    total_duration_s = sum(i["duration_s"] or 0 for i in item_dicts)
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "is_active": _active_program_id() == row["id"],
        "items": item_dicts,
        "total_duration_s": total_duration_s,
    }


def _touch_program(conn, program_id: int):
    conn.execute("UPDATE programs SET updated_at=? WHERE id=?", (_now_iso(), program_id))


def _renumber_positions(conn, program_id: int):
    """Rewrite positions to a contiguous 0..N-1 sequence in current order."""
    items = conn.execute(
        "SELECT id FROM program_items WHERE program_id=? ORDER BY position ASC, id ASC",
        (program_id,),
    ).fetchall()
    for new_pos, row in enumerate(items):
        conn.execute("UPDATE program_items SET position=? WHERE id=?", (new_pos, row["id"]))


def _clamp_position_for_active(program_id: int):
    """If `program_id` is the active program, clamp position to within current items
    and clear the pending-prefetch marker — its position reference may no longer
    point at the item Liquidsoap has actually buffered after a mutation."""
    if _active_program_id() != program_id:
        return
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM program_items WHERE program_id=?", (program_id,)).fetchone()["n"]
    pos = int(get_config("program_current_position") or "0")
    if pos > n:
        set_config("program_current_position", str(n))
    set_config("program_pending_position", "")


@router.get("/admin/programs")
def list_programs(auth=Depends(require_admin)):
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, updated_at FROM programs ORDER BY name COLLATE NOCASE"
        ).fetchall()
        active = _active_program_id()
        programs = []
        for r in rows:
            summary = _program_summary(conn, r)
            summary["is_active"] = r["id"] == active
            programs.append(summary)
    return {"programs": programs}


@router.post("/admin/programs")
def create_program(payload: ProgramCreate, auth=Depends(require_admin)):
    now = _now_iso()
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO programs (name, created_at, updated_at) VALUES (?, ?, ?)",
            (payload.name, now, now),
        )
        program_id = cursor.lastrowid
        detail = _program_detail(conn, program_id)
    logger.info(f"Created program {program_id}: {payload.name!r}")
    return detail


@router.get("/admin/programs/{program_id}")
def get_program(program_id: int, auth=Depends(require_admin)):
    with db() as conn:
        detail = _program_detail(conn, program_id)
    if not detail:
        raise HTTPException(404, "Program not found")
    return detail


@router.patch("/admin/programs/{program_id}")
def rename_program(program_id: int, payload: ProgramRename, auth=Depends(require_admin)):
    with db() as conn:
        row = conn.execute("SELECT id FROM programs WHERE id=?", (program_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Program not found")
        conn.execute(
            "UPDATE programs SET name=?, updated_at=? WHERE id=?",
            (payload.name, _now_iso(), program_id),
        )
        return _program_detail(conn, program_id)


@router.delete("/admin/programs/{program_id}")
def delete_program(program_id: int, auth=Depends(require_admin)):
    with db() as conn:
        row = conn.execute("SELECT id FROM programs WHERE id=?", (program_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Program not found")
        conn.execute("DELETE FROM programs WHERE id=?", (program_id,))

    if _active_program_id() == program_id:
        set_config("active_program_id", "")
        set_config("program_current_position", "0")
        set_config("program_pending_position", "")
        if get_config("programming_mode") == "programmed":
            set_config("programming_mode", "rotation")
            logger.info(f"Active program {program_id} deleted while in programmed mode; reverting to rotation")
    logger.info(f"Deleted program {program_id}")
    return {"ok": True}


@router.post("/admin/programs/{program_id}/items")
def add_program_item(program_id: int, payload: ProgramItemCreate, auth=Depends(require_admin)):
    with db() as conn:
        program = conn.execute("SELECT id FROM programs WHERE id=?", (program_id,)).fetchone()
        if not program:
            raise HTTPException(404, "Program not found")
        track = conn.execute("SELECT id FROM tracks WHERE id=?", (payload.track_id,)).fetchone()
        if not track:
            raise HTTPException(404, "Track not found")

        if payload.position is None:
            next_pos = conn.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) AS p FROM program_items WHERE program_id=?",
                (program_id,),
            ).fetchone()["p"]
            position = next_pos
        else:
            position = max(0, payload.position)
            conn.execute(
                "UPDATE program_items SET position = position + 1 WHERE program_id=? AND position >= ?",
                (program_id, position),
            )

        conn.execute(
            "INSERT INTO program_items (program_id, track_id, position) VALUES (?, ?, ?)",
            (program_id, payload.track_id, position),
        )
        _renumber_positions(conn, program_id)
        _touch_program(conn, program_id)
        detail = _program_detail(conn, program_id)

    _clamp_position_for_active(program_id)
    return detail


@router.delete("/admin/programs/{program_id}/items/{item_id}")
def remove_program_item(program_id: int, item_id: int, auth=Depends(require_admin)):
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM program_items WHERE id=? AND program_id=?",
            (item_id, program_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Item not found")
        conn.execute("DELETE FROM program_items WHERE id=?", (item_id,))
        _renumber_positions(conn, program_id)
        _touch_program(conn, program_id)
        detail = _program_detail(conn, program_id)

    _clamp_position_for_active(program_id)
    return detail


@router.patch("/admin/programs/{program_id}/items")
def reorder_program_items(program_id: int, payload: ProgramItemsReorder, auth=Depends(require_admin)):
    with db() as conn:
        program = conn.execute("SELECT id FROM programs WHERE id=?", (program_id,)).fetchone()
        if not program:
            raise HTTPException(404, "Program not found")

        existing_rows = conn.execute("SELECT id FROM program_items WHERE program_id=?", (program_id,)).fetchall()
        existing_ids = {r["id"] for r in existing_rows}
        if set(payload.item_ids) != existing_ids:
            raise HTTPException(400, "item_ids must contain exactly the current items of this program")
        for new_pos, item_id in enumerate(payload.item_ids):
            conn.execute(
                "UPDATE program_items SET position=? WHERE id=? AND program_id=?",
                (new_pos, item_id, program_id),
            )
        _touch_program(conn, program_id)
        result = _program_detail(conn, program_id)

    _clamp_position_for_active(program_id)
    return result


@router.post("/admin/programs/{program_id}/activate")
def activate_program(program_id: int, auth=Depends(require_admin)):
    with db() as conn:
        row = conn.execute("SELECT id FROM programs WHERE id=?", (program_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Program not found")
        item_count = conn.execute("SELECT COUNT(*) FROM program_items WHERE program_id=?", (program_id,)).fetchone()[0]
    if item_count == 0:
        raise HTTPException(400, "Cannot activate an empty program")
    set_config("active_program_id", str(program_id))
    set_config("program_current_position", "0")
    set_config("program_pending_position", "")
    logger.info(f"Activated program {program_id}")
    return {"ok": True, "active_program_id": program_id}


@router.get("/admin/programs/{program_id}/export")
def export_program(program_id: int, auth=Depends(require_admin)):
    with db() as conn:
        row = conn.execute("SELECT id, name FROM programs WHERE id=?", (program_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Program not found")
        items = conn.execute(
            """
            SELECT t.id AS track_id, t.title, t.artist, t.submitter
            FROM program_items pi
            JOIN tracks t ON pi.track_id = t.id
            WHERE pi.program_id = ?
            ORDER BY pi.position ASC
            """,
            (program_id,),
        ).fetchall()

    manifest = {
        "version": MANIFEST_VERSION,
        "name": row["name"],
        "items": [
            {
                "track_id": item["track_id"],
                "title": item["title"],
                "artist": item["artist"],
                "submitter": item["submitter"],
            }
            for item in items
        ],
    }
    body = json.dumps(manifest, indent=2)
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", row["name"]).strip("_") or "program"
    filename = f"{safe_name}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _match_track(conn, item: ProgramManifestItem) -> str | None:
    """Resolve a manifest item to a track_id in the local library, or None."""
    if item.track_id:
        row = conn.execute("SELECT id FROM tracks WHERE id=?", (item.track_id,)).fetchone()
        if row:
            return row["id"]
    row = conn.execute(
        """
        SELECT id FROM tracks
        WHERE LOWER(title)=LOWER(?) AND LOWER(artist)=LOWER(?) AND LOWER(submitter)=LOWER(?)
        ORDER BY submitted_at ASC LIMIT 1
        """,
        (item.title, item.artist, item.submitter),
    ).fetchone()
    if row:
        return row["id"]
    row = conn.execute(
        """
        SELECT id FROM tracks
        WHERE LOWER(title)=LOWER(?) AND LOWER(artist)=LOWER(?)
        ORDER BY submitted_at ASC LIMIT 1
        """,
        (item.title, item.artist),
    ).fetchone()
    if row:
        return row["id"]
    return None


@router.post("/admin/programs/import")
async def import_program(file: UploadFile = File(...), auth=Depends(require_admin)):
    raw = await file.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(400, f"Invalid manifest JSON: {e}") from None
    try:
        manifest = ProgramManifest.model_validate(data)
    except ValueError as e:
        raise HTTPException(400, f"Manifest schema error: {e}") from None
    if manifest.version != MANIFEST_VERSION:
        raise HTTPException(400, f"Unsupported manifest version {manifest.version} (expected {MANIFEST_VERSION})")

    now = _now_iso()
    unmatched: list[dict] = []
    matched_track_ids: list[str] = []
    with db() as conn:
        for original_position, item in enumerate(manifest.items):
            tid = _match_track(conn, item)
            if tid is None:
                unmatched.append(
                    {
                        "position": original_position,
                        "title": item.title,
                        "artist": item.artist,
                        "submitter": item.submitter,
                    }
                )
            else:
                matched_track_ids.append(tid)

        cursor = conn.execute(
            "INSERT INTO programs (name, created_at, updated_at) VALUES (?, ?, ?)",
            (manifest.name, now, now),
        )
        program_id = cursor.lastrowid
        for new_pos, tid in enumerate(matched_track_ids):
            conn.execute(
                "INSERT INTO program_items (program_id, track_id, position) VALUES (?, ?, ?)",
                (program_id, tid, new_pos),
            )

    logger.info(
        f"Imported program {program_id} ({manifest.name!r}): "
        f"{len(matched_track_ids)} matched, {len(unmatched)} unmatched"
    )
    return {
        "program_id": program_id,
        "name": manifest.name,
        "imported": len(matched_track_ids),
        "unmatched": unmatched,
    }
