"""
email_intake.py — files that reach Cavnar AI by email, received through
Resend's Receiving (the `email.received` event on the one signed
/webhooks/resend endpoint).

An intake ADDRESS is a row in `email_intake_routes`: the address, the
restaurant it belongs to, the kind of file it takes ('fintech' — the
distributor invoices EJ's pays through Fintech, read by fintech_invoices)
and the SENDERS it takes mail from (`allowed_senders`: whole addresses, or
"@domain" for any address there). Mail to an address with no route is
recorded and never fetched: the address is the key, so it is unguessable
(`new_route` adds a random part), and a route is retired by `active=0`,
never by reuse — `rotate_route` retires the old address and issues a new
one with the same senders (the admin's Invoice intake card).

Who may write a restaurant's invoices (bar fix round 10/15/26, V12): the
address alone used to be enough — a forged CSV sent to it rewrote real
invoices. Now three things must hold before a file is read:
  1. the sender is on the route's allow-list — checked on the webhook's
     `from` (receive) and again on the fetched email (process);
  2. Resend's own verdict on the message passes (`authentication`, from the
     Received-emails API — the webhook carries no verdict): DMARC pass, or,
     where the sender's domain publishes no DMARC policy, a DKIM pass
     (`auth_verdict`). A From header is trivially forged; these are the
     receiving server's results, which the sender cannot write;
  3. the email carries the file as an ATTACHMENT — its own text is never
     read as a file (only Gmail's forwarding confirmation is kept, for a
     person to confirm).
Anything else is HELD — `held_sender` or `held_auth` — never read, for an
admin to look at and release (`release`, the admin's Invoice intake card).

Lost mail (V13): the webhook only records the email and queues it
(`job_queue` kind 'email_intake'), and `receive` answers 'error' when it
could not — the webhook then answers 503 so Resend delivers it again. A
re-delivery of a row still `queued` with no live job queues it again
(`_ensure_job`), and the hourly `intake_reconcile` job requeues `queued`
rows older than an hour that have no live job.

The feed (V22): every processed intake stamps the restaurant's `invoices`
source (data_health.record_attempt, data_through = the newest invoice
date); the daily `invoice_feed_watch` records a failure when no Fintech file
has arrived for FEED_MISSING_BUSINESS_DAYS business days — the admin
console raises it as an issue at once (admin_ops SOURCE_FAIL_ISSUE_AT).

Untrusted files: an attachment is streamed and dropped past
MAX_ATTACHMENT_BYTES (`too_large`, the rest of the email is still read —
V39); a PDF is read in a child process with a page cap and a wall-clock
budget (`pdf_text`, R20).

Retention (R5, R24): `email_intake` rows 180 days (ops._RETENTION_DAYS),
`no_route` rows 30 (`prune_unrouted`), and a stored file's text 365 days
(`expire_file_content`, its object-store copy deleted with it); the
addresses are scrubbed from off-site copies (offsite_backup.SCRUB_COLUMNS).
"""
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone

import models as _models

RESEND_API = "https://api.resend.com"
HTTP_TIMEOUT = 20
# Attachments larger than this are recorded and skipped: a day's invoices
# are tens of kilobytes, a year's export a few megabytes.
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
# One attachment's whole download, however slowly the bytes arrive.
DOWNLOAD_SECONDS = 60
# A PDF is read in a child process: at most this many pages, in this many
# seconds (Fintech's daily printout is a few pages).
PDF_MAX_PAGES = 50
PDF_SECONDS = 30
# A queued row this old with no live job is requeued by intake_reconcile;
# after this many jobs for one email it is marked failed instead.
STALE_QUEUED_MINUTES = 60
MAX_REQUEUES = 3
# No Fintech file for this many business days (weekdays) is a feed failure.
FEED_MISSING_BUSINESS_DAYS = 2
FEED_WATCH_SECONDS = 120
GMAIL_FORWARDING = "forwarding-noreply@google.com"
# Statuses an admin can release.
HELD = ("held_sender", "held_auth")
# Statuses the admin's Invoice intake card flags for a look.
NEEDS_A_LOOK = HELD + ("conflict", "location_unmapped", "failed", "ignored")

