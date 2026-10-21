"""Audit #23 (10/21/26), fix group F3 — sales and money semantics.

One classification of every POS line, frozen when the night was archived
(A2-01), with the lines that are not revenue said apart (cancelled tickets
A2-02, transfers A2-10, gift cards and account payments A2-14); one loss
value (A2-03); one check, guest and tip rule (A2-04, A2-06, A2-11); refunds
beside net (A2-05); a restated night derived again (A2-07, A6-05); the live
read learns a new sales type (A2-08); comps reconciled (A2-09); units sold
vs made (A2-12, A3-12) and no stale item rows (A2-17); the Labor window on
complete nights (A2-13); Ask's window (A2-19); tax_adjust (A2-20); DST
(A2-23); the DSR's drink sales from the Bar (A3-11); drink margins at the
level rung (A3-13); price changes dated by the sale (A3-21)."""
import json
from datetime import date, datetime, timedelta

import pytest

import models
import pos
import pos_archive
import pos_checks
import rpower

DAY = date.today() - timedelta(days=10)          # closed, and past the freshness window
ISO = DAY.isoformat()
NULL = "2000-01-01T00:00:00"


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos_archive, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(pos, "PROVIDERS", None)
    pos_archive._change_memo.clear()
    rpower.clear_caches()
    yield
    rpower.clear_caches()


def _rid(db_path, **cols):
    conn = models.get_conn(db_path)
    base = {"id": 1, "name": "Money Co", "owner_email": "m@x.test", "rpower_token": "tok", "rpower_cg": 1280,
            "rpower_store_mid": "1123", "timezone": "America/Chicago"}
    base.update(cols)
    conn.execute(f"INSERT INTO restaurants ({','.join(base)}) VALUES ({','.join('?' for _ in base)})",
                 tuple(base.values()))
    conn.commit()
    conn.close()
    return 1


SALE = {"mid": "S", "name": "Sale", "impacts_sales": 1, "impacts_costs": 1, "type_sale": 1}


def _routes(day_iso, stamp="T23:00:00"):
    d = f"{day_iso}T00:00:00"
    ts = f"{day_iso}{stamp}"
    return {
        "salestype/getbycg": [SALE,
                              {"mid": "D", "name": "Discount", "impacts_sales": 1, "type_discnt": 1},
                              {"mid": "C", "name": "Comp", "impacts_sales": 1, "impacts_costs": 1, "type_comp": 1},
                              {"mid": "E", "name": "Error Correct", "type_error": 1},
                              {"mid": "R", "name": "Refund", "type_refund": 1}],
        "salescategory/getbycg": [{"mid": "K1", "name": "Entrees", "slsdep_mid": "DP1"},
                                  {"mid": "K2", "name": "Gift Cards", "slsdep_mid": "DP2"}],
        "salesdepartment/getbycg": [{"mid": "DP1", "name": "Food"}, {"mid": "DP2", "name": "Other"}],
        "menuitem/getbycg": [{"mid": "M1", "name": "Brisket", "slscat_mid": "K1"},
                             {"mid": "M2", "name": "Wings", "slscat_mid": "K1"},
                             {"mid": "G1", "name": "Gift Card $50", "slscat_mid": "K2"}],
        "voidreason/getbycg": [{"mid": "R1", "name": "Error Made"}, {"mid": "R2", "name": "Wrong Check"},
                               {"mid": "R3", "name": "TRANSFERRED"}],
        "job/getbycg": [{"mid": "J1", "name": "Server"}],
        "employee/getbycg": [{"mid": "E1", "fname": "Dana", "lname": "Reyes"}],
        "table/getbystore": [], "room/getbystore": [], "mealtime/getbycg": [], "profitcenter/getbycg": [],
        "paymentmethod/getbycg": [{"mid": "PM1", "name": "Cash", "is_cash": 1}], "payoutcategory/getbycg": [],
        "ticketpayment/getbybusinessdate": [], "payout/getbydaterange": [], "timeclock/getbydaterange": [],
        "ticketitem/getbybusinessdate": [],
        "ticketsales/getbybusinessdate": [
            {"rid": "L1", "ticket_rid": "T1", "date": d, "menuitem_mid": "M1", "slstype_mid": "S", "qty": 2,
             "sales": 40.0, "price": 20.0, "regular_price": 20.0, "item_dttm": f"{day_iso}T18:05:00", "voided": 0,
             "time_stamp": ts},
            {"rid": "L2", "ticket_rid": "T1", "date": d, "slstype_mid": "D", "qty": 1, "sales": -5.0, "voided": 0,
             "time_stamp": ts},
            {"rid": "L3", "ticket_rid": "T1", "date": d, "menuitem_mid": "M2", "slstype_mid": "C", "qty": 1,
             "sales": 9.5, "price": 0.0, "regular_price": 0.0, "mgr_mid": "E1", "voidrsn_mid": "R1", "voided": 0,
             "time_stamp": ts},
            {"rid": "L4", "ticket_rid": "T2", "date": d, "menuitem_mid": "M2", "slstype_mid": "E", "qty": 1,
             "sales": 11.0, "voidmgr_mid": "E1", "voidrsn_mid": "R2", "voided": 0, "time_stamp": ts}],
        "ticket/getbybusinessdate": [
            {"rid": "T1", "date": d, "open_dttm": f"{day_iso}T18:00:00", "close_dttm": f"{day_iso}T19:00:00",
             "kvs_dttm": NULL, "bump_dttm": NULL, "guest_count": 2, "tax": 3.1, "type_sale": 40.0,
             "type_discnt": -5.0, "type_promo": 0, "type_comp": 9.5, "is_cancelled": 0, "time_stamp": ts},
            {"rid": "T2", "date": d, "open_dttm": NULL, "close_dttm": NULL, "guest_count": 1, "tax": 0.0,
             "is_cancelled": 0, "time_stamp": ts}],
    }


