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
# The route's senders (bar fix round 10/15/26, V12) and Resend's verdict on
# a genuine email (the Received-emails API's `authentication`).
SENDERS = "will@cavnar.ai\n@fintech.net\nforwarding-noreply@google.com"
PASSED = {"spf": "pass", "dkim": "pass", "dmarc": "pass"}


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
    conn.execute("INSERT INTO email_intake_routes (address, restaurant_id, kind, active, created_at, allowed_senders) "
                 "VALUES (?,?, 'fintech', 1, '2026-10-14 12:00:00', ?)", (ROUTE, rid, SENDERS))
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
    assert fi.uom_word("KE") == "keg" and fi.uom_word("DS") == "deposit"
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


# The export's invoice as the printout prints it (same total, its own spelling
# of the distributor and an unpadded number, no unit of measure).
EXPORT_AS_PRINTOUT = """Page: 1
Store Number   : 000001  St. Charles, IL
Distributor    : BREAKTHRU BEVERAGE ILLINOIS LLC
Vendor ID      :
Invoice Number : 128850598
Invoice Date   : 09/15/2026
Invoice Total  : $264.00
Due Date       : 10/15/2026

Item            Description                           Qty      Price           Cost
----------      ------------------------------       ----   --------      ---------
9637400         HIGH NOON VODKA SELTZER               6        $42.00       $252.00
9039158         Service Fee                           1        $12.00        $12.00
                                                        Invoice Total:      $264.00
"""


