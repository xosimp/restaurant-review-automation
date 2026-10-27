"""The guest list from the host stand to the next-day text (guest_hub,
10/20/26): Tock's webhooks, the host stand's add and invite, the spreadsheet
import, the next-day thank-you, replies to the restaurant's own number, and
RPOWER checks with a customer attached. Every source adds a guest
unconsented; consent is only ever the guest's own Y or an owner-attested
prior yes."""
import json
from datetime import datetime, timedelta

import pytest

import guest_hub as gh
import guest_marketing as gm
import models
import notify
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, gm):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    gm.init_guest_marketing(db_path)
    monkeypatch.setattr(gm, "DB_PATH", db_path)
    monkeypatch.setattr(gm, "_sms_local_now", lambda rid: datetime.now().replace(hour=12, minute=0))
    gh._SENDER_CACHE["at"] = 0.0
    yield


@pytest.fixture
def rid(db_path):
    return create_restaurant(Restaurant(name="Simple EJ's", owner_email="erik@x.test", google_place_id="ChIJx"),
                             db_path=db_path)


@pytest.fixture
def texts(monkeypatch):
    sent = []

    def fake(phone, body, use_case="alert", validity_seconds=None, **k):
        sent.append({"phone": phone, "body": body, "use_case": use_case})
        return True
    monkeypatch.setattr(gm, "send_sms", fake)
    return sent