def _stub(monkeypatch, routes):
    calls = []

    def fake(token, path, params=None):
        calls.append(path)
        return routes[path]
    monkeypatch.setattr(rpower, "_request", fake)
    return calls


def _failures(job):
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM job_failures WHERE job=?", (job,)).fetchall()]
    finally:
        conn.close()


def _add_line(routes, **line):
    d = f"{ISO}T00:00:00"
    routes["ticketsales/getbybusinessdate"].append(
        {"date": d, "voided": 0, "qty": 1, "time_stamp": f"{ISO}T23:00:00", **line})


# ── A2-01: an archived line keeps the sales type it was rung under ─────────

def test_an_archived_line_keeps_its_sales_types_flags(db_path, monkeypatch):
    rid = _rid(db_path)
    routes = _routes(ISO)
    _stub(monkeypatch, routes)
    out = rpower.archive_rows(rid, DAY)
    flags = {ln["line_id"]: json.loads(ln["sales_type_flags"]) for ln in out["lines"]}
    assert flags["L1"]["type_sale"] == 1 and flags["L1"]["impacts_sales"] == 1 and flags["L1"]["mid"] == "S"
    assert flags["L4"]["type_error"] == 1


def test_a_sales_type_changed_at_the_store_never_rescores_an_archived_night(db_path, monkeypatch):
    rid = _rid(db_path)
    routes = _routes(ISO)
    _stub(monkeypatch, routes)
    pos_archive.archive_day(rid, DAY)
    rpower.clear_caches()
    before = rpower.fetch_business_days(rid, DAY, DAY)
    sales_before = rpower.fetch_day_sales(rid, DAY)
    assert before == {ISO: 35.0}
    # The store deletes its "Sale" type and stops the discount counting.
    routes["salestype/getbycg"] = [t for t in routes["salestype/getbycg"] if t["mid"] != "S"]
    routes["salestype/getbycg"] = [dict(t, impacts_sales=0) if t["mid"] == "D" else t
                                   for t in routes["salestype/getbycg"]]
    with rpower.fresh_lists():
        rpower.sales_types(rid)
    rpower.clear_caches()
    assert rpower.fetch_business_days(rid, DAY, DAY) == before
    assert rpower.fetch_day_sales(rid, DAY)["gross"] == sales_before["gross"]
    gone = _failures("pos_sales_type_gone")
    assert len(gone) == 1 and "Sale (S)" in gone[0]["error"]
    rpower.clear_caches()
    rpower.fetch_business_days(rid, DAY, DAY)
    assert len(_failures("pos_sales_type_gone")) == 1                  # raised once, not every read


def test_a_line_archived_before_the_flags_reads_the_current_list(db_path, monkeypatch):
    rid = _rid(db_path)
    _stub(monkeypatch, _routes(ISO))
    pos_archive.archive_day(rid, DAY)
    conn = models.get_conn()
    conn.execute("UPDATE pos_ticket_lines SET sales_type_flags=NULL")
    conn.commit()
    conn.close()
    rpower.clear_caches()
    assert rpower.fetch_business_days(rid, DAY, DAY) == {ISO: 35.0}


