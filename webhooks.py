"""
webhooks.py — Outbound webhook delivery for client events.

Events fired:
  review.received   — new review fetched for a restaurant
  alert.fired       — any alert trigger fires
  response.approved — client approves a draft response

Every event is written to webhook_outbox BEFORE it is handed to the delivery
pool, with its own event id (#75): the pool lives in process memory, so a
deploy mid-delivery used to lose the event without a trace, and an endpoint
had no id to de-duplicate a retry by. The row is marked delivered or failed
from the delivery's own result; reap_webhook_outbox re-drives rows a restart
left behind. Delivery goes through net_safety — resolved once, connected to
the vetted address, redirects never followed (#156).
"""
import hashlib, hmac, json, os, threading, time, uuid
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
    non-public address space. One rule with delivery: net_safety.vet — and
    delivery vets again, connecting to the address it vetted (#156)."""
    import net_safety
    try:
        net_safety.vet(url)
    except net_safety.UnsafeURL as e:
        raise InvalidWebhookURL(str(e))

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
-- ops.prune_ledgers deletes by created_at (DATA-40).
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_created ON webhook_deliveries(created_at);
-- Every event, written before it is handed to the pool (#75). state:
-- queued -> delivering -> delivered | failed; 'expired' when a restart left
-- it behind for longer than it is worth sending.
CREATE TABLE IF NOT EXISTS webhook_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL UNIQUE,
    restaurant_id   INTEGER NOT NULL,
    event_type      TEXT NOT NULL,
    payload_json    TEXT,
    state           TEXT NOT NULL DEFAULT 'queued',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT,
    done_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_webhook_outbox_state ON webhook_outbox(state, created_at);
CREATE INDEX IF NOT EXISTS idx_webhook_outbox_restaurant ON webhook_outbox(restaurant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_webhook_outbox_created ON webhook_outbox(created_at);
"""

def init_webhooks(db_path=DB_PATH):
    conn = get_conn(db_path)
    conn.executescript(_SCHEMA)
    # Migration for webhooks rows created before consecutive_failures/disabled_reason existed.
    for col_sql in (
        "ALTER TABLE webhooks ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE webhooks ADD COLUMN disabled_reason TEXT",
        # The last delivery that SUCCEEDED (#156): last_fired_at is the last
        # attempt, and the console showed it as "last success".
        "ALTER TABLE webhooks ADD COLUMN last_success_at TEXT",
        # Which event a delivery row was (#75): the id the endpoint received.
        "ALTER TABLE webhook_deliveries ADD COLUMN event_id TEXT",
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


def new_event_id() -> str:
    return "evt_" + uuid.uuid4().hex


def _deliver(webhook, event_type, data, db_path=DB_PATH, event_id=None):
    """POST one event to the webhook's URL, with retry, and record it.

    The payload carries `id` — the event id, the same on every retry of this
    event and in the X-Cavnar-Event-Id header — so an endpoint can drop a
    duplicate (#75). The request goes through net_safety (#156): resolved
    once, connected to the vetted address, no redirect followed; a URL that
    now resolves somewhere private is refused and not retried."""
    import net_safety
    event_id = event_id or new_event_id()
    payload_str = json.dumps({
        "id":            event_id,
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
            resp = net_safety.safe_post(
                webhook["url"],
                data=payload_str,
                headers={
                    "Content-Type":       "application/json",
                    "X-Cavnar-Signature": sig,
                    "X-Cavnar-Event":     event_type,
                    "X-Cavnar-Event-Id":  event_id,
                    "User-Agent":         "Cavnar-AI/1.0",
                },
                timeout=5,
            )
            status = resp.status_code
            if resp.ok:
                ok = True
                break
            if 300 <= int(status or 0) < 400:
                error = f"redirected (HTTP {status}); redirects are not followed"
                break
        except net_safety.UnsafeURL as e:
            error = f"refused: {e}"[:300]
            status = 0
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
        try:
            conn.execute(
                """INSERT INTO webhook_deliveries
                   (webhook_id, restaurant_id, event_type, status, ok, attempts, error, event_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (webhook["id"], webhook["restaurant_id"], event_type, status, int(ok), attempts, error, event_id)
            )
        except Exception:
            conn.execute(
                """INSERT INTO webhook_deliveries
                   (webhook_id, restaurant_id, event_type, status, ok, attempts, error)
                   VALUES (?,?,?,?,?,?,?)""",
                (webhook["id"], webhook["restaurant_id"], event_type, status, int(ok), attempts, error)
            )
        if ok:
            # last_success_at is added at boot (init_webhooks), as
            # consecutive_failures is: one write, not a second one whose
            # failure was swallowed (scripts/check_silent_handlers.py).
            conn.execute("UPDATE webhooks SET consecutive_failures=0, last_success_at=datetime('now') WHERE id=?",
                         (webhook["id"],))
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
    except Exception as e:
        # consecutive_failures is what auto-disables a broken endpoint. If
        # this write vanishes, a dead webhook is retried indefinitely and
        # nothing in the admin console ever says so.
        try:
            import ops
            ops.capture(e, job="webhook_delivery_log", context=f"event_type={event_type}")
        except Exception:
            pass
    return {"ok": ok, "status": status, "attempts": attempts, "error": error}


# Same bounded pool as push.fire_push, for the same reason: _deliver retries
# with backoff, and a scheduler pass that fires one event per restaurant used
# to start one thread per restaurant, all at once.
_MAX_WEBHOOK_WORKERS = int(os.getenv("WEBHOOK_MAX_WORKERS", "4"))
# How many events may wait in memory for the pool (#75: the executor's queue
# was unbounded). Past it an event stays 'queued' in webhook_outbox and
# reap_webhook_outbox sends it — nothing is dropped, and memory is bounded.
_MAX_WEBHOOK_QUEUED = int(os.getenv("WEBHOOK_MAX_QUEUED", "500"))

_executor = None
_executor_lock = threading.Lock()
_in_pool = 0


def _webhook_executor():
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                from concurrent.futures import ThreadPoolExecutor
                _executor = ThreadPoolExecutor(
                    max_workers=_MAX_WEBHOOK_WORKERS, thread_name_prefix="webhook"
                )
    return _executor


def _outbox_write(restaurant_id, event_type, data, db_path=DB_PATH):
    """The event's durable row, written before any delivery; returns
    (outbox id, event id), or (None, event id) when it could not be written
    (the event is then delivered from memory, as before)."""
    event_id = new_event_id()
    try:
        conn = get_conn(db_path)
        try:
            cur = conn.execute(
                "INSERT INTO webhook_outbox (event_id, restaurant_id, event_type, payload_json, updated_at) "
                "VALUES (?,?,?,?, datetime('now'))",
                (event_id, restaurant_id, event_type, json.dumps(data, default=str)[:20000]))
            conn.commit()
            return cur.lastrowid, event_id
        finally:
            conn.close()
    except Exception as e:
        print(f"[webhook] outbox write failed ({event_type}, rid={restaurant_id}): {e}")
        return None, event_id


def _outbox_claim(outbox_id, db_path=DB_PATH) -> bool:
    """queued -> delivering, by exactly one runner."""
    try:
        conn = get_conn(db_path)
        try:
            won = conn.execute("UPDATE webhook_outbox SET state='delivering', attempts=attempts+1, "
                               "updated_at=datetime('now') WHERE id=? AND state='queued'",
                               (outbox_id,)).rowcount == 1
            conn.commit()
            return won
        finally:
            conn.close()
    except Exception:
        return True                      # deliver rather than lose it


def _outbox_finish(outbox_id, state, error=None, db_path=DB_PATH):
    if not outbox_id:
        return
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE webhook_outbox SET state=?, last_error=?, updated_at=datetime('now'), "
                         "done_at=CASE WHEN ? IN ('delivered','failed','expired','skipped') THEN datetime('now') "
                         "ELSE done_at END WHERE id=?", (state, (error or None) and str(error)[:300], state, outbox_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[webhook] outbox update failed ({outbox_id}): {e}")


def _run_outbox(outbox_id, restaurant_id, event_type, data, event_id, db_path=DB_PATH, force=False):
    """Deliver one outbox event from the pool; its row says what happened."""
    global _in_pool
    try:
        if outbox_id and not _outbox_claim(outbox_id, db_path):
            return None                  # another runner has it
        webhook = get_webhook(restaurant_id, db_path)
        subscribed = json.loads((webhook or {}).get("events") or "[]")
        if not webhook or (not force and event_type not in subscribed):
            _outbox_finish(outbox_id, "skipped", "no active webhook subscribed to it", db_path)
            return None
        res = _deliver(webhook, event_type, data, db_path, event_id=event_id)
        _outbox_finish(outbox_id, "delivered" if res.get("ok") else "failed", res.get("error")
                       or (None if res.get("ok") else f"HTTP {res.get('status')}"), db_path)
        return res
    except Exception as e:
        _outbox_finish(outbox_id, "failed", e, db_path)
        print(f"[webhook] outbox delivery {outbox_id} failed: {e}")
        return None
    finally:
        with _executor_lock:
            _in_pool -= 1


def _submit(outbox_id, restaurant_id, event_type, data, event_id, db_path, force=False) -> bool:
    """Hand an event to the pool, unless the pool's queue is full — then it
    waits in webhook_outbox for the reaper. True when submitted."""
    global _in_pool
    with _executor_lock:
        if outbox_id and _in_pool >= _MAX_WEBHOOK_QUEUED:
            return False
        _in_pool += 1
    try:
        _webhook_executor().submit(_run_outbox, outbox_id, restaurant_id, event_type, data, event_id, db_path, force)
        return True
    except Exception:
        with _executor_lock:
            _in_pool -= 1
        raise


def fire_webhook(restaurant_id, event_type, data, db_path=DB_PATH):
    """Fire webhook on a bounded background pool — never blocks the caller.
    The event is written to webhook_outbox first (#75); returns its event id,
    or None when no active webhook is subscribed to it."""
    try:
        webhook = get_webhook(restaurant_id, db_path)
        if not webhook:
            return None
        subscribed = json.loads(webhook.get("events") or "[]")
        if event_type not in subscribed:
            return None
        outbox_id, event_id = _outbox_write(restaurant_id, event_type, data, db_path)
        _submit(outbox_id, restaurant_id, event_type, data, event_id, db_path)
        return event_id
    except Exception as e:
        print(f"[webhook] fire_webhook error ({event_type}, rid={restaurant_id}): {e}")
        return None


def queue_test_delivery(restaurant_id, db_path=DB_PATH):
    """The owner's "Send test": one "test" event through the same outbox and
    pool as every real event — asynchronous, so a slow endpoint no longer
    holds a request thread for three attempts and their backoff (#156).
    Returns the event id, or None with no active webhook. The result lands
    in webhook_deliveries (/api/webhook/deliveries)."""
    if not get_webhook(restaurant_id, db_path):
        return None
    data = {"message": "This is a test webhook from Cavnar AI", "restaurant_id": restaurant_id}
    outbox_id, event_id = _outbox_write(restaurant_id, "test", data, db_path)
    _submit(outbox_id, restaurant_id, "test", data, event_id, db_path, force=True)
    return event_id


# A delivery a restart left 'delivering' is re-driven after this long; an
# event still waiting after WEBHOOK_OUTBOX_MAX_AGE_HOURS is not worth sending.
WEBHOOK_OUTBOX_STALE_MINUTES = 10
WEBHOOK_OUTBOX_MAX_AGE_HOURS = int(os.getenv("WEBHOOK_OUTBOX_MAX_AGE_HOURS", "24"))


def reap_webhook_outbox(db_path=DB_PATH, limit=200) -> dict:
    """Re-drive what a restart or a full pool left behind (#75): 'delivering'
    rows older than WEBHOOK_OUTBOX_STALE_MINUTES go back to 'queued', rows
    older than WEBHOOK_OUTBOX_MAX_AGE_HOURS expire, and up to `limit` queued
    rows are handed to the pool. Returns {"requeued", "expired", "submitted"}.
    Scheduled by the integration wave (a minute duty)."""
    out = {"requeued": 0, "expired": 0, "submitted": 0}
    conn = get_conn(db_path)
    try:
        out["expired"] = conn.execute(
            "UPDATE webhook_outbox SET state='expired', done_at=datetime('now'), updated_at=datetime('now'), "
            "last_error=COALESCE(last_error, 'not delivered in time') WHERE state IN ('queued','delivering') "
            "AND created_at < datetime('now', ?)", (f"-{WEBHOOK_OUTBOX_MAX_AGE_HOURS} hours",)).rowcount
        out["requeued"] = conn.execute(
            "UPDATE webhook_outbox SET state='queued', updated_at=datetime('now') WHERE state='delivering' "
            "AND COALESCE(updated_at, created_at) < datetime('now', ?)",
            (f"-{WEBHOOK_OUTBOX_STALE_MINUTES} minutes",)).rowcount
        conn.commit()
        rows = conn.execute(
            "SELECT id, event_id, restaurant_id, event_type, payload_json FROM webhook_outbox WHERE state='queued' "
            "AND COALESCE(updated_at, created_at) < datetime('now', '-1 minutes') ORDER BY id LIMIT ?",
            (int(limit),)).fetchall()
    finally:
        conn.close()
    for r in rows:
        try:
            data = json.loads(r["payload_json"] or "null")
        except (TypeError, ValueError):
            data = None
        if _submit(r["id"], r["restaurant_id"], r["event_type"], data, r["event_id"], db_path,
                   force=r["event_type"] == "test"):
            out["submitted"] += 1
        else:
            break
    return out


def outbox_counts(db_path=DB_PATH) -> dict:
    """{state: n, "oldest_queued_at"} — the console's Queues panel (#75)."""
    conn = get_conn(db_path)
    try:
        by_state = {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, COUNT(*) AS n FROM webhook_outbox GROUP BY state").fetchall()}
        oldest = conn.execute("SELECT MIN(created_at) FROM webhook_outbox WHERE state IN ('queued','delivering')"
                              ).fetchone()[0]
        failed_24h = conn.execute("SELECT COUNT(*) FROM webhook_outbox WHERE state='failed' "
                                  "AND created_at >= datetime('now','-1 day')").fetchone()[0]
    finally:
        conn.close()
    return {"by_state": by_state, "oldest_pending_at": oldest, "failed_24h": failed_24h}
