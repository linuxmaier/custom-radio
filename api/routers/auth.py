import hashlib
import json as _json
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
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth")

MAGIC_LINK_TTL_MINUTES = 15
CLAIM_CODE_TTL_MINUTES = 5
SESSION_TTL_DAYS = 30
CHALLENGE_TTL_MINUTES = 5
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
_hostname = os.environ.get("SERVER_HOSTNAME", "localhost")
IS_LOCAL = _hostname in ("localhost", "")
STATION_NAME = os.environ.get("STATION_NAME", "Family Radio")
_origin = f"{'http' if IS_LOCAL else 'https'}://{_hostname}"

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
      margin: 1rem 0;
      font-family: monospace;
    }}
    p {{ color: #94a3b8; font-size: 0.9rem; margin: 0.35rem 0; }}
    .copy-btn {{
      background: #6c63ff;
      color: #fff;
      border: none;
      border-radius: 8px;
      padding: 0.5rem 1.25rem;
      font-size: 0.9rem;
      cursor: pointer;
      margin: 0.5rem 0 1.25rem;
    }}
    .copy-btn:active {{ opacity: 0.8; }}
    .instructions {{
      max-width: 360px;
      text-align: left;
      background: #1a1d27;
      border-radius: 10px;
      padding: 1rem 1.25rem;
      margin-top: 1rem;
    }}
    .instructions p {{ margin: 0.4rem 0; }}
    .section-label {{ color: #e2e8f0; font-weight: 600; margin-top: 0.75rem !important; }}
    .instructions a {{ color: #6c63ff; }}
  </style>
</head>
<body>
  <h1>Your sign-in code</h1>
  <div class="code">{code_display}</div>
  <button class="copy-btn" id="copyBtn" onclick="copyCode()">Copy code</button>
  <p>This code expires in {ttl} minutes.</p>
  <div class="instructions">
    <p class="section-label">On your phone:</p>
    <p>Open the {station_name} app and enter this code. If you haven't installed it
    yet, <a href="{base_url}">open the station</a> in your browser, tap
    <strong>Share &rarr; Add to Home Screen</strong>, then open the app and enter the code.</p>
    <p class="section-label">On a computer:</p>
    <p><a href="{base_url}">Return to {station_name}</a>, enter your email address,
    then enter this code when prompted.</p>
  </div>
  <script>
    function copyCode() {{
      navigator.clipboard.writeText('{code_raw}').then(function() {{
        var btn = document.getElementById('copyBtn');
        btn.textContent = 'Copied!';
        setTimeout(function() {{ btn.textContent = 'Copy code'; }}, 2000);
      }}).catch(function() {{
        var el = document.querySelector('.code');
        var range = document.createRange();
        range.selectNode(el);
        window.getSelection().removeAllRanges();
        window.getSelection().addRange(range);
      }});
    }}
  </script>
</body>
</html>"""

_REVEAL_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Sign In — {station_name}</title>
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
    h1 {{ font-size: 1.4rem; font-weight: 600; margin-bottom: 0.5rem; }}
    p {{ color: #94a3b8; font-size: 0.9rem; margin: 0.35rem 0; }}
    .reveal-btn {{
      background: #6c63ff;
      color: #fff;
      border: none;
      border-radius: 8px;
      padding: 0.75rem 2rem;
      font-size: 1rem;
      cursor: pointer;
      margin-top: 1.5rem;
    }}
    .reveal-btn:active {{ opacity: 0.8; }}
  </style>
</head>
<body>
  <h1>Sign in to {station_name}</h1>
  <p>Tap the button below to get your sign-in code.</p>
  <p>This link expires in {ttl} minutes.</p>
  <form method="POST" action="{action_url}">
    <button class="reveal-btn" type="submit">Get my sign-in code</button>
  </form>
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


def _store_challenge(conn, challenge_bytes: bytes, user_id: str | None, ctype: str) -> str:
    b64 = bytes_to_base64url(challenge_bytes)
    conn.execute(
        "DELETE FROM passkey_challenges WHERE expires_at < ?",
        (datetime.now(UTC).isoformat(),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO passkey_challenges (challenge, user_id, type, expires_at) VALUES (?, ?, ?, ?)",
        (b64, user_id, ctype, _expires_iso(minutes=CHALLENGE_TTL_MINUTES)),
    )
    return b64


def _consume_challenge(conn, b64: str, ctype: str) -> dict | None:
    """Fetch-and-delete a challenge row. Returns None if missing or expired."""
    row = conn.execute(
        "SELECT * FROM passkey_challenges WHERE challenge = ? AND type = ?",
        (b64, ctype),
    ).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM passkey_challenges WHERE challenge = ?", (b64,))
    return None if _is_expired(row["expires_at"]) else dict(row)


def _challenge_from_credential(credential_dict: dict) -> str:
    """Decode clientDataJSON to extract the echoed challenge (base64url string)."""
    import base64 as _base64

    raw = credential_dict.get("response", {}).get("clientDataJSON", "")
    client_data = _json.loads(_base64.urlsafe_b64decode(raw + "=="))
    return client_data.get("challenge", "")


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


class PasskeyCredentialBody(BaseModel):
    credential: dict


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


def _verify_token_check(token_hash: str) -> HTMLResponse | None:
    """Validate a token hash. Returns an error HTMLResponse if invalid, None if OK."""
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
    return None


@router.get("/verify")
def verify_token_get(token: str) -> HTMLResponse:
    """Validate magic link token and show a button — does NOT consume the token.
    Keeps link prefetchers (iMessage, email clients) from burning single-use tokens."""
    token_hash = _hash(token)
    err = _verify_token_check(token_hash)
    if err:
        return err
    protocol = "http" if IS_LOCAL else "https"
    action_url = f"{protocol}://{_hostname}/api/auth/verify?token={token}"
    html = _REVEAL_HTML.format(
        station_name=STATION_NAME,
        ttl=MAGIC_LINK_TTL_MINUTES,
        action_url=action_url,
    )
    return HTMLResponse(content=html)


@router.post("/verify")
def verify_token_post(token: str) -> HTMLResponse:
    """Consume the magic link token, generate a claim code, and show it."""
    token_hash = _hash(token)
    err = _verify_token_check(token_hash)
    if err:
        return err

    with db() as conn:
        row = conn.execute("SELECT * FROM auth_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
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
    protocol = "http" if IS_LOCAL else "https"
    html = _VERIFY_HTML.format(
        code_display=code_display,
        code_raw=code_str,
        station_name=STATION_NAME,
        ttl=CLAIM_CODE_TTL_MINUTES,
        base_url=f"{protocol}://{_hostname}",
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


@router.get("/claimable-names")
def claimable_names(user: dict = Depends(require_user)) -> dict:
    """Return distinct submitter names not already claimed by an approved user."""
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT submitter FROM tracks"
            " WHERE submitter NOT IN"
            " (SELECT name FROM users WHERE name IS NOT NULL AND status = 'approved')"
            " ORDER BY submitter"
        ).fetchall()
    return {"names": [r["submitter"] for r in rows]}


@router.patch("/me")
def update_me(body: UpdateNameBody, user: dict = Depends(require_user)) -> dict:
    name = body.name.strip()[:50]
    if not name:
        raise HTTPException(400, "Name cannot be empty")
    with db() as conn:
        taken = conn.execute(
            "SELECT id FROM users WHERE name = ? AND id != ? AND status = 'approved'",
            (name, user["id"]),
        ).fetchone()
        if taken:
            raise HTTPException(409, "That name is already taken by another member")
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user["id"]))
        conn.execute(
            "UPDATE tracks SET user_id = ? WHERE submitter = ? AND user_id IS NULL",
            (user["id"], name),
        )
    return {"ok": True, "name": name}


# ── Passkey endpoints ─────────────────────────────────────────────────────────


@router.post("/passkey/register/begin")
def passkey_register_begin(user: dict = Depends(require_user)) -> dict:
    with db() as conn:
        existing = conn.execute("SELECT id FROM passkey_credentials WHERE user_id = ?", (user["id"],)).fetchall()
        exclude = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["id"])) for r in existing]
        opts = generate_registration_options(
            rp_id=_hostname,
            rp_name=STATION_NAME,
            user_id=user["id"].encode(),
            user_name=user["email"],
            user_display_name=user["name"] or user["email"],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED,
            ),
            exclude_credentials=exclude,
        )
        _store_challenge(conn, opts.challenge, user["id"], "registration")
    return {"options": _json.loads(options_to_json(opts))}


@router.post("/passkey/register/complete")
def passkey_register_complete(body: PasskeyCredentialBody, user: dict = Depends(require_user)) -> dict:
    challenge_b64 = _challenge_from_credential(body.credential)
    with db() as conn:
        row = _consume_challenge(conn, challenge_b64, "registration")
        if not row:
            raise HTTPException(400, "Challenge invalid or expired")
        if row["user_id"] != user["id"]:
            raise HTTPException(403, "Challenge user mismatch")
        try:
            verified = verify_registration_response(
                credential=body.credential,
                expected_challenge=base64url_to_bytes(challenge_b64),
                expected_rp_id=_hostname,
                expected_origin=_origin,
            )
        except Exception as exc:
            raise HTTPException(400, f"Registration verification failed: {exc}") from exc

        cred_id_b64 = bytes_to_base64url(verified.credential_id)
        existing = conn.execute("SELECT id FROM passkey_credentials WHERE id = ?", (cred_id_b64,)).fetchone()
        if existing:
            raise HTTPException(409, "Credential already registered")

        aaguid = str(verified.aaguid) if verified.aaguid else ""
        conn.execute(
            "INSERT INTO passkey_credentials (id, user_id, public_key, sign_count, aaguid) VALUES (?, ?, ?, ?, ?)",
            (cred_id_b64, user["id"], verified.credential_public_key, verified.sign_count, aaguid),
        )
    return {"ok": True}


@router.post("/passkey/authenticate/begin")
def passkey_authenticate_begin() -> dict:
    with db() as conn:
        opts = generate_authentication_options(
            rp_id=_hostname,
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        _store_challenge(conn, opts.challenge, None, "authentication")
    return {"options": _json.loads(options_to_json(opts))}


@router.post("/passkey/authenticate/complete")
def passkey_authenticate_complete(body: PasskeyCredentialBody, response: Response) -> dict:
    cred_id_raw = body.credential.get("id", "")
    challenge_b64 = _challenge_from_credential(body.credential)
    with db() as conn:
        ch_row = _consume_challenge(conn, challenge_b64, "authentication")
        if not ch_row:
            raise HTTPException(400, "Challenge invalid or expired")

        cred_row = conn.execute(
            "SELECT pc.id, pc.public_key, pc.sign_count, pc.aaguid,"
            " u.id as uid, u.email, u.name, u.status"
            " FROM passkey_credentials pc JOIN users u ON pc.user_id = u.id"
            " WHERE pc.id = ?",
            (cred_id_raw,),
        ).fetchone()
        if not cred_row:
            raise HTTPException(400, "Credential not found")
        if cred_row["status"] != "approved":
            raise HTTPException(403, "Account not approved")

        try:
            verified = verify_authentication_response(
                credential=body.credential,
                expected_challenge=base64url_to_bytes(challenge_b64),
                expected_rp_id=_hostname,
                expected_origin=_origin,
                credential_public_key=bytes(cred_row["public_key"]),
                credential_current_sign_count=cred_row["sign_count"],
                require_user_verification=False,
            )
        except Exception as exc:
            raise HTTPException(400, f"Authentication verification failed: {exc}") from exc

        conn.execute(
            "UPDATE passkey_credentials SET sign_count = ?, last_used_at = ? WHERE id = ?",
            (verified.new_sign_count, datetime.now(UTC).isoformat(), cred_id_raw),
        )

        raw_session = secrets.token_urlsafe(32)
        session_hash = _hash(raw_session)
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (session_hash, cred_row["uid"], _expires_iso(days=SESSION_TTL_DAYS)),
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
        "user": {
            "id": cred_row["uid"],
            "email": cred_row["email"],
            "name": cred_row["name"],
        },
    }


@router.get("/passkey/list")
def passkey_list(user: dict = Depends(require_user)) -> dict:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, aaguid, created_at, last_used_at"
            " FROM passkey_credentials WHERE user_id = ? ORDER BY created_at",
            (user["id"],),
        ).fetchall()
    return {
        "passkeys": [
            {
                "id": r["id"],
                "aaguid": r["aaguid"],
                "created_at": r["created_at"],
                "last_used_at": r["last_used_at"],
            }
            for r in rows
        ]
    }


@router.delete("/passkey/{credential_id}")
def passkey_delete(credential_id: str, user: dict = Depends(require_user)) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM passkey_credentials WHERE id = ? AND user_id = ?",
            (credential_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Credential not found")
        conn.execute("DELETE FROM passkey_credentials WHERE id = ?", (credential_id,))
    return {"ok": True}


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
