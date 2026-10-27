"""Audit #25 A06-08 / A01-13: provider payloads in their real shapes, through
the production readers.

tests/fixtures/providers/ holds a corpus of SYNTHETIC but realistic payloads
— the repo is public, so nothing in it is a customer's (finding R1):

rpower/postman_shape_day.json
    One invented store's night ("Fixture Tavern", cg 4242). Every record
    carries exactly the fields of RPOWER's own example for its endpoint
    (docs/integrations/rpower_core_api.postman.json), with the shapes found
    live that the collection does not show: a Windows time-zone name,
    RPOWER's 2000-01-01T00:00:00 "never set" stamp, a void as an Error
    Correct line with voided 0, a comp carrying its value in `sales` with
    both prices 0, whole-dollar money as integers, a station login, a $0
    rate meaning "not entered", and timeclock's EXCLUSIVE enddate (the
    transport below answers it the way the live API does).
rpower/demo_store_day.json
    The showcase simulator's answer (demo_store/, Halsted & Rye — invented)
    for the closed night of 10/10/26, frozen and trimmed to seven checks:
    a second, independently built set of records in the same shapes.
stripe/*.json
    Webhook events as Stripe sends them, for both invoice shapes the code
    reads: API 2025-02-24.acacia (the installed stripe==11.6.0 SDK's
    version: invoice.subscription, charge, payment_intent, tax,
    total_tax_amounts, line `type`/`price`) and 2025-03-31.basil and later
    (invoice.parent.subscription_details, line `parent`/`pricing`,
    total_taxes; no charge or payment_intent on the invoice; a
    subscription's current_period_end on its items). Each is built into the
    real SDK's objects with stripe.Event.construct_from — no stand-in
    module — before it reaches a reader.

The RPOWER files go through the whole HTTP read path (rpower._request with
`requests.get` answering from the file, the response contract, paging) into
the nightly archive read (archive_rows: tickets, lines, ticket items,
payments, payouts, punches and every list that names them),
fetch_day_sales, fetch_business_days, fetch_loss_lines, fetch_time_entries,
fetch_people, fetch_employee_jobs, open_punches, fetch_day_closed,
test_token and bootstrap. The
Stripe files go through webhook_routes._resolve_stripe_event,
billing_jobs.invoice_facts / invoice_metadata and subscription_facts. Each
test states the facts a reader must derive.

To replace a synthetic file with a recorded one: a Stripe TEST-mode event
(`stripe trigger invoice.paid`, or the dashboard's event JSON in test mode)
carries no customer data and can be dropped in as is; an RPOWER answer from
a live store must have every name, phone, id and amount replaced first.
"""
import json
import os
from datetime import date

import pytest
import stripe

import billing_jobs
import models
import pos
import pos_io
import rpower
import webhook_routes

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "providers")


