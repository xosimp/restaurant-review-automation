"""
webhooks.py — Outbound webhook delivery for client events.

Events fired:
  review.received   — new review fetched for a restaurant
  alert.fired       — any alert trigger fires
  response.approved — client approves a draft response
"""
import hashlib, hmac, json, threading, time
import ipaddress, socket
from urllib.parse import urlparse
from datetime import datetime, timezone
from models import get_conn, DB_PATH

# Auto-disable a webhook after this many consecutive failed deliveries —
# previously a broken endpoint (dead Zapier hook, expired URL) just kept
# firing into the void forever with no visibility and no way to stop it
# short of the client manually removing it.
_AUTO_DISABLE_AFTER = 10


class InvalidWebhookURL(ValueError):
    pass


def _validate_webhook_url(url):
    """Block SSRF: webhook URLs are client-supplied and the server will POST to
    whatever's configured, so refuse anything that resolves to loopback, private,
    link-local (incl. cloud metadata endpoints like 169.254.169.254), or otherwise
    non-public address space."""
    try:
        parsed = urlparse(url)
    except Exception:
        raise InvalidWebhookURL("Could not parse URL")
    if parsed.scheme not in ("http", "https"):
        raise InvalidWebhookURL("URL must start with http:// or https://")
    host = parsed.hostname
    if not host:
        raise InvalidWebhookURL("URL must include a host")
    if host.lower() in ("localhost", "metadata.google.internal"):
        raise InvalidWebhookURL("That host isn't allowed")
    try:
        # Resolve every A/AAAA record — block if ANY resolves to non-public space,
        # since DNS can return multiple addresses and an attacker only needs one.
        infos = socket.getaddrinfo(host, None)
    except Exception:
        raise InvalidWebhookURL("Could not resolve host")
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except Exception:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise InvalidWebhookURL("That URL points to a private or internal address")

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
    last_status     INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    disabled_reason TEXT
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_id      INTEGER NOT NULL,
    restaurant_id   INTEGER NOT NULL,
    event_type      TEXT NOT NULL,
    status          INTEGER,
    ok              INTEGER NOT NULL,
    attempts        INTEGER NOT NULL,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

def init_webhooks(db_path=DB_PATH):
    conn = get_conn(db_path)
    conn.executescript(_SCHEMA)
    # Migration for webhooks rows created before consecutive_failures/disabled_reason existed.
    for col_sql in (
        "ALTER TABLE webhooks ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE webhooks ADD COLUMN disabled_reason TEXT",
    ):
        try:
            conn.execute(col_sql)
        except Exception:
            pass
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
    _validate_webhook_url(url)
    conn = get_conn(db_path)
    existing = conn.execute(
        "SELECT id, secret FROM webhooks WHERE restaurant_id=? LIMIT 1",
        (restaurant_id,)
    ).fetchone()
    if existing:
        # Saving/editing a webhook re-activates it and clears any auto-disable —
        # a client updating the URL is explicitly trying to fix it.
        conn.execute(
            "UPDATE webhooks SET url=?, events=?, is_active=1, consecutive_failures=0, disabled_reason=NULL WHERE id=?",
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


def reactivate_webhook(restaurant_id, db_path=DB_PATH):
    """Manually clear an auto-disable and resume delivery — the client saw
    the "this webhook looks broken" banner, fixed whatever was wrong on
    their end (Zapier, Slack, etc.), and wants to try again without having
    to re-enter the URL and secret from scratch."""
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE webhooks SET is_active=1, consecutive_failures=0, disabled_reason=NULL WHERE restaurant_id=?",
        (restaurant_id,)
    )
    conn.commit()
    conn.close()


def get_webhook_deliveries(restaurant_id, limit=20, db_path=DB_PATH):
    conn = get_conn(db_path)
    rows = conn.execute(
        """SELECT event_type, status, ok, attempts, error, created_at
           FROM webhook_deliveries WHERE restaurant_id=?
           ORDER BY id DESC LIMIT ?""",
        (restaurant_id, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
    ok     = False
    error  = None
    # 3 attempts with exponential backoff (0s, 2s, 6s) instead of 2 back-to-back
    # tries — gives a flaky-but-recovering endpoint (a Zapier hook cold-starting,
    # a brief Slack outage) a real chance instead of failing twice in ~10ms.
    backoffs = [0, 2, 6]
    attempts = 0
    for i, delay in enumerate(backoffs):
        if delay:
            time.sleep(delay)
        attempts += 1
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
                ok = True
                break
        except Exception as e:
            error = str(e)[:300]
            print(f"[webhook] delivery error ({webhook.get('url')}): {e}")
            status = 0
    try:
        conn = get_conn(db_path)
        conn.execute(
            "UPDATE webhooks SET last_fired_at=datetime('now'), last_status=? WHERE id=?",
            (status, webhook["id"])
        )
        conn.execute(
            """INSERT INTO webhook_deliveries
               (webhook_id, restaurant_id, event_type, status, ok, attempts, error)
               VALUES (?,?,?,?,?,?,?)""",
            (webhook["id"], webhook["restaurant_id"], event_type, status, int(ok), attempts, error)
        )
        if ok:
            conn.execute("UPDATE webhooks SET consecutive_failures=0 WHERE id=?", (webhook["id"],))
        else:
            row = conn.execute("SELECT consecutive_failures FROM webhooks WHERE id=?", (webhook["id"],)).fetchone()
            failures = (row["consecutive_failures"] or 0) + 1 if row else 1
            if failures >= _AUTO_DISABLE_AFTER:
                conn.execute(
                    "UPDATE webhooks SET consecutive_failures=?, is_active=0, disabled_reason=? WHERE id=?",
                    (failures, f"Auto-disabled after {failures} consecutive failed deliveries", webhook["id"])
                )
            else:
                conn.execute("UPDATE webhooks SET consecutive_failures=? WHERE id=?", (failures, webhook["id"]))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return {"ok": ok, "status": status, "attempts": attempts, "error": error}


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
    except Exception as e:
        print(f"[webhook] fire_webhook error ({event_type}, rid={restaurant_id}): {e}")
