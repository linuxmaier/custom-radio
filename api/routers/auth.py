import hashlib
import logging
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from database import db
from email_utils import send_email
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth")

MAGIC_LINK_TTL_MINUTES = 15
CLAIM_CODE_TTL_MINUTES = 5
SESSION_TTL_DAYS = 30
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
_hostname = os.environ.get("SERVER_HOSTNAME", "localhost")
IS_LOCAL = _hostname in ("localhost", "")
STATION_NAME = os.environ.get("STATION_NAME", "Family Radio")

_VERIFY_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Sign-in Code</title>
  <style>
    body {{
      font-family: system-ui, sans-serif;
      background: #0f1117;
      color: #e2e8f0;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      text-align: center;
      padding: 1.5rem;
      box-sizing: border-box;
    }}
    h1 {{ font-size: 1.4rem; font-weight: 600; margin-bottom: 0.25rem; }}
    .code {{
      font-size: 3rem;
      font-weight: 700;
      letter-spacing: 0.2em;
      color: #6c63ff;
      margin: 1.5rem 0;
      font-family: monospace;
    }}
    p {{ color: #94a3b8; font-size: 0.9rem; margin: 0.35rem 0; }}
  </style>
</head>
<body>
  <h1>Your sign-in code</h1>
  <div class="code">{code_display}</div>
  <p>Enter this code in the {station_name} app.</p>
  <p>This code expires in {ttl} minutes.</p>
</body>
</html>"""

_ERROR_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Sign-in Error</title>
  <style>
    body {{
      font-family: system-ui, sans-serif;
      background: #0f1117;
      color: #e2e8f0;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      text-align: center;
      padding: 1.5rem;
      box-sizing: border-box;
    }}
    h1 {{ font-size: 1.4rem; font-weight: 600; color: #ef4444; margin-bottom: 0.5rem; }}
    p {{ color: #94a3b8; font-size: 0.9rem; }}
  </style>
</head>
<body>
  <h1>Sign-in link {reason}</h1>
  <p>Please request a new sign-in link from the {station_name} app.</p>
</body>
</html>"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _expires_iso(*, minutes: int = 0, days: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes, days=days)).isoformat()


def _is_expired(s: str) -> bool:
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt < datetime.now(UTC)
    except Exception:
        return True


def _make_link(raw_token: str) -> str:
    protocol = "http" if IS_LOCAL else "https"
    return f"{protocol}://{_hostname}/api/auth/verify?token={raw_token}"


def _generate_magic_token(conn, user_id: str) -> str:
    """Generate a magic link token (raw), store hash in DB."""
    raw = secrets.token_urlsafe(32)
    token_hash = _hash(raw)
    expires = _expires_iso(minutes=MAGIC_LINK_TTL_MINUTES)
    # Delete existing tokens for this user before inserting (keeps table tidy)
    conn.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,))
    conn.execute(
        "INSERT INTO auth_tokens (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (token_hash, user_id, expires),
    )
    return raw


def _send_magic_link_email(to: str, raw_token: str) -> None:
    link = _make_link(raw_token)
    send_email(
        to,
        f"Sign in to {STATION_NAME}",
        f"Click this link to sign in to {STATION_NAME}:\n\n{link}"
        f"\n\nThis link expires in {MAGIC_LINK_TTL_MINUTES} minutes.",
    )


# ── Auth dependencies ─────────────────────────────────────────────────────────


def require_user(session: str | None = Cookie(default=None)) -> dict:
    """FastAPI dependency: validate session cookie and return user dict."""
    if not session:
        raise HTTPException(401, "Not authenticated")
    token_hash = _hash(session)
    with db() as conn:
        row = conn.execute(
            """
            SELECT s.token_hash, s.expires_at, u.id, u.email, u.name, u.status
            FROM sessions s JOIN users u ON s.user_id = u.id
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if not row or _is_expired(row["expires_at"]):
            if row:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            raise HTTPException(401, "Session expired or invalid")
        # Slide expiry on every use
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            (_expires_iso(days=SESSION_TTL_DAYS), token_hash),
        )
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "status": row["status"],
    }


def _require_admin(x_admin_token: str = Header(None)):
    if not ADMIN_TOKEN:
        raise HTTPException(500, "ADMIN_TOKEN not configured")
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")


# ── Pydantic models ───────────────────────────────────────────────────────────


class RequestAccessBody(BaseModel):
    email: str


class ClaimBody(BaseModel):
    code: str