def test_the_export_replaces_the_printout_and_never_the_other_way(db):
    """10/15/26 (R3): the printout's own spelling and number padding — the
    10/14 version of this test renamed the printout's distributor to match,
    so the rule was only proven for identical strings."""
    rid = _rid(db)
    printout = EXPORT_AS_PRINTOUT
    fi.ingest_file(rid, "a.txt", printout.encode(), db_path=db)
    fi.ingest_file(rid, "b.csv", EXPORT.encode(), db_path=db)
    inv = _rows(db, "SELECT * FROM fintech_invoices WHERE invoice_number='0128850598'")[0]
    assert inv["source_format"] == "export"
    assert [r["uom"] for r in _rows(db, "SELECT uom FROM fintech_invoice_lines WHERE invoice_id=? ORDER BY line_no",
                                    (inv["id"],))] == ["CA", "BO"]
    res = fi.ingest_file(rid, "c.txt", (printout + "\n").encode(), db_path=db)   # a resend of the printout
    assert res["status"] == "unchanged" and res["invoices"] == 0 and res["kept"] == 1
    again = _rows(db, "SELECT * FROM fintech_invoices WHERE invoice_number='0128850598'")[0]
    assert again["source_format"] == "export"
    assert len(_rows(db, "SELECT id FROM fintech_invoices")) == 1


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
    email_intake.receive({"email_id": "em_4", "to": [ROUTE], "from": "noreply@fintech.net"}, db_path=db)
    api = {"/emails/receiving/em_4": {"from": "Fintech <noreply@fintech.net>", "subject": "Daily file",
                                      "authentication": PASSED},
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
    email_intake.receive({"email_id": "em_5", "to": [ROUTE], "from": "forwarding-noreply@google.com"}, db_path=db)
    api = {"/emails/receiving/em_5": {"from": "Gmail Team <forwarding-noreply@google.com>",
                                      "subject": "(#123456789) Gmail Forwarding Confirmation", "text": "click",
                                      "authentication": PASSED},
           "/emails/receiving/em_5/attachments": {"data": []}}
    monkeypatch.setattr(email_intake, "_api_get", lambda path: api[path])
    assert email_intake.process("em_5", db_path=db)["status"] == "gmail_confirmation"
    row = _rows(db, "SELECT * FROM email_intake WHERE email_id='em_5'")[0]
    assert row["status"] == "gmail_confirmation" and "123456789" in row["subject"]
    assert _rows(db, "SELECT id FROM fintech_files") == []


def test_a_failed_fetch_marks_the_row_and_raises_for_the_queue_to_retry(db, monkeypatch):
    rid = _rid(db)
    _route(db, rid)
    email_intake.receive({"email_id": "em_6", "to": [ROUTE], "from": "noreply@fintech.net"}, db_path=db)

    def boom(path):
        raise RuntimeError("Resend receiving answered 503")
    monkeypatch.setattr(email_intake, "_api_get", boom)
    with pytest.raises(RuntimeError):
        email_intake.run_queued({"payload": {"email_id": "em_6"}})
    assert _rows(db, "SELECT status FROM email_intake WHERE email_id='em_6'") == [{"status": "failed"}]


def test_the_invoice_details_report_reads_its_own_column_names(db):
    """Fintech's My Invoices "Invoice Details" CSV (10/14/26): Quantity, Unit
    Cost, Extended Price, UPC Number, Invoice Due Date."""
    rid = _rid(db)
    csv_ = ("Vendor Name,Retailer Name,Invoice Date,Invoice Due Date,Invoice Number,Invoice Total,Product Number,"
            "Quantity,Unit Cost,Unit of Measure,UPC Number,Product Description,Total Adjustments,Extended Price\n"
            "Breakthru,EJs,09/22/2026,10/22/2026,0128949524,$84.00,9006996,2,$42.00,CA,611269991000,RED BULL 8.4Z,$0.00,$84.00\n")
    res = fi.ingest_file(rid, "invoices-report.csv", csv_.encode(), db_path=db)
    assert res["status"] == "stored" and res["unbalanced"] == []
    ln = _rows(db, "SELECT upc, uom, qty, unit_price, cost FROM fintech_invoice_lines")[0]
    assert ln == {"upc": "611269991000", "uom": "CA", "qty": 2.0, "unit_price": 42.0, "cost": 84.0}
    assert _rows(db, "SELECT due_date FROM fintech_invoices")[0]["due_date"] == "2026-10-22"


# ══ Bar fix round 10/15/26 (W1: ingest & parse) ════════════════════════════

# The payment-activity export's real header (EJ's file, 10/14/26): "Item
# Adjustments" plural, "Extended Price", "Bank Account Number", two
# "Customer ID" columns, "<null>" for an empty cell.
BANK_HEAD = ("Retailer Name,Vendor Name,Location ID,Customer ID,Invoice Number,Invoice Date,Invoice Total,"
             "Line Item Count,Received Date,Bank Account Number,Product Number,Product Description,U.P.C,"
             "Unit Of Measure,Item Quantity,Item Cost,Item Adjustments,Extended Price,GL Code,Retailer Item Number,"
             "Customer ID,Pack UPC, PPC,Retailer Relationship ID,PO Number,PO Date,Ref Invoice Number,Due Date,"
             "Status\n")

WINDY = "Chicago Beverage System, Inc dba Windy City"
EUCLID = "Euclid Beverage, LLC. - North Aurora"
SUPERIOR = "Superior Beverage Co., Inc."
BREAKTHRU = "Breakthru Beverage Illinois, LLC"


def _bank(vendor, inv, day, total, code, desc, uom, qty, each, adj, ext, upc="<null>", loc="000001",
          po="<null>", po_date="<null>", ref="<null>"):
    return (f'Simple EJs Kitchen & Tap,"{vendor}",{loc},6695,{inv},{day},"{total}",1,{day},********8998,{code},'
            f'{desc},{upc},{uom},{qty},"{each}","{adj}","{ext}",Beer,<null>,<null>,{upc},1,<null>,{po},{po_date},'
            f'{ref},{day},Paid\n')


def _lines(db, rid):
    from bar.purchases import _lines as lines
    return lines(rid, db_path=db)


def _beer(db, rid):
    return round(sum(l["net"] for l in _lines(db, rid) if l["category"] == "beer"), 2)


def _printout(dist, num, day, total, lines, store="000001", extra=""):
    body = "".join(f"{c:<16}{d:<38}{q:>4}{p:>14}{x:>15}\n" for c, d, q, p, x in lines)
    return (f"Page: 1\nStore Number   : {store}  St. Charles, IL\nDistributor    : {dist}\nVendor ID      :\n"
            f"Invoice Number : {num}\nInvoice Date   : {day}\nInvoice Total  : {total}\nDue Date       : {day}\n"
            f"{extra}\nItem            Description                           Qty      Price           Cost\n"
            f"----------      ------------------------------       ----   --------      ---------\n{body}"
            f"                                                        Invoice Total:      {total}\n \n")


# ── V4: keg deposits on the export, empties as deposits ───────────────────

def test_export_keg_deposit_and_empties_are_not_beer_cost(db):
    """FIN-2 / PRC-6: every EJ's invoice is export format, where a keg's cost
    is qty x price + $30 x qty in Item Adjustments; Euclid's EMPTY (CA) and
    Superior's EMPTIES (KE) read as negative beer."""
    rid = _rid(db)
    csv_ = (BANK_HEAD
            + _bank(WINDY, "101153056", "10/9/2026", "$428.00", "13682", "HALF ACRE DAISY CUTTER K 1/2BBL", "KE", 2,
                    "$199.00", "$60.00", "$458.00")
            + _bank(WINDY, "101153056", "10/9/2026", "$428.00", "00121", "POS EMPTY KEG $30 MISCELLANEOUS", "DS", -1,
                    "$0.00", "($30.00)", "($30.00)")
            + _bank(EUCLID, "W-4791847", "10/7/2026", "$87.00", "1993", "EMPTY 1/2 BBL", "CA", -4, "$30.00", "$0.00",
                    "($120.00)")
            + _bank(EUCLID, "W-4791847", "10/7/2026", "$87.00", "1461154", "EAGLE PARK SLTZR SPEKTO KOOLER 1/2BBL",
                    "KE", 1, "$177.00", "$30.00", "$207.00")
            + _bank(SUPERIOR, "867148", "10/13/2026", "$110.00", "97094", "EMPTIES-AB BEER 1/2 BBL", "KE", -3,
                    "$30.00", "$0.00", "($90.00)")
            + _bank(SUPERIOR, "867148", "10/13/2026", "$110.00", "150094", "ULTRA 1/2 BARREL", "KE", 2, "$70.00",
                    "$60.00", "$200.00"))
    res = fi.ingest_file(rid, "bankactivity_detail.csv", csv_.encode(), db_path=db)
    assert res["status"] == "stored" and res["invoices"] == 3 and res["unbalanced"] == []
    by = {r["description"]: r for r in _rows(db, "SELECT description, kind, deposit, adjustment, cost "
                                                 "FROM fintech_invoice_lines")}
    assert by["HALF ACRE DAISY CUTTER K 1/2BBL"]["deposit"] == 60.0
    assert by["HALF ACRE DAISY CUTTER K 1/2BBL"]["adjustment"] is None
    assert by["EAGLE PARK SLTZR SPEKTO KOOLER 1/2BBL"]["deposit"] == 30.0
    for empty in ("POS EMPTY KEG $30 MISCELLANEOUS", "EMPTY 1/2 BBL", "EMPTIES-AB BEER 1/2 BBL"):
        assert by[empty]["kind"] == "keg_credit", empty
    lines = _lines(db, rid)
    assert {l["category"] for l in lines if l["kind"] == "keg_credit"} == {"deposit"}
    # beer is what the beer cost: 2 x 199 + 177 + 2 x 70
    assert _beer(db, rid) == 715.0
    assert round(sum(l["net"] for l in lines if l["category"] == "deposit"), 2) == -240.0


def test_a_returned_keg_credit_takes_its_deposit_back_out():
    """A keg credited back (-1 at $129, cost -$159) is -$129 of beer and -$30 of deposit."""
    ln = fi._settle(fi._line("1461154", "EAGLE PARK 1/2BBL", -1.0, 129.0, -159.0, uom="KE", raw_adjustment=-30.0))
    assert ln["deposit"] == -30.0 and ln["adjustment"] is None


def test_a_keg_deposit_is_a_whole_30_per_keg():
    """EDG-8: a keg line with a $5 a keg discount is not a $50 deposit; a $10
    difference on a keg is an adjustment, never a deposit."""
    two = fi._settle(fi._line("1", "ULTRA 1/2 BARREL", 2.0, 100.0, 250.0, uom="KE"))
    assert two["deposit"] is None and two["adjustment"] == 50.0
    one = fi._settle(fi._line("1", "ULTRA 1/2 BARREL", 1.0, 100.0, 110.0, uom="KE"))
    assert one["deposit"] is None and one["adjustment"] == 10.0
    many = fi._settle(fi._line("1", "ULTRA 1/2 BARREL", 1.0, 100.0, 160.0, uom="KE"))
    assert many["deposit"] is None, "two deposits on one keg is not a deposit"


# ── V26: the parser's written forms ───────────────────────────────────────

@pytest.mark.parametrize("text", ["($30.00)", "-$30.00", "$-30.00", "30.00-", "$30.00-", "CR 30.00", "30.00 CR",
                                  "30.00 CREDIT", "-30"])
def test_credits_in_every_written_form_are_negative(text):
    assert fi.money(text) == -30.0


@pytest.mark.parametrize("text,want", [("$1,467.22", 1467.22), ("1813.29", 1813.29), ("$0.00", 0.0), ("", None),
                                       ("<null>", None), ("n/a", None), ("$", None)])
def test_money_reads_amounts_and_blanks(text, want):
    assert fi.money(text) == want


def test_a_credit_written_as_dollar_minus_is_a_credit_on_the_invoice(db):
    """EDG-14: '$-300.00' was stored as +300 and the invoice balanced."""
    rid = _rid(db)
    csv_ = (BANK_HEAD + _bank(BREAKTHRU, "C1", "10/1/2026", "$-300.00", "9008508", "TITOS VDK HANDMADE 1L", "CA", -1,
                              "$300.00", "$0.00", "$-300.00"))
    fi.ingest_file(rid, "c.csv", csv_.encode(), db_path=db)
    assert _rows(db, "SELECT cost FROM fintech_invoice_lines") == [{"cost": -300.0}]
    assert _rows(db, "SELECT total, balanced FROM fintech_invoices") == [{"total": -300.0, "balanced": 1}]


def test_a_line_without_a_product_number_is_still_a_product(db):
    """EDG-7: a blank or 0000 product number filed $600 of Tito's as fees."""
    rid = _rid(db)
    csv_ = (BANK_HEAD
            + _bank("Craft House", "X1", "10/1/2026", "$610.00", "<null>", "TITOS VDK 1L", "CA", 2, "$300.00", "$0.00",
                    "$600.00")
            + _bank("Craft House", "X1", "10/1/2026", "$610.00", "0000", "Summary Level Adjustments", "EA", 1,
                    "$0.00", "$10.00", "$10.00"))
    fi.ingest_file(rid, "x.csv", csv_.encode(), db_path=db)
    kinds = {r["description"]: r["kind"] for r in _rows(db, "SELECT description, kind FROM fintech_invoice_lines")}
    assert kinds == {"TITOS VDK 1L": "item", "Summary Level Adjustments": "adjustment"}
    liquor = [l for l in _lines(db, rid) if l["category"] == "liquor"]
    assert round(sum(l["net"] for l in liquor), 2) == 600.0


def test_a_printout_line_discount_is_not_a_deposit():
    """EDG-8: a $10 discount became a -$10 deposit (overstating liquor) and
    $19.44 of folded tax was removed; cost is the truth."""
    text = _printout("Southern Glazer's Wine & Spirits of IL", "501", "10/01/2026", "$2517.44", [
        ("9008508", "TITOS VDK 1L", "6", "$250.00", "$1490.00"),
        ("561664", "WHISTLEPIG BBN PIGGYBACK 6YR 1", "8", "$126.00", "$1027.44")])
    lines = fi.parse_printout(text)[0]["lines"]
    assert [(l["deposit"], l["adjustment"], l["cost"]) for l in lines] == [(None, -10.0, 1490.0),
                                                                        (None, 19.44, 1027.44)]


def test_a_quantity_with_a_comma_is_read_and_an_unreadable_one_is_never_zero(db):
    """EDG-21: '1,200' read as 0 and the line cost $0."""
    assert fi.number("1,200") == 1200.0 and fi.number("2-") == -2.0 and fi.number("abc") is None
    rid = _rid(db)
    head = "Vendor Name,Invoice Number,Invoice Date,Invoice Total,Product Number,Product Description,Unit Of Measure," \
           "Item Quantity,Item Cost\n"
    fi.ingest_file(rid, "q.csv", (head + 'Craft House,Q1,10/01/2026,"$1,200.00",77,CANS,CA,"1,200",$1.00\n'
                                  + "Craft House,Q2,10/01/2026,$50.00,78,BOTTLES,CA,lots,$5.00\n").encode(), db_path=db)
    got = {r["invoice_number"]: r for r in _rows(
        db, "SELECT i.invoice_number, i.balanced, l.qty, l.cost, l.unparsed, l.kind FROM fintech_invoice_lines l "
            "JOIN fintech_invoices i ON i.id=l.invoice_id")}
    assert got["Q1"]["qty"] == 1200.0 and got["Q1"]["cost"] == 1200.0 and got["Q1"]["balanced"] == 1
    assert got["Q2"]["qty"] is None and got["Q2"]["cost"] is None and got["Q2"]["kind"] == "unparsed"
    assert got["Q2"]["balanced"] == 0, "a line that could not be read leaves its invoice unbalanced, not short"


def test_an_unreadable_printout_line_is_kept_and_unbalances_its_invoice(db):
    """EDG-13: a line without '$' was skipped and the invoice counted half."""
    rid = _rid(db)
    text = _printout("Breakthru Beverage Illinois, LLC", "501", "10/01/2026", "$1500.00", [
        ("9008508", "TITOS VDK 1L", "3", "$250.00", "$750.00"),
        ("9008509", "KETEL ONE 1L", "3", "250.00", "750.00")])
    res = fi.ingest_file(rid, "p.txt", text.encode(), db_path=db)
    assert res["unbalanced"] == ["501"]
    lines = _lines(db, rid)
    assert [l["description"] for l in lines] == ["TITOS VDK 1L"]
    assert lines[0]["balanced"] is False
    assert _rows(db, "SELECT kind FROM fintech_invoice_lines ORDER BY line_no") == [{"kind": "item"},
                                                                                    {"kind": "unparsed"}]


# ── V27: dates ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["2026-10-02T00:00:00", "2026-10-02 00:00:00", "10/02/2026 00:00:00", "10-02-2026",
                                  "Oct 2, 2026", "October 2 2026", "2-Oct-2026", "20261002", "10/2/26", "2026/10/02"])