def test_a_live_line_of_a_deleted_type_is_classified_from_the_archives_kept_flags(db_path, monkeypatch):
    rid = _rid(db_path)
    routes = _routes(ISO)
    _stub(monkeypatch, routes)
    pos_archive.archive_day(rid, DAY)                               # the archive knows type S
    routes["salestype/getbycg"] = [t for t in routes["salestype/getbycg"] if t["mid"] != "S"]
    with rpower.fresh_lists():
        rpower.sales_types(rid)
    rpower.clear_caches()
    monkeypatch.setenv("POS_ARCHIVE_READS", "0")
    assert rpower._business_days(rid, DAY, DAY, use_archive=False) == {ISO: 35.0}


# ── A2-02: a cancelled ticket's lines are in nothing ───────────────────────

def _with_cancelled(routes):
    _add_line(routes, rid="L9", ticket_rid="T9", menuitem_mid="M1", slstype_mid="S", sales=30.0, price=30.0)
    routes["ticket/getbybusinessdate"].append(
        {"rid": "T9", "date": f"{ISO}T00:00:00", "open_dttm": f"{ISO}T20:00:00", "guest_count": 4, "tax": 2.0,
         "type_sale": 30.0, "is_cancelled": 1, "time_stamp": f"{ISO}T23:00:00"})
    return routes


def test_a_cancelled_tickets_lines_leave_net_checks_and_covers_alike(db_path, monkeypatch):
    rid = _rid(db_path)
    _stub(monkeypatch, _with_cancelled(_routes(ISO)))
    monkeypatch.setenv("POS_ARCHIVE_READS", "0")
    live = rpower.fetch_day_sales(rid, DAY)
    assert live["gross"] == 49.5 and live["transactions"] == 1 and live["guests"] == 2   # never blanked
    assert live["source_checks"]["cancelled_lines"] == 1 and live["source_checks"]["cancelled_sales"] == 30.0
    monkeypatch.setenv("POS_ARCHIVE_READS", "1")
    pos_archive.archive_day(rid, DAY)
    rpower.clear_caches()
    archived = rpower.fetch_day_sales(rid, DAY)
    assert (archived["gross"], archived["transactions"], archived["guests"]) == (49.5, 1, 2)
    assert rpower.fetch_business_days(rid, DAY, DAY) == {ISO: 35.0}


def test_the_nightly_reconcile_leaves_a_cancelled_ticket_out_on_both_sides(db_path, monkeypatch):
    rid = _rid(db_path)
    _stub(monkeypatch, _routes(ISO))
    gap = rpower.archive_rows(rid, DAY)["checks"]["gap"]
    rpower.clear_caches()
    _stub(monkeypatch, _with_cancelled(_routes(ISO)))
    assert rpower.archive_rows(rid, DAY)["checks"]["gap"] == gap


# ── A2-03: one loss value ──────────────────────────────────────────────────

def test_one_loss_value_for_every_path():
    priced = {"sales": -5.0, "price": 5.0, "regular_price": 8.0, "qty": 1}
    assert rpower.loss_value(priced) == rpower._line_value(priced) == rpower._loss_amount(priced) == 5.0
    assert rpower.loss_value({"sales": 0, "regular_price": 8.0, "price": 5.0, "qty": 2}) == 16.0
    assert rpower.loss_value({"sales": 0, "price": 4.0, "qty": 3}) == 12.0
    assert rpower.loss_value({"sales": "x"}) == 0.0


def test_the_reports_voids_and_the_archives_loss_amount_agree_on_a_priced_void(db_path, monkeypatch):
    rid = _rid(db_path)
    routes = _routes(ISO)
    _add_line(routes, rid="L7", ticket_rid="T1", menuitem_mid="M1", slstype_mid="S", voided=1, sales=5.0,
              price=5.0, regular_price=8.0)
    _stub(monkeypatch, routes)
    rows = rpower.archive_rows(rid, DAY)
    archived = {ln["line_id"]: ln["loss_amount"] for ln in rows["lines"] if ln["kind"] == "void"}
    rpower.clear_caches()
    monkeypatch.setenv("POS_ARCHIVE_READS", "0")
    assert rpower.fetch_day_sales(rid, DAY)["voids"] == round(sum(archived.values()), 2) == 16.0


# ── A2-04 / A2-06 / A2-11: one check, guest, tip and payment rule ──────────

