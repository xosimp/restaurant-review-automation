"""The Fintech invoice intake (10/14/26): Resend Receiving's `email.received`
on the signed /webhooks/resend endpoint -> email_intake (route, ledger,
queue) -> fintech_invoices (the daily printout and the portal's CSV export).

Resend is never reached: the API reads and the download are stubbed.
"""
import base64
import hashlib
import hmac
import json
import time

import pytest
from flask import Flask

import email_intake
import fintech_invoices as fi
import job_queue
import models
import webhook_routes

# The printout's shape, from a real Fintech file (amounts changed).
PRINTOUT = """Page: 1
Store Number   : 000001  St. Charles, IL
Distributor    : Chicago Beverage System, Inc dba Windy City
Vendor ID      :
Invoice Number : 101153056
Invoice Date   : 10/09/2026
Invoice Total  : $439.00
Due Date       : 10/09/2026
PO Number      :

Item            Description                           Qty      Price           Cost
----------      ------------------------------       ----   --------      ---------
13682           HALF ACRE DAISY CUTTER K 1/2BB        2       $199.00       $458.00
00121           POS EMPTY KEG $30 MISCELLANEOU       -1         $0.00      ($30.00)
00500           POS FUEL CHARGE MISCELLANEOUS         1        $11.00        $11.00
                                                                       ------------
                                                        Invoice Total:      $439.00

Page: 2
Store Number   : 000001  St. Charles, IL
Distributor    : Southern Glazer's Wine & Spirits of IL
Vendor ID      :
Invoice Number : 02508778
Invoice Date   : 10/13/2026
Invoice Total  : $1,265.79
Due Date       : 10/20/2026

Item            Description                           Qty      Price           Cost
----------      ------------------------------       ----   --------      ---------
000028728       BACARDI RUM SUPERIOR WHITE 80         1       $249.50       $249.50
000561664       WHISTLEPIG BBN PIGGYBACK 6YR 1        8       $126.00      $1008.00
000028728       BACARDI RUM SUPERIOR WHITE 80         1         $2.29         $2.29
0               Summary Level Adjustments             1         $0.00         $6.00
                                                                       ------------
                                                        Invoice Total:     $1265.79
"""

EXPORT = (
    "Retailer Name,Vendor Name,Location ID,Customer ID,Invoice Number,Invoice Date,Invoice Total,"
    "Line Item Count,Received Date,Bank Account,Withdraw Date,Transaction ID,Trace Number,Batch ID,"
    "Product Number,Product Description,U.P.C,Unit Of Measure,Item Quantity,Item Cost,Item Adjustment\n"
    "Simple EJs,\"Breakthru Beverage Illinois, LLC\",000001,700442049,0128850598,09/15/2026,$264.00,2,"
    "09/15/2026,******8991,10/14/2026,239356661,091409000000001,A,9637400,HIGH NOON VODKA SELTZER,"
    "085000000000,CA,6,$42.00,$0.00\n"
    "Simple EJs,\"Breakthru Beverage Illinois, LLC\",000001,700442049,0128850598,09/15/2026,$264.00,2,"
    "09/15/2026,******8991,10/14/2026,239356661,091409000000001,A,9039158,Service Fee,9039158,BO,1,"
    "$12.00,$0.00\n"
)

ROUTE = "fintech-abc123def4@intake.resend.app"


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda p=None: real(p or db_path))
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    return db_path


def _rid(db_path):
    return models.create_restaurant(models.Restaurant(name="EJ's", owner_email="o@x.test"), db_path=db_path)


def _route(db_path, rid):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO email_intake_routes (address, restaurant_id, kind, active, created_at) "
                 "VALUES (?,?, 'fintech', 1, '2026-10-14 12:00:00')", (ROUTE, rid))
    conn.commit()
    conn.close()