def test_an_invoice_date_with_a_time_or_another_form_is_read(text):
    assert fi.iso_date(text) == "2026-10-02"


def test_a_file_without_dates_is_not_stored_as_read(db):
    """EDG-12: a CSV with no readable Invoice Date was 'stored' and every
    invoice invisible."""
    rid = _rid(db)
    head = "Vendor Name,Invoice Number,Invoice Date,Invoice Total,Product Number,Product Description,Unit Of Measure," \
           "Item Quantity,Item Cost\n"
    res = fi.ingest_file(rid, "d.csv", (head + "Craft,D1,someday,$10.00,1,X,CA,1,$10.00\n").encode(), db_path=db)
    assert res["status"] == "unrecognized" and res["undated"] == 1
    assert _rows(db, "SELECT id FROM fintech_invoices") == []
    res = fi.ingest_file(rid, "e.csv", (head + "Craft,D1,someday,$10.00,1,X,CA,1,$10.00\n"
                                        + "Craft,D2,10/01/2026,$10.00,1,X,CA,1,$10.00\n").encode(), db_path=db)
    assert res["status"] == "stored" and res["undated"] == 1 and res["invoices"] == 2


# ── V33: the export's other columns ───────────────────────────────────────

def test_the_invoice_details_report_reads_total_discount_and_adjustments(db):
    rid = _rid(db)
    head = ("Vendor Name,Retailer Name,Retailer Vendor ID,Customer ID,Store ID,Retailer Relationship ID,Process Date,"
            "Invoice Date,Invoice Due Date,Invoice Number,Invoice Total,Item Count,PO Number,PO Date,"
            "Reference Invoice Number,Product Number,Quantity,Unit Cost,Unit of Measure,UPC Number,"
            "Product Description,Total Adjustments,Total Discount,Extended Price,PPC,Pack UPC,GL Code\n")
    row = ('"Breakthru Beverage Illinois, LLC","HHB, LLC",V77,0700442049,000001,,10/13/2026 00:00:00,'
           "10/13/2026 00:00:00,11/12/2026 00:00:00,0129271609,79,1,PO-5,10/12/2026,0129271609,9031406,2,42,CA,"
           "088004144678,FIREBALL CINN WHSKY 1L,0,5,79,12,088004144678,Spirit\n")
    res = fi.ingest_file(rid, "invoices.csv", (head + row).encode(), db_path=db)
    assert res["status"] == "stored" and res["unbalanced"] == []
    ln = _rows(db, "SELECT discount, cost, adjustment FROM fintech_invoice_lines")[0]
    assert ln == {"discount": -5.0, "cost": 79.0, "adjustment": -5.0}
    inv = _rows(db, "SELECT vendor_id, po_number, po_date, ref_invoice_number, location_id, invoice_date "
                    "FROM fintech_invoices")[0]
    assert inv == {"vendor_id": "V77", "po_number": "PO-5", "po_date": "2026-10-12", "ref_invoice_number": None,
                   "location_id": "000001", "invoice_date": "2026-10-13"}