def test_a_check_is_a_ticket_with_an_item_sold_and_guests_are_on_checks_only():
    tickets = {"a": {"guest_count": 2}, "b": {"guest_count": 3}, "comped": {"guest_count": 4}}
    out = pos_checks.count(tickets, {"a", "b"})
    assert (out["checks"], out["guests"]) == (2, 5)
    assert pos_checks.count(tickets, {"a", "missing"})["guests"] is None       # a partial feed: no covers
    assert pos_checks.count({"a": {"guest_count": 0}}, {"a"})["guests"] is None  # no covers recorded


def test_the_tip_rate_is_the_payments_tips_over_the_checks_net():
    tickets = [{"ticket_id": "a", "net_sales": 50.0}, {"ticket_id": "b", "net_sales": 30.0},
               {"ticket_id": "c", "net_sales": 20.0}]
    tips = pos_checks.tips_by_ticket([{"ticket_id": "a", "tip": 10.0}, {"ticket_id": "b", "tip": 6.0},
                                      {"ticket_id": "b", "tip": 0.0}])
    assert pos_checks.tip_rate(tickets, tips) == (20.0, 2)
    assert pos_checks.tip_rate(tickets, {}) == (None, 0)


def test_a_cash_payment_counts_what_it_put_on_the_check():
    assert pos_checks.applied_amount(78.0) == 78.0                       # no change: paid is applied
    assert pos_checks.applied_amount(100.0, 22.0, 78.0) == 78.0          # tendered $100, $22 back
    assert pos_checks.applied_amount(78.0, 22.0, 78.0) == 78.0           # already applied: kept
    assert pos_checks.applied_amount(100.0, 22.0) == 78.0                # no target: paid less change
    assert pos_checks.applied_amount(10.0, 22.0) == 0.0                  # never below zero


def test_the_archive_keeps_change_and_target_beside_the_applied_amount(db_path, monkeypatch):
    rid = _rid(db_path)
    routes = _routes(ISO)
    routes["ticketpayment/getbybusinessdate"] = [
        {"rid": "Y1", "ticket_rid": "T1", "date": f"{ISO}T00:00:00", "paymeth_mid": "PM1", "paid_ticket": 100.0,
         "cash_change": 61.9, "target_amt": 38.1, "paid_tip": 0.0}]
    _stub(monkeypatch, routes)
    (p,) = rpower.archive_rows(rid, DAY)["payments"]
    assert (p["amount"], p["cash_change"], p["target_amt"]) == (38.1, 61.9, 38.1)


def test_service_performance_counts_checks_and_leaves_cancelled_payments_out(db_path, monkeypatch):
    import service_performance as sp
    rid = _rid(db_path)
    routes = _routes(ISO)
    routes["ticketpayment/getbybusinessdate"] = [
        {"rid": "Y1", "ticket_rid": "T1", "date": f"{ISO}T00:00:00", "paymeth_mid": "PM1", "paid_ticket": 38.1},
        {"rid": "Y9", "ticket_rid": "T9", "date": f"{ISO}T00:00:00", "paymeth_mid": "PM1", "paid_ticket": 32.0}]
    _stub(monkeypatch, _with_cancelled(routes))
    pos_archive.archive_day(rid, DAY)
    house = sp.servers(rid, days=1, today=DAY + timedelta(days=1))["house"]
    # T1 is the night's one check ($35); T2 (a void only) is a ticket, not a check.
    assert (house["tickets"], house["checks"], house["check_average"], house["covers"]) == (2, 1, 35.0, 2)
    assert sp.payments(rid, days=1, today=DAY + timedelta(days=1))["total_paid"] == 38.1


# ── A2-05: refunds beside net, said ────────────────────────────────────────

def test_a_night_with_refunds_says_net_after_them_to_who_may_see_refunds():
    from dsr import access, block_sales
    notes, loss = block_sales._night_notes({"refunds": 25.0, "source_checks": {}}, 1000.0)
    assert notes == [] and loss and "Net after refunds: $975.00" in loss[0] and "$25.00 refunded" in loss[0]
    assert block_sales._night_notes({"refunds": 0.0}, 1000.0) == ([], [])
    manager = {"id": 9, "role": "manager", "is_admin": 0, "permissions": []}
    assert not access.line_allowed(manager, access.MANAGER, "loss_notes")
    assert not access.line_allowed(manager, access.MANAGER, "loss_refunds")


# ── A2-07 / A6-05: a restated night derived again, raised once ──────────────

