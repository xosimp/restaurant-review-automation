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
import os
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


def _collapse_id(restaurant_id, alert_type, data) -> str:
    """A stable identifier for one logical alert, for APNs' apns-collapse-id.

    The retry loop below treats a client-side timeout as a failure and sends
    again, twice more — but a timeout often means Apple took the push and the
    response was what got lost. Without this header those become two or three
    separate banners on the same phone for the same event.

    It has to be stable across the retries of ONE alert and different between
    two alerts, so the review id is in the key: two 1-star reviews arriving in
    the same fetch are two things the owner needs to see, not one. Alerts with
    no review behind them (labor over target, food waste) get the local date
    instead, which also means the daily job cannot stack duplicates if it runs
    twice. Apple caps this at 64 bytes.
    """
    parts = [str(restaurant_id), str(alert_type or "alert")]
    review_id = (data or {}).get("review_id")
    if review_id:
        parts.append(f"r{review_id}")
    else:
        parts.append(datetime.now(timezone.utc).strftime("%Y%m%d"))
    key = "-".join(parts)
    if len(key.encode()) > 64:
        import hashlib
        key = hashlib.sha256(key.encode()).hexdigest()[:32]
    return key


def _deliver(device_token_row, alert_type, title, body, data, db_path=DB_PATH):
    import httpx
    bundle_id = __import__("os").getenv("APNS_BUNDLE_ID", "")
    aps = {"alert": {"title": title, "body": body}, "sound": "default"}
    try:
        rid = device_token_row["restaurant_id"] if "restaurant_id" in device_token_row.keys() else None
        if rid:
            _c = get_conn(db_path)
            _r = _c.execute("SELECT push_sound, alert_health_bypass_quiet FROM restaurants WHERE id=?", (rid,)).fetchone()
            _c.close()
            if _r and _r["push_sound"] == 0:
                aps.pop("sound", None)
            # Health/safety mentions that opt out of quiet hours also break
            # through the phone's Focus modes (Time Sensitive — needs the
            # matching entitlement on the app, see project.yml).
            if alert_type == "health" and _r and _r["alert_health_bypass_quiet"]:
                aps["interruption-level"] = "time-sensitive"
    except Exception:
        pass
    payload = {
        "aps": aps,
        "cavnar": {"alert_type": alert_type, **(data or {})},
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    url = f"https://{_apns_host(device_token_row['environment'])}/3/device/{device_token_row['apns_token']}"
    headers = {
        "authorization": f"bearer {_provider_jwt()}",
        "apns-topic": bundle_id,
        "apns-push-type": "alert",
        "apns-collapse-id": _collapse_id(device_token_row.get("restaurant_id"), alert_type, data),
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
    except Exception as e:
        # This bookkeeping is what advances consecutive_failures, and that
        # counter is the only thing that ever deletes a dead token. Losing it
        # silently means retrying a token that will never work, forever.
        try:
            import ops
            ops.capture(e, job="push_delivery_log", context=f"alert_type={alert_type}")
        except Exception:
            pass
    return {"ok": ok, "status": status, "attempts": attempts, "error": error}


# Deliveries run on a small shared pool rather than a thread per device.
# _deliver retries with backoff, so a thread lives for seconds, and the
# scheduler fires push for every restaurant in one pass — 50 restaurants
# times a couple of devices each used to mean a hundred-plus concurrent
# threads on a small Railway container, every one of them holding an HTTP/2
# connection and writing to the same SQLite file.
_MAX_PUSH_WORKERS = int(os.getenv("PUSH_MAX_WORKERS", "4"))
# A ceiling on how far behind the pool may fall before deliveries are
# dropped. Dropping an alert is bad; exhausting the container's memory takes
# the whole app down with it, so there has to be a number — generous enough
# that a normal digest sweep never reaches it.
_MAX_PUSH_QUEUED = int(os.getenv("PUSH_MAX_QUEUED", "500"))

_executor = None
_executor_lock = threading.Lock()
_queued = 0


def _push_executor():
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                from concurrent.futures import ThreadPoolExecutor
                _executor = ThreadPoolExecutor(
                    max_workers=_MAX_PUSH_WORKERS, thread_name_prefix="push"
                )
    return _executor


def _run_delivery(token_row, alert_type, title, body, data, db_path):
    global _queued
    try:
        _deliver(token_row, alert_type, title, body, data, db_path)
    finally:
        with _executor_lock:
            _queued -= 1


def fire_push(restaurant_id, alert_type, title, body, data=None, db_path=DB_PATH):
    """Fire push to every device registered for this restaurant, on a bounded
    background pool — never blocks the caller. Mirrors webhooks.fire_webhook()'s
    fire-and-forget shape."""
    global _queued
    try:
        tokens = get_device_tokens(restaurant_id, db_path)
        for token_row in tokens:
            with _executor_lock:
                if _queued >= _MAX_PUSH_QUEUED:
                    print(f"[push] queue full ({_queued}) — dropping {alert_type} for rid={restaurant_id}")
                    try:
                        import ops
                        ops.capture(RuntimeError(f"push queue full at {_queued}"),
                                    job="fire_push", context=f"rid={restaurant_id} {alert_type}")
                    except Exception:
                        pass
                    break
                _queued += 1
            _push_executor().submit(
                _run_delivery, token_row, alert_type, title, body, data, db_path
            )
    except Exception as e:
        print(f"[push] fire_push error ({alert_type}, rid={restaurant_id}): {e}")