# ── V15 (parse side): references on the printout ──────────────────────────

def test_the_printout_reads_po_and_the_referenced_invoice(db):
    rid = _rid(db)
    extra = "PO Number      : B-369188433\nPO Date        : 10/13/2026\nRef Inv Number : 1142280\n"
    text = _printout("Elgin Beverage Company", "1142364", "10/08/2026", "($100.00)", [
        ("12345", "MODELO ESP 1/2 BBL", "-5", "$180.00", "($900.00)"),
        ("12345", "MODELO ESP 1/2 BBL", "5", "$160.00", "$800.00")], extra=extra)
    inv = fi.parse_printout(text)[0]
    assert (inv["po_number"], inv["po_date"], inv["ref_invoice_number"]) == ("B-369188433", "2026-10-13", "1142280")
    fi.ingest_file(rid, "p.txt", text.encode(), db_path=db)
    assert _rows(db, "SELECT po_number, po_date, ref_invoice_number FROM fintech_invoices") == [
        {"po_number": "B-369188433", "po_date": "2026-10-13", "ref_invoice_number": "1142280"}]
    assert {l["ref_invoice_number"] for l in _lines(db, rid)} == {"1142280"}


# ── V36: encodings ─────────────────────────────────────────────────────────

def test_a_utf16_export_reads_like_utf8(db):
    rid = _rid(db)
    fmt8, _k, inv8 = fi.read("x.csv", EXPORT.encode("utf-8"))
    fmt16, kept, inv16 = fi.read("x.csv", EXPORT.encode("utf-16"))
    assert fmt16 == fmt8 == "export" and inv16 == inv8 and "\x00" not in kept
    # without its BOM the NULs give it away: never stored as a garbage invoice
    assert fi.read("x.csv", EXPORT.encode("utf-16-le")) == (None, None, [])
    assert fi.ingest_file(rid, "y.csv", EXPORT.encode("utf-16-le"), db_path=db)["status"] == "unrecognized"


def test_a_cp1252_file_and_a_utf8_file_name_one_distributor(db):
    """EDG-5 S15b: 'Southern Glazer\\x92s' (Windows-1252) and 'Southern
    Glazer’s' were two invoices."""
    rid = _rid(db)
    text = _printout("Southern Glazer’s Wine & Spirits of IL", "02508778", "10/13/2026", "$252.00",
                     [("28728", "TITOS VDK 1L", "12", "$21.00", "$252.00")])
    fi.ingest_file(rid, "a.txt", text.encode("utf-8"), db_path=db)
    fi.ingest_file(rid, "b.txt", (text + "\n").encode("cp1252"), db_path=db)
    assert len(_rows(db, "SELECT id FROM fintech_invoices")) == 1
    assert fi.distributor_key("Southern Glazer’s Wine & Spirits of IL") == \
        fi.distributor_key("Southern Glazers Wine and Spirits of Illinois")


# ── R3: one invoice whatever its spelling ─────────────────────────────────

def test_the_same_invoice_under_two_spellings_is_one_invoice(db):
    """EDG-5 S10b: "…Illinois, LLC", "…Illinois LLC" and an unpadded number
    gave three invoices and $10,200 of liquor for $4,200."""
    rid = _rid(db)
    head = "Vendor Name,Invoice Number,Invoice Date,Invoice Total,Product Number,Product Description,Unit Of Measure," \
           "Item Quantity,Item Cost\n"
    fi.ingest_file(rid, "a.csv", (head + '"Breakthru Beverage Illinois, LLC",0128850598,10/01/2026,$1200.00,'
                                  "9008508,TITOS VDK 1L,CA,4,$300.00\n").encode(), db_path=db)
    fi.ingest_file(rid, "b.csv", (head + "Breakthru Beverage Illinois LLC,128850598,10/01/2026,$1200.00,"
                                  "9008508,TITOS VDK 1L,CA,4,$300.00\n").encode(), db_path=db)
    fi.ingest_file(rid, "c.csv", (head + "BREAKTHRU BEVERAGE ILLINOIS,000128850598,10/01/2026,$1200.00,"
                                  "9008508,TITOS VDK 1L,CA,4,$300.00\n"
                                  + '"Breakthru Beverage Illinois, LLC",0128850599,10/02/2026,$3000.00,'
                                  "9008508,TITOS VDK 1L,CA,10,$300.00\n").encode(), db_path=db)
    assert len(_rows(db, "SELECT id FROM fintech_invoices")) == 2
    assert round(sum(l["net"] for l in _lines(db, rid) if l["category"] == "liquor"), 2) == 4200.0