def test_a_restated_night_queues_one_rederive_and_it_moves_every_figure(db_path, monkeypatch):
    import job_queue
    import pos_rederive
    from dsr import pipeline
    rid = _rid(db_path)
    routes = _routes(ISO)
    _stub(monkeypatch, routes)
    pos_archive.archive_day(rid, DAY)
    conn = models.get_conn()
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales, labor_cost, labor_pct) "
                 "VALUES (?,?,?,?,?)", (rid, ISO, 35.0, 10.0, 28.6))
    conn.commit()
    conn.close()
    # A comp entered after close: the POS rewrites the night.
    _add_line(routes, rid="L8", ticket_rid="T1", menuitem_mid="M1", slstype_mid="D", sales=-10.0,
              time_stamp=f"{ISO}T23:59:00")
    rpower.clear_caches()
    assert pos_archive.archive_day(rid, DAY)["restated"] is True
    conn = models.get_conn()
    rows = conn.execute("SELECT payload_json FROM jobs WHERE kind='pos_rederive'").fetchall()
    conn.close()
    assert [json.loads(r[0])["date"] for r in rows] == [ISO]
    assert job_queue.HANDLERS["pos_rederive"] == "pos_rederive:run_queued"
    rechecked = []
    monkeypatch.setattr(pipeline, "recheck_final", lambda r, night: rechecked.append(night) or {"action": "new"})
    rpower.clear_caches()
    out = pos_rederive.rederive(rid, "rpower", ISO)
    assert out["failed"] == [] and rechecked == [ISO]
    conn = models.get_conn()
    lab = conn.execute("SELECT sales, labor_pct FROM labor_daily_history WHERE restaurant_id=? AND date=?",
                       (rid, ISO)).fetchone()
    conn.close()
    assert (lab[0], lab[1]) == (25.0, 40.0)


def test_the_dsr_rechecks_a_restated_night_from_one_dollar_at_any_age():
    from dsr import pipeline
    assert pipeline.RECHECK_MIN_DOLLARS == 1.0


def test_a_sales_mismatch_is_raised_once_per_night_and_figures(db_path):
    import data_freshness
    _rid(db_path)
    m = [{"date": ISO, "dsr": 3000.0, "pos": 2900.0, "diff_pct": 3.3}]
    assert data_freshness.report_sales_mismatches(1, m) == 1
    assert data_freshness.report_sales_mismatches(1, m) == 0                     # the same finding again
    assert data_freshness.report_sales_mismatches(1, [dict(m[0], pos=2800.0)]) == 1   # it moved: new
    assert len(_failures("dsr_pos_consistency")) == 2


# ── A2-08: a live read learns a sales type added today ────────────────────

def test_a_live_read_reads_the_sales_type_list_again_for_a_type_it_does_not_know(db_path, monkeypatch):
    rid = _rid(db_path)
    routes = _routes(ISO)
    _add_line(routes, rid="LN", ticket_rid="T1", menuitem_mid="M1", slstype_mid="NEW", sales=12.0)
    _stub(monkeypatch, routes)
    rpower.sales_types(rid)                                       # the list as it was this morning
    routes["salestype/getbycg"] = routes["salestype/getbycg"] + [
        {"mid": "NEW", "name": "Brunch Special", "impacts_sales": 1, "type_sale": 1}]
    assert rpower.fetch_sales_today(rid, DAY) == 47.0               # 35 + the new type's 12


# ── A2-09: comps against RPOWER's own comp totals ─────────────────────────

def test_comps_that_do_not_reconcile_are_said_on_the_night_and_raised_once(db_path, monkeypatch):
    rid = _rid(db_path)
    routes = _routes(ISO)
    monkeypatch.setenv("POS_ARCHIVE_READS", "0")
    _stub(monkeypatch, routes)
    assert rpower.fetch_day_sales(rid, DAY)["source_checks"]["comps_reconciled"] is True
    routes["ticket/getbybusinessdate"][0]["type_comp"] = 0.0
    rpower.clear_caches()
    sc = rpower.fetch_day_sales(rid, DAY)["source_checks"]
    assert sc["comps_reconciled"] is False and sc["comp_gap"] == 9.5
    rpower.archive_rows(rid, DAY)                                  # the archive's reconcile: the same finding
    assert len(_failures("pos_comp_reconcile")) == 1
    from dsr import block_sales
    _notes, loss = block_sales._night_notes({"source_checks": sc}, 35.0)
    assert loss and "don't match" in loss[0]


# ── A2-10: a transfer is no void ──────────────────────────────────────────

