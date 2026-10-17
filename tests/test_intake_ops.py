"""The email intake's operations (bar fix round 10/15/26, workstream W2):
who may write a restaurant's invoices (V12), mail that must never be lost
(V13), the feed's health (V22), untrusted attachments and PDFs (V39, R20),
where the files' text lives and for how long (R5, R24).

Resend is never reached: its API reads and the attachment download are
stubbed; nothing leaves the process.
"""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import pytest
from flask import Flask

import blob_store
import data_freshness
import data_health
import email_intake
import fintech_invoices as fi
import job_queue
import models
import ops
import webhook_routes
from bar.feed import FEED_STALE_DAYS, feed_status

ROUTE = "fintech-0011223344556677@intake.resend.app"
SENDERS = "processor@fintech.net\nwill@cavnar.ai\nforwarding-noreply@google.com"
PASSED = {"spf": "pass", "dkim": "pass", "dmarc": "pass"}

PRINTOUT = """Page: 1
Store Number   : 000001  St. Charles, IL
Distributor    : Chicago Beverage System, Inc dba Windy City
Invoice Number : 101153056
Invoice Date   : {d}
Invoice Total  : $398.00
Due Date       : {d}

Item            Description                           Qty      Price           Cost
----------      ------------------------------       ----   --------      ---------
13682           HALF ACRE DAISY CUTTER K 1/2BB        2       $199.00       $398.00
                                                                       ------------
                                                        Invoice Total:      $398.00
"""

EXPORT = (
    "Vendor Name,Invoice Number,Invoice Date,Invoice Total,Product Number,Product Description,U.P.C,"
    "Unit Of Measure,Item Quantity,Item Cost,Item Adjustment\n"
    "Breakthru,0128850598,09/15/2026,$264.00,9637400,HIGH NOON VODKA SELTZER,085000000000,CA,6,$42.00,$0.00\n"
    "Breakthru,0128850598,09/15/2026,$264.00,9039158,TITOS 750,9039158,BO,1,$12.00,$0.00\n"
)


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda p=None: real(p or db_path))
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    data_health.invalidate()
    return db_path


def _rid(db_path, name="EJ's"):
    return models.create_restaurant(models.Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


def _route(db_path, rid, senders=SENDERS, address=ROUTE, created_at="2026-10-14 12:00:00"):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO email_intake_routes (address, restaurant_id, kind, active, created_at, allowed_senders) "
                 "VALUES (?,?, 'fintech', 1, ?, ?)", (address, rid, created_at, senders))
    conn.commit()
    conn.close()