def _contact(rid, phone):
    conn = models.get_conn()
    try:
        r = conn.execute("SELECT * FROM guest_contacts WHERE restaurant_id=? AND phone=?", (rid, phone)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


# ── Tock ───────────────────────────────────────────────────────────────────

def _tock(monkeypatch, rid, token="s3cret"):
    monkeypatch.setenv(gh.TOCK_TOKENS_ENV, f"{rid}:{token}")


def test_a_tock_delivery_without_the_secret_is_refused(monkeypatch, rid):
    _tock(monkeypatch, rid)
    body, status = gh.receive_tock("reservations", "wrong", b"{}")
    assert status == 401
    body, status = gh.receive_tock("reservations", "s3cret", b"not json")
    assert status == 400


def test_a_reservation_adds_the_guest_unconsented_books_covers_and_seating_is_one_visit(monkeypatch, rid):
    _tock(monkeypatch, rid)
    when = (datetime.now() + timedelta(days=2)).replace(hour=19, minute=0, second=0, microsecond=0)
    event = {"eventId": "e1", "type": "reservation.created",
             "reservation": {"id": "R100", "partySize": 4, "dateTime": when.isoformat(),
                             "guest": {"firstName": "Pat", "lastName": "Lee", "phone": "(630) 555-0101",
                                       "email": "Pat@x.test", "marketingOptIn": True}}}
    body, status = gh.receive_tock("reservations", "s3cret", json.dumps(event).encode())
    assert status == 200 and body["new"] == 1
    assert gh.receive_tock("reservations", "s3cret", json.dumps(event).encode())[0]["new"] == 0   # same event id
    c = _contact(rid, "+16305550101")
    assert c["consent"] == 0 and c["name"] == "Pat Lee" and c["tock_marketing_optin"] == 1 and c["visit_count"] == 0
    conn = models.get_conn()
    try:
        sig = conn.execute("SELECT covers, source FROM demand_signals WHERE restaurant_id=? AND date=?",
                           (rid, when.date().isoformat())).fetchone()
    finally:
        conn.close()
    assert sig["covers"] == 4 and sig["source"] == "tock"
    seated = dict(event, eventId="e2", type="reservation.seated")
    gh.receive_tock("reservations", "s3cret", json.dumps(seated).encode())
    gh.receive_tock("reservations", "s3cret", json.dumps(dict(seated, eventId="e3")).encode())   # seated again
    assert _contact(rid, "+16305550101")["visit_count"] == 1
    cancelled = dict(event, eventId="e4", type="reservation.cancelled")
    gh.receive_tock("reservations", "s3cret", json.dumps(cancelled).encode())
    conn = models.get_conn()
    try:
        assert conn.execute("SELECT covers FROM demand_signals WHERE restaurant_id=? AND date=?",
                            (rid, when.date().isoformat())).fetchone()["covers"] == 0
    finally:
        conn.close()


def test_an_event_the_build_cant_apply_is_kept_for_reprocessing(monkeypatch, rid):
    _tock(monkeypatch, rid)
    monkeypatch.setattr(gh, "apply_tock_event", lambda *a, **k: (_ for _ in ()).throw(ValueError("shape")))
    body, status = gh.receive_tock("walkins", "s3cret", json.dumps({"eventId": "w1"}).encode())
    assert status == 200
    conn = models.get_conn()
    try:
        assert "shape" in conn.execute("SELECT error FROM tock_events WHERE event_key='w1'").fetchone()["error"]
    finally:
        conn.close()


# ── the host stand ─────────────────────────────────────────────────────────

def test_the_host_stand_invite_goes_only_when_the_guest_asked_and_consent_waits_on_their_y(rid, texts):
    res = gh.host_add(rid, "Sam Ortiz", "630-555-0102", party=3, invite=True, asked=False)
    assert not res["ok"] and not texts
    res = gh.host_add(rid, "Sam Ortiz", "630-555-0102", party=3, invite=True, asked=True, user_id=9)
    assert res["ok"] and res["invited"] and len(texts) == 1 and "Reply Y" in texts[0]["body"]
    c = _contact(rid, "+16305550102")
    assert c["consent"] == 0 and c["visit_count"] == 1 and c["last_party_size"] == 3     # the refused call saved nothing
    assert gm.consent_events(rid, phone="+16305550102")[-1]["source"] == "host_stand"
    gm.handle_inbound_sms("+16305550102", "Y")
    assert _contact(rid, "+16305550102")["consent"] == 1
    listed = {g["phone"]: g for g in gh.guest_list(rid)["guests"]}
    assert listed["(630) 555-0102"]["texts"] == "joined"


# ── the spreadsheet ────────────────────────────────────────────────────────

CSV = ("Guest Name,Cell,Date Visited,Replied Yes\n"
       "Ana Cruz,6305550103,10/18/26,yes\n"
       "Ben Fox,(630) 555-0104,10/18/26,\n"
       "No Phone,,10/18/26,yes\n")


def test_an_import_brings_guests_in_unconsented_unless_the_owner_attests_a_prior_yes(rid):
    prev = gh.import_guests(rid, CSV)
    assert prev["rows"] == 2 and prev["skipped"] == 1 and prev["said_yes"] == 1
    assert _contact(rid, "+16305550103") is None                     # a preview writes nothing
    gh.import_guests(rid, CSV, commit=True)
    assert _contact(rid, "+16305550103")["consent"] == 0
    gh.import_guests(rid, CSV, commit=True, attest_consent=True, user_id=3)
    a = _contact(rid, "+16305550103")
    assert a["consent"] == 1 and a["visit_count"] == 1                # the same dated row counted once
    assert _contact(rid, "+16305550104")["consent"] == 0
    assert gm.consent_events(rid, phone="+16305550103")[-1]["source"] == "import_attested"


# ── the next-day thank-you ─────────────────────────────────────────────────

def _visited(rid, phone, day, consent):
    cid = gh._touch_contact(rid, phone, name="G", source="host_stand")
    conn = models.get_conn()
    try:
        conn.execute("UPDATE guest_contacts SET last_visit=?, consent=? WHERE id=?", (day + "T19:00:00", consent, cid))
        conn.commit()
    finally:
        conn.close()
    return cid


def test_the_thank_you_goes_once_to_yesterdays_opted_in_guests(rid, texts):
    now = datetime.now().replace(hour=11, minute=30)
    yday = (now.date() - timedelta(days=1)).isoformat()
    _visited(rid, "+16305550105", yday, 1)
    _visited(rid, "+16305550106", yday, 0)                            # never joined: never texted
    _visited(rid, "+16305550107", (now.date() - timedelta(days=3)).isoformat(), 1)
    assert gh.followup_one(rid, now=now)["skipped"] == 1             # off until the owner turns it on
    gh.set_followup(rid, True, "Thanks for coming in yesterday!", review_link=True)
    res = gh.followup_one(rid, now=now)
    assert res["ok"] == 1 and [t["phone"] for t in texts] == ["+16305550105"]
    assert texts[0]["body"].startswith("Simple EJ's: Thanks for coming in yesterday!") and "writereview" in texts[0]["body"]
    assert texts[0]["body"].endswith(gm.STOP_LINE)
    assert gh.followup_one(rid, now=now)["ok"] == 0                  # claimed: never twice
    assert gh.followup_one(rid, now=now.replace(hour=9))["skipped"] == 1


def test_a_thank_you_too_long_for_one_text_is_refused(rid):
    import guest_marketing as gm
    res = gh.set_followup(rid, True, "x" * (gm.CAMPAIGN_MAX_CHARS + 1))
    assert not res["ok"] and "characters" in res["error"]


# ── replies and the restaurant's own number ────────────────────────────────

def test_a_reply_to_the_restaurants_own_number_reaches_its_inbox_and_can_be_answered(rid, texts):
    assert gh.set_sender(rid, "MG" + "a" * 32, "(630) 555-0199")["ok"]
    assert gh.sender_for(rid)["service_sid"] == "MG" + "a" * 32
    assert notify._own_guest_service(rid) == "MG" + "a" * 32
    gm.handle_inbound_sms("+16305550108", "What time do you open Sunday?", to_phone="+16305550199")
    threads = gh.inbox_threads(rid)
    assert threads and threads[0]["unread"] == 1
    assert gh.thread(rid, "+16305550108")[0]["body"].startswith("What time")
    assert gh.inbox_threads(rid)[0]["unread"] == 0
    assert gh.reply(rid, "+16305550108", "We open at 10am!")["ok"]
    assert texts[-1]["body"] == "Simple EJ's: We open at 10am!"
    assert not gh.reply(rid, "+16305550109", "Hi")["ok"]             # never texted us: no reply
    gm.handle_inbound_sms("+16305550110", "hello")                    # the shared number: no restaurant
    assert all(t["phone"] != "+16305550110" for t in gh.inbox_threads(rid))


# ── RPOWER checks ──────────────────────────────────────────────────────────

def test_a_check_with_a_real_customer_is_a_visit_and_the_placeholder_is_not(rid):
    day = "2026-10-18"
    conn = models.get_conn()
    try:
        for tid, cust in (("t1", "1"), ("t2", "C9"), ("t3", "H1")):
            conn.execute("INSERT INTO pos_tickets (restaurant_id, provider, ticket_id, business_date, net_sales, "
                         "customer_id) VALUES (?,?,?,?,?,?)", (rid, "rpower", tid, day, 50.0, cust))
        conn.execute("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, ticket_id, business_date, "
                     "item_name, kind, qty) VALUES (?,?,?,?,?,?,?,?)", (rid, "rpower", "l1", "t2", day, "Wings", "sale", 2))
        conn.commit()
    finally:
        conn.close()
    customers = {"C9": {"name": "Dana Kim", "phone": "6305550111", "email": None, "company": None},
                 "H1": {"name": "Grubhub", "phone": "6305550112", "email": None, "company": "Grubhub"}}
    res = gh.match_pos_visits(rid, day, customers=customers)
    assert res == {"tickets": 2, "matched": 1}
    c = _contact(rid, "+16305550111")
    assert c["consent"] == 0 and c["visit_count"] == 1
    assert gh.match_pos_visits(rid, day, customers=customers)["matched"] == 0
    assert gh.usual_items(rid, [c["id"]]) == {c["id"]: ["Wings"]}
    assert _contact(rid, "+16305550112") is None


def test_a_sheet_is_read_by_what_its_headers_say_not_their_exact_words():
    p = gh.parse_import("Guest,Phone Number,E-mail,Last Visit,Texts OK?\n"
                        "Ana Cruz,(630) 555-0103,a@x.co,10/18/26,Yes\nBen,630.555.0104,,10/19/2026,\n")
    assert p["columns"] == {"name": 0, "phone": 1, "email": 2, "date": 3, "consent": 4}
    assert [(r["phone"], r["consent"]) for r in p["rows"]] == [("+16305550103", True), ("+16305550104", False)]


def test_an_excel_workbook_reads_like_its_csv():
    import io
    import zipfile
    m = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    files = {
        "xl/workbook.xml": f'<workbook xmlns="{m}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
                           'relationships"><sheets><sheet name="Guests" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                      '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/></Relationships>',
        "xl/sharedStrings.xml": f'<sst xmlns="{m}"><si><t>Name</t></si><si><t>Cell</t></si><si><t>Date</t></si>'
                                '<si><t>Opted in</t></si><si><t>Dana Kim</t></si><si><t>yes</t></si></sst>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{m}"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c>'
                                    '<c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c>'
                                    '</row><row r="2"><c r="A2" t="s"><v>4</v></c><c r="B2"><v>6.305550111E9</v></c>'
                                    '<c r="C2"><v>46313</v></c><c r="D2" t="s"><v>5</v></c></row></sheetData></worksheet>',
    }
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, c in files.items():
            z.writestr(n, c)
    rows = gh.parse_import(gh.xlsx_to_csv(b.getvalue()))["rows"]
    assert [(r["name"], r["phone"], r["date"].isoformat(), r["consent"]) for r in rows] == \
        [("Dana Kim", "+16305550111", "2026-10-18", True)]
    with pytest.raises(ValueError):
        gh.xlsx_to_csv(b"not a workbook")