def test_a_spelling_the_key_misses_is_caught_by_date_and_total(db):
    """EDG-5 S10c: "Southern Glazers of IL" on one file, "Southern Glazer's
    Wine & Spirits of IL" on the other, invoice 02508778 on both."""
    rid = _rid(db)
    a = _printout("Southern Glazers of IL", "02508778", "10/13/2026", "$252.00",
                  [("28728", "TITOS VDK 1L", "12", "$21.00", "$252.00")])
    b = _printout("Southern Glazer's Wine & Spirits of IL", "02508778", "10/13/2026", "$252.00",
                  [("28728", "TITOS VDK 1L", "12", "$21.00", "$252.00")])
    fi.ingest_file(rid, "a.txt", a.encode(), db_path=db)
    res = fi.ingest_file(rid, "b.txt", b.encode(), db_path=db)
    assert res["near_duplicates"] == ["02508778"]
    assert len(_rows(db, "SELECT id FROM fintech_invoices")) == 1


def test_an_older_file_never_replaces_a_newer_copy(db):
    """FIN-11: re-reading an older file wrote its lines over the newer ones."""
    rid = _rid(db)
    head = "Vendor Name,Invoice Number,Invoice Date,Invoice Total,Product Number,Product Description,Unit Of Measure," \
           "Item Quantity,Item Cost\n"
    old = fi.ingest_file(rid, "old.csv", (head + "Craft,N1,10/01/2026,$100.00,1,OLD NAME,CA,1,$100.00\n").encode(),
                         db_path=db)
    fi.ingest_file(rid, "new.csv", (head + "Craft,N1,10/01/2026,$100.00,1,NEW NAME,CA,1,$100.00\n").encode(),
                   db_path=db)
    assert fi.reread_file(rid, old["file_id"], db_path=db)["invoices"] == 0
    assert _rows(db, "SELECT description FROM fintech_invoice_lines") == [{"description": "NEW NAME"}]


# ── V12 (storage half): versions, never a silent overwrite, purge ─────────

GENUINE = _printout("Southern Glazer's Wine & Spirits of IL", "02508778", "10/13/2026", "$252.00",
                    [("000028728", "TITOS VDK 1L", "12", "$21.00", "$252.00")])
FORGED = ("Vendor Name,Invoice Number,Invoice Date,Invoice Total,Product Number,Product Description,Unit Of Measure,"
          "Item Quantity,Item Cost\n"
          "Southern Glazer's Wine & Spirits of IL,02508778,10/13/2026,$9999.00,000028728,TITOS VDK 1L,CA,12,$833.25\n")


def test_later_file_never_overwrites_balanced_invoice(db):
    """SEC-1: a forged export of $9,999 replaced a genuine $252 printout and
    the genuine resend was refused while reported as 'stored'."""
    rid = _rid(db)
    assert fi.ingest_file(rid, "a.txt", GENUINE.encode(), db_path=db)["status"] == "stored"
    res = fi.ingest_file(rid, "x.csv", FORGED.encode(), db_path=db)
    assert res["status"] == "conflict" and res["invoices"] == 0
    assert res["conflicts"] == [{"distributor": "Southern Glazer's Wine & Spirits of IL", "invoice_number": "02508778",
                                 "kept_total": 252.0, "file_total": 9999.0}]
    assert _rows(db, "SELECT unit_price, cost FROM fintech_invoice_lines") == [{"unit_price": 21.0, "cost": 252.0}]
    states = {r["state"] for r in _rows(db, "SELECT state FROM fintech_invoice_versions")}
    assert states == {"current", "conflict"}
    # the genuine file again is not "stored" when nothing was written
    again = fi.ingest_file(rid, "b.txt", (GENUINE + "\n").encode(), db_path=db)
    assert again["status"] == "unchanged" and again["invoices"] == 0
    # the bad file taken out: its version goes, the genuine invoice stays
    assert fi.purge_file(rid, res["file_id"], db_path=db) == {"ok": True, "restored": 0, "deleted": 0}
    assert {r["state"] for r in _rows(db, "SELECT state FROM fintech_invoice_versions")} == {"current", "kept"}
    assert _rows(db, "SELECT status FROM fintech_files WHERE id=?", (res["file_id"],)) == [{"status": "purged"}]
    assert fi.ingest_file(rid, "x2.csv", FORGED.encode(), db_path=db)["status"] == "duplicate"


def test_purging_a_file_restores_the_invoice_from_another_version(db):
    rid = _rid(db)
    fi.ingest_file(rid, "a.txt", EXPORT_AS_PRINTOUT.encode(), db_path=db)
    exp = fi.ingest_file(rid, "b.csv", EXPORT.encode(), db_path=db)
    assert _rows(db, "SELECT source_format FROM fintech_invoices") == [{"source_format": "export"}]
    assert fi.purge_file(rid, exp["file_id"], db_path=db)["restored"] == 1
    assert _rows(db, "SELECT source_format FROM fintech_invoices") == [{"source_format": "printout"}]
    assert len(_rows(db, "SELECT id FROM fintech_invoice_lines")) == 2
    # a file only it carried: its invoices go with it
    only = fi.ingest_file(rid, "c.txt", GENUINE.encode(), db_path=db)
    assert fi.purge_file(rid, only["file_id"], db_path=db)["deleted"] == 1
    assert len(_rows(db, "SELECT id FROM fintech_invoices")) == 1
    assert fi.purge_file(rid + 1, only["file_id"], db_path=db)["ok"] is False, "another restaurant's file"


