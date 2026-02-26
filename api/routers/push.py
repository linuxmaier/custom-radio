import os

from database import db
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()


@router.get("/manifest.json")
def get_manifest():
    """PWA web app manifest — station name comes from STATION_NAME env var."""
    station_name = os.environ.get("STATION_NAME", "Family Radio")
    short_name = station_name.split()[0] if station_name else "Radio"
    manifest = {
        "name": station_name,
        "short_name": short_name,
        "start_url": "/playing.html",
        "scope": "/",
        "display": "standalone",
        "theme_color": "#6c63ff",
        "background_color": "#1a1a2e",
        "icons": [
            {
                "src": "/static/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
            },
            {
                "src": "/static/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
            },
        ],
    }
    return JSONResponse(
        content=manifest,
        headers={"Content-Type": "application/manifest+json"},
    )


@router.get("/push/vapid-key")
def get_vapid_key():
    """Return the VAPID public key for push subscription."""
    key = os.environ.get("VAPID_PUBLIC_KEY")
    if not key:
        raise HTTPException(503, "Push notifications not configured")
    return {"public_key": key}


class SubscriptionRequest(BaseModel):
    endpoint: str
    p256dh: str
    auth: str


@router.post("/push/subscribe")
def subscribe(req: SubscriptionRequest):
    """Upsert a push subscription."""
    with db() as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions (endpoint, p256dh, auth)
            VALUES (?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth
            """,
            (req.endpoint, req.p256dh, req.auth),
        )
    return {"ok": True}


@router.post("/push/unsubscribe")
def unsubscribe(req: SubscriptionRequest):
    """Remove a push subscription."""
    with db() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (req.endpoint,))
    return {"ok": True}