def _rows(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _exec(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _stub_resend(monkeypatch, email_id, email, attachments, files):
    api = {f"/emails/receiving/{email_id}": email,
           f"/emails/receiving/{email_id}/attachments": {"data": attachments}}
    monkeypatch.setattr(email_intake, "_api_get", lambda path: api[path])
    monkeypatch.setattr(email_intake, "_download", lambda url, limit=None: files[url])


def _printout(d="10/09/2026"):
    return PRINTOUT.format(d=d).encode()


# ── V12: who may write a restaurant's invoices ─────────────────────────────

def test_unlisted_sender_is_refused(db):
    rid = _rid(db)
    _route(db, rid)
    assert email_intake.receive({"email_id": "e1", "to": [ROUTE], "from": "Mallory <m@evil.test>"}, db_path=db) \
        == "held_sender"
    row = _rows(db, "SELECT * FROM email_intake WHERE email_id='e1'")[0]
    assert row["status"] == "held_sender" and "m@evil.test" in row["detail"]
    assert _rows(db, "SELECT id FROM jobs WHERE kind='email_intake'") == []
    # A listed sender is queued; an @domain entry takes any address there.
    _exec(db, "UPDATE email_intake_routes SET allowed_senders=?", (SENDERS + "\n@breakthru.test",))
    assert email_intake.receive({"email_id": "e2", "to": [ROUTE], "from": "Bob <bob@breakthru.test>"}, db_path=db) \
        == "queued"
    assert len(_rows(db, "SELECT id FROM jobs WHERE kind='email_intake'")) == 1


def test_mail_from_an_unlisted_sender_is_held_not_read(db, monkeypatch):
    """The webhook's `from` is a header anyone can write: the fetched email
    is checked again, and Resend's own authentication verdict must pass."""
    rid = _rid(db)
    _route(db, rid)
    read = []
    monkeypatch.setattr(fi, "ingest_file", lambda *a, **k: read.append(a) or {"status": "stored", "invoices": 1})
    att = [{"id": "a1", "filename": "daily.txt", "download_url": "u1", "size": 10}]
    # The header said Fintech; the fetched email says someone else.
    email_intake.receive({"email_id": "e3", "to": [ROUTE], "from": "processor@fintech.net"}, db_path=db)
    _stub_resend(monkeypatch, "e3", {"from": "m@evil.test", "authentication": PASSED}, att, {"u1": b"x"})
    assert email_intake.process("e3", db_path=db)["status"] == "held_sender"
    # A forged From: the sender is listed but DMARC failed.
    email_intake.receive({"email_id": "e4", "to": [ROUTE], "from": "processor@fintech.net"}, db_path=db)
    _stub_resend(monkeypatch, "e4", {"from": "processor@fintech.net",
                                     "authentication": {"spf": "fail", "dkim": "fail", "dmarc": "fail"}},
                 att, {"u1": b"x"})
    assert email_intake.process("e4", db_path=db)["status"] == "held_auth"
    row = _rows(db, "SELECT * FROM email_intake WHERE email_id='e4'")[0]
    assert row["status"] == "held_auth" and "DMARC fail" in row["detail"] and json.loads(row["auth"])["dmarc"] == "fail"
    # No verdict at all (an API that stopped returning one) is not a pass.
    email_intake.receive({"email_id": "e5", "to": [ROUTE], "from": "processor@fintech.net"}, db_path=db)
    _stub_resend(monkeypatch, "e5", {"from": "processor@fintech.net"}, att, {"u1": b"x"})
    assert email_intake.process("e5", db_path=db)["status"] == "held_auth"
    assert read == []
    assert _rows(db, "SELECT id FROM fintech_files") == []


def test_the_authentication_rule():
    assert email_intake.auth_verdict(PASSED)[0] is True
    # A domain with no DMARC policy: its DKIM signature decides.
    assert email_intake.auth_verdict({"spf": "fail", "dkim": "pass", "dmarc": "gray"})[0] is True
    assert email_intake.auth_verdict({"spf": "pass", "dkim": "fail", "dmarc": "gray"})[0] is False
    assert email_intake.auth_verdict({"spf": "pass", "dkim": "pass", "dmarc": "fail"})[0] is False
    assert email_intake.auth_verdict({"dmarc": "processing_failed", "dkim": "pass"})[0] is False
    assert email_intake.auth_verdict(None)[0] is False
    assert email_intake.sender_allowed("Fintech <Processor@Fintech.net>", ["processor@fintech.net"])
    assert not email_intake.sender_allowed("processor@fintech.net.evil.test", ["@fintech.net"])
    assert email_intake.sender_allowed("x@fintech.net", ["@fintech.net"])
    with pytest.raises(ValueError):
        email_intake.parse_senders("not an address")


def test_body_text_not_ingested(db, monkeypatch):
    """A printout pasted into an email's body used to be read as a file."""
    rid = _rid(db)
    _route(db, rid)
    email_intake.receive({"email_id": "e6", "to": [ROUTE], "from": "will@cavnar.ai"}, db_path=db)
    _stub_resend(monkeypatch, "e6", {"from": "will@cavnar.ai", "authentication": PASSED,
                                     "text": _printout().decode()}, [], {})
    assert email_intake.process("e6", db_path=db)["status"] == "no_files"
    assert _rows(db, "SELECT status FROM email_intake WHERE email_id='e6'") == [{"status": "no_files"}]
    assert _rows(db, "SELECT id FROM fintech_files") == []
    assert _rows(db, "SELECT id FROM fintech_invoices") == []


def test_stored_is_reported_only_when_a_file_wrote_invoices(db, monkeypatch):
    rid = _rid(db)
    _route(db, rid)
    email_intake.receive({"email_id": "e7", "to": [ROUTE], "from": "processor@fintech.net"}, db_path=db)
    _stub_resend(monkeypatch, "e7", {"from": "processor@fintech.net", "authentication": PASSED},
                 [{"id": "a", "filename": "x.csv", "download_url": "u", "size": 5}], {"u": b"hello"})
    monkeypatch.setattr(fi, "ingest_file", lambda *a, **k: {"status": "stored", "invoices": 0})
    out = email_intake.process("e7", db_path=db)
    assert out["status"] == "ignored"
    assert _rows(db, "SELECT files FROM email_intake WHERE email_id='e7'") == [{"files": 0}]


def test_every_reader_outcome_has_an_email_status():
    """fintech_invoices.ingest_file's outcomes (W1): only a written invoice
    is "done"; a conflict and an unmapped store are flagged for a look;
    invoices already on file are "unchanged"."""
    def st(*xs):
        return email_intake.email_status([{"status": x, "invoices": 1 if x == "stored" else 0} for x in xs])[0]
    assert st("stored", "conflict") == "done"
    assert st("conflict", "unchanged") == "conflict"
    assert st("location_unmapped", "duplicate") == "location_unmapped"
    assert st("unchanged", "duplicate") == "unchanged"
    assert st("unrecognized") == "ignored" and st("too_large") == "ignored"
    assert {"conflict", "location_unmapped"} <= set(email_intake.NEEDS_A_LOOK)
    # Another reader's "stored" with no invoice count (bar.counts' valuation) is read.
    assert email_intake.email_status([{"status": "stored"}])[0] == "done"


def test_a_held_email_released_by_an_admin_is_read(db, monkeypatch):
    rid = _rid(db)
    _route(db, rid)
    email_intake.receive({"email_id": "e8", "to": [ROUTE], "from": "erik@ejs.test"}, db_path=db)
    with pytest.raises(ValueError):
        email_intake.release(rid + 1, "e8", db_path=db)              # not this restaurant's
    email_intake.release(rid, "e8", released_by="will", db_path=db)
    jobs = _rows(db, "SELECT payload_json, idempotency_key FROM jobs WHERE kind='email_intake'")
    assert len(jobs) == 1 and json.loads(jobs[0]["payload_json"]) == {"email_id": "e8", "released": True}
    _stub_resend(monkeypatch, "e8", {"from": "erik@ejs.test", "authentication": None},
                 [{"id": "a", "filename": "daily.txt", "download_url": "u", "size": 50}], {"u": _printout()})
    out = email_intake.run_queued({"payload": json.loads(jobs[0]["payload_json"])})
    assert out["status"] == "done"
    row = _rows(db, "SELECT * FROM email_intake WHERE email_id='e8'")[0]
    assert row["released_by"] == "will" and row["files"] == 1
    with pytest.raises(ValueError):
        email_intake.release(rid, "e8", db_path=db)                  # no longer held


# ── V13: mail is never lost ────────────────────────────────────────────────

_SECRET = "whsec_" + base64.b64encode(b"intake-ops").decode()


def _post(event):
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    body = json.dumps(event).encode()
    ts = int(time.time())
    key = base64.b64decode(_SECRET.split("_", 1)[1])
    sig = base64.b64encode(hmac.new(key, f"msg_1.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return app.test_client().post("/webhooks/resend", data=body, headers={
        "svix-id": "msg_1", "svix-timestamp": str(ts), "svix-signature": f"v1,{sig}",
        "Content-Type": "application/json"})


def test_receive_failure_answers_non_200(db, monkeypatch):
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", _SECRET)
    rid = _rid(db)
    _route(db, rid)

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(email_intake, "_route_for", locked)
    resp = _post({"type": "email.received", "data": {"email_id": "e9", "to": [ROUTE], "from": "will@cavnar.ai"}})
    assert resp.status_code == 503
    monkeypatch.undo()
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", _SECRET)
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda p=None: real(p or db))
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    resp = _post({"type": "email.received", "data": {"email_id": "e9", "to": [ROUTE], "from": "will@cavnar.ai"}})
    assert resp.status_code == 200
    assert _rows(db, "SELECT status FROM email_intake WHERE email_id='e9'") == [{"status": "queued"}]


def test_a_failed_enqueue_is_retried_on_the_next_delivery(db, monkeypatch):
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", _SECRET)
    rid = _rid(db)
    _route(db, rid)
    real_enqueue = job_queue.enqueue
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_enqueue(*a, **k)
    monkeypatch.setattr(job_queue, "enqueue", flaky)
    event = {"type": "email.received", "data": {"email_id": "e10", "to": [ROUTE], "from": "will@cavnar.ai"}}
    assert _post(event).status_code == 503                       # Resend delivers it again
    assert _rows(db, "SELECT status FROM email_intake WHERE email_id='e10'") == [{"status": "queued"}]
    assert _rows(db, "SELECT id FROM jobs WHERE kind='email_intake'") == []
    assert _post(event).status_code == 200                       # the re-delivery queues it
    assert len(_rows(db, "SELECT id FROM jobs WHERE kind='email_intake'")) == 1
    assert _post(event).status_code == 200                       # and a third is a plain duplicate
    assert len(_rows(db, "SELECT id FROM jobs WHERE kind='email_intake'")) == 1


def test_the_reconcile_requeues_stale_queued_mail_and_gives_up_after_three(db, monkeypatch):
    rid = _rid(db)
    _route(db, rid)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    _exec(db, "INSERT INTO email_intake (email_id, address, restaurant_id, kind, sender, status, received_at) "
              "VALUES ('e11', ?, ?, 'fintech', 'will@cavnar.ai', 'queued', ?)", (ROUTE, rid, old))
    # A fresh queued row is left alone (its job may simply not have run yet).
    _exec(db, "INSERT INTO email_intake (email_id, address, restaurant_id, kind, sender, status, received_at) "
              "VALUES ('e12', ?, ?, 'fintech', 'will@cavnar.ai', 'queued', ?)", (ROUTE, rid, email_intake._now()))
    out = email_intake.reconcile_stale(db_path=db)
    assert out["ok"] == 1 and out["failed"] == 0
    assert [r["idempotency_key"] for r in _rows(db, "SELECT idempotency_key FROM jobs")] == ["email_intake:e11"]
    out = email_intake.reconcile_stale(db_path=db)              # its job is still queued
    assert out["ok"] == 0 and out["skipped"] == 1
    for n in range(2):
        _exec(db, "UPDATE jobs SET state='failed'")
        assert email_intake.reconcile_stale(db_path=db)["ok"] == 1
    assert len(_rows(db, "SELECT id FROM jobs")) == 3
    _exec(db, "UPDATE jobs SET state='failed'")
    out = email_intake.reconcile_stale(db_path=db)
    assert out["failed"] == 1
    assert _rows(db, "SELECT status FROM email_intake WHERE email_id='e11'") == [{"status": "failed"}]
    assert set(out) >= {"attempted", "ok", "failed", "skipped", "hit_bound"}


# ── V39, R20: untrusted attachments and PDFs ───────────────────────────────

class _Resp:
    def __init__(self, body, headers=None):
        self.status_code, self.headers, self._body, self.closed = 200, headers or {}, body, False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


def test_oversized_attachment_without_size_is_skipped_not_failed(db, monkeypatch):
    import requests
    rid = _rid(db)
    _route(db, rid)
    monkeypatch.setattr(email_intake, "MAX_ATTACHMENT_BYTES", 1000)
    big = _Resp(b"x" * 5000)
    bodies = {"https://x.test/big": big, "https://x.test/good": _Resp(_printout())}
    seen = []

    def get(url, timeout=None, stream=False, **kw):
        assert stream and timeout
        seen.append(url)
        return bodies[url]
    monkeypatch.setattr(requests, "get", get)
    email_intake.receive({"email_id": "e13", "to": [ROUTE], "from": "processor@fintech.net"}, db_path=db)
    api = {"/emails/receiving/e13": {"from": "processor@fintech.net", "authentication": PASSED},
           "/emails/receiving/e13/attachments": {"data": [
               {"id": "a", "filename": "huge.csv", "download_url": "https://x.test/big"},       # no size
               {"id": "b", "filename": "daily.txt", "download_url": "https://x.test/good", "size": 0}]}}
    monkeypatch.setattr(email_intake, "_api_get", lambda path: api[path])
    out = email_intake.process("e13", db_path=db)
    assert out["status"] == "done"
    assert [r["status"] for r in out["files"]] == ["too_large", "stored"]
    assert big.closed
    assert len(_rows(db, "SELECT id FROM fintech_invoices WHERE restaurant_id=?", (rid,))) == 1


def test_a_declared_length_past_the_limit_is_never_read(monkeypatch):
    import requests
    resp = _Resp(b"", headers={"Content-Length": str(10 ** 9)})
    monkeypatch.setattr(requests, "get", lambda *a, **k: resp)
    with pytest.raises(email_intake.AttachmentTooLarge):
        email_intake._download("https://x.test/a")
    assert resp.closed


def _pdf(pages):
    import io
    import pypdf
    w = pypdf.PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_pdf_page_cap(db, monkeypatch):
    rid = _rid(db)
    with pytest.raises(email_intake.FileRefused) as e:
        email_intake.pdf_text(_pdf(51))
    assert e.value.status == "too_many_pages" and "51 pages" in e.value.detail
    assert email_intake.pdf_text(_pdf(2)) is not None             # under the cap: read
    res = fi.ingest_file(rid, "daily.pdf", _pdf(51), db_path=db)
    assert res["status"] == "too_many_pages" and res["invoices"] == 0
    row = _rows(db, "SELECT status, content, detail FROM fintech_files")[0]
    assert row["status"] == "too_many_pages" and row["content"] is None and "51" in row["detail"]
    # The time budget: a read past it is killed and refused.
    import subprocess

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pdf", timeout=k.get("timeout"))
    monkeypatch.setattr(email_intake.subprocess, "run", slow)
    with pytest.raises(email_intake.FileRefused) as e:
        email_intake.pdf_text(_pdf(1))
    assert e.value.status == "too_slow"


# ── R5: where a file's text lives, and how big a file may be ───────────────

def test_row_cap_refuses_oversized_export(db, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(fi, "MAX_FILE_LINES", 1)
    res = fi.ingest_file(rid, "year.csv", EXPORT.encode(), db_path=db)
    assert res["status"] == "too_many_rows" and res["invoices"] == 0
    assert _rows(db, "SELECT status, content FROM fintech_files") == [{"status": "too_many_rows", "content": None}]
    assert _rows(db, "SELECT id FROM fintech_invoices") == []
    assert fi.MAX_FILE_INVOICES == 2000 and fi.COMMIT_EVERY == 500


def test_a_file_whose_read_was_cut_short_is_read_again_whole(db, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(fi, "COMMIT_EVERY", 1)
    real = fi._store_invoice
    calls = []

    def dies_once(conn, *a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("worker killed")
        return real(conn, *a, **k)
    monkeypatch.setattr(fi, "_store_invoice", dies_once)
    with pytest.raises(RuntimeError):
        fi.ingest_file(rid, "x.csv", EXPORT.encode(), db_path=db)
    assert _rows(db, "SELECT status FROM fintech_files") == [{"status": "reading"}]
    res = fi.ingest_file(rid, "x.csv", EXPORT.encode(), db_path=db)          # the queue's retry
    assert res["status"] == "stored" and res["invoices"] == 1
    assert _rows(db, "SELECT status FROM fintech_files") == [{"status": "stored"}]
    assert fi.ingest_file(rid, "x.csv", EXPORT.encode(), db_path=db)["status"] == "duplicate"


def test_file_text_goes_to_the_object_store_and_expires(db, monkeypatch, tmp_path):
    monkeypatch.setenv("BLOB_STORE", "local")
    monkeypatch.setenv("BLOB_STORE_DIR", str(tmp_path / "blobs"))
    rid = _rid(db)
    res = fi.ingest_file(rid, "x.csv", EXPORT.encode(), db_path=db)
    content = _rows(db, "SELECT content FROM fintech_files WHERE id=?", (res["file_id"],))[0]["content"]
    assert blob_store.is_text_pointer(content) and "HIGH NOON" not in content
    assert "HIGH NOON" in blob_store.resolve_text(content)
    again = fi.reread_file(rid, res["file_id"], db_path=db)
    assert again["ok"] and again["parsed"] == 1
    # Every reader of the kept text goes through the store: the re-read of
    # every file (scripts/bar_reread_fintech.py), and a purge that puts an
    # invoice back to another file's copy.
    assert fi.reread_all(rid, apply=False, db_path=db)["files"][0]["file_id"] == res["file_id"]
    other = EXPORT.replace("HIGH NOON VODKA SELTZER", "HIGH NOON VODKA SELTZER 12PK")
    res2 = fi.ingest_file(rid, "y.csv", other.encode(), db_path=db)
    assert res2["file_id"] != res["file_id"]
    assert fi.purge_file(rid, res2["file_id"], db_path=db)["ok"]
    assert _rows(db, "SELECT COUNT(*) AS n FROM fintech_invoices WHERE restaurant_id=?", (rid,))[0]["n"] == 1
    stored = [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()]
    assert len(stored) == 2
    _exec(db, "UPDATE fintech_files SET created_at='2025-01-01 00:00:00'")
    conn = models.get_conn(db)
    try:
        assert email_intake.expire_file_content(conn, 365) == 2
    finally:
        conn.close()
    assert _rows(db, "SELECT DISTINCT content FROM fintech_files") == [{"content": None}]
    assert [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()] == []


# ── R24: retention and the off-site scrub ──────────────────────────────────

def test_email_intake_in_retention_registry(db):
    assert ops._RETENTION_DAYS["email_intake"] == 180
    assert ops._RETENTION_COLUMN["email_intake"] == "received_at"
    assert ops.retention_state("email_intake")["state"] == "ok"
    special = ops._special_retention()
    assert special["email_intake_unrouted"][0] == 30 and special["fintech_file_content"][0] == 365
    rid = _rid(db)
    for eid, status, age in (("old_spam", "no_route", 40), ("new_spam", "no_route", 5),
                             ("kept", "done", 40), ("ancient", "done", 200)):
        at = (datetime.now(timezone.utc) - timedelta(days=age)).strftime("%Y-%m-%d %H:%M:%S")
        _exec(db, "INSERT INTO email_intake (email_id, restaurant_id, status, received_at) VALUES (?,?,?,?)",
              (eid, rid, status, at))
    out = ops.prune_ledgers(db)
    left = {r["email_id"] for r in _rows(db, "SELECT email_id FROM email_intake")}
    assert left == {"new_spam", "kept"}
    assert out.get("email_intake_unrouted") == 1 and out.get("email_intake") == 1


def test_intake_address_scrubbed_offsite(db, tmp_path):
    import offsite_backup
    rid = _rid(db)
    _route(db, rid)
    _route(db, rid, address="fintech-ffeeddccbbaa9988@intake.resend.app")
    email_intake.receive({"email_id": "e14", "to": [ROUTE], "from": "will@cavnar.ai"}, db_path=db)
    if not os.path.exists(db):
        pytest.skip("the off-site scrub runs on the SQLite snapshot")
    copy = str(tmp_path / "copy.db")
    src, dst = sqlite3.connect(db), sqlite3.connect(copy)
    try:
        src.backup(dst)                # the live file's WAL included
    finally:
        src.close()
        dst.close()
    assert ("email_intake_routes", "address") in offsite_backup.SCRUB_COLUMNS
    assert ("email_intake", "address") in offsite_backup.SCRUB_COLUMNS
    did = offsite_backup.redact(copy)
    assert "email_intake_routes.address" in did["columns"] and "email_intake.address" in did["columns"]
    conn = sqlite3.connect(copy)
    try:
        text = "\n".join(str(r) for r in conn.execute("SELECT * FROM email_intake_routes").fetchall())
        text += "\n".join(str(r) for r in conn.execute("SELECT * FROM email_intake").fetchall())
        assert conn.execute("SELECT COUNT(*) FROM email_intake_routes").fetchone()[0] == 2
    finally:
        conn.close()
    assert "intake.resend.app" not in text


# ── V22: the feed ──────────────────────────────────────────────────────────

def test_invoices_older_than_a_week_say_the_feed_has_stopped(db):
    rid = _rid(db)
    fi.ingest_file(rid, "daily.txt", _printout("07/27/2026"), db_path=db)
    _exec(db, "INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) "
              "VALUES (?, '2026-10-13', 'sales.cat:Liquor', 900, 'ok')", (rid,))
    f = feed_status(rid, today=date(2026, 10, 14), db_path=db)
    assert f["invoices_through"] == "2026-07-27" and f["invoices_through_mdy"] == "7/27/26"
    assert f["sales_through"] == "2026-10-13" and f["sales_through_mdy"] == "10/13/26"
    assert f["stale_days"] == 79 and f["last_file_mdy"]
    # Within a week it is not stopped.
    assert feed_status(rid, today=date(2026, 7, 27) + timedelta(days=FEED_STALE_DAYS),
                       db_path=db)["stale_days"] is None
    # A future invoice date never dates the feed.
    fi.ingest_file(rid, "typo.txt", _printout("10/09/2027").replace(b"101153056", b"999"), db_path=db)
    assert feed_status(rid, today=date(2026, 10, 14), db_path=db)["invoices_through"] == "2026-07-27"
    # The Bar page's payload carries it.
    import bar_cost
    page = bar_cost.build(rid, db_path=db, today=date(2026, 10, 14))
    assert page["feed"]["stale_days"] == 79 and page["feed"]["sales_through_mdy"] == "10/13/26"


def test_no_fintech_file_for_two_days_raises_a_data_health_failure(db, monkeypatch):
    rid = _rid(db)
    _route(db, rid)
    res = fi.ingest_file(rid, "daily.txt", _printout("10/12/2026"), db_path=db)
    # The file arrived Monday 10/12; Wednesday 10/14 is the second business day without one.
    _exec(db, "UPDATE fintech_files SET created_at='2026-10-12 14:00:00' WHERE id=?", (res["file_id"],))
    assert email_intake.watch_feeds(db_path=db, today=date(2026, 10, 13))["late"] == []
    assert data_health.health_rows(rid, db_path=db).get("invoices") is None
    out = email_intake.watch_feeds(db_path=db, today=date(2026, 10, 14))
    assert out["late"] == [rid] and out["ok"] == 1 and out["failed"] == 0
    sh = data_health.health_rows(rid, db_path=db)["invoices"]
    assert sh["consecutive_failures"] == 1 and "No Fintech file since 10/12/26" in sh["last_error"]
    assert sh["data_through"] == "2026-10-12"
    # The admin console raises it as an issue at the first failure.
    import admin_ops
    assert admin_ops.SOURCE_FAIL_ISSUE_AT["invoices"] == 1
    # A weekend is not late: Friday's file, Monday.
    assert email_intake.business_days_after(date(2026, 10, 9), date(2026, 10, 12)) == 1
    # The source in the freshness registry is failing too.
    r = models.get_restaurant(rid, db_path=db)
    st = data_freshness.source_state(r, "invoices", db_path=db,
                                     now=datetime(2026, 10, 14, 18, tzinfo=timezone.utc))
    assert st["error"] and st["as_of_iso"] == "2026-10-12"
    # A file arriving clears it.
    email_intake.receive({"email_id": "e15", "to": [ROUTE], "from": "processor@fintech.net"}, db_path=db)
    _stub_resend(monkeypatch, "e15", {"from": "processor@fintech.net", "authentication": PASSED},
                 [{"id": "a", "filename": "d.txt", "download_url": "u", "size": 5}],
                 {"u": _printout("10/13/2026").replace(b"101153056", b"101153057")})
    assert email_intake.process("e15", db_path=db)["status"] == "done"
    sh = data_health.health_rows(rid, db_path=db)["invoices"]
    assert sh["consecutive_failures"] == 0 and sh["data_through"] == "2026-10-13"


def test_the_invoices_source_is_registered(db):
    assert data_freshness.SOURCES["invoices"]["expected_lag"] == 1.0
    assert data_freshness.SOURCES["invoices"]["grace"] == 3.0
    assert data_health.OWNER_LABEL["invoices"] == "Bar invoices"
    assert "invoices" in data_freshness.MODULE_SOURCES["bar"]
    rid = _rid(db)
    r = models.get_restaurant(rid, db_path=db)
    assert data_freshness.source_state(r, "invoices", db_path=db)["state"] == "not_connected"
    _route(db, rid)
    assert data_freshness.source_state(r, "invoices", db_path=db)["state"] == "unknown"
    yesterday = (date.today() - timedelta(days=1)).strftime("%m/%d/%Y")
    fi.ingest_file(rid, "d.txt", _printout(yesterday), db_path=db)
    assert data_freshness.source_state(r, "invoices", db_path=db)["state"] == "current"


def test_the_intake_jobs_are_scheduled_jobs():
    import inspect
    import jobs_registry
    import scheduler
    src = inspect.getsource(scheduler.scheduler_loop)
    for name, fn in (("intake_reconcile", "reconcile_stale"), ("invoice_feed_watch", "watch_feeds")):
        assert jobs_registry.JOBS[name]["target"] == ("email_intake", fn)
        assert f'run_job("{name}"' in src
        assert jobs_registry.JOBS[name]["sends"] is False


def test_a_late_feed_with_a_held_file_says_it_is_held_and_where_to_release_it(db):
    """EJ's 10/15-10/16/26: Fintech's own mail failed DMARC twice, both files
    sat held, and the console read only "sync failing"."""
    rid = _rid(db)
    _route(db, rid)
    res = fi.ingest_file(rid, "daily.txt", _printout("10/12/2026"), db_path=db)
    _exec(db, "UPDATE fintech_files SET created_at='2026-10-12 14:00:00' WHERE id=?", (res["file_id"],))
    _exec(db, "INSERT INTO email_intake (email_id, address, restaurant_id, kind, sender, subject, status, received_at) "
              "VALUES ('h1', ?, ?, 'fintech', 'processor@fintech.net', 'HHB, LLC: fintech.net has generated a file', "
              "'held_auth', '2026-10-13 20:27:42')", (ROUTE, rid))
    email_intake.watch_feeds(db_path=db, today=date(2026, 10, 14))
    sh = data_health.health_rows(rid, db_path=db)["invoices"]
    assert "1 file came and is held, unread" in sh["last_error"] and "Release" in sh["last_error"]
    assert sh["error_class"] == "held"


# Unsigned mail with no DMARC verdict against it: the only mail a file may
# vouch for. Fintech's own 10/15 and 10/16/26 mail read DMARC FAIL - the
# same verdict a spoofed From produces - and is held (FC/Bar audit
# 10/16/26, ING-05 / SEC-02; tests/test_fix_fcbar_c2.py).
UNSIGNED = {"spf": "gray", "dkim": "gray", "dmarc": "gray"}
FINTECH_1015 = {"spf": "gray", "dkim": "gray", "dmarc": "fail"}


def _unsigned_email(db, monkeypatch, rid, eid, body, auth=UNSIGNED):
    email_intake.receive({"email_id": eid, "to": [ROUTE], "from": "processor@fintech.net"}, db_path=db)
    _stub_resend(monkeypatch, eid, {"from": "processor@fintech.net", "authentication": auth},
                 [{"id": "a", "filename": "d.txt", "download_url": "u", "size": len(body)}], {"u": body})
    return email_intake.process(eid, db_path=db)


def test_unsigned_mail_whose_file_vouches_for_itself_is_read_without_a_release(db, monkeypatch):
    """Owner, 10/16/26 ("I shouldn't have to keep doing this"): Fintech's mail
    went out unsigned twice. From a listed sender, unsigned (not forged), its
    file read without writing names only a store and a distributor already on
    file: it is read, and marked as having vouched for itself."""
    rid = _rid(db)
    _route(db, rid)
    fi.ingest_file(rid, "first.txt", _printout("10/09/2026"), db_path=db)
    body = _printout("10/14/2026").replace(b"101153056", b"101153099")
    out = _unsigned_email(db, monkeypatch, rid, "u1", body)
    assert out["status"] == "done" and out["files"][0]["vouched"] is True
    assert _rows(db, "SELECT COUNT(*) AS n FROM fintech_invoices WHERE restaurant_id=?", (rid,))[0]["n"] == 2


def test_unsigned_mail_with_a_new_distributor_or_a_forged_signature_stays_held(db, monkeypatch):
    rid = _rid(db)
    _route(db, rid)
    fi.ingest_file(rid, "first.txt", _printout("10/09/2026"), db_path=db)
    stranger = _printout("10/14/2026").replace(b"Chicago Beverage System, Inc dba Windy City", b"Acme Liquor Wholesale") \
        .replace(b"101153056", b"55501")
    out = _unsigned_email(db, monkeypatch, rid, "u2", stranger)
    assert out["status"] == "held_auth"
    row = _rows(db, "SELECT detail FROM email_intake WHERE email_id='u2'")[0]
    assert "didn't vouch for themselves" in row["detail"] and "new to this restaurant" in row["detail"]
    forged = {"spf": "fail", "dkim": "gray", "dmarc": "fail"}
    good = _printout("10/14/2026").replace(b"101153056", b"101153098")
    assert _unsigned_email(db, monkeypatch, rid, "u3", good, auth=forged)["status"] == "held_auth"
    # DMARC fail with nothing signed is a forgery verdict too (ING-05).
    assert _unsigned_email(db, monkeypatch, rid, "u4", good, auth=FINTECH_1015)["status"] == "held_auth"