def test_an_unbalanced_copy_never_replaces_a_balanced_one_and_a_balanced_one_replaces_it(db):
    rid = _rid(db)
    short = _printout("Craft House", "77", "10/01/2026", "$500.00", [("1", "TITOS VDK 1L", "1", "$250.00", "$250.00")])
    full = _printout("Craft House", "77", "10/01/2026", "$500.00", [("1", "TITOS VDK 1L", "2", "$250.00", "$500.00")])
    fi.ingest_file(rid, "full.txt", full.encode(), db_path=db)
    assert fi.ingest_file(rid, "short.txt", short.encode(), db_path=db)["status"] == "unchanged"
    assert _rows(db, "SELECT balanced, lines_total FROM fintech_invoices") == [{"balanced": 1, "lines_total": 500.0}]
    rid2 = models.create_restaurant(models.Restaurant(name="Two", owner_email="t@x.test"), db_path=db)
    fi.ingest_file(rid2, "short.txt", short.encode(), db_path=db)
    assert fi.ingest_file(rid2, "full.txt", full.encode(), db_path=db)["status"] == "stored"
    assert _rows(db, "SELECT balanced FROM fintech_invoices WHERE restaurant_id=?", (rid2,)) == [{"balanced": 1}]


# ── V14 (ingest side): the printout takes its unit from the export ────────

FIREBALL_EXPORT = (BANK_HEAD
                   + _bank(BREAKTHRU, "0129000001", "9/1/2026", "$228.00", "9031406", "FIREBALL CINN WHSKY 1L", "CA",
                           1, "$228.00", "$0.00", "$228.00", upc="088004144678")
                   + _bank(BREAKTHRU, "0129000002", "9/2/2026", "$199.00", "13682", "DAISY CUTTER K 1/2BBL", "KE", 1,
                           "$199.00", "$30.00", "$229.00"))


def test_a_printout_line_takes_unit_and_upc_from_the_same_codes_export_history(db):
    rid = _rid(db)
    fi.ingest_file(rid, "hist.csv", FIREBALL_EXPORT.encode(), db_path=db)
    text = _printout("BREAKTHRU BEVERAGE ILLINOIS LLC", "129271609", "10/13/2026", "$298.87", [
        ("000009031406", "FIREBALL CINN WHSKY 1L", "1", "$232.00", "$232.00"),
        ("000009031406", "FIREBALL CINN WHSKY 1L", "3", "$22.29", "$66.87")])
    fi.ingest_file(rid, "daily.txt", text.encode(), db_path=db)
    got = _rows(db, "SELECT l.uom, l.upc, l.uom_inferred, l.unit_price FROM fintech_invoice_lines l JOIN "
                    "fintech_invoices i ON i.id=l.invoice_id WHERE i.source_format='printout' ORDER BY l.line_no")
    # the case price is the case; the bottle bought from a case code never shares its unit
    assert got == [{"uom": "CA", "upc": "088004144678", "uom_inferred": 1, "unit_price": 232.0},
                   {"uom": None, "upc": None, "uom_inferred": None, "unit_price": 22.29}]
    lines = [l for l in _lines(db, rid) if l["description"].startswith("FIREBALL")]
    assert len({l["product_key"] for l in lines}) == 1, "padded and unpadded codes are one product"
    assert [l["uom_inferred"] for l in lines] == [False, True, False]


def test_a_printout_keg_without_a_keg_word_takes_its_deposit_from_the_export_unit(db):
    """Euclid's printout cuts 'EAGLE PARK SLTZR SPEKTO KOOLER' at 30
    characters: no keg word, but the code is a keg on the export."""
    rid = _rid(db)
    fi.ingest_file(rid, "hist.csv", (BANK_HEAD + _bank(EUCLID, "W-1", "9/1/2026", "$159.00", "1461154",
                                                       "EAGLE PARK SLTZR SPEKTO KOOLER 1/2BBL", "KE", 1, "$129.00",
                                                       "$30.00", "$159.00")).encode(), db_path=db)
    text = _printout(EUCLID, "W-4798602", "10/13/2026", "($70.00)", [
        ("1461154", "EAGLE PARK SLTZR SPEKTO KOOLER", "-1", "$129.00", "($159.00)"),
        ("1461154", "EAGLE PARK SLTZR SPEKTO KOOLER", "1", "$59.00", "$89.00")])
    fi.ingest_file(rid, "daily.txt", text.encode(), db_path=db)
    got = _rows(db, "SELECT l.uom, l.deposit FROM fintech_invoice_lines l JOIN fintech_invoices i "
                    "ON i.id=l.invoice_id WHERE i.invoice_number='W-4798602' ORDER BY l.line_no")
    assert got == [{"uom": "KE", "deposit": -30.0}, {"uom": "KE", "deposit": 30.0}]


def test_a_printout_stored_before_its_export_takes_the_unit_when_the_export_arrives(db):
    rid = _rid(db)
    text = _printout("BREAKTHRU BEVERAGE ILLINOIS LLC", "555", "10/13/2026", "$232.00", [
        ("9031406", "FIREBALL CINN WHSKY 1L", "1", "$232.00", "$232.00")])
    fi.ingest_file(rid, "daily.txt", text.encode(), db_path=db)
    assert _rows(db, "SELECT uom FROM fintech_invoice_lines") == [{"uom": None}]
    fi.ingest_file(rid, "hist.csv", FIREBALL_EXPORT.encode(), db_path=db)
    assert _rows(db, "SELECT l.uom, l.uom_inferred FROM fintech_invoice_lines l JOIN fintech_invoices i "
                     "ON i.id=l.invoice_id WHERE i.invoice_number='555'") == [{"uom": "CA", "uom_inferred": 1}]


def test_product_keys_ignore_leading_zeros_and_distributor_spelling():
    from bar.classify import product_key
    assert product_key("Breakthru Beverage Illinois, LLC", "000009031", "TITOS") == \
        product_key("BREAKTHRU BEVERAGE ILLINOIS LLC", "9031", "TITOS VDK")
    assert product_key("Craft", None, "Mystery Thing") == product_key("Craft", "0000", " mystery thing ")