def _rows(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ── reading ────────────────────────────────────────────────────────────────

def test_the_printout_reads_every_invoice_and_balances():
    invs = fi.parse_printout(PRINTOUT)
    assert [i["invoice_number"] for i in invs] == ["101153056", "02508778"]
    windy, sgws = invs
    assert windy["invoice_date"] == "2026-10-09" and windy["total"] == 439.0
    assert windy["lines"][0]["deposit"] == 60.0          # two kegs' $30 deposit inside the cost
    assert windy["lines"][1]["cost"] == -30.0             # an empty keg back
    assert sgws["total"] == 1265.79
    assert round(sum(ln["cost"] for ln in sgws["lines"]), 2) == 1265.79
    # a description ending in a number keeps it; the qty is the next field
    assert sgws["lines"][1]["description"].endswith("6YR 1") and sgws["lines"][1]["qty"] == 8


def test_the_export_reads_unit_of_measure_and_upc_and_drops_the_bank_columns():
    fmt, kept, invs = fi.read("bankactivity_detail.csv", EXPORT.encode())
    assert fmt == "export" and len(invs) == 1
    lines = invs[0]["lines"]
    assert lines[0]["uom"] == "CA" and fi.uom_word("CA") == "case" and fi.uom_word("BO") == "bottle"
    assert fi.uom_word("BL") == "bottle"
    assert lines[0]["upc"] == "085000000000" and lines[0]["item_code"] == "9637400"
    assert lines[0]["cost"] == 252.0                      # 6 cases at $42 each
    assert "Bank Account" not in kept and "******8991" not in kept and "Trace Number" not in kept
    assert "HIGH NOON" in kept


def test_a_file_that_is_neither_format_writes_nothing(db):
    rid = _rid(db)
    res = fi.ingest_file(rid, "menu.txt", b"just a note", db_path=db)
    assert res["status"] == "unrecognized"
    assert _rows(db, "SELECT * FROM fintech_invoices") == []


# ── storing ────────────────────────────────────────────────────────────────

def test_lines_are_classified_and_a_file_sent_twice_is_one_file(db):
    rid = _rid(db)
    res = fi.ingest_file(rid, "daily.txt", PRINTOUT.encode(), db_path=db)
    assert res["status"] == "stored" and res["invoices"] == 2 and res["unbalanced"] == []
    kinds = {r["description"]: r["kind"] for r in _rows(db, "SELECT description, kind FROM fintech_invoice_lines")}
    assert kinds["POS EMPTY KEG $30 MISCELLANEOU"] == "keg_credit"
    assert kinds["POS FUEL CHARGE MISCELLANEOUS"] == "fee"
    assert kinds["Summary Level Adjustments"] == "adjustment"
    assert sorted(_rows(db, "SELECT kind FROM fintech_invoice_lines WHERE description LIKE 'BACARDI%' "
                            "ORDER BY line_no"), key=lambda r: r["kind"]) == [{"kind": "charge"}, {"kind": "item"}]
    assert {r["balanced"] for r in _rows(db, "SELECT balanced FROM fintech_invoices")} == {1}
    assert fi.ingest_file(rid, "daily.txt", PRINTOUT.encode(), db_path=db)["status"] == "duplicate"
    assert len(_rows(db, "SELECT id FROM fintech_files")) == 1


def test_the_export_replaces_the_printout_and_never_the_other_way(db):
    rid = _rid(db)
    printout = PRINTOUT.replace("Southern Glazer's Wine & Spirits of IL", "Breakthru Beverage Illinois, LLC") \
                       .replace("02508778", "0128850598")
    fi.ingest_file(rid, "a.txt", printout.encode(), db_path=db)
    fi.ingest_file(rid, "b.csv", EXPORT.encode(), db_path=db)
    inv = _rows(db, "SELECT * FROM fintech_invoices WHERE invoice_number='0128850598'")[0]
    assert inv["source_format"] == "export"
    assert [r["uom"] for r in _rows(db, "SELECT uom FROM fintech_invoice_lines WHERE invoice_id=? ORDER BY line_no",
                                    (inv["id"],))] == ["CA", "BO"]
    fi.ingest_file(rid, "c.txt", (printout + "\n").encode(), db_path=db)   # a resend of the printout
    again = _rows(db, "SELECT * FROM fintech_invoices WHERE invoice_number='0128850598'")[0]
    assert again["source_format"] == "export"


# ── the webhook and the job ────────────────────────────────────────────────

_SECRET = "whsec_" + base64.b64encode(b"intake-secret").decode()


def _signed(body):
    ts = int(time.time())
    key = base64.b64decode(_SECRET.split("_", 1)[1])
    sig = base64.b64encode(hmac.new(key, f"msg_9.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"svix-id": "msg_9", "svix-timestamp": str(ts), "svix-signature": f"v1,{sig}",
            "Content-Type": "application/json"}


def _post(event):
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    body = json.dumps(event).encode()
    return app.test_client().post("/webhooks/resend", data=body, headers=_signed(body))


def test_mail_to_an_intake_address_is_recorded_once_and_queued(db, monkeypatch):
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", _SECRET)
    rid = _rid(db)
    _route(db, rid)
    event = {"type": "email.received", "data": {"email_id": "em_1", "to": [f"Intake <{ROUTE.upper()}>"],
                                                "from": "Will <will@cavnar.ai>", "subject": "Fintech daily"}}
    assert _post(event).status_code == 200
    assert _post(event).status_code == 200                 # Resend retrying
    rows = _rows(db, "SELECT * FROM email_intake")
    assert len(rows) == 1 and rows[0]["status"] == "queued" and rows[0]["restaurant_id"] == rid
    jobs = _rows(db, "SELECT * FROM jobs WHERE kind='email_intake'")
    assert len(jobs) == 1 and json.loads(jobs[0]["payload_json"]) == {"email_id": "em_1"}


def test_mail_to_any_other_address_is_kept_but_never_fetched(db, monkeypatch):
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", _SECRET)
    _rid(db)
    _post({"type": "email.received", "data": {"email_id": "em_2", "to": ["guess@intake.resend.app"]}})
    assert _rows(db, "SELECT status FROM email_intake") == [{"status": "no_route"}]
    assert _rows(db, "SELECT id FROM jobs WHERE kind='email_intake'") == []


def test_an_unsigned_received_event_is_refused(db, monkeypatch):
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", _SECRET)
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    body = json.dumps({"type": "email.received", "data": {"email_id": "em_3", "to": [ROUTE]}}).encode()
    resp = app.test_client().post("/webhooks/resend", data=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 403
    assert _rows(db, "SELECT id FROM email_intake") == []


def test_the_job_fetches_the_attachment_and_stores_the_invoices(db, monkeypatch):
    rid = _rid(db)
    _route(db, rid)
    email_intake.receive({"email_id": "em_4", "to": [ROUTE]}, db_path=db)
    api = {"/emails/receiving/em_4": {"from": "Fintech <noreply@fintech.net>", "subject": "Daily file"},
           "/emails/receiving/em_4/attachments": {"data": [{"id": "a1", "filename": "daily.txt",
                                                            "download_url": "https://x.test/a1", "size": 900}]}}
    monkeypatch.setattr(email_intake, "_api_get", lambda path: api[path])
    monkeypatch.setattr(email_intake, "_download", lambda url: PRINTOUT.encode())
    out = email_intake.process("em_4", db_path=db)
    assert out["status"] == "done"
    row = _rows(db, "SELECT * FROM email_intake WHERE email_id='em_4'")[0]
    assert row["status"] == "done" and row["files"] == 1 and row["sender"].startswith("Fintech")
    assert len(_rows(db, "SELECT id FROM fintech_invoices WHERE restaurant_id=?", (rid,))) == 2


def test_gmails_forwarding_confirmation_is_kept_for_a_person_to_confirm(db, monkeypatch):
    rid = _rid(db)
    _route(db, rid)
    email_intake.receive({"email_id": "em_5", "to": [ROUTE]}, db_path=db)
    api = {"/emails/receiving/em_5": {"from": "Gmail Team <forwarding-noreply@google.com>",
                                      "subject": "(#123456789) Gmail Forwarding Confirmation", "text": "click"},
           "/emails/receiving/em_5/attachments": {"data": []}}
    monkeypatch.setattr(email_intake, "_api_get", lambda path: api[path])
    assert email_intake.process("em_5", db_path=db)["status"] == "gmail_confirmation"
    row = _rows(db, "SELECT * FROM email_intake WHERE email_id='em_5'")[0]
    assert row["status"] == "gmail_confirmation" and "123456789" in row["subject"]
    assert _rows(db, "SELECT id FROM fintech_files") == []


def test_a_failed_fetch_marks_the_row_and_raises_for_the_queue_to_retry(db, monkeypatch):
    rid = _rid(db)
    _route(db, rid)
    email_intake.receive({"email_id": "em_6", "to": [ROUTE]}, db_path=db)

    def boom(path):
        raise RuntimeError("Resend receiving answered 503")
    monkeypatch.setattr(email_intake, "_api_get", boom)
    with pytest.raises(RuntimeError):
        email_intake.run_queued({"payload": {"email_id": "em_6"}})
    assert _rows(db, "SELECT status FROM email_intake WHERE email_id='em_6'") == [{"status": "failed"}]