def test_a_void_whose_reason_is_a_transfer_is_no_void_and_no_loss(db_path, monkeypatch):
    rid = _rid(db_path)
    routes = _routes(ISO)
    _add_line(routes, rid="LT", ticket_rid="T1", menuitem_mid="M1", slstype_mid="E", sales=20.0,
              voidrsn_mid="R3", voidmgr_mid="E1")
    _stub(monkeypatch, routes)
    monkeypatch.setenv("POS_ARCHIVE_READS", "0")
    day = rpower.fetch_day_sales(rid, DAY)
    assert day["voids"] == 11.0 and day["source_checks"]["transfer_lines"] == 1
    assert [l["kind"] for l in rpower.fetch_loss_lines(rid, DAY, DAY)].count("void") == 1
    kinds = {ln["line_id"]: ln["kind"] for ln in rpower.archive_rows(rid, DAY)["lines"]}
    assert kinds["LT"] == "transfer" and kinds["L4"] == "void"


def test_a_store_can_say_its_transfer_reason_is_a_real_void(db_path, monkeypatch):
    rid = _rid(db_path, pos_sales_rules=json.dumps({"not_transfer_reasons": ["Transferred"]}))
    routes = _routes(ISO)
    _add_line(routes, rid="LT", ticket_rid="T1", menuitem_mid="M1", slstype_mid="E", sales=20.0, voidrsn_mid="R3")
    _stub(monkeypatch, routes)
    monkeypatch.setenv("POS_ARCHIVE_READS", "0")
    assert rpower.fetch_day_sales(rid, DAY)["voids"] == 31.0


# ── A2-14: a gift card sold is not revenue ────────────────────────────────

def test_a_gift_card_sold_as_an_item_is_out_of_net_and_said(db_path, monkeypatch):
    rid = _rid(db_path)
    routes = _routes(ISO)
    _add_line(routes, rid="LG", ticket_rid="T1", menuitem_mid="G1", slstype_mid="S", sales=50.0)
    routes["ticket/getbybusinessdate"][0]["type_sale"] = 90.0       # RPOWER's own type_sale carries it
    _stub(monkeypatch, routes)
    monkeypatch.setenv("POS_ARCHIVE_READS", "0")
    day = rpower.fetch_day_sales(rid, DAY)
    assert day["gross"] == 49.5 and day["source_checks"]["not_revenue_sales"] == 50.0
    assert rpower.fetch_business_days(rid, DAY, DAY) == {ISO: 35.0}
    rows = rpower.archive_rows(rid, DAY)
    assert {ln["line_id"]: ln["kind"] for ln in rows["lines"]}["LG"] == "not_revenue"
    assert rows["checks"]["gap"] == 0.0                              # both sides of the reconcile keep it
    assert _failures("pos_sales_left_out")


def test_a_store_can_keep_an_item_the_rule_would_leave_out(db_path, monkeypatch):
    rid = _rid(db_path, pos_sales_rules=json.dumps({"revenue_items": ["Gift Card $50"]}))
    routes = _routes(ISO)
    _add_line(routes, rid="LG", ticket_rid="T1", menuitem_mid="G1", slstype_mid="S", sales=50.0)
    _stub(monkeypatch, routes)
    monkeypatch.setenv("POS_ARCHIVE_READS", "0")
    assert rpower.fetch_business_days(rid, DAY, DAY) == {ISO: 85.0}


def test_the_store_rules_column_has_its_four_touch_points(db_path):
    rid = _rid(db_path)
    models.update_restaurant(rid, {"pos_sales_rules": json.dumps({"transfer_reasons": ["Moved"]})})
    assert json.loads(models.get_restaurant(rid).pos_sales_rules) == {"transfer_reasons": ["Moved"]}
    assert rpower.SalesRules(rid).is_transfer({rpower.ARCHIVED_REASON: "moved"})


# ── A2-12 / A3-12 / A2-17: units sold vs made, no stale rows ───────────────