# kind -> "module:function" taking (restaurant_id, filename, content_bytes,
# intake_id) and returning {"status", "detail", ...}. Reached by name.
READERS = {
    "fintech": "fintech_invoices:ingest_file",
    # Back Office's "inventory valuation by category" report into bar_counts
    # (source 'backoffice'). UNVERIFIED: built without a sample — the expected
    # headers are in bar.counts and docs/plans/BACK_OFFICE_INTEGRATION.md.
    "backoffice_valuation": "bar.counts:read_valuation_file",
}


class AttachmentTooLarge(Exception):
    """An attachment past MAX_ATTACHMENT_BYTES: recorded, never read."""


class FileRefused(Exception):
    """An untrusted file this reader will not read (`status`: too_many_pages,
    too_slow, unreadable_pdf), with a sentence for the ledger."""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


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
        # Bar fix round 10/15/26 (V12, F10): the senders a route takes mail
        # from, who made it and when it was retired; Resend's verdict on each
        # email and who released a held one.
        for table, col, typ in (("email_intake_routes", "allowed_senders", "TEXT"),
                                ("email_intake_routes", "created_by", "TEXT"),
                                ("email_intake_routes", "retired_at", "TEXT"),
                                ("email_intake", "auth", "TEXT"),
                                ("email_intake", "released_by", "TEXT"),
                                ("email_intake", "released_at", "TEXT")):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_intake_rid ON email_intake(restaurant_id, received_at)")
        # The retention delete's column, and the reconcile's read of queued rows.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_intake_received ON email_intake(received_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_intake_status ON email_intake(status, received_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_intake_routes_rid "
                     "ON email_intake_routes(restaurant_id, kind, active)")
        conn.commit()
    finally:
        conn.close()


# ── senders ────────────────────────────────────────────────────────────────

def _bare(value):
    """'Name <a@b.c>' or 'a@b.c' -> 'a@b.c' (lowercased), else ''."""
    s = str(value or "").strip()
    if "<" in s and ">" in s:
        s = s[s.index("<") + 1:s.index(">")]
    s = s.strip().lower()
    return s if "@" in s else ""


def parse_senders(value) -> list:
    """The allow-list from a textarea (one per line, commas too) or a list:
    lowercased whole addresses, or "@domain" for any address there. Raises
    ValueError naming an entry that is neither."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").replace(",", "\n").splitlines()
    out = []
    for raw in items:
        s = str(raw or "").strip().lower()
        if not s:
            continue
        if "<" in s:
            s = _bare(s)
        local, at, domain = s.rpartition("@")
        if not at or "." not in domain or " " in s or domain.startswith(".") or domain.endswith("."):
            raise ValueError(f"“{raw}” isn't an email address or an @domain")
        if s not in out:
            out.append(s)
    if len(out) > 20:
        raise ValueError("At most 20 senders per address")
    return out


def _senders(stored) -> list:
    try:
        return parse_senders(stored or "")
    except ValueError:
        return []


def sender_allowed(sender, allowed) -> bool:
    """True when `sender` (any 'Name <addr>' form) is on `allowed` — as a
    whole address, or by an "@domain" entry for its exact domain."""
    addr = _bare(sender)
    if not addr:
        return False
    domain = "@" + addr.rpartition("@")[2]
    allowed = allowed if isinstance(allowed, (list, tuple)) else _senders(allowed)
    return addr in allowed or domain in allowed


def auth_verdict(auth) -> tuple:
    """(passed, why) from Resend's `authentication` on a received email —
    {spf, dkim, dmarc}, each pass | fail | gray | processing_failed |
    unknown, the receiving server's results (the sender cannot write them).
    Passes on DMARC pass (the From domain is aligned with a passing SPF or
    DKIM); where the domain publishes no DMARC policy (gray / unknown), on a
    DKIM pass. No verdict at all (an email from before Resend recorded one,
    or an API that stopped returning it) does not pass."""
    if not isinstance(auth, dict):
        return False, "Resend gave no authentication result for this email"
    spf, dkim, dmarc = (str(auth.get(k) or "unknown").lower() for k in ("spf", "dkim", "dmarc"))
    said = f"SPF {spf}, DKIM {dkim}, DMARC {dmarc}"
    if dmarc == "pass":
        return True, said
    if dmarc in ("gray", "unknown") and dkim == "pass":
        return True, said + " (the sender's domain publishes no DMARC policy; its DKIM signature passed)"
    return False, said


# ── routes ─────────────────────────────────────────────────────────────────

def new_route(restaurant_id, kind, domain, note=None, allowed_senders=None, created_by=None, db_path=None):
    """Add an intake address `<kind>-<random>@<domain>` for a restaurant and
    return it. The random part is what keeps strangers' mail out; the
    allow-list is what keeps a stranger who learned it out."""
    if kind not in READERS:
        raise ValueError(f"unknown intake kind {kind!r}")
    domain = str(domain or "").strip().lower().lstrip("@")
    if not domain or "." not in domain or " " in domain:
        raise ValueError("an intake address needs a domain")
    senders = parse_senders(allowed_senders or [])
    address = f"{kind}-{secrets.token_hex(8)}@{domain}".lower()
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO email_intake_routes (address, restaurant_id, kind, active, note, created_at, "
                     "allowed_senders, created_by) VALUES (?,?,?,1,?,?,?,?)",
                     (address, int(restaurant_id), kind, note, _now(), "\n".join(senders) or None,
                      (str(created_by)[:120] if created_by else None)))
        conn.commit()
    finally:
        conn.close()
    return address


def routes_for(restaurant_id, db_path=None) -> list:
    """Every address a restaurant has had, active first, newest first."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM email_intake_routes WHERE restaurant_id=? "
                            "ORDER BY active DESC, created_at DESC", (int(restaurant_id),)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["allowed_senders"] = _senders(d.get("allowed_senders"))
        out.append(d)
    return out


