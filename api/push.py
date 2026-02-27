import json
import logging
import os
import threading

from database import db

logger = logging.getLogger(__name__)

_push_lock = threading.Lock()


def _vapid_config() -> dict | None:
    """Return VAPID config dict, or None if not configured."""
    private_key = os.environ.get("VAPID_PRIVATE_KEY")
    claims_email = os.environ.get("VAPID_CLAIMS_EMAIL")
    if not private_key or not claims_email:
        return None
    return {"private_key": private_key, "claims_email": claims_email}


def send_push_to_all(title: str, body: str, url: str = "/playing.html") -> None:
    """Send a push notification to all subscribers in a background thread.

    No-op if VAPID_PRIVATE_KEY / VAPID_CLAIMS_EMAIL are not set.
    Dead subscriptions (HTTP 404/410) are removed automatically.
    """
    cfg = _vapid_config()
    if not cfg:
        return

    def _send():
        try:
            from pywebpush import WebPushException, webpush
        except ImportError:
            logger.warning("pywebpush not installed — push notifications disabled")
            return

        with _push_lock:
            with db() as conn:
                rows = conn.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions").fetchall()

            if not rows:
                return

            payload = json.dumps({"title": title, "body": body, "url": url})
            dead: list[str] = []

            for row in rows:
                subscription_info = {
                    "endpoint": row["endpoint"],
                    "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
                }
                try:
                    webpush(
                        subscription_info=subscription_info,
                        data=payload,
                        vapid_private_key=cfg["private_key"],
                        vapid_claims={"sub": f"mailto:{cfg['claims_email']}"},
                    )
                except WebPushException as e:
                    status = e.response.status_code if e.response is not None else None
                    if status in (404, 410):
                        dead.append(row["endpoint"])
                        logger.info(
                            "Push subscription expired (%s), removing: %.60s…",
                            status,
                            row["endpoint"],
                        )
                    else:
                        logger.warning("Push failed for %.60s…: %s", row["endpoint"], e)
                except Exception as e:
                    logger.warning("Push failed: %s", e)

            if dead:
                with db() as conn:
                    for endpoint in dead:
                        conn.execute(
                            "DELETE FROM push_subscriptions WHERE endpoint=?",
                            (endpoint,),
                        )

    threading.Thread(target=_send, daemon=True, name="push-sender").start()