def test_item_sales_keep_sold_and_made_and_drop_a_dish_no_longer_sold(db_path, monkeypatch):
    import inventory_ledger
    rid = _rid(db_path)
    conn = models.get_conn()
    for guid, name in (("M1", "Brisket"), ("M2", "Wings")):
        conn.execute("INSERT INTO menu_items (restaurant_id, name, toast_guid, is_active) VALUES (?,?,?,1)",
                     (rid, name, guid))
    conn.commit()
    conn.close()
    sels = [{"item": {"guid": "M1"}, "quantity": 3.0, "sold": 2.0}, {"item": {"guid": "M2"}, "quantity": 1.0, "sold": 1.0}]
    monkeypatch.setattr(pos, "fetch_order_selections", lambda r, d: (list(sels), "rpower"))
    inventory_ledger.compute_daily_depletion(rid, DAY)
    conn = models.get_conn()
    got = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT m.name, s.qty_sold, s.qty_made FROM menu_item_sales s JOIN menu_items m ON m.id=s.menu_item_id "
        "WHERE s.restaurant_id=?", (rid,)).fetchall()}
    conn.close()
    assert got == {"Brisket": (2.0, 3.0), "Wings": (1.0, 1.0)}
    sels[:] = sels[:1]                                             # the last wings voided after the 5am run
    inventory_ledger.compute_daily_depletion(rid, DAY)
    conn = models.get_conn()
    names = [r[0] for r in conn.execute("SELECT m.name FROM menu_item_sales s JOIN menu_items m "
                                        "ON m.id=s.menu_item_id WHERE s.restaurant_id=?", (rid,)).fetchall()]
    conn.close()
    assert names == ["Brisket"]
    assert inventory_ledger.refresh_item_sales_day(rid, DAY) == {"rows": 1, "removed": 0}


def test_the_dsr_food_estimate_costs_units_made_and_prices_units_sold(db_path, monkeypatch):
    rid = _rid(db_path)
    _stub(monkeypatch, _routes(ISO))
    monkeypatch.setenv("POS_ARCHIVE_READS", "0")
    data, _p = pos.fetch_day_sales(rid, DAY)
    made = {i["guid"]: i["made_qty"] for i in data["items_made"]}
    sold = {i["guid"]: i["qty"] for i in data["items"]}
    assert made == {"M1": 2.0, "M2": 1.0} and sold == {"M1": 2.0}           # the comped wings were made, not sold


# ── A2-13: the Labor window ends on the last complete night ───────────────

def test_the_labor_window_drops_a_day_still_trading():
    import labor
    rows = [{"date": "2026-10-19", "employee": "A"}, {"date": "2026-10-20", "employee": "A"},
            {"date": "2026-10-21", "employee": "A"}]
    kept = labor.current_window(rows, 28, complete_through=date(2026, 10, 20))
    assert [r["date"] for r in kept] == ["2026-10-19", "2026-10-20"]
    assert labor.current_window(rows, 28) == rows


# ── A2-19: Ask's service window ───────────────────────────────────────────

def test_the_service_window_ends_on_the_restaurants_last_complete_night(db_path):
    import service_performance as sp
    rid = _rid(db_path)
    start, end = sp._window(28, restaurant_id=rid)
    assert end == pos.local_complete_through(rid) and (end - start).days == 27


# ── A2-20 / A2-23: tax_adjust kept, DST said ──────────────────────────────

def test_tax_adjust_is_kept_and_shown_never_added(db_path, monkeypatch):
    from dsr import block_sales
    rid = _rid(db_path)
    routes = _routes(ISO)
    routes["ticket/getbybusinessdate"][0]["tax_adjust"] = -1.25
    _stub(monkeypatch, routes)
    pos_archive.archive_day(rid, DAY)
    rpower.clear_caches()
    data = rpower.fetch_day_sales(rid, DAY)
    assert data["tax"] == 3.1 and data["tax_adjust"] == -1.25
    notes, _loss = block_sales._night_notes({"tax_adjust": -1.25}, 35.0)
    assert notes and "-$1.25 of tax adjustments" in notes[0]


def test_a_night_across_a_clock_change_is_labelled(db_path):
    rid = _rid(db_path)
    assert rpower.dst_shift(rid, date(2026, 10, 31))["kind"] == "repeated"     # clocks back 11/1/26, 2am
    assert rpower.dst_shift(rid, date(2026, 3, 7))["kind"] == "missing"        # clocks forward 3/8/26
    assert rpower.dst_shift(rid, date(2026, 10, 20)) is None


# ── A3-11: the report's drink sales are the Bar's ─────────────────────────

def test_the_beverage_kpi_reads_drink_sales_on_the_bars_basis(db_path):
    from dsr import kpis
    rid = _rid(db_path)
    metrics = {"net": 1000.0, "cat:Draft": 150.0, "cat:Food": 850.0}
    # "Draft" is no name in the old fixed list: the report had no drink sales.
    assert kpis._bev_sales(rid, DAY, metrics, db_path) == 150.0
    blocks = {"sales": {"status": "ready", "metrics": metrics}}
    assert kpis._value("bev_mix", blocks, bev=150.0) == 15.0
    assert kpis._bev_sales(rid, DAY, {}, db_path) is None