def _active_routes(restaurant_id, kind, conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM email_intake_routes WHERE restaurant_id=? AND kind=? AND active=1 ORDER BY created_at DESC",
        (int(restaurant_id), kind)).fetchall()]


def default_domain(db_path=None):
    """The domain a new address is made on: INTAKE_EMAIL_DOMAIN, else the
    domain of the newest address on file (Resend's receiving domain)."""
    env = (os.getenv("INTAKE_EMAIL_DOMAIN") or "").strip().lower().lstrip("@")
    if env:
        return env
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT address FROM email_intake_routes ORDER BY created_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return row["address"].rpartition("@")[2] if row else None


def create_route(restaurant_id, kind="fintech", allowed_senders=None, note=None, created_by=None,
                 domain=None, db_path=None) -> dict:
    """The restaurant's first address of `kind` (refused when it has an
    active one: rotate that instead). {"address", "before", "after"}."""
    conn = get_conn(db_path)
    try:
        live = _active_routes(restaurant_id, kind, conn)
    finally:
        conn.close()
    if live:
        raise ValueError("This restaurant already has an active intake address — rotate it instead")
    domain = domain or default_domain(db_path)
    if not domain:
        raise ValueError("No intake domain: set INTAKE_EMAIL_DOMAIN")
    addr = new_route(restaurant_id, kind, domain, note=note, allowed_senders=allowed_senders,
                     created_by=created_by, db_path=db_path)
    return {"address": addr, "before": None, "after": {"address": addr, "allowed_senders": parse_senders(
        allowed_senders or [])}}


def rotate_route(restaurant_id, kind="fintech", note=None, created_by=None, db_path=None) -> dict:
    """Retire the restaurant's active address of `kind` (active=0,
    retired_at) and issue a new one on the same domain with the same
    senders. Mail to the old address is `no_route` from now on. Whoever
    sends to it (Fintech's settings, a Gmail forward) has to be given the
    new one. {"address", "old", "before", "after"}."""
    conn = get_conn(db_path)
    try:
        live = _active_routes(restaurant_id, kind, conn)
    finally:
        conn.close()
    if not live:
        raise ValueError("This restaurant has no active intake address to rotate")
    old = live[0]
    senders = _senders(old.get("allowed_senders"))
    domain = old["address"].rpartition("@")[2]
    addr = new_route(restaurant_id, kind, domain, note=note or old.get("note"), allowed_senders=senders,
                     created_by=created_by, db_path=db_path)
    conn = get_conn(db_path)
    try:
        for r in live:
            conn.execute("UPDATE email_intake_routes SET active=0, retired_at=? WHERE address=?",
                         (_now(), r["address"]))
        conn.commit()
    finally:
        conn.close()
    return {"address": addr, "old": [r["address"] for r in live],
            "before": {"addresses": [r["address"] for r in live]},
            "after": {"address": addr, "allowed_senders": senders}}