def test_the_owner_corrections_move_to_the_new_product_keys_once(db):
    from bar import classify as bcl
    rid = _rid(db)
    conn = models.get_conn(db)
    for key, cat, at in (("breakthru beverage illinois, llc|000009031", "wine", "2026-10-01 10:00:00"),
                         ("breakthru beverage illinois, llc|9031", "liquor", "2026-10-02 10:00:00"),
                         ("winebow inc|mystery thing", "wine", "2026-10-01 10:00:00")):
        conn.execute("INSERT INTO bar_product_categories (restaurant_id, product_key, category, set_by, set_at) "
                     "VALUES (?,?,?,?,?)", (rid, key, cat, 1, at))
    conn.commit()
    assert bcl.migrate_product_keys(conn) == 3
    conn.commit()
    got = {r["product_key"]: r["category"] for r in conn.execute(
        "SELECT product_key, category FROM bar_product_categories WHERE restaurant_id=?", (rid,))}
    assert got == {"breakthru beverage il|9031": "liquor", "winebow|mystery thing": "wine"}
    assert bcl.migrate_product_keys(conn) == 0, "idempotent"
    conn.close()
    assert bcl.product_key("Breakthru Beverage Illinois, LLC", "000009031", "") in got


# ── R14: a charge line is a charge on any invoice ─────────────────────────

def test_a_charge_line_alone_on_an_invoice_is_still_a_charge(db):
    """PRC-9: Jameson Black Barrel 2 at $2.29, alone on an invoice, became a
    $2.29 bottle price (a later real buy would read +1,406%)."""
    rid = _rid(db)
    sg = "Southern Glazer's Wine & Spirits of IL"
    alone = BANK_HEAD + _bank(sg, "02318193", "5/22/2026", "$4.58", "1234", "JAMESON IRISH BLACK BRL 80 1.0L", "BO",
                              2, "$2.29", "$0.00", "$4.58")
    fi.ingest_file(rid, "a.csv", alone.encode(), db_path=db)
    assert _rows(db, "SELECT kind FROM fintech_invoice_lines") == [{"kind": "item"}]
    real = BANK_HEAD + _bank(sg, "02400001", "9/1/2026", "$34.50", "0001234", "JAMESON IRISH BLACK BRL 80 1.0L",
                             "BO", 1, "$34.50", "$0.00", "$34.50")
    fi.ingest_file(rid, "b.csv", real.encode(), db_path=db)
    assert [r["kind"] for r in _rows(db, "SELECT kind FROM fintech_invoice_lines ORDER BY id")] == ["charge", "item"]
    # and once the price is known, the next lone charge is a charge at once
    fi.ingest_file(rid, "c.csv", (BANK_HEAD + _bank(sg, "02400002", "9/9/2026", "$2.29", "1234",
                                                    "JAMESON IRISH BLACK BRL 80 1.0L", "BO", 1, "$2.29", "$0.00",
                                                    "$2.29")).encode(), db_path=db)
    assert _rows(db, "SELECT kind FROM fintech_invoice_lines ORDER BY id DESC LIMIT 1") == [{"kind": "charge"}]
    lines = _lines(db, rid)
    assert {l["category"] for l in lines} == {"liquor"}, "a charge takes its item's category"


def test_a_free_case_is_not_a_charge():
    assert fi._kind("BACARDI RUM SUPERIOR WHITE 80 1.0L", 0.0, 249.5) == "item"
    assert fi._kind("BACARDI RUM SUPERIOR WHITE 80 1.0L", 2.29, 249.5) == "charge"


# ── R4: other stores' invoices ─────────────────────────────────────────────

def test_other_store_invoices_not_written_to_route_restaurant(db):
    """SEC-3: every invoice in a file went to the route's restaurant."""
    rid = _rid(db)
    fi.ingest_file(rid, "first.txt", GENUINE.encode(), db_path=db)          # teaches store 000001
    assert fi.location_map(rid, db_path=db) == {"1": rid}
    two = (_printout("Craft House", "A1", "10/02/2026", "$10.00", [("1", "TITOS", "1", "$10.00", "$10.00")])
           + _printout("Craft House", "B1", "10/02/2026", "$20.00", [("1", "TITOS", "2", "$10.00", "$20.00")],
                       store="000002"))
    res = fi.ingest_file(rid, "two.txt", two.encode(), db_path=db)
    assert res["status"] == "stored" and res["invoices"] == 1
    assert res["skipped_locations"] == [{"location_id": "000002", "invoices": 1}]
    assert {r["invoice_number"] for r in _rows(db, "SELECT invoice_number FROM fintech_invoices")} == {"02508778", "A1"}
    # mapped to the group's second restaurant, the store's invoices go there
    rid2 = models.create_restaurant(models.Restaurant(name="EJ's Two", owner_email="o2@x.test"), db_path=db)
    fi.set_location(rid, "000002", rid2, db_path=db)
    res = fi.ingest_file(rid, "two-again.txt", (two + "\n").encode(), db_path=db)
    assert res["routed"] == {str(rid2): 1}
    assert _rows(db, "SELECT restaurant_id FROM fintech_invoices WHERE invoice_number='B1'") == [{"restaurant_id": rid2}]
    assert _rows(db, "SELECT DISTINCT restaurant_id FROM fintech_invoice_lines l WHERE invoice_id IN "
                     "(SELECT id FROM fintech_invoices WHERE invoice_number='B1')") == [{"restaurant_id": rid2}]


def test_a_first_file_naming_several_stores_writes_nothing_until_they_are_mapped(db):
    rid = _rid(db)
    two = (_printout("Craft House", "A1", "10/02/2026", "$10.00", [("1", "TITOS", "1", "$10.00", "$10.00")])
           + _printout("Craft House", "B1", "10/02/2026", "$20.00", [("1", "TITOS", "2", "$10.00", "$20.00")],
                       store="000002"))
    res = fi.ingest_file(rid, "two.txt", two.encode(), db_path=db)
    assert res["status"] == "location_unmapped" and res["invoices"] == 0
    assert _rows(db, "SELECT id FROM fintech_invoices") == []


# ── R21, R22, V42: what is kept, where lines belong ──────────────────────

