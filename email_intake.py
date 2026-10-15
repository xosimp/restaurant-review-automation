"""
email_intake.py — files that reach Cavnar AI by email, received through
Resend's Receiving (the `email.received` event on the one signed
/webhooks/resend endpoint).

An intake ADDRESS is a row in `email_intake_routes`: the address, the
restaurant it belongs to and the kind of file it takes ('fintech' — the
distributor invoices EJ's pays through Fintech, read by fintech_invoices).
Mail to an address with no route is recorded and never fetched: the
address is the key, so make one unguessable (`new_route` adds a random
part), and a route is retired by `active=0`, never by reuse.

The webhook only records the email and queues it (`job_queue` kind
'email_intake', one row per email by its id), so Resend gets its 200 at
once and a failed fetch or parse is retried by the queue; the work —
fetching the email and its attachments from Resend's API and handing each
file to its reader — runs in the job. Received again (Resend retries, a
forward sent twice) it is the same row and the same job.

Gmail asks a forwarding address to confirm before it forwards anything:
that confirmation lands here like any mail and is kept as
'gmail_confirmation' with its subject (the code is in it), so it can be
confirmed from the Resend dashboard's Receiving tab or from this row.
"""
import json
import os
import secrets
from datetime import datetime, timezone

import models as _models

RESEND_API = "https://api.resend.com"
HTTP_TIMEOUT = 20
# Attachments larger than this are recorded and skipped: a day's invoices
# are tens of kilobytes, a year's export a few megabytes.
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024

# kind -> "module:function" taking (restaurant_id, filename, content_bytes,
# intake_id) and returning {"status", "detail", ...}. Reached by name.
READERS = {
    "fintech": "fintech_invoices:ingest_file",
    # Back Office's "inventory valuation by category" report into bar_counts
    # (source 'backoffice'). UNVERIFIED: built without a sample — the expected
    # headers are in bar.counts and docs/plans/BACK_OFFICE_INTEGRATION.md.
    "backoffice_valuation": "bar.counts:read_valuation_file",
}


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    return _models.get_conn(db_path) if db_path else _models.get_conn()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def init_email_intake(db_path=None):
    """The routes and the received-email ledger. Boot only (init_db)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS email_intake_routes (
            address TEXT PRIMARY KEY,
            restaurant_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            note TEXT,
            created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS email_intake (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id TEXT NOT NULL UNIQUE,
            address TEXT,
            restaurant_id INTEGER,
            kind TEXT,
            sender TEXT,
            subject TEXT,
            status TEXT NOT NULL,
            detail TEXT,
            files INTEGER NOT NULL DEFAULT 0,
            received_at TEXT NOT NULL,
            processed_at TEXT)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_intake_rid ON email_intake(restaurant_id, received_at)")
        conn.commit()
    finally:
        conn.close()


def new_route(restaurant_id, kind, domain, note=None, db_path=None):
    """Add an intake address `<kind>-<random>@<domain>` for a restaurant and
    return it. The random part is what keeps strangers' mail out."""
    if kind not in READERS:
        raise ValueError(f"unknown intake kind {kind!r}")
    address = f"{kind}-{secrets.token_hex(5)}@{domain}".lower()
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO email_intake_routes (address, restaurant_id, kind, active, note, created_at) "
                     "VALUES (?,?,?,1,?,?)", (address, int(restaurant_id), kind, note, _now()))
        conn.commit()
    finally:
        conn.close()
    return address


def _addresses(value):
    """Lowercased bare addresses from a Resend `to` field (a list or one
    string; entries may read 'Name <addr>')."""
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        s = str(item or "").strip()
        if "<" in s and ">" in s:
            s = s[s.index("<") + 1:s.index(">")]
        if s:
            out.append(s.strip().lower())
    return out


def _route_for(addresses, db_path=None):
    if not addresses:
        return None
    conn = get_conn(db_path)
    try:
        for addr in addresses:
            row = conn.execute("SELECT address, restaurant_id, kind FROM email_intake_routes "
                               "WHERE address=? AND active=1", (addr,)).fetchone()
            if row:
                return dict(row)
    finally:
        conn.close()
    return None