# ── A3-13 / A3-21: drink prices ───────────────────────────────────────────

def _price_rows(db_path, rid, rows):
    conn = models.get_conn(db_path)
    for item, level_id, level, price in rows:
        conn.execute("INSERT INTO pos_menu_prices (restaurant_id, provider, item_id, level_id, item_name, level, price) "
                     "VALUES (?,?,?,?,?,?,?)", (rid, "rpower", item, level_id, "Tito's", level, price))
    conn.commit()
    conn.close()


def _rung(db_path, rid, item, level, n, day=None, price=None):
    conn = models.get_conn(db_path)
    for i in range(n):
        conn.execute("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, business_date, item_id, kind, "
                     "price_level_id, price) VALUES (?,?,?,?,?,?,?,?)",
                     (rid, "rpower", f"{item}-{level}-{i}-{day}", (day or date.today()).isoformat(), item, "sale",
                      level, price or 0.0))
    conn.commit()
    conn.close()


def test_a_drinks_margin_prices_the_level_it_is_rung_at(db_path):
    from bar import margins
    rid = _rid(db_path)
    _price_rows(db_path, rid, [("V1", "L1", "Level 1", 9.0), ("V1", "L9", "Level 9", 12.0)])
    assert margins._menu(rid, db_path)[("rpower", "V1")]["price"] == 12.0      # nothing rung: the highest
    _rung(db_path, rid, "V1", "L1", 10)
    _rung(db_path, rid, "V1", "L9", 1)
    m = margins._menu(rid, db_path)[("rpower", "V1")]
    assert m["price"] == 9.0 and m["level_basis"] == "the level it was rung at most"


def test_a_price_change_is_dated_by_its_first_sale_and_an_unlisted_price_goes(db_path, monkeypatch):
    import change_log
    rid = _rid(db_path)
    today = date.today()
    _rung(db_path, rid, "M1", "L1", 3, day=today - timedelta(days=1), price=18.0)
    prices = [{"item_id": "M1", "item_name": "Brisket", "level_id": "L1", "level": "Level 1", "price": 18.0},
              {"item_id": "M2", "item_name": "Gone Dish", "level_id": "L1", "level": "Level 1", "price": 9.0},
              {"item_id": "M3", "item_name": "Wings", "level_id": "L1", "level": "Level 1", "price": 11.0}]
    fake = type("P", (), {"fetch_menu_prices": staticmethod(lambda r: [dict(p) for p in prices]),
                          "archive_rows": staticmethod(lambda r, d: None)})
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    assert pos_archive.sync_prices(rid, db_path=db_path, today=today)["baseline"] is True
    conn = models.get_conn(db_path)
    conn.execute("UPDATE pos_archive_state SET prices_read_at=datetime('now','-3 days')")
    conn.commit()
    conn.close()
    sold_on = today - timedelta(days=2)
    _rung(db_path, rid, "M1", "L1", 1, day=sold_on, price=19.0)                # rung at the new price 2 nights ago
    prices[0]["price"] = 19.0
    del prices[1]                                                              # discontinued
    out = pos_archive.sync_prices(rid, db_path=db_path, today=today)
    assert (out["changes"], out["dropped"], out["dated_by_sale"]) == (1, 1, 1)
    (row,) = change_log.history(rid, kinds="price", db_path=db_path)
    assert str(row.get("changed_at") or row.get("at") or "")[:10] == sold_on.isoformat()
    conn = models.get_conn(db_path)
    items = sorted(r[0] for r in conn.execute("SELECT item_id FROM pos_menu_prices WHERE restaurant_id=?", (rid,)))
    conn.close()
    assert items == ["M1", "M3"]


def test_a_short_price_read_drops_nothing(db_path, monkeypatch):
    rid = _rid(db_path)
    _rung(db_path, rid, "M1", "L1", 1)
    prices = [{"item_id": f"M{i}", "item_name": f"D{i}", "level_id": "L1", "level": "Level 1", "price": 9.0}
              for i in range(6)]
    fake = type("P", (), {"fetch_menu_prices": staticmethod(lambda r: [dict(p) for p in prices]),
                          "archive_rows": staticmethod(lambda r, d: None)})
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    pos_archive.sync_prices(rid, db_path=db_path)
    del prices[1:]                                                             # a read that lists 1 of 6
    assert pos_archive.sync_prices(rid, db_path=db_path)["dropped"] == 0