def set_allowed_senders(restaurant_id, address, senders, db_path=None) -> dict:
    """Replace one of this restaurant's routes' allow-list. Raises
    ValueError on a bad entry or an address that is not the restaurant's.
    {"before", "after"}."""
    clean = parse_senders(senders)
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT allowed_senders FROM email_intake_routes WHERE address=? AND restaurant_id=?",
                           (str(address or "").strip().lower(), int(restaurant_id))).fetchone()
        if not row:
            raise ValueError("No such intake address for this restaurant")
        conn.execute("UPDATE email_intake_routes SET allowed_senders=? WHERE address=? AND restaurant_id=?",
                     ("\n".join(clean) or None, str(address).strip().lower(), int(restaurant_id)))
        conn.commit()
    finally:
        conn.close()
    return {"before": _senders(row["allowed_senders"]), "after": clean}


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
            row = conn.execute("SELECT address, restaurant_id, kind, allowed_senders FROM email_intake_routes "
                               "WHERE address=? AND active=1", (addr,)).fetchone()
            if row:
                return dict(row)
    finally:
        conn.close()
    return None


def _route(address, db_path=None):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM email_intake_routes WHERE address=?",
                           (str(address or "").lower(),)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


# ── receiving ──────────────────────────────────────────────────────────────

def receive(data, db_path=None):
    """The `email.received` webhook's data: record the email once and queue
    it when its address is a route and its sender is on the route's list
    (else `held_sender`, never fetched). Returns the ledger status, or
    'error' when it could not be recorded or queued — the webhook answers
    non-200 so Resend delivers it again. Never raises."""
    data = data or {}
    email_id = str(data.get("email_id") or data.get("id") or "").strip()
    if not email_id:
        return "no_id"
    try:
        route = _route_for(_addresses(data.get("to")), db_path)
        sender = str(data.get("from") or "")
        detail = None
        if not route:
            status = "no_route"
        elif sender_allowed(sender, route.get("allowed_senders")):
            status = "queued"
        else:
            status = "held_sender"
            detail = (f"{_bare(sender) or 'An unknown sender'} is not on this address's list of senders — "
                      "held, never read")
        conn = get_conn(db_path)
        try:
            cur = conn.execute(
                "INSERT INTO email_intake (email_id, address, restaurant_id, kind, sender, subject, status, detail, "
                "received_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(email_id) DO NOTHING",
                (email_id, (route or {}).get("address") or ",".join(_addresses(data.get("to")))[:300],
                 (route or {}).get("restaurant_id"), (route or {}).get("kind"),
                 sender[:300], str(data.get("subject") or "")[:500], status, detail, _now()))
            conn.commit()
            fresh = (cur.rowcount or 0) > 0
        finally:
            conn.close()
        if fresh:
            if status == "queued":
                _ensure_job(email_id, route["restaurant_id"], db_path=db_path)
            return status
        # Delivered again (Resend retrying, a forward sent twice). A row
        # still queued whose enqueue failed the first time is queued now.
        row = _row(email_id, db_path)
        if row and row.get("status") == "queued" and row.get("restaurant_id"):
            _ensure_job(email_id, row["restaurant_id"], db_path=db_path)
        return "duplicate"
    except Exception as e:
        print(f"[email_intake] receive failed: {e}")
        try:
            import ops
            ops.capture(e, job="email_intake", context=f"receive email_id={email_id}")
        except Exception:
            pass
        return "error"


def _job_rows(email_id, conn):
    base = f"email_intake:{email_id}"
    return [dict(r) for r in conn.execute(
        "SELECT idempotency_key, state FROM jobs WHERE idempotency_key=? OR "
        "(idempotency_key > ? AND idempotency_key < ?)", (base, base + ":", base + ";")).fetchall()]


def _ensure_job(email_id, restaurant_id, released=False, db_path=None) -> bool:
    """Queue the email's job unless one is queued or running. A first job is
    keyed `email_intake:<id>` (the idempotency key a re-delivery reuses);
    a later one (a requeue, a release) `email_intake:<id>:<n>`. True when a
    job was created."""
    import job_queue
    conn = get_conn(db_path)
    try:
        rows = _job_rows(email_id, conn)
    finally:
        conn.close()
    if any(r["state"] in ("queued", "running") for r in rows):
        return False
    base = f"email_intake:{email_id}"
    key = base if not rows else f"{base}:{len(rows)}"
    payload = {"email_id": email_id}
    if released:
        payload["released"] = True
    _qid, created = job_queue.enqueue("email_intake", payload, restaurant_id=restaurant_id,
                                      idempotency_key=key, db_path=db_path)
    return bool(created)


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