class UpdateNameBody(BaseModel):
    name: str


class BootstrapBody(BaseModel):
    email: str


class CreateUserBody(BaseModel):
    email: str
    name: str | None = None


# ── Public endpoints ──────────────────────────────────────────────────────────


@router.post("/request-access")
def request_access(body: RequestAccessBody) -> dict:
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Invalid email address")

    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        if not user:
            user_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO users (id, email, status) VALUES (?, ?, 'pending')",
                (user_id, email),
            )
        elif user["status"] in ("pending", "rejected"):
            return {
                "status": "pending",
                "msg": "Your request is under review. You'll receive an email once it's approved.",
            }
        else:
            # status == 'approved' — send magic link
            # Rate limit: max 3 tokens created in the last 60 minutes.
            # Tokens have 15-min TTL, so "created in last 60 min" means
            # expires_at > now - 45 min.
            cutoff = _expires_iso(minutes=-45)
            count = conn.execute(
                "SELECT COUNT(*) FROM auth_tokens WHERE user_id = ? AND expires_at > ?",
                (user["id"], cutoff),
            ).fetchone()[0]
            if count >= 3:
                return {
                    "status": "approved",
                    "msg": "Too many sign-in attempts. Please wait a few minutes and try again.",
                }
            raw_token = _generate_magic_token(conn, user["id"])

    if not user:
        # New registration — send admin alert
        alert_to = os.environ.get("ALERT_TO", "")
        if alert_to:
            from alerts import send_alert

            send_alert(
                f"New access request — {STATION_NAME}",
                f"New access request from: {email}\n\nApprove or reject in the admin panel.",
            )
        return {
            "status": "pending",
            "msg": "Your request has been submitted. You'll receive an email once it's approved.",
        }

    # Send magic link email after DB commit
    _send_magic_link_email(email, raw_token)
    result: dict = {
        "status": "approved",
        "msg": "Check your email for a sign-in link.",
    }
    if IS_LOCAL:
        result["debug_token"] = raw_token
        result["debug_url"] = _make_link(raw_token)
    return result


@router.get("/verify")
def verify_token(token: str) -> HTMLResponse:
    """Validate magic link token, generate a claim code, return HTML page."""
    token_hash = _hash(token)
    with db() as conn:
        row = conn.execute("SELECT * FROM auth_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
        if not row:
            return HTMLResponse(
                _ERROR_HTML.format(reason="not found", station_name=STATION_NAME),
                status_code=404,
            )
        if row["used"]:
            return HTMLResponse(
                _ERROR_HTML.format(reason="already used", station_name=STATION_NAME),
                status_code=410,
            )
        if _is_expired(row["expires_at"]):
            return HTMLResponse(
                _ERROR_HTML.format(reason="expired", station_name=STATION_NAME),
                status_code=410,
            )

        # Mark token used
        conn.execute("UPDATE auth_tokens SET used = 1 WHERE token_hash = ?", (token_hash,))

        # Generate 6-digit claim code
        code_int = secrets.randbelow(1_000_000)
        code_str = f"{code_int:06d}"
        code_hash = _hash(code_str)
        expires = _expires_iso(minutes=CLAIM_CODE_TTL_MINUTES)

        # Remove any stale claim codes for this user
        conn.execute("DELETE FROM claim_codes WHERE user_id = ?", (row["user_id"],))
        conn.execute(
            "INSERT INTO claim_codes (code_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (code_hash, row["user_id"], expires),
        )

    code_display = code_str[:3] + " " + code_str[3:]
    html = _VERIFY_HTML.format(
        code_display=code_display,
        station_name=STATION_NAME,
        ttl=CLAIM_CODE_TTL_MINUTES,
    )
    return HTMLResponse(content=html)


@router.post("/claim")
def claim_session(body: ClaimBody, response: Response) -> dict:
    """Exchange a claim code for a session cookie."""
    # Normalize: strip spaces and dashes
    code = body.code.replace(" ", "").replace("-", "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, "Invalid code format")
    code_hash = _hash(code)

    with db() as conn:
        row = conn.execute(
            "SELECT cc.user_id, cc.expires_at, u.id, u.email, u.name, u.status "
            "FROM claim_codes cc JOIN users u ON cc.user_id = u.id "
            "WHERE cc.code_hash = ?",
            (code_hash,),
        ).fetchone()
        if not row or _is_expired(row["expires_at"]):
            raise HTTPException(400, "Code is invalid or expired")

        # Delete claim code (single-use)
        conn.execute("DELETE FROM claim_codes WHERE code_hash = ?", (code_hash,))

        # Create session
        raw_session = secrets.token_urlsafe(32)
        session_hash = _hash(raw_session)
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (session_hash, row["user_id"], _expires_iso(days=SESSION_TTL_DAYS)),
        )

    response.set_cookie(
        key="session",
        value=raw_session,
        httponly=True,
        secure=not IS_LOCAL,
        samesite="lax",
        max_age=SESSION_TTL_DAYS * 86400,
        path="/",
    )
    return {
        "ok": True,
        "needs_name": row["name"] is None,
        "user": {
            "id": row["id"],
            "email": row["email"],
            "name": row["name"],
        },
    }


@router.post("/logout")
def logout(response: Response, session: str | None = Cookie(default=None)) -> dict:
    if session:
        token_hash = _hash(session)
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
    response.delete_cookie(key="session", path="/")
    return {"ok": True}


@router.post("/bootstrap")
def bootstrap(body: BootstrapBody) -> dict:
    """Create the first user (approved). Returns 403 if any user already exists."""
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Invalid email address")

    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count > 0:
            raise HTTPException(403, "Bootstrap disabled: users already exist")

        user_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO users (id, email, status) VALUES (?, ?, 'approved')",
            (user_id, email),
        )
        raw_token = _generate_magic_token(conn, user_id)

    link = _make_link(raw_token)
    _send_magic_link_email(email, raw_token)
    return {"ok": True, "debug_url": link}


