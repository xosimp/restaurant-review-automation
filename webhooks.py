"""
webhooks.py — Outbound webhook delivery for client events.

Events fired:
  review.received   — new review fetched for a restaurant
  alert.fired       — any alert trigger fires
  response.approved — client approves a draft response
"""
import hashlib, hmac, json, threading
from datetime import datetime, timezone
from models import get_conn, DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhooks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    url             TEXT NOT NULL,
    secret          TEXT NOT NULL,
    events          TEXT NOT NULL DEFAULT '["review.received","alert.fired","response.approved"]',
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_fired_at   TEXT,
    last_status     INTEGER
);
"""

def init_webhooks(db_path=DB_PATH):
    conn = get_conn(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()


def get_webhook(restaurant_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM webhooks WHERE restaurant_id=? AND is_active=1 LIMIT 1",
        (restaurant_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def save_webhook(restaurant_id, url, events, db_path=DB_PATH):
    import secrets
    conn = get_conn(db_path)
    existing = conn.execute(
        "SELECT id, secret FROM webhooks WHERE restaurant_id=? LIMIT 1",
        (restaurant_id,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE webhooks SET url=?, events=?, is_active=1 WHERE id=?",
            (url, json.dumps(events), existing["id"])
        )
        secret = existing["secret"]
    else:
        secret = "whsec_" + secrets.token_hex(24)
        conn.execute(
            "INSERT INTO webhooks (restaurant_id, url, secret, events) VALUES (?,?,?,?)",
            (restaurant_id, url, secret, json.dumps(events))
        )
    conn.commit()
    conn.close()
    return secret


def delete_webhook(restaurant_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    conn.execute("UPDATE webhooks SET is_active=0 WHERE restaurant_id=?", (restaurant_id,))
    conn.commit()
    conn.close()


def _sign(secret, payload_str):
    return "sha256=" + hmac.new(secret.encode(), payload_str.encode(), hashlib.sha256).hexdigest()


def _deliver(webhook, event_type, data, db_path=DB_PATH):
    import requests as _req
    payload_str = json.dumps({
        "event":         event_type,
        "restaurant_id": webhook["restaurant_id"],
        "timestamp":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data":          data,
    }, separators=(",", ":"))
    sig    = _sign(webhook["secret"], payload_str)
    status = 0
    for _ in range(2):
        try:
            resp = _req.post(
                webhook["url"],
                data=payload_str,
                headers={
                    "Content-Type":       "application/json",
                    "X-Cavnar-Signature": sig,
                    "X-Cavnar-Event":     event_type,
                    "User-Agent":         "Cavnar-AI/1.0",
                },
                timeout=5,
            )
            status = resp.status_code
            if resp.ok:
                break
        except Exception:
            status = 0
    try:
        conn = get_conn(db_path)
        conn.execute(
            "UPDATE webhooks SET last_fired_at=datetime('now'), last_status=? WHERE id=?",
            (status, webhook["id"])
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def fire_webhook(restaurant_id, event_type, data, db_path=DB_PATH):
    """Fire webhook in background thread — never blocks the caller."""
    try:
        webhook = get_webhook(restaurant_id, db_path)
        if not webhook:
            return
        subscribed = json.loads(webhook.get("events") or "[]")
        if event_type not in subscribed:
            return
        t = threading.Thread(target=_deliver, args=(webhook, event_type, data, db_path), daemon=True)
        t.start()
    except Exception:
        pass