def _download(url, limit=None):
    """The attachment's bytes, streamed: past `limit` (by its declared length
    or as it arrives) AttachmentTooLarge, before the rest is read; a
    download slower than DOWNLOAD_SECONDS in all is abandoned."""
    import requests
    limit = int(limit or MAX_ATTACHMENT_BYTES)
    r = requests.get(url, timeout=HTTP_TIMEOUT, stream=True)
    try:
        if r.status_code != 200:
            raise RuntimeError(f"attachment download answered {r.status_code}")
        declared = r.headers.get("Content-Length")
        if declared and str(declared).isdigit() and int(declared) > limit:
            raise AttachmentTooLarge(f"{int(declared)} bytes")
        buf = bytearray()
        stop = time.monotonic() + DOWNLOAD_SECONDS
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                buf += chunk
            if len(buf) > limit:
                raise AttachmentTooLarge(f"more than {limit} bytes")
            if time.monotonic() > stop:
                raise RuntimeError(f"attachment download took longer than {DOWNLOAD_SECONDS}s")
        return bytes(buf)
    finally:
        r.close()


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


def process(email_id, db_path=None, released=False):
    """Fetch one recorded email and hand each attachment to its route's
    reader — once its sender is on the route's list and Resend's
    authentication passes (`auth_verdict`), unless an admin released it.
    Raises on a fetch failure, so the queue retries it."""
    row = _row(email_id, db_path)
    if not row or not row.get("kind"):
        return {"status": "skipped"}
    if row.get("status") in HELD and not released:
        return {"status": row["status"]}           # a stale job for a held email
    route = _route(row.get("address"), db_path) or {}
    if not released and not route.get("active"):
        _set(email_id, db_path, status="no_route", processed_at=_now(),
             detail="The address was retired before this email was read")
        return {"status": "no_route"}
    email = _api_get(f"/emails/receiving/{email_id}")
    sender = str(email.get("from") or row.get("sender") or "")
    subject = str(email.get("subject") or row.get("subject") or "")
    auth = email.get("authentication")
    auth_json = json.dumps(auth, default=str)[:300] if isinstance(auth, dict) else None
    if not released:
        if not sender_allowed(sender, route.get("allowed_senders")):
            _set(email_id, db_path, status="held_sender", sender=sender[:300], subject=subject[:500], auth=auth_json,
                 detail=f"{_bare(sender) or 'An unknown sender'} is not on this address's list of senders — "
                        "held, never read", processed_at=_now())
            return {"status": "held_sender"}
        passed, why = auth_verdict(auth)
        if not passed:
            _set(email_id, db_path, status="held_auth", sender=sender[:300], subject=subject[:500], auth=auth_json,
                 detail=f"Resend could not vouch for the sender ({why}) — held, never read", processed_at=_now())
            return {"status": "held_auth"}
    listing = _api_get(f"/emails/receiving/{email_id}/attachments")
    attachments = listing.get("data") or []
    if not attachments:
        if GMAIL_FORWARDING in sender.lower():
            _set(email_id, db_path, status="gmail_confirmation", sender=sender[:300], subject=subject[:500],
                 auth=auth_json, processed_at=_now(),
                 detail="Gmail forwarding confirmation: confirm it from the Resend dashboard's Receiving tab")
            return {"status": "gmail_confirmation"}
        # An email's own text is never read as a file (V12): a pasted CSV
        # or printout in the body would have been ingested.
        _set(email_id, db_path, status="no_files", sender=sender[:300], subject=subject[:500], auth=auth_json,
             files=0, processed_at=_now(),
             detail="No attachment. An email's own text is never read as a file — send the file attached.")
        return {"status": "no_files", "files": []}
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
        try:
            content = _download(url)
        except AttachmentTooLarge:
            results.append({"file": name, "status": "too_large"})
            continue
        res = reader(row["restaurant_id"], name, content, row["id"], db_path=db_path) or {}
        results.append({"file": name, **res})
    status, read = email_status(results)
    _set(email_id, db_path, status=status, sender=sender[:300], subject=subject[:500], files=len(read), auth=auth_json,
         detail=json.dumps(results, default=str)[:4000], processed_at=_now())
    if row["kind"] == "fintech":
        _record_feed(row["restaurant_id"], results, db_path=db_path)
    return {"status": status, "files": results}


# What a reader's file statuses (fintech_invoices.ingest_file) say about the
# feed: a file read, or one whose invoices were already on file, is a feed
# that works; a conflict is read too (its invoices are kept and reported).
_FEED_OK = ("stored", "unchanged", "duplicate", "conflict")