# ── Session-required endpoints ────────────────────────────────────────────────


@router.get("/me")
def get_me(user: dict = Depends(require_user)) -> dict:
    return {"id": user["id"], "email": user["email"], "name": user["name"]}


@router.patch("/me")
def update_me(body: UpdateNameBody, user: dict = Depends(require_user)) -> dict:
    name = body.name.strip()[:50]
    if not name:
        raise HTTPException(400, "Name cannot be empty")
    with db() as conn:
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user["id"]))
    return {"ok": True, "name": name}


# ── Admin-only endpoints ──────────────────────────────────────────────────────


@router.get("/users")
def list_users(auth=Depends(_require_admin)) -> dict:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, email, name, status, created_at FROM users ORDER BY status, created_at"
        ).fetchall()
    return {
        "users": [
            {
                "id": r["id"],
                "email": r["email"],
                "name": r["name"],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }


@router.post("/users")
def create_user(body: CreateUserBody, auth=Depends(_require_admin)) -> dict:
    """Admin creates an approved user directly, bypassing the approval flow."""
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Invalid email address")

    with db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            raise HTTPException(409, "User with that email already exists")

        user_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO users (id, email, name, status) VALUES (?, ?, ?, 'approved')",
            (user_id, email, body.name.strip()[:50] if body.name else None),
        )
        raw_token = _generate_magic_token(conn, user_id)

    link = _make_link(raw_token)
    _send_magic_link_email(email, raw_token)
    result: dict = {"ok": True}
    if IS_LOCAL:
        result["debug_url"] = link
    return result


@router.post("/users/{user_id}/approve")
def approve_user(user_id: str, auth=Depends(_require_admin)) -> dict:
    with db() as conn:
        row = conn.execute("SELECT id, email FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found")

        conn.execute("UPDATE users SET status = 'approved' WHERE id = ?", (user_id,))
        raw_token = _generate_magic_token(conn, user_id)

    email = row["email"]
    link = _make_link(raw_token)
    send_email(
        email,
        f"You've been approved — {STATION_NAME}",
        f"Great news! Your access to {STATION_NAME} has been approved.\n\n"
        f"Click this link to sign in:\n\n{link}"
        f"\n\nThis link expires in {MAGIC_LINK_TTL_MINUTES} minutes.",
    )
    result: dict = {"ok": True}
    if IS_LOCAL:
        result["debug_url"] = link
    return result


@router.post("/users/{user_id}/reject")
def reject_user(user_id: str, auth=Depends(_require_admin)) -> dict:
    with db() as conn:
        row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        conn.execute("UPDATE users SET status = 'rejected' WHERE id = ?", (user_id,))
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(user_id: str, auth=Depends(_require_admin)) -> dict:
    with db() as conn:
        row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        # ON DELETE CASCADE handles sessions, auth_tokens, claim_codes
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"ok": True}