def test_only_read_columns_are_kept(db):
    """SEC-10: 'Bank Account Number' (EJ's real header) was kept; a deny-list
    misses any renamed payment column."""
    rid = _rid(db)
    csv_ = BANK_HEAD.replace("Status\n", "Status,ACH Routing\n") + \
        _bank(BREAKTHRU, "1", "9/1/2026", "$42.00", "9637400", "HIGH NOON", "CA", 1, "$42.00", "$0.00",
              "$42.00").replace("Paid\n", "Paid,071000013\n")
    res = fi.ingest_file(rid, "b.csv", csv_.encode(), db_path=db)
    kept = _rows(db, "SELECT content FROM fintech_files WHERE id=?", (res["file_id"],))[0]["content"]
    header = kept.splitlines()[0].split(",")
    assert "Bank Account Number" not in header and "ACH Routing" not in header and "Customer ID" not in header
    assert "8998" not in kept and "071000013" not in kept
    for col in ("Vendor Name", "Invoice Number", "Product Number", "Item Adjustments", "Extended Price",
                "Ref Invoice Number", " PPC", "GL Code"):
        assert col in header, col
    # what was kept still reads the same invoice
    assert fi.reread_file(rid, res["file_id"], db_path=db)["ok"] is True
    assert _rows(db, "SELECT cost FROM fintech_invoice_lines") == [{"cost": 42.0}]


def test_lines_carry_restaurant_id_and_two_tenants_same_item_code_isolated(db):
    a, b = _rid(db), models.create_restaurant(models.Restaurant(name="Other", owner_email="b@x.test"), db_path=db)
    fi.ingest_file(a, "a.csv", EXPORT.encode(), db_path=db)
    fi.ingest_file(b, "b.csv", EXPORT.replace("$42.00", "$40.00").replace("$264.00", "$252.00").encode(), db_path=db)
    assert {r["restaurant_id"] for r in _rows(db, "SELECT l.restaurant_id FROM fintech_invoice_lines l JOIN "
                                                  "fintech_invoices i ON i.id=l.invoice_id WHERE i.restaurant_id=?",
                                              (a,))} == {a}
    assert [l["unit_price"] for l in _lines(db, a) if l["item_code"] == "9637400"] == [42.0]
    assert [l["unit_price"] for l in _lines(db, b) if l["item_code"] == "9637400"] == [40.0]
    idx = _rows(db, "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='fintech_invoice_lines'")
    assert "idx_fintech_lines_rid_item" in {r["name"] for r in idx}


def test_delete_restaurant_leaves_no_fintech_invoice_lines(db):
    """RPW-15: lines outlived a deleted restaurant."""
    rid = _rid(db)
    keep = models.create_restaurant(models.Restaurant(name="Stays", owner_email="s@x.test"), db_path=db)
    fi.ingest_file(rid, "a.txt", PRINTOUT.encode(), db_path=db)
    fi.ingest_file(keep, "a.txt", PRINTOUT.encode(), db_path=db)
    models.delete_restaurant(rid, db_path=db)
    assert {r["restaurant_id"] for r in _rows(db, "SELECT restaurant_id FROM fintech_invoice_lines")} == {keep}
    for table in ("fintech_invoices", "fintech_files", "fintech_invoice_versions", "fintech_locations"):
        assert _rows(db, f"SELECT 1 FROM {table} WHERE restaurant_id=?", (rid,)) == [], table


def test_orphan_lines_are_counted_then_cleaned(db):
    rid = _rid(db)
    fi.ingest_file(rid, "a.txt", PRINTOUT.encode(), db_path=db)
    conn = models.get_conn(db)
    gone = conn.execute("SELECT id FROM fintech_invoices ORDER BY id LIMIT 1").fetchone()["id"]
    conn.execute("DELETE FROM fintech_invoices WHERE id=?", (gone,))
    conn.commit()
    conn.close()
    assert fi.delete_orphan_lines(db_path=db) == 3                    # dry run: counted only
    assert fi.delete_orphan_lines(db_path=db) == 3
    assert fi.delete_orphan_lines(dry_run=False, db_path=db) == 3
    assert fi.delete_orphan_lines(db_path=db) == 0


def test_boot_backfills_the_keys_and_line_restaurant_once(db):
    rid = _rid(db)
    fi.ingest_file(rid, "a.txt", PRINTOUT.encode(), db_path=db)
    conn = models.get_conn(db)
    conn.execute("UPDATE fintech_invoice_lines SET restaurant_id=NULL")
    conn.execute("UPDATE fintech_invoices SET distributor_key=NULL, number_key=NULL")
    conn.commit()
    conn.close()
    fi.init_fintech_invoices(db)
    fi.init_fintech_invoices(db)
    assert {r["restaurant_id"] for r in _rows(db, "SELECT restaurant_id FROM fintech_invoice_lines")} == {rid}
    assert _rows(db, "SELECT number_key FROM fintech_invoices ORDER BY id") == [{"number_key": "101153056"},
                                                                                {"number_key": "2508778"}]


# ── the re-read every stored file (scripts/bar_reread_fintech.py) ────────

def test_reread_all_is_a_dry_run_by_default_and_idempotent(db):
    rid = _rid(db)
    res = fi.ingest_file(rid, "b.csv", FIREBALL_EXPORT.encode(), db_path=db)
    # as the 10/14 reader stored it: no deposit split, padded content kept whole, no store map
    conn = models.get_conn(db)
    conn.execute("UPDATE fintech_invoice_lines SET deposit=NULL")
    conn.execute("UPDATE fintech_files SET content=? WHERE id=?", (FIREBALL_EXPORT, res["file_id"]))
    conn.execute("DELETE FROM fintech_locations")
    conn.commit()
    conn.close()
    dry = fi.reread_all(rid, db_path=db)
    assert dry["applied"] is False and dry["before"]["kinds"]["item"]["deposit"] == 0.0
    assert dry["after"]["kinds"]["item"]["deposit"] == 30.0 and dry["files"][0]["trimmed"] is True
    assert _rows(db, "SELECT SUM(deposit) AS d FROM fintech_invoice_lines") == [{"d": None}], "dry run wrote"
    done = fi.reread_all(rid, apply=True, db_path=db)
    assert done["applied"] is True and done["store_map_seeded"] == "1"
    assert _rows(db, "SELECT SUM(deposit) AS d FROM fintech_invoice_lines") == [{"d": 30.0}]
    again = fi.reread_all(rid, apply=True, db_path=db)
    assert again["before"] == again["after"] == done["after"] and again["files"][0]["trimmed"] is False


def test_the_reread_script_runs_dry_by_default(db, capsys):
    import importlib
    script = importlib.import_module("scripts.bar_reread_fintech")
    rid = _rid(db)
    fi.ingest_file(rid, "b.csv", FIREBALL_EXPORT.encode(), db_path=db)
    assert script.main(["--restaurant", str(rid), "--db", db]) == 0
    assert "Dry run" in capsys.readouterr().out