def _load(*parts):
    with open(os.path.join(FIXTURES, *parts), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def _world(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(webhook_routes, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0)
    monkeypatch.setattr(rpower, "FLEET_RPS", 0)
    monkeypatch.setattr(rpower.time, "sleep", lambda s: None)
    pos_io.reset_for_tests()
    yield
    pos_io.reset_for_tests()
    # The lane counts these reads made belong to this test's database, which
    # is gone by the time the process exits and flushes them.
    import pos_capacity
    pos_capacity.reset_for_tests()
    rpower._note_store(None)


# ── RPOWER: a transport that answers from a recorded file ───────────────────

class _Resp:
    def __init__(self, body):
        self.status_code, self.ok, self.headers = 200, True, {}
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


_DAY_KEYED = {"ticket/getbybusinessdate": "date", "ticketsales/getbybusinessdate": "date",
              "ticketitem/getbybusinessdate": "date", "ticketpayment/getbybusinessdate": "date",
              "payout/getbydaterange": "date", "closeday/getbybusinessdate": "from_date"}


class RecordedRpower:
    """requests.get for rpower._request: the file's rows for the path asked,
    filtered by the request's dates the way RPOWER filters them —
    getbybusinessdate inclusive of both ends, timeclock/getbydaterange's
    enddate EXCLUSIVE (checked live 9/28/26) — and paged at 1,000."""

    def __init__(self, corpus):
        self.responses = corpus["responses"]
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        path = url.split("/coreapi/", 1)[-1].split("?", 1)[0].strip("/").lower()
        params = {str(k).lower(): v for k, v in dict(params or {}).items()}
        self.calls.append((path, params))
        if path not in self.responses:
            raise AssertionError(f"the reader asked for {path}, which the corpus does not hold")
        rows = list(self.responses[path])
        lo, hi = params.get("startdate"), params.get("enddate")
        if path in _DAY_KEYED and lo and hi:
            key = _DAY_KEYED[path]
            rows = [r for r in rows if str(lo)[:10] <= str(r[key])[:10] <= str(hi)[:10]]
        elif path == "timeclock/getbydaterange" and lo and hi:
            rows = [r for r in rows if str(lo)[:10] <= str(r["in_dttm"])[:10] < str(hi)[:10]]
        if path == "store/get":
            return _Resp(rows)
        page = int(params.get("pagenumber") or 1)
        return _Resp(rows[(page - 1) * 1000: page * 1000])


def _connected(db_path, corpus, rid=1, token="tok-fixture", **kw):
    conn = models.get_conn(db_path)
    cols = {"id": rid, "name": f"Corpus {rid}", "owner_email": f"o{rid}@fixture.test", "timezone": "America/Chicago",
            "rpower_token": token, "rpower_cg": corpus["store"]["cg"],
            "rpower_store_mid": corpus["store"]["store_mid"]}
    cols.update(kw)
    conn.execute(f"INSERT INTO restaurants ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                 tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


@pytest.fixture
def postman(db_path, monkeypatch):
    corpus = _load("rpower", "postman_shape_day.json")
    api = RecordedRpower(corpus)
    monkeypatch.setattr("requests.get", api.get)
    api.rid = _connected(db_path, corpus)
    api.day = corpus["store"]["business_date"]
    return api


@pytest.fixture
def demo(db_path, monkeypatch):
    corpus = _load("rpower", "demo_store_day.json")
    api = RecordedRpower(corpus)
    monkeypatch.setattr("requests.get", api.get)
    # A plain token: a demo-store: token is answered by the live simulator, not the frozen file.
    api.rid = _connected(db_path, corpus, token="tok-frozen-demo")
    api.day = corpus["store"]["business_date"]
    api.corpus = corpus
    return api


def test_every_corpus_record_passes_the_response_contract():
    for name in ("postman_shape_day.json", "demo_store_day.json"):
        for path, rows in _load("rpower", name)["responses"].items():
            # The simulator's night had no payout; every other list holds rows.
            assert rows or (name, path) == ("demo_store_day.json", "payout/getbydaterange"), f"{name}: {path}"
            assert rpower._contract_rows(path, rows) == rows


def test_the_postman_shaped_records_carry_exactly_rpowers_documented_fields():
    """The file is built from the collection's own example records: each
    record has the example's field set (the invented values aside)."""
    with open(os.path.join(os.path.dirname(FIXTURES), "..", "..", "docs", "integrations",
                           "rpower_core_api.postman.json"), encoding="utf-8") as f:
        collection = json.load(f)
    examples = {}
    for item in collection["item"]:
        url = item.get("request", {}).get("url", {})
        raw = url.get("raw") if isinstance(url, dict) else str(url)
        path = str(raw).split("/coreapi/", 1)[-1].split("?", 1)[0].lower()
        try:
            body = json.loads(item["response"][0]["body"])
        except (KeyError, IndexError, ValueError):
            continue                         # store/get's example is not valid JSON in the collection
        if isinstance(body, list) and body and isinstance(body[0], dict):
            examples.setdefault(path, set(body[0]))
    corpus = _load("rpower", "postman_shape_day.json")["responses"]
    checked = 0
    for path, rows in corpus.items():
        if path not in examples:
            continue
        for row in rows:
            assert set(row) == examples[path], (path, set(row) ^ examples[path])
        checked += 1
    assert checked >= 18, checked


def test_a_windows_timezone_name_maps_at_connect_and_bind(postman, db_path, monkeypatch):
    out = rpower.test_token("tok-fixture")
    assert out["ok"]
    (s,) = out["stores"]
    assert (s["cg"], s["store_mid"], s["timezone"], s["timezone_raw"], s["timezone_problem"]) == \
        (4242, "4242000000000000001", "America/Chicago", "Central Standard Time", None)
    import strategy_jobs
    monkeypatch.setattr(strategy_jobs, "enqueue_pos_onboard", lambda *a, **k: None)
    rid = postman.rid                      # a token, not yet bound, and no timezone set yet
    conn = models.get_conn(db_path)
    conn.execute("UPDATE restaurants SET rpower_cg=NULL, rpower_store_mid=NULL, timezone=NULL WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    out = rpower.bootstrap(rid)
    assert out["ok"], out
    r = models.get_restaurant(rid)
    assert (r.timezone, r.rpower_cg, r.rpower_store_mid, r.rpower_store_name) == \
        ("America/Chicago", 4242, "4242000000000000001", "Fixture Tavern")


def test_the_nights_sales_from_the_documented_shapes(postman):
    data, provider = pos.fetch_day_sales(postman.rid, date.fromisoformat(postman.day))
    assert provider == "rpower"
    # gross: the sale lines 12 + 15 + 1.50 + 14 + 15 = 57.50, plus the comped Wings' value 12
    assert (data["gross"], data["discounts"], data["comps"], data["net"]) == (69.5, 4.25, 12.0, 53.25)
    # the Error Correct line (voided 0) is the night's void; the refund rung negative is $8
    assert (data["voids"], data["refunds"]) == (7.0, 8.0)
    # tax and covers from the two tickets that stood; the cancelled one is in nothing
    assert (data["tax"], data["transactions"], data["guests"]) == (6.2, 2, 5)
    checks = data["source_checks"]
    assert (checks["unknown_type_lines"], checks["cancelled_lines"], checks["cancelled_sales"]) == (0, 1, 15.0)
    assert (checks["not_revenue_lines"], checks["not_revenue_sales"]) == (1, 25.0)   # the gift card sold
    assert checks["excluded_lines"] == 1                                              # the gratuity line
    assert checks["comps_reconciled"] and checks["comp_gap"] == 0.0
    assert checks["ticket_type_sale"] == 82.5
    # net by department and hour: the comp comes off Food, the discount line is Discounts' own
    assert data["by_department"] == {"Food": 43.5, "Beer": 14.0, "Discounts": -4.25}
    assert data["by_hour"] == {"11": 38.25, "18": 15.0}
    items = {i["name"]: i for i in data["items"]}
    assert set(items) == {"Wings", "Smash Burger", "House IPA"}, "the modifier and the gift card are not items"
    assert (items["Wings"]["qty"], items["House IPA"]["qty"], items["Smash Burger"]["qty"]) == (1.0, 2.0, 2.0)
    made = {i["name"]: i["made_qty"] for i in data["items_made"]}
    assert made["Wings"] == 2.0, "one Wings sold and one comped plate both left the kitchen"
    # Read live, before the night is archived, the business-day figure has no
    # ticket read to drop the cancelled check's line from (rpower._on_cancelled,
    # deliberately: no second RPOWER read per live sales read); the archive's
    # figure replaces it at the next sync.
    assert rpower.fetch_business_days(postman.rid, postman.day, postman.day) == \
        {postman.day: round(data["net"] + checks["cancelled_sales"], 2)}


def test_the_nights_comps_voids_and_refunds_name_their_approver_and_reason(postman):
    lines = rpower.fetch_loss_lines(postman.rid, postman.day, postman.day)
    got = sorted((l["kind"], l["amount"], l["approver_name"], l["reason"]) for l in lines)
    assert got == [("comp", 12.0, "Jordan Rivera", None),
                   ("refund", 8.0, "Jordan Rivera", None),
                   ("void", 7.0, "Jordan Rivera", "Kitchen error")]


def test_punches_with_an_exclusive_enddate_an_open_punch_and_a_station_login(postman):
    entries = rpower.fetch_time_entries(postman.rid, postman.day, postman.day)
    asked = [p for path, p in postman.calls if path == "timeclock/getbydaterange"]
    assert asked and asked[0]["startdate"] == "2026-03-14" and asked[0]["enddate"] == "2026-03-15"
    assert sorted(e["rid"][-1] for e in entries) == ["1", "2", "3", "4"], "the next morning's punch is not 3/14's"
    people = rpower.fetch_people(postman.rid)
    assert people["employees"]["4242500000000000001"] == "Jordan Rivera"
    assert people["jobs"]["4242400000000000001"] == "Cook"
    # A $0 job rate is "not entered", never a rate of zero.
    assert people["job_rates"] == {"4242400000000000001": 16.5, "4242400000000000003": 9.0}
    assert set(people["station_names"]) == {"4242500000000000004"}          # "Day Bar"
    still_on = rpower.open_punches(entries, people)
    assert [(p["employee"], p["role"], p["clock_in"]) for p in still_on] == \
        [("Taylor Brooks", "Bartender", "2026-03-14T17:00")]
    closed = [e for e in entries if rpower._is_closed_punch(e)]
    assert len(closed) == 3, "2000-01-01 is RPOWER's 'never set', not a clock-out"


def test_who_holds_which_job(postman):
    jobs = sorted((j["employee_name"], j["role"], j["rate"], j["primary"])
                  for j in rpower.fetch_employee_jobs(postman.rid))
    # Casey's $0 is RPOWER's "not entered" (payroll pays it), read as a 0 the rate chain skips.
    assert jobs == [("Casey Moore", "Server", 0.0, True), ("Day Bar", "Bartender", 9.0, True),
                    ("Jordan Rivera", "Cook", 16.5, True), ("Taylor Brooks", "Bartender", 9.0, True)]


def test_the_close_with_an_unset_thru_date_closes_its_own_night_only(postman):
    assert rpower.fetch_day_closed(postman.rid, "2026-03-13") is True
    assert rpower.fetch_day_closed(postman.rid, "2026-03-14") is True
    rpower.clear_caches()
    assert rpower.fetch_day_closed(postman.rid, "2026-03-15") is False


def test_the_nightly_archive_read_of_the_documented_shapes(postman):
    night = rpower.archive_rows(postman.rid, postman.day)
    assert night["source"] == "business_date" and night["links_ok"] is True
    checks = night["checks"]
    # RPOWER's own ticket totals and our lines agree, the cancelled check left out of both sides
    assert checks["gap"] == 0.0 and checks["unknown_type_lines"] == 0 and checks["tickets"] == 2
    assert checks["lines_by_type"] == {"type_discnt": -4.25, "type_sale": 82.5}     # the gift card is in type_sale
    assert {k: v for k, v in checks["tickets_by_type"].items() if v} == checks["lines_by_type"]
    assert night["net_sales"] == 53.25
    tickets = {t["ticket_no"]: t for t in night["tickets"]}
    assert (tickets["101"]["server_name"], tickets["101"]["table_name"], tickets["101"]["room_name"],
            tickets["101"]["mealtime"]) == ("Casey Moore", "Table 12", "Dining Room", "Lunch")
    assert tickets["102"]["is_bar"] == 1 and tickets["102"]["profit_center"] == "Bar"      # RPOWER's "BAR", tidied
    assert tickets["103"]["cancelled"] == 1
    assert tickets["101"]["need_at"] is None, "2000-01-01 is RPOWER's 'never set', not a time"
    kinds = sorted(l["kind"] for l in night["lines"])
    assert kinds == ["comp", "discount", "not_revenue", "other", "refund", "sale", "sale", "sale", "sale", "sale",
                     "sale", "void"]
    by_item = {(l["ticket_id"][-1], l["item_name"]): l for l in night["lines"]}
    cheese, burger = by_item[("1", "Add Cheese")], by_item[("1", "Smash Burger")]
    assert cheese["parent_line_id"] == burger["line_id"] and cheese["mod_level"] == 1
    assert by_item[("2", "House IPA")]["reason"] == "Kitchen error" and by_item[("2", "House IPA")]["loss_amount"] == 7.0
    assert by_item[("1", "Wings")]["department"] == "Food" and by_item[("1", "House IPA")]["department"] == "Beer"
    pays = sorted((p["method"], p["amount"], p["is_cash"], p["tip"]) for p in night["payments"])
    assert pays == [("Cash", 42.4, 1, 0.0), ("Visa", 42.05, 0, 8.0)], "cash is the amount applied, not tendered"
    (payout,) = night["payouts"]
    assert (payout["category"], payout["amount"], payout["manager_name"]) == ("Misc Paid Out", 20.0, "Jordan Rivera")
    # The night's punches run 4am to 4am: the next morning's opener is not 3/14's
    assert len(night["punches"]) == 4


def test_the_frozen_simulator_night_reconciles_like_a_live_store(demo):
    """Live, 10/10/26: the lines' sale total matched RPOWER's own ticket
    summary to the cent. The simulator's night must too, through the same
    readers — and its totals are pinned so a reader change shows."""
    data, _ = pos.fetch_day_sales(demo.rid, date.fromisoformat(demo.day))
    checks = data["source_checks"]
    tickets = demo.corpus["responses"]["ticket/getbybusinessdate"]
    assert checks["unknown_type_lines"] == 0
    assert checks["lines_sale"] == checks["ticket_type_sale"]
    assert checks["comps_reconciled"]
    assert data["transactions"] == len(tickets) == checks["tickets"]
    assert data["guests"] == sum(t["guest_count"] for t in tickets)
    assert data["tax"] == round(sum(t["tax"] for t in tickets), 2)
    assert rpower.fetch_business_days(demo.rid, demo.day, demo.day) == {demo.day: data["net"]}
    assert (data["gross"], data["discounts"], data["comps"], data["voids"], data["net"]) == PINNED_DEMO_NIGHT


def test_the_frozen_simulator_night_archives_whole(demo):
    night = rpower.archive_rows(demo.rid, demo.day)
    corpus = demo.corpus["responses"]
    assert night["checks"]["gap"] == 0.0 and night["checks"]["unknown_type_lines"] == 0
    assert night["links_ok"] is True
    assert len(night["tickets"]) == len(corpus["ticket/getbybusinessdate"])
    assert len(night["lines"]) == len(corpus["ticketsales/getbybusinessdate"])
    assert len(night["payments"]) == len(corpus["ticketpayment/getbybusinessdate"])
    data, _ = pos.fetch_day_sales(demo.rid, date.fromisoformat(demo.day))
    assert night["net_sales"] == data["net"], "the archive's net and the report's net are one number"


def test_the_frozen_simulator_punches_and_store(demo):
    out = rpower.test_token("tok-frozen-demo")
    assert out["stores"][0]["timezone"] == "America/Chicago" and out["stores"][0]["name"] == "Halsted & Rye"
    entries = rpower.fetch_time_entries(demo.rid, demo.day, demo.day)
    assert len(entries) == len(demo.corpus["responses"]["timeclock/getbydaterange"])
    assert all(rpower._punch_dt(e["in_dttm"]).date().isoformat() == demo.day for e in entries)
    assert rpower.fetch_day_closed(demo.rid, demo.day) is True


# gross, discounts, comps, voids, net of the frozen night, as the readers derive them on 10/26/26
PINNED_DEMO_NIGHT = (334.5, 8.25, 12.0, 24.0, 314.25)


# ── Stripe: events in the real SDK's objects ────────────────────────────────

ACACIA, BASIL = "2025-02-24.acacia", "2025-03-31.basil"
SUB, CUS, RID = "sub_FIXTURE0001", "cus_FIXTURE0001", 41


def _event(name):
    """The file as Stripe sends it, built into the installed SDK's objects —
    what stripe.Webhook.construct_event hands the webhook."""
    return stripe.Event.construct_from(_load("stripe", name), "sk_test_fixture")


@pytest.fixture
def billed(db_path):
    """Restaurant 41 ("Fixture Bistro"), billed to the corpus's customer."""
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO restaurants (id, name, owner_email, billing_status, stripe_customer_id) "
                 "VALUES (?,?,?,?,?)", (RID, "Fixture Bistro", "owner@fixture.test", "active", CUS))
    conn.commit()
    conn.close()
    return RID


def test_every_stripe_event_is_the_real_sdks_object_for_a_shape_the_code_reads():
    names = sorted(os.listdir(os.path.join(FIXTURES, "stripe")))
    assert len(names) >= 9
    for name in names:
        ev = _event(name)
        assert type(ev) is stripe.Event and isinstance(ev.data.object, stripe.StripeObject), name
        assert ev["api_version"] in (ACACIA, BASIL) and ev["livemode"] is False, name
        assert name.startswith(ev["type"] + "."), name
    assert type(_event("invoice.paid.basil.json").data.object) is stripe.Invoice


@pytest.mark.parametrize("name,facts", [
    # acacia: the setup fee beside the first month, the ids on the invoice itself; no tax charged
    ("invoice.paid.acacia.json", {"subscription_id": SUB, "customer_id": CUS, "kind": "mixed",
                                  "recurring_cents": 34900, "setup_cents": 75000, "tax_cents": None,
                                  "amount_paid_cents": 109900, "charge_id": "ch_FIXTURE_ACACIA",
                                  "payment_intent_id": "pi_FIXTURE_ACACIA", "billing_reason": "subscription_create"}),
    # basil: the subscription under parent.subscription_details, lines typed by parent, total_taxes;
    # the invoice no longer names its charge or payment intent
    ("invoice.paid.basil.json", {"subscription_id": SUB, "customer_id": CUS, "kind": "recurring",
                                 "recurring_cents": 34900, "setup_cents": 0, "tax_cents": 2051,
                                 "amount_paid_cents": 36951, "charge_id": None, "payment_intent_id": None,
                                 "billing_reason": "subscription_cycle"}),
    ("invoice.payment_failed.acacia.json", {"subscription_id": SUB, "kind": "recurring", "amount_paid_cents": 0,
                                            "amount_remaining_cents": 34900, "attempt_count": 1,
                                            "next_payment_attempt": "2026-11-28 03:48:20", "paid_at": None,
                                            "charge_id": "ch_FIXTURE_ACACIA_FAILED"}),
    ("invoice.payment_failed.basil.json", {"subscription_id": SUB, "kind": "recurring", "amount_paid_cents": 0,
                                           "amount_remaining_cents": 34900, "attempt_count": 2,
                                           "next_payment_attempt": "2026-11-30 03:48:20", "paid_at": None,
                                           "charge_id": None}),
])
def test_invoice_facts_read_both_api_versions(name, facts):
    inv = _event(name).data.object
    got = billing_jobs.invoice_facts(inv)
    assert {k: got[k] for k in facts} == facts
    assert billing_jobs.invoice_metadata(inv)["restaurant_id"] == "41"


def test_subscription_facts_find_the_period_end_on_either_shape():
    acacia = billing_jobs.subscription_facts(_event("customer.subscription.deleted.acacia.json").data.object)
    basil = billing_jobs.subscription_facts(_event("customer.subscription.deleted.basil.json").data.object)
    for f in (acacia, basil):
        assert (f["subscription_id"], f["customer_id"], f["status"], f["interval"], f["amount_cents"]) == \
            (SUB, CUS, "canceled", "month", 34900)
        assert f["current_period_end"] == "2026-11-25 03:46:40", "basil moved it onto the item"
        assert f["module_keys"] == "reviews,labor" and f["cancellation_reason"] == "cancellation_requested · too_expensive"


@pytest.mark.parametrize("name", ["checkout.session.completed.json", "invoice.paid.acacia.json",
                                  "invoice.paid.basil.json", "invoice.payment_failed.acacia.json",
                                  "invoice.payment_failed.basil.json", "customer.subscription.deleted.acacia.json",
                                  "customer.subscription.deleted.basil.json"])
def test_each_event_finds_its_restaurant_by_metadata_then_by_the_subscription_mirror(billed, name):
    ev = _event(name)
    assert webhook_routes._resolve_stripe_event(ev) == (RID, "metadata")
    billing_jobs.upsert_subscription(RID, subscription_id=SUB, facts={"customer_id": CUS, "status": "active"})
    assert webhook_routes._resolve_stripe_event(ev) == (RID, "subscription")


def test_a_refund_finds_its_restaurant_by_the_invoice_it_paid_else_by_the_customer(billed):
    refund = _event("charge.refunded.acacia.json")
    assert webhook_routes._resolve_stripe_event(refund) == (RID, "customer")
    billing_jobs.record_invoice(_event("invoice.paid.acacia.json").data.object, RID, "evt_FIXTURE_INV_PAID_A",
                                paid=True)
    assert webhook_routes._resolve_stripe_event(refund) == (RID, "invoice")
    # A charge with no invoice id, whose invoice (basil) recorded no charge: the stored customer.
    assert webhook_routes._resolve_stripe_event(_event("charge.refunded.no_invoice.json")) == (RID, "customer")


def test_an_event_for_a_customer_nobody_is_billed_to_matches_nobody(db_path):
    assert webhook_routes._resolve_stripe_event(_event("charge.refunded.no_invoice.json")) == (None, None)
    assert webhook_routes._resolve_stripe_event(_event("invoice.paid.basil.json")) == (None, None)