def email_status(results) -> tuple:
    """(the email's ledger status, the files that wrote invoices) from its
    files' results. "done" only when a file wrote an invoice (V12: a
    refused or empty file used to report "stored"); else what needs a look
    first — "conflict" (a balanced invoice on file disagrees: kept,
    reported), "location_unmapped" (a store this restaurant has no mapping
    for) — then "unchanged" (every invoice already on file as well or
    better, or the same file again), else "ignored" (nothing readable)."""
    read = [r for r in results if r.get("status") == "stored" and int(r.get("invoices", 1) or 0) > 0]
    seen = {r.get("status") for r in results}
    if read:
        return "done", read
    for st in ("conflict", "location_unmapped"):
        if st in seen:
            return st, read
    if results and seen <= {"unchanged", "duplicate"}:
        return "unchanged", read
    return "ignored", read


def _record_feed(restaurant_id, results, db_path=None):
    """Stamp the restaurant's `invoices` source: a success when a file was
    read (or its invoices were already on file), a failure naming what was
    refused when none was. Never raises."""
    try:
        ok = any(r.get("status") in _FEED_OK for r in results)
        err = None
        if not ok:
            what = sorted({str(r.get("status") or "unread") for r in results})
            err = "A file arrived but none could be read (" + ", ".join(what) + ")"
        import data_health
        from bar.feed import invoices_through
        data_health.record_attempt(restaurant_id, "invoices", ok, provider="fintech", error=err,
                                   error_class=None if ok else "empty",
                                   data_through=invoices_through(restaurant_id, db_path=db_path), db_path=db_path)
    except Exception as e:
        print(f"[email_intake] feed record failed for {restaurant_id}: {e}")


def run_queued(job):
    """job_queue handler for kind 'email_intake'."""
    payload = job.get("payload") or {}
    email_id = payload.get("email_id")
    try:
        return process(email_id, released=bool(payload.get("released")))
    except Exception as e:
        try:
            _set(email_id, status="failed", detail=str(e)[:500], processed_at=_now())
        except Exception:
            pass
        raise


# ── untrusted PDFs ─────────────────────────────────────────────────────────

_PDF_CHILD = r"""
import io, sys
try:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
except Exception:
    pass
import pypdf
limit = int(sys.argv[1])
reader = pypdf.PdfReader(io.BytesIO(sys.stdin.buffer.read()))
n = len(reader.pages)
if n > limit:
    sys.stdout.write(str(n))
    sys.exit(3)
sys.stdout.buffer.write("\n".join((p.extract_text() or "") for p in reader.pages).encode("utf-8"))
"""


def pdf_text(content, max_pages=PDF_MAX_PAGES, seconds=PDF_SECONDS) -> str:
    """The text of an untrusted PDF, read by pypdf in a child process (an
    address-space limit where the OS allows one) that is killed past
    `seconds`. Raises FileRefused: too_many_pages past `max_pages` (the
    count is read before any page is), too_slow, unreadable_pdf."""
    try:
        res = subprocess.run([sys.executable, "-I", "-c", _PDF_CHILD, str(int(max_pages))], input=bytes(content),
                             capture_output=True, timeout=seconds)
    except subprocess.TimeoutExpired:
        raise FileRefused("too_slow", f"The PDF took longer than {seconds}s to read — not read")
    if res.returncode == 3:
        pages = res.stdout.decode("utf-8", "replace").strip()
        raise FileRefused("too_many_pages", f"The PDF has {pages} pages; at most {max_pages} are read — not read")
    if res.returncode != 0:
        tail = res.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        raise FileRefused("unreadable_pdf", f"The PDF could not be read ({tail[0][:160]})")
    return res.stdout.decode("utf-8", "replace")


# ── the admin's actions ────────────────────────────────────────────────────

def release(restaurant_id, email_id, released_by=None, db_path=None) -> dict:
    """Read a held email after all: queued with `released` (the sender and
    authentication checks are skipped — an admin vouched for it). Raises
    ValueError when the email is not this restaurant's or not held.
    {"before", "after"}."""
    row = _row(str(email_id or ""), db_path)
    if not row or row.get("restaurant_id") != int(restaurant_id):
        raise ValueError("No such email for this restaurant")
    if row.get("status") not in HELD:
        raise ValueError("Only a held email can be released")
    _set(row["email_id"], db_path, status="queued", released_by=(str(released_by)[:120] if released_by else None),
         released_at=_now(), detail=f"Released by {released_by or 'an admin'}")
    _ensure_job(row["email_id"], row["restaurant_id"], released=True, db_path=db_path)
    return {"before": {"status": row["status"], "sender": row.get("sender")}, "after": {"status": "queued"}}


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


