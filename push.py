"""
push.py — APNs (Apple Push Notification service) delivery for the Cavnar AI
iOS app.

Modeled directly on webhooks.py's proven delivery pattern (retry-with-
backoff, per-delivery logging, auto-disable after repeated failures) — the
two real differences are: APNs requires HTTP/2 (requests has no HTTP/2
support, hence httpx here), and APNs' own "this token will never work again"
signal (410 / BadDeviceToken / Unregistered) gets an immediate delete rather
than waiting out the failure counter, since retrying a dead token is pure
waste.
"""
import json
import threading
import time
from datetime import datetime, timezone

from models import get_conn, DB_PATH

# Same meaning as webhooks.py's _AUTO_DISABLE_AFTER: a token that's failed
# this many consecutive deliveries for reasons OTHER than the fast-path
# "definitely dead" APNs responses below gets deleted too — no visibility
# into a broken token otherwise, and nothing to gain from retrying forever.
_AUTO_DISABLE_AFTER = 10

# APNs responses that mean "this token will never be valid again" — no point
# waiting for _AUTO_DISABLE_AFTER consecutive failures, delete on the first.
_PERMANENT_FAILURE_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_tokens (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              INTEGER NOT NULL REFERENCES users(id),
    restaurant_id        INTEGER NOT NULL REFERENCES restaurants(id),
    apns_token           TEXT NOT NULL UNIQUE,
    environment          TEXT NOT NULL DEFAULT 'production',
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    last_success_at      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    disabled_reason      TEXT
);
CREATE TABLE IF NOT EXISTS push_deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_token_id INTEGER NOT NULL,
    restaurant_id   INTEGER NOT NULL,
    alert_type      TEXT NOT NULL,
    status          INTEGER,
    ok              INTEGER NOT NULL,
    attempts        INTEGER NOT NULL,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_push(db_path=DB_PATH):
    conn = get_conn(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()


def register_device_token(user_id, restaurant_id, apns_token, environment="production", db_path=DB_PATH):
    """Upsert by apns_token — a reinstall or token rotation just re-points the
    existing row (and clears any prior failure count), same idea as
    webhooks.save_webhook()'s upsert-by-restaurant."""
    conn = get_conn(db_path)
    existing = conn.execute("SELECT id FROM device_tokens WHERE apns_token=?", (apns_token,)).fetchone()
    if existing:
        conn.execute(
            """UPDATE device_tokens SET user_id=?, restaurant_id=?, environment=?,
               consecutive_failures=0, disabled_reason=NULL WHERE apns_token=?""",
            (user_id, restaurant_id, environment, apns_token)
        )
    else:
        conn.execute(
            "INSERT INTO device_tokens (user_id, restaurant_id, apns_token, environment) VALUES (?,?,?,?)",
            (user_id, restaurant_id, apns_token, environment)
        )
    conn.commit()
    conn.close()


def remove_device_token(apns_token, db_path=DB_PATH):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM device_tokens WHERE apns_token=?", (apns_token,))
    conn.commit()
    conn.close()


def get_device_tokens(restaurant_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM device_tokens WHERE restaurant_id=?", (restaurant_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


_jwt_cache = {"token": None, "minted_at": 0}
_JWT_MAX_AGE = 50 * 60  # Apple allows up to 60 min; regenerate a bit early


def _provider_jwt():
    """ES256 JWT signed with Apple's .p8 auth key — Apple's own APNs auth
    scheme, unrelated to this app's user sessions. Cached for ~50 minutes
    since Apple explicitly discourages minting a new one on every request."""
    import os
    now = time.time()
    if _jwt_cache["token"] and (now - _jwt_cache["minted_at"]) < _JWT_MAX_AGE:
        return _jwt_cache["token"]
    import jwt as _pyjwt
    key_id = os.getenv("APNS_KEY_ID")
    team_id = os.getenv("APNS_TEAM_ID")
    private_key = os.getenv("APNS_PRIVATE_KEY", "").replace("\\n", "\n")
    token = _pyjwt.encode(
        {"iss": team_id, "iat": int(now)},
        private_key,
        algorithm="ES256",
        headers={"kid": key_id},
    )
    _jwt_cache["token"] = token
    _jwt_cache["minted_at"] = now
    return token


def _apns_host(environment):
    return "api.push.apple.com" if environment == "production" else "api.sandbox.push.apple.com"


def _deliver(device_token_row, alert_type, title, body, data, db_path=DB_PATH):
    import httpx
    bundle_id = __import__("os").getenv("APNS_BUNDLE_ID", "")
    payload = {
        "aps": {"alert": {"title": title, "body": body}, "sound": "default"},
        "cavnar": {"alert_type": alert_type, **(data or {})},
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    url = f"https://{_apns_host(device_token_row['environment'])}/3/device/{device_token_row['apns_token']}"
    headers = {
        "authorization": f"bearer {_provider_jwt()}",
        "apns-topic": bundle_id,
        "apns-push-type": "alert",
        "content-type": "application/json",
    }
    status = 0
    ok = False
    error = None
    permanent_failure = False
    backoffs = [0, 2, 6]
    attempts = 0
    for delay in backoffs:
        if delay:
            time.sleep(delay)
        attempts += 1
        try:
            with httpx.Client(http2=True, timeout=5) as client:
                resp = client.post(url, content=payload_bytes, headers=headers)
            status = resp.status_code
            if status == 200:
                ok = True
                break
            try:
                reason = resp.json().get("reason", "")
            except Exception:
                reason = ""
            error = reason or f"HTTP {status}"
            if reason in _PERMANENT_FAILURE_REASONS:
                permanent_failure = True
                break
        except Exception as e:
            error = str(e)[:300]
            print(f"[push] delivery error (token={device_token_row['apns_token'][:12]}...): {e}")
            status = 0

    try:
        conn = get_conn(db_path)
        conn.execute(
            """INSERT INTO push_deliveries
               (device_token_id, restaurant_id, alert_type, status, ok, attempts, error)
               VALUES (?,?,?,?,?,?,?)""",
            (device_token_row["id"], device_token_row["restaurant_id"], alert_type, status, int(ok), attempts, error)
        )
        if ok:
            conn.execute(
                "UPDATE device_tokens SET last_success_at=datetime('now'), consecutive_failures=0 WHERE id=?",
                (device_token_row["id"],)
            )
        elif permanent_failure:
            # Apple has told us this token will never work again — delete it
            # immediately rather than waiting out _AUTO_DISABLE_AFTER.
            conn.execute("DELETE FROM device_tokens WHERE id=?", (device_token_row["id"],))
        else:
            row = conn.execute(
                "SELECT consecutive_failures FROM device_tokens WHERE id=?", (device_token_row["id"],)
            ).fetchone()
            failures = (row["consecutive_failures"] or 0) + 1 if row else 1
            if failures >= _AUTO_DISABLE_AFTER:
                conn.execute("DELETE FROM device_tokens WHERE id=?", (device_token_row["id"],))
            else:
                conn.execute(
                    "UPDATE device_tokens SET consecutive_failures=? WHERE id=?",
                    (failures, device_token_row["id"])
                )
        conn.commit()
        conn.close()
    except Exception:
        pass
    return {"ok": ok, "status": status, "attempts": attempts, "error": error}


def fire_push(restaurant_id, alert_type, title, body, data=None, db_path=DB_PATH):
    """Fire push to every device registered for this restaurant, in a
    background thread per device — never blocks the caller. Mirrors
    webhooks.fire_webhook()'s fire-and-forget shape."""
    try:
        tokens = get_device_tokens(restaurant_id, db_path)
        for token_row in tokens:
            t = threading.Thread(
                target=_deliver, args=(token_row, alert_type, title, body, data, db_path), daemon=True
            )
            t.start()
    except Exception as e:
        print(f"[push] fire_push error ({alert_type}, rid={restaurant_id}): {e}")