def receive(data, db_path=None):
    """The `email.received` webhook's data: record the email once and queue
    it when its address is a route. Returns the ledger status. Never
    raises: the webhook answers 200 whatever happens here."""
    try:
        email_id = str((data or {}).get("email_id") or (data or {}).get("id") or "").strip()
        if not email_id:
            return "no_id"
        route = _route_for(_addresses((data or {}).get("to")), db_path)
        status = "queued" if route else "no_route"
        conn = get_conn(db_path)
        try:
            cur = conn.execute(
                "INSERT INTO email_intake (email_id, address, restaurant_id, kind, sender, subject, status, "
                "received_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(email_id) DO NOTHING",
                (email_id, (route or {}).get("address") or ",".join(_addresses(data.get("to")))[:300],
                 (route or {}).get("restaurant_id"), (route or {}).get("kind"),
                 str(data.get("from") or "")[:300], str(data.get("subject") or "")[:500], status, _now()))
            conn.commit()
            fresh = (cur.rowcount or 0) > 0
        finally:
            conn.close()
        if route and fresh:
            import job_queue
            job_queue.enqueue("email_intake", {"email_id": email_id},
                              restaurant_id=route["restaurant_id"],
                              idempotency_key=f"email_intake:{email_id}", db_path=db_path)
        return status if fresh else "duplicate"
    except Exception as e:
        print(f"[email_intake] receive failed: {e}")
        return "error"


# ── the job ────────────────────────────────────────────────────────────────

def _api_get(path):
    import requests
    key = os.getenv("RESEND_API_KEY", "")
    if not key:
        raise RuntimeError("RESEND_API_KEY is not set")
    r = requests.get(f"{RESEND_API}{path}", headers={"Authorization": f"Bearer {key}"}, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Resend {path.split('/')[2]} answered {r.status_code}")
    return r.json()


def _download(url):
    import requests
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"attachment download answered {r.status_code}")
    if len(r.content) > MAX_ATTACHMENT_BYTES:
        raise ValueError("attachment too large")
    return r.content


def _set(email_id, db_path=None, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    conn = get_conn(db_path)
    try:
        conn.execute(f"UPDATE email_intake SET {cols} WHERE email_id=?", (*fields.values(), email_id))
        conn.commit()
    finally:
        conn.close()


def _row(email_id, db_path=None):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM email_intake WHERE email_id=?", (email_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _reader(kind):
    import importlib
    mod, fn = READERS[kind].split(":")
    return getattr(importlib.import_module(mod), fn)


def process(email_id, db_path=None):
    """Fetch one recorded email and hand each file to its route's reader.
    Raises on a fetch failure, so the queue retries it."""
    row = _row(email_id, db_path)
    if not row or not row.get("kind"):
        return {"status": "skipped"}
    email = _api_get(f"/emails/receiving/{email_id}")
    sender = str(email.get("from") or row.get("sender") or "")
    subject = str(email.get("subject") or row.get("subject") or "")
    listing = _api_get(f"/emails/receiving/{email_id}/attachments")
    attachments = listing.get("data") or []
    reader = _reader(row["kind"])
    results = []
    for att in attachments:
        name = att.get("filename") or "attachment"
        size = att.get("size") or 0
        if size and size > MAX_ATTACHMENT_BYTES:
            results.append({"file": name, "status": "too_large"})
            continue
        url = att.get("download_url")
        if not url:
            detail = _api_get(f"/emails/receiving/{email_id}/attachments/{att.get('id')}")
            url = detail.get("download_url")
        content = _download(url)
        res = reader(row["restaurant_id"], name, content, row["id"], db_path=db_path) or {}
        results.append({"file": name, **res})
    if not attachments:
        text = email.get("text") or ""
        if "forwarding-noreply@google.com" in sender.lower():
            _set(email_id, db_path, status="gmail_confirmation", sender=sender[:300], subject=subject[:500],
                 detail="Gmail forwarding confirmation: confirm it from the Resend dashboard's Receiving tab",
                 processed_at=_now())
            return {"status": "gmail_confirmation"}
        if text.strip():
            res = reader(row["restaurant_id"], "body.txt", text.encode("utf-8"), row["id"], db_path=db_path) or {}
            results.append({"file": "(email body)", **res})
    ok = [r for r in results if r.get("status") == "stored"]
    status = "done" if ok else ("ignored" if results else "no_files")
    _set(email_id, db_path, status=status, sender=sender[:300], subject=subject[:500], files=len(ok),
         detail=json.dumps(results, default=str)[:4000], processed_at=_now())
    return {"status": status, "files": results}


def run_queued(job):
    """job_queue handler for kind 'email_intake'."""
    email_id = (job.get("payload") or {}).get("email_id")
    try:
        return process(email_id)
    except Exception as e:
        try:
            _set(email_id, status="failed", detail=str(e)[:500], processed_at=_now())
        except Exception:
            pass
        raise


def recent(restaurant_id=None, limit=20, db_path=None):
    """The latest received emails, newest first (ops reads)."""
    conn = get_conn(db_path)
    try:
        if restaurant_id:
            rows = conn.execute("SELECT * FROM email_intake WHERE restaurant_id=? ORDER BY id DESC LIMIT ?",
                                (int(restaurant_id), int(limit))).fetchall()
        else:
            rows = conn.execute("SELECT * FROM email_intake ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]