def admin_view(restaurant_id, db_path=None) -> dict:
    """The admin's Invoice intake card: the restaurant's addresses (active
    first, each with its senders), its last 20 received emails, its last 20
    stored files and the feed's state."""
    conn = get_conn(db_path)
    try:
        files = [dict(r) for r in conn.execute(
            "SELECT id, intake_id, filename, format, invoices, lines, status, detail, created_at, "
            "CASE WHEN content IS NULL THEN 0 ELSE 1 END AS has_content FROM fintech_files "
            "WHERE restaurant_id=? ORDER BY id DESC LIMIT 20", (int(restaurant_id),)).fetchall()]
        sh = conn.execute("SELECT * FROM source_health WHERE restaurant_id=? AND source='invoices'",
                          (int(restaurant_id),)).fetchone()
    finally:
        conn.close()
    for f in files:
        try:
            f["detail"] = json.loads(f["detail"]) if f.get("detail") and str(f["detail"]).startswith("{") \
                else f.get("detail")
        except (TypeError, ValueError):
            pass
    try:
        import fintech_invoices
        stores = fintech_invoices.location_map(restaurant_id, db_path=db_path)
    except Exception:
        stores = {}
    emails = recent(restaurant_id, 20, db_path=db_path)
    for e in emails:
        try:
            e["auth"] = json.loads(e["auth"]) if e.get("auth") else None
        except (TypeError, ValueError):
            e["auth"] = None
        e["releasable"] = e.get("status") in HELD
        e["needs_a_look"] = e.get("status") in NEEDS_A_LOOK
    from bar.feed import feed_status
    return {"routes": routes_for(restaurant_id, db_path=db_path), "emails": emails, "files": files,
            "feed": feed_status(restaurant_id, db_path=db_path), "source_health": dict(sh) if sh else None,
            "stores": [{"location_key": k, "restaurant_id": v} for k, v in sorted(stores.items())],
            "domain": default_domain(db_path)}


# ── jobs ───────────────────────────────────────────────────────────────────

def reconcile_stale(db_path=None, now=None) -> dict:
    """The hourly `intake_reconcile` job (V13): every email `queued` for more
    than STALE_QUEUED_MINUTES with no queued or running job is queued again
    — or, once it has had MAX_REQUEUES jobs, marked failed (captured).
    Standard counts."""
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(minutes=STALE_QUEUED_MINUTES)) \
        .strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT email_id, restaurant_id, released_by FROM email_intake WHERE status='queued' AND received_at < ? "
            "ORDER BY received_at LIMIT 500", (cutoff,)).fetchall()]
    finally:
        conn.close()
    out["hit_bound"] = len(rows) >= 500
    for r in rows:
        out["attempted"] += 1
        try:
            conn = get_conn(db_path)
            try:
                jobs = _job_rows(r["email_id"], conn)
            finally:
                conn.close()
            if any(j["state"] in ("queued", "running") for j in jobs):
                out["attempted"] -= 1
                out["skipped"] += 1
                continue
            if len(jobs) >= MAX_REQUEUES:
                _set(r["email_id"], db_path, status="failed", processed_at=_now(),
                     detail=f"Gave up after {len(jobs)} tries — read it again from the admin's Invoice intake")
                try:
                    import ops
                    ops.capture(RuntimeError(f"intake email {r['email_id']} gave up after {len(jobs)} jobs"),
                                job="intake_reconcile", restaurant_id=r["restaurant_id"])
                except Exception:
                    pass
                out["failed"] += 1
                continue
            _ensure_job(r["email_id"], r["restaurant_id"], released=bool(r.get("released_by")), db_path=db_path)
            out["ok"] += 1
        except Exception as e:
            out["failed"] += 1
            print(f"[email_intake] reconcile {r['email_id']} failed: {e}")
    return out


def business_days_after(start, end) -> int:
    """Weekdays d with start < d <= end."""
    if start is None or end is None or end <= start:
        return 0
    n, d = 0, start + timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def _watch_one(restaurant_id, since, db_path=None, today=None) -> bool:
    """One restaurant's feed: True when it is late (a failure recorded)."""
    from time_utils import local_iso, mdy, restaurant_now
    conn = get_conn(db_path)
    try:
        f = conn.execute("SELECT MAX(created_at) AS at FROM fintech_files WHERE restaurant_id=? "
                         "AND status<>'purged'",
                         (int(restaurant_id),)).fetchone()
        tzr = conn.execute("SELECT timezone FROM restaurants WHERE id=?", (int(restaurant_id),)).fetchone()
    finally:
        conn.close()
    tz = (tzr["timezone"] if tzr else None) or None
    last = (f["at"] if f else None) or since
    last_day = date.fromisoformat(local_iso(last, tz))
    today = today or restaurant_now(tz).date()
    missed = business_days_after(last_day, today)
    if missed < FEED_MISSING_BUSINESS_DAYS:
        return False
    import data_health
    from bar.feed import invoices_through
    what = "No Fintech file since" if f and f["at"] else "No Fintech file yet since the address was made on"
    data_health.record_attempt(restaurant_id, "invoices", False, provider="fintech", error_class="empty",
                               error=f"{what} {mdy(last_day)} ({missed} business days)",
                               data_through=invoices_through(restaurant_id, db_path=db_path), db_path=db_path)
    return True


def watch_feeds(db_path=None, today=None) -> dict:
    """The daily `invoice_feed_watch` job (V22): each restaurant with an
    active Fintech address and no file for FEED_MISSING_BUSINESS_DAYS
    business days gets a failure on its `invoices` source — the admin
    console raises it as an issue at once. Bounded and resumable
    (scheduler.resumable_sweep). Standard counts plus `late`."""
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False, "late": []}
    conn = get_conn(db_path)
    try:
        routes = {r["restaurant_id"]: r["since"] for r in conn.execute(
            "SELECT restaurant_id, MIN(created_at) AS since FROM email_intake_routes WHERE active=1 "
            "AND kind='fintech' GROUP BY restaurant_id").fetchall()}
    finally:
        conn.close()
    if not routes:
        return out

    def _one(rid):
        out["attempted"] += 1
        try:
            if _watch_one(rid, routes[rid], db_path=db_path, today=today):
                out["late"].append(rid)
            out["ok"] += 1
        except Exception as e:
            out["failed"] += 1
            print(f"[email_intake] feed watch {rid} failed: {e}")

    import scheduler
    _done, hit = scheduler.resumable_sweep("invoice_feed_watch_cursor", sorted(routes), _one, FEED_WATCH_SECONDS,
                                           workers=1, job="invoice_feed_watch")
    out["hit_bound"] = bool(hit)
    return out


# ── retention (ops.prune_ledgers' special prunes) ──────────────────────────

def prune_unrouted(conn, days, deadline=None, max_rows=200000, chunk=5000) -> int:
    """Delete `no_route` rows (mail to no address of ours — mostly spam)
    older than `days`, in chunks with a commit each. Rows deleted."""
    total = 0
    while total < max_rows:
        n = min(chunk, max_rows - total)
        cur = conn.execute("DELETE FROM email_intake WHERE id IN (SELECT id FROM email_intake WHERE status='no_route' "
                           f"AND received_at < datetime('now', ?) LIMIT {int(n)})", (f"-{int(days)} days",))
        got = max(0, cur.rowcount or 0)
        conn.commit()
        total += got
        if got < n or (deadline is not None and time.monotonic() > deadline):
            break
    return total


def expire_file_content(conn, days, deadline=None, max_rows=200000, chunk=500) -> int:
    """A stored intake file's text past `days`: the column is cleared and
    its object-store copy deleted (the invoice lines hold the history; a
    re-read is no longer possible). Rows cleared."""
    import blob_store
    total = 0
    while total < max_rows:
        n = min(chunk, max_rows - total)
        rows = conn.execute("SELECT id, content FROM fintech_files WHERE content IS NOT NULL "
                            f"AND created_at < datetime('now', ?) ORDER BY id LIMIT {int(n)}",
                            (f"-{int(days)} days",)).fetchall()
        for r in rows:
            blob_store.delete_text(r["content"])
            conn.execute("UPDATE fintech_files SET content=NULL WHERE id=?", (r["id"],))
        conn.commit()
        total += len(rows)
        if len(rows) < n or (deadline is not None and time.monotonic() > deadline):
            break
    return total
