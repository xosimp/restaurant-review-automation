"""Re-audit, group A: the outcome engine and the money (9/24/26).

Each test names the fix-list item it pins (A1 … A37, contracts K4 and K7)
and fails on the code before the fix. The engine is outcomes.py +
metrics.py + value_delivered.py; the surfaces are the /outcomes and /value
routes, the Home value block, the win push, the owner report, milestones
and the emails' value lines.

No model, network, email, SMS or push is reached. Holidays are pinned to
none unless a test sets them.
"""
import json
import sys
import threading
from datetime import date, timedelta

import pytest
from flask import Flask

import metrics
import models
import rec_ledger
from models import Restaurant, create_restaurant

# Imported before the fixture patches get_conn (the bound-import hazard).
import milestones  # noqa: E402
import outcomes  # noqa: E402
import owner_report  # noqa: E402
import strategy_jobs  # noqa: E402
import strategy_routes  # noqa: E402
import value_delivered  # noqa: E402

TODAY = date.today()


@pytest.fixture(autouse=True)
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(outcomes, "_holidays_between", lambda s, e: {})
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    return db_path


def _rid(name="Group A Co", **kw):
    for m in ("module_labor", "module_inventory", "module_reviews", "module_marketing"):
        kw.setdefault(m, 1)
    return create_restaurant(Restaurant(name=name, owner_email="a@x.test", **kw))


def _x(sql, args=()):
    c = models.get_conn()
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _q(sql, args=()):
    c = models.get_conn()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _days(rid, start, n, labor=300.0, sales=1000.0, skip=None):
    """labor_daily_history for n days from start; `skip(d)` leaves a day out."""
    c = models.get_conn()
    for i in range(n):
        d = start + timedelta(days=i)
        if skip and skip(d):
            continue
        s = sales(d) if callable(sales) else sales
        lab = labor(d) if callable(labor) else labor
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_cost, sales, labor_pct) "
                  "VALUES (?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), lab, s,
                                          round(lab / s * 100, 1) if s else None))
    c.commit()
    c.close()


def _loss(rid, start, n, amount, events, kind="comp", skip=None):
    for i in range(n):
        d = start + timedelta(days=i)
        if skip and skip(d):
            continue
        _x("INSERT INTO pos_loss_daily (restaurant_id, business_date, kind, amount, events) VALUES (?,?,?,?,?)",
           (rid, d.isoformat(), kind, amount(d) if callable(amount) else amount, events))


def _insert(rid, title, metric, dollars, started_on, verdict="improved", module=None, source="ask",
            window=28, checkin=None, key=None):
    """An evaluated tracker as outcomes.evaluate leaves it."""
    s = date.fromisoformat(started_on)
    e = s + timedelta(days=window)
    return _x("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, baseline_value, "
              "baseline_start, baseline_end, started_on, evaluate_on, after_value, after_start, after_end, verdict, "
              "delta, dollars_monthly, status, module, attribution, concurrent, baseline_kind, owner_checkin) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'evaluated',?,?,?,'prior window',?)",
              (rid, source, key or f"{source}:{title.lower()}", title, metric, 32.0,
               (s - timedelta(days=window)).isoformat(), (s - timedelta(days=1)).isoformat(), started_on,
               e.isoformat(), 29.0, started_on, (e - timedelta(days=1)).isoformat(), verdict, -3.0, dollars,
               module, "consistent", "[]", json.dumps(checkin) if checkin else None))


def _vday(rid, oid, day, dollars, metric="labor_pct", family="labor_cost", module="labor"):
    _x("INSERT INTO outcome_value_days (outcome_id, restaurant_id, day, module, metric, family, sign, dollars, held, "
       "counted, basis) VALUES (?,?,?,?,?,?,?,?,1,1,'daily')",
       (oid, rid, day.isoformat(), module, metric, family, 1 if dollars >= 0 else -1, dollars))


def _route(fn, user, body=None, args=(), query=None):
    app = Flask(__name__)
    with app.test_request_context(json=body or {}, query_string=query or {}):
        return fn(user, *args)


def _owner(rid):
    return {"id": 1, "restaurant_id": rid, "role": "client", "is_admin": 0, "grants": ()}


def _manager(rid):
    return {"id": 2, "restaurant_id": rid, "role": "manager", "is_admin": 0, "grants": ()}


# ── A1 trading days ─────────────────────────────────────────────────────────

def _closed_mondays():
    return lambda d: d.weekday() == 0


def test_a1_labor_dollars_are_priced_on_the_restaurants_trading_days():
    """Closed Mondays: $50 saved on each of 26 trading days a month is $1,300,
    not $1,516.67 (30.33 days) — and the window accrues what the 24 trading
    days actually moved."""
    rid = _rid()
    t0 = TODAY - timedelta(days=28)
    _days(rid, t0 - timedelta(days=28), 28, labor=300, skip=_closed_mondays())
    _days(rid, t0, 28, labor=250, skip=_closed_mondays())
    o = outcomes.record(rid, "manual", "manual:x", "Trim", "labor_pct", today=t0)
    e = outcomes.evaluate(o["id"], today=TODAY)
    assert e["verdict"] == "improved" and e["dollars_monthly"] == 1300.0
    assert outcomes.cumulative(rid)["total"] == 1200.0        # 24 trading days x $50
    assert metrics.days_per_month(rid, "labor_pct", t0, TODAY - timedelta(days=1)) == 26.0


def test_a1_comp_moves_accrue_only_on_trading_days():
    rid = _rid()
    t0 = TODAY - timedelta(days=28)
    closed = _closed_mondays()
    _days(rid, t0 - timedelta(days=28), 56, sales=1000, skip=closed)
    # The POS reports every day, closed or not; comps 2% of sales, then 1%.
    _loss(rid, t0 - timedelta(days=28), 56, lambda d: 0.0 if closed(d) else (20.0 if d < t0 else 10.0), 1)
    o = outcomes.record(rid, "manual", "manual:c", "Tighten comps", "comp_rate", today=t0)
    e = outcomes.evaluate(o["id"], today=TODAY)
    assert e["delta"] == -1.0 and e["dollars_monthly"] == 260.0      # $10 x 26 trading days
    assert outcomes.cumulative(rid)["total"] == 240.0                 # $10 x the 24 open days


# ── A2 coverage floor ───────────────────────────────────────────────────────

def test_a2_one_day_before_and_one_after_is_unknown_not_a_verdict():
    rid = _rid()
    t0 = TODAY - timedelta(days=28)
    _days(rid, t0 - timedelta(days=1), 1, labor=300)
    _days(rid, t0 + timedelta(days=5), 1, labor=250)
    o = outcomes.record(rid, "manual", "manual:x", "Trim", "labor_pct", today=t0)
    assert o["baseline_value"] is None and "trading days" in o["baseline_detail"]
    e = outcomes.evaluate(o["id"], today=TODAY)
    assert e["verdict"] == "unknown" and e["dollars_monthly"] is None
    assert outcomes.total_value(rid)["monthly"] == 0


def test_a2_a_closed_weekday_is_not_a_missing_day():
    rid = _rid()
    start = TODAY - timedelta(days=28)
    _days(rid, start - timedelta(days=28), 56, skip=_closed_mondays())
    cov = metrics.coverage(rid, "labor_pct", start.isoformat(), (TODAY - timedelta(days=1)).isoformat())
    assert cov["share"] == 1.0 and cov["expected"] == cov["measured"] == 24


# ── A3 comp / void rate over the days asked ─────────────────────────────────

def test_a3_a_comp_sync_that_stopped_is_not_a_fall():
    """Comps synced for 10 of 28 days while sales kept syncing: the rate is
    read over the 10 days asked (and the window is too thin to judge), never
    10 days of comps over 28 days of sales."""
    rid = _rid()
    start = TODAY - timedelta(days=28)
    _days(rid, start, 28, sales=1000)
    _loss(rid, start, 10, 20.0, 2)
    v, detail = metrics.measure(rid, "comp_rate", start.isoformat(), (TODAY - timedelta(days=1)).isoformat())
    assert v == 2.0 and "10 days of sales" in detail
    t0 = TODAY - timedelta(days=28)
    rid2 = _rid(name="b")
    _days(rid2, t0 - timedelta(days=28), 56, sales=1000)
    _loss(rid2, t0 - timedelta(days=28), 28, 20.0, 2)
    _loss(rid2, t0, 10, 20.0, 2)
    o = outcomes.record(rid2, "manual", "manual:c", "Tighten comps", "comp_rate", today=t0)
    assert outcomes.evaluate(o["id"], today=TODAY)["verdict"] == "unknown"


def test_a3_comps_that_really_went_to_zero_are_a_measured_zero():
    rid = _rid()
    t0 = TODAY - timedelta(days=28)
    _days(rid, t0 - timedelta(days=28), 56, sales=1000)
    _loss(rid, t0 - timedelta(days=28), 28, 20.0, 2)
    _loss(rid, t0, 28, 0.0, 0)
    o = outcomes.record(rid, "manual", "manual:c", "Tighten comps", "comp_rate", today=t0)
    e = outcomes.evaluate(o["id"], today=TODAY)
    assert e["after_value"] == 0.0 and e["verdict"] == "improved"


# ── A4 / A22 one grading rule with the check-in ─────────────────────────────

def _ninety_day_win(name):
    rid = _rid(name=name)
    t0 = TODAY - timedelta(days=90)
    _days(rid, t0 - timedelta(days=28), 28, labor=300)
    _days(rid, t0, 90, labor=250)
    o = outcomes.record(rid, "manual", "manual:x", "Trim lunch", "labor_pct", today=t0)
    return rid, o["id"], t0


def test_a4_conditions_changed_is_not_undone_by_the_recheck():
    rid, oid, t0 = _ninety_day_win("changed")
    outcomes.evaluate(oid, today=t0 + timedelta(days=28))
    outcomes.apply_checkin(rid, oid, "yes", True)
    rc = outcomes.recheck(oid, today=TODAY)
    assert rc["recheck_verdict"] == "held" and rc["attribution"] == "associated" and not rc["validated"]
    assert outcomes.total_value(rid)["validated"] == 0


def test_a4_a_disowned_result_is_never_validated():
    rid, oid, t0 = _ninety_day_win("no")
    outcomes.evaluate(oid, today=t0 + timedelta(days=28))
    outcomes.apply_checkin(rid, oid, "no", False)
    rc = outcomes.recheck(oid, today=TODAY)
    assert rc["attribution"] != "held" and rc["validated"] is False and rc["counts"] is False


def test_a4_a_checkin_given_while_tracking_is_read_by_the_evaluation():
    rid, oid, t0 = _ninety_day_win("early")
    outcomes.apply_checkin(rid, oid, "yes", True)
    e = outcomes.evaluate(oid, today=t0 + timedelta(days=28))
    assert e["attribution"] == "associated" and "something else changed" in e["attribution_label"]


def test_a22_a_yes_after_a_held_recheck_stays_held_and_a_tracking_checkin_is_regraded():
    rid, oid, t0 = _ninety_day_win("held")
    outcomes.evaluate(oid, today=t0 + timedelta(days=28))
    outcomes.apply_checkin(rid, oid, "yes", False)
    assert outcomes.recheck(oid, today=TODAY)["attribution"] == "held"
    again = outcomes.apply_checkin(rid, oid, "yes", False)
    assert again["attribution"] == "held" and again["validated"] is True
    rid, oid, t0 = _ninety_day_win("tracking")
    outcomes.apply_checkin(rid, oid, "yes", False)
    outcomes.evaluate(oid, today=t0 + timedelta(days=28))
    outcomes.apply_checkin(rid, oid, "yes", False)
    assert _q("SELECT attribution FROM recommendation_outcomes WHERE id=?", (oid,))[0]["attribution"] == "consistent"


# ── A5 / A6 sales are not savings ───────────────────────────────────────────

def _sales_lift_world():
    rid = _rid()
    t0 = TODAY - timedelta(days=28)
    _days(rid, t0 - timedelta(days=28), 28, labor=300, sales=1000)
    _days(rid, t0, 28, labor=300, sales=1150)            # labor dollars flat, sales +15%
    a = outcomes.record(rid, "slow_day_campaign", "campaign:x", "Guest text", "sales", today=t0, module="marketing")
    b = outcomes.record(rid, "schedule", "sched:x", "Trim Monday lunch", "labor_pct", today=t0, module="labor",
                        gate=None)
    return rid, [outcomes.evaluate(i, today=TODAY) for i in (a["id"], b["id"])]


def test_a5_a_sales_rise_is_listed_beside_a_labor_share_and_never_counted_as_a_saving():
    rid, (sales, lab) = _sales_lift_world()
    kinds = {c["kind"] for c in lab["concurrent"]}
    assert "sales_move" in kinds and "tracker" in kinds and lab["attribution"] == "associated"
    v = outcomes.total_value(rid)
    assert v["monthly"] == 0 and v["wins"] == 0              # labor dollars never moved
    assert v["sales_lift"]["monthly"] == sales["dollars_monthly"]
    cum = outcomes.cumulative(rid)
    # The labor share was read alongside a sales move and another tracker:
    # confounded, so it accrues nothing at all (re-audit B2 #5) — nothing
    # measured, not $0.
    assert cum["total"] in (None, 0.0) and cum["sales_lift"]["total"] > 0


def test_a6_the_sales_lift_is_reported_apart_and_the_payload_says_so():
    rid = _rid()
    _insert(rid, "Tuesday promo", "sales", 500.0, "2026-03-01", module="marketing")
    _insert(rid, "Trim Monday", "labor_pct", 300.0, "2026-06-01", module="labor")
    d = value_delivered.delivered(rid)
    assert d["monthly"] == 300.0 and d["sales_lift"]["monthly"] == 500.0
    assert d["sales_pricing"] == "separate" and "not profit" in d["sales_lift"]["basis"]
    assert "not profit" in outcomes.summarise(outcomes.get_outcome(
        _q("SELECT id FROM recommendation_outcomes WHERE metric='sales'")[0]["id"]))


# ── A7 / A20 one family, netted, the non-overlapping best set ───────────────

def test_a7_a_labor_win_and_an_overtime_loss_over_the_same_weeks_are_the_labor_reading():
    rid = _rid()
    a = _insert(rid, "Trim lunch", "labor_pct", 1516.67, "2026-06-01", module="labor")
    b = _insert(rid, "Overtime", "overtime_hours", -216.67, "2026-06-01", verdict="worsened", module="labor")
    v = outcomes.total_value(rid)
    assert v["monthly"] == 1516.67 and v["worsened"]["count"] == 0 and v["net_monthly"] == 1516.67
    for i in range(10):
        d = date(2026, 6, 1) + timedelta(days=i)
        _vday(rid, a, d, 50.0)
        _vday(rid, b, d, -7.1, metric="overtime_hours")
    cum = outcomes.cumulative(rid)
    assert cum["gained"] == 500.0 and cum["lost"] == 0.0


def test_a20_overlaps_are_not_chained():
    def row(metric, start, dollars):
        s = date.fromisoformat(start)
        return {"metric": metric, "after_start": start, "after_end": (s + timedelta(days=27)).isoformat(),
                "dollars_monthly": dollars}
    kept = outcomes.distinct([row("labor_pct", "2026-01-01", 600.0), row("overtime_hours", "2026-01-25", 100.0),
                              row("labor_pct", "2026-02-18", 600.0)])
    assert sorted(r["after_start"] for r in kept) == ["2026-01-01", "2026-02-18"]
    # Same breadth: the non-overlapping set of largest dollars, not one per chain.
    kept = outcomes.distinct([row("labor_pct", "2026-01-01", 300.0), row("labor_pct", "2026-01-20", 400.0),
                              row("labor_pct", "2026-02-10", 300.0)])
    assert sum(r["dollars_monthly"] for r in kept) == 600.0


# ── A8 the restaurant's own date ────────────────────────────────────────────

def test_a8_local_today_is_the_restaurants_date_and_record_uses_it(monkeypatch):
    east = _rid(name="Kiritimati", timezone="Pacific/Kiritimati")      # UTC+14
    west = _rid(name="Pago", timezone="Pacific/Pago_Pago")             # UTC-11
    assert outcomes.local_today(east) > outcomes.local_today(west)
    _days(west, TODAY - timedelta(days=60), 60)
    monkeypatch.setattr(outcomes, "local_today", lambda rid, db_path=None: date(2026, 6, 1))
    o = outcomes.record(west, "manual", "manual:x", "Trim", "labor_pct")
    assert o["started_on"] == "2026-06-01" and o["baseline_end"] == "2026-05-31"


# ── A9 zero and uncosted waste ──────────────────────────────────────────────

def test_a9_nothing_to_nothing_is_no_change_and_uncosted_waste_is_unknown():
    assert metrics.compare("weekly_waste", 0.0, 0.0)["verdict"] == "no_clear_change"
    assert metrics.compare("sales", 0.0, 0.0)["verdict"] == "no_clear_change"
    assert metrics.compare("weekly_waste", 0.0, 0.01)["verdict"] == "no_clear_change"
    rid = _rid()
    iid = _x("INSERT INTO ingredients (restaurant_id, name) VALUES (?, 'Parsley')", (rid,))
    for i in range(1, 28, 3):
        _x("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source) "
           "VALUES (?,?,?,?,?,?)", (rid, iid, "waste", 2.0, (TODAY - timedelta(days=i)).isoformat(), "manual"))
    v, detail = metrics.measure(rid, "weekly_waste", (TODAY - timedelta(days=28)).isoformat(), TODAY.isoformat())
    assert v is None and "no unit cost" in detail


# ── A10 / A23 what is not another change ───────────────────────────────────

def test_a10_the_reprice_tracker_is_its_own_repricing():
    rid = _rid()
    key = rec_ledger.rec_key("reprice", "Burger")
    rec_ledger.present_many(rid, [{"key": key, "module": "food", "title": "Reprice the burger to $16",
                                   "expected_metric": "food_cost_pct"}], "food")
    rec_ledger.record(rid, key, "accepted", surface="food")
    o = outcomes.record(rid, "reprice", f"reprice:{TODAY.strftime('%Y-%m')}", "Menu prices changed",
                        "food_cost_pct", module="inventory", today=TODAY)
    row = dict(outcomes.get_outcome(o["id"]))
    assert outcomes.find_concurrent(row, TODAY.isoformat(), (TODAY + timedelta(days=27)).isoformat()) == []


def test_a23_a_disowned_tracker_is_not_a_change_on_another():
    rid = _rid()
    _days(rid, TODAY - timedelta(days=60), 60)
    a = outcomes.record(rid, "manual", "manual:a", "Trim", "labor_pct", today=TODAY - timedelta(days=10))
    b = outcomes.record(rid, "manual", "manual:b", "Overtime", "overtime_hours", today=TODAY - timedelta(days=10),
                        gate=None)
    row = dict(outcomes.get_outcome(a["id"]))
    span = ((TODAY - timedelta(days=10)).isoformat(), (TODAY + timedelta(days=17)).isoformat())
    assert [c["kind"] for c in outcomes.find_concurrent(row, *span)] == ["tracker"]
    outcomes.apply_checkin(rid, b["id"], "no", False)
    assert outcomes.find_concurrent(row, *span) == []


# ── A11 / A25 / A30 whose recommendation, which number ─────────────────────

def test_a11_a25_fill_tuesdays_is_marketing_and_tuesday_sales_wherever_it_was_shown():
    rid = _rid()
    rec_ledger.present(rid, "slow_day:Tuesday", "labor", "brief_email", title="Tuesdays run 22% under")
    assert outcomes.metric_for_rec(rid, "slow_day:Tuesday") == ("weekday_sales:Tuesday", True)
    # The screen iOS sends does not beat the recommendation's own module.
    assert outcomes.resolve_module(rid, "recommendation", "slow_day:Tuesday", "weekday_sales:Tuesday",
                                   module="labor") == "marketing"
    rec_ledger.present(rid, "promote_day:Friday", "marketing", "home", title="Fill Fridays")
    assert outcomes.resolve_module(rid, "recommendation", "promote_day:Friday", "sales", module="labor") == "marketing"
    rec_ledger.record(rid, "slow_day:Tuesday", "accepted", surface="home")
    lab = {"id": 999, "restaurant_id": rid, "metric": "labor_pct", "source_key": "manual:x",
           "baseline_kind": "matched weekdays", "baseline_start": (TODAY - timedelta(days=28)).isoformat(),
           "baseline_end": (TODAY - timedelta(days=1)).isoformat()}
    span = ((TODAY - timedelta(days=2)).isoformat(), (TODAY + timedelta(days=25)).isoformat())
    assert outcomes.find_concurrent(lab, *span) == []
    sal = dict(lab, metric="weekday_sales:Tuesday", source_key="campaign:Tuesday:x", baseline_kind="prior window")
    assert [c["kind"] for c in outcomes.find_concurrent(sal, *span)] == ["accepted_rec"]


def test_a30_overtime_recommendations_carry_overtime_hours():
    rid = _rid()
    assert outcomes.metric_for_rec(rid, "overtime_move:Ana:2026-09-26") == ("overtime_hours", True)
    assert outcomes.metric_for_rec(rid, "overtime") == ("overtime_hours", True)


# ── A12 holidays by name against last year ──────────────────────────────────

def test_a12_a_same_weeks_last_year_baseline_flags_a_holiday_that_moved(monkeypatch):
    cal = {"2027-03-17": "St. Patrick's Day", "2027-03-28": "Easter", "2026-03-18": "St. Patrick's Day"}
    monkeypatch.setattr(outcomes, "_holidays_between",
                        lambda s, e: {d: n for d, n in cal.items() if str(s)[:10] <= d <= str(e)[:10]})
    rid = _rid()
    s, e = date(2027, 3, 1), date(2027, 3, 28)
    r = {"id": 1, "restaurant_id": rid, "metric": "sales", "source_key": "manual:x", "source": "manual",
         "baseline_kind": "same weeks last year", "baseline_start": (s - timedelta(days=28)).isoformat(),
         "baseline_end": (s - timedelta(days=1)).isoformat()}
    assert [(c["kind"], c["label"]) for c in outcomes.find_concurrent(r, s.isoformat(), e.isoformat())] \
        == [("holiday", "Easter")]


# ── A13 a trend already under way ───────────────────────────────────────────

def test_a13_a_number_already_drifting_up_is_not_validated():
    rid = _rid()
    t0 = TODAY - timedelta(days=90)
    start = t0 - timedelta(days=28)
    _days(rid, start, 118, sales=lambda d: round(1000 * (1 + 0.004 * (d - start).days), 2))
    o = outcomes.record(rid, "manual", "manual:x", "New lunch special", "sales", today=t0)
    e = outcomes.evaluate(o["id"], today=t0 + timedelta(days=28))
    assert e["verdict"] == "improved" and e["attribution"] == "associated"
    assert any(c["kind"] == "trend" for c in e["concurrent"]) and "already moving" in e["attribution_label"]
    rc = outcomes.recheck(o["id"], today=TODAY)
    assert rc["validated"] is False and outcomes.total_value(rid)["validated"] == 0


def test_a13_a_flat_baseline_is_not_a_trend():
    rid = _rid()
    t0 = TODAY - timedelta(days=28)
    _days(rid, t0 - timedelta(days=28), 28, labor=300)
    _days(rid, t0, 28, labor=250)
    o = outcomes.record(rid, "manual", "manual:x", "Trim", "labor_pct", today=t0)
    assert outcomes.evaluate(o["id"], today=TODAY)["attribution"] == "consistent"


# ── A14 the win push ────────────────────────────────────────────────────────

def test_a14_the_win_push_claims_no_cause_and_reaches_only_who_may_see_it(monkeypatch):
    import permissions
    rid = _rid()
    tid = _insert(rid, "Cut 12 hours (owner-only figures)", "labor_pct", 260.0, (TODAY - timedelta(days=40)).isoformat(),
                  source="recommendation", key="dsr_action:control_hours:labor")
    rec_ledger.present(rid, "dsr_action:control_hours:labor", "labor", "home", title="Cut 12 hours", owner_only=1)
    rec_ledger.link_tracker(rid, "dsr_action:control_hours:labor", tid)
    sent = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda rid_, kind, title, body, data, db, subject=None, **k:
                        sent.append((subject, title, k.get("permissions"))) or 1)
    row = dict(outcomes.get_outcome(tid), restaurant_id=rid)
    assert strategy_jobs._tell_owners_what_worked([row], models.DB_PATH) == 1
    subject, title, perms = sent[0]
    assert "paid off" not in subject and "after your change" not in title
    assert title.startswith("Labor % improved while your change was in place")
    assert permissions.TEAM_INVITE in perms and permissions.LABOR_VIEW in perms
    loss = strategy_jobs._win_permissions(rid, {"id": 0, "metric": "labor_pct", "source_key": "loss:2026-09-14:comps"})
    assert permissions.LOSS_VIEW in loss


def test_a14_a5_a_result_the_value_figures_set_aside_is_not_announced(monkeypatch):
    rid, (sales, lab) = _sales_lift_world()
    sent = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda rid_, kind, title, body, data, db, subject=None, **k:
                        sent.append(title) or 1)
    rows = [dict(outcomes.get_outcome(lab["id"]), restaurant_id=rid)]
    assert strategy_jobs._tell_owners_what_worked(rows, models.DB_PATH) == 0 and sent == []
    rows.append(dict(outcomes.get_outcome(sales["id"]), restaurant_id=rid))
    assert strategy_jobs._tell_owners_what_worked(rows, models.DB_PATH) == 1
    assert "revenue, not profit" in sent[0]


# ── A15 / K7 a disowned result carries no dollars ───────────────────────────

def test_a15_k7_the_summary_of_a_disowned_result_names_no_dollars():
    rid = _rid()
    tid = _insert(rid, "Trim Monday lunch", "labor_pct", 1516.67, "2026-06-01", checkin={"did_it": "no"})
    row = outcomes.get_outcome(tid)
    s = outcomes.summarise(row)
    assert row["counts"] is False and "$" not in s and "isn't counted: you said the change wasn't made" in s


# ── A16 two starts at once ──────────────────────────────────────────────────

def test_a16_two_starts_on_one_key_at_the_same_moment_are_one_tracker(monkeypatch):
    rid = _rid()
    _days(rid, TODAY - timedelta(days=60), 59)
    barrier = threading.Barrier(2, timeout=5)
    real = outcomes._baseline

    def slow(*a, **k):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return real(*a, **k)
    monkeypatch.setattr(outcomes, "_baseline", slow)
    out = {}

    def go(name, key):
        try:
            out[name] = outcomes.start(rid, "manual", key, name, "labor_pct")
        except Exception as e:          # the bug: an IntegrityError, a 500
            out[name] = e
    ts = [threading.Thread(target=go, args=(n, "manual:same")) for n in ("A", "B")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert all(isinstance(v, dict) and v["ok"] for v in out.values()), out
    assert out["A"]["outcome"]["id"] == out["B"]["outcome"]["id"]
    assert len(_q("SELECT id FROM recommendation_outcomes WHERE restaurant_id=?", (rid,))) == 1


# ── A17 one spelling per metric ─────────────────────────────────────────────

def test_a17_weekday_keys_are_one_number_however_they_are_spelled():
    assert metrics.normalize("weekday_sales: tuesday") == "weekday_sales:Tuesday"
    assert metrics.normalize("complaints:Food Quality") == "complaints:food_quality"
    rid = _rid()
    _days(rid, TODAY - timedelta(days=80), 79)
    assert outcomes.start(rid, "slow_day_campaign", "campaign:Tuesday:x", "Text", "weekday_sales:Tuesday")["ok"]
    assert outcomes.start(rid, "ask", "ask:tuesday", "Special", "weekday_sales:tuesday")["ok"] is False
    assert outcomes.start(rid, "manual", "manual:tue", "Happy hour", "weekday_sales: Tuesday")["ok"] is False


# ── A18 / A19 automatic starts ──────────────────────────────────────────────

def test_a18_an_observed_schedule_is_refused_while_overtime_is_measured():
    rid = _rid()
    _days(rid, date(2026, 7, 1), 92)
    outcomes.record(rid, "manual", "manual:ot", "Cut overtime", "overtime_hours", today=date(2026, 8, 1))
    assert outcomes.observe(rid, "schedule_published", user_id=1, today=date(2026, 8, 2)) is None
    # A reprice or a campaign is automatic too: the family gate.
    with pytest.raises(outcomes.TrackerRefused):
        outcomes.record(rid, "schedule", "sched:x", "Trim", "labor_pct", today=date(2026, 8, 2))


def test_a19_once_per_metric_per_month_holds_after_evaluation():
    rid = _rid()
    _days(rid, date(2026, 7, 1), 92)
    a = outcomes.observe(rid, "schedule_published", detail="week of 2026-08-03", user_id=1, today=date(2026, 8, 1))
    outcomes.evaluate(a["id"], today=date(2026, 8, 29))
    assert outcomes.observe(rid, "schedule_published", user_id=1, today=date(2026, 8, 30)) is None
    assert len(_q("SELECT id FROM recommendation_outcomes WHERE restaurant_id=?", (rid,))) == 1


# ── A21 whole weeks ─────────────────────────────────────────────────────────

def test_a21_windows_are_whole_weeks_and_a_legacy_recheck_holds_the_same_weekdays():
    rid = _rid()
    _days(rid, TODAY - timedelta(days=200), 199, sales=lambda d: 2500.0 if d.weekday() in (4, 5) else 1000.0)
    o = outcomes.record(rid, "manual", "manual:x", "Promo", "sales", window_days=30, today=TODAY - timedelta(days=150))
    assert o["window_days"] == 28
    # A 30-day tracker from before: its re-check window starts a whole number
    # of weeks after it did, so it holds the after-window's weekdays.
    s = TODAY - timedelta(days=120)
    legacy = {"started_on": s.isoformat(), "evaluate_on": (s + timedelta(days=30)).isoformat(), "metric": "sales"}
    start, end = outcomes._aligned_window(legacy, date.fromisoformat(outcomes.recheck_on_for(legacy))
                                          - timedelta(days=1))
    assert (start - s).days % 7 == 0 and (end - start).days == 29
    base = metrics.measure(rid, "sales", s.isoformat(), (s + timedelta(days=29)).isoformat())[0]
    later = metrics.measure(rid, "sales", start.isoformat(), end.isoformat())[0]
    assert base == later


# ── A24 / A26 / A27 / A28 the routes ────────────────────────────────────────

def _world_for_routes():
    rid = _rid()
    _days(rid, TODAY - timedelta(days=60), 60)
    return rid


def test_a24_a_dsr_action_is_measured_by_its_kind_on_post_outcomes():
    rid = _world_for_routes()
    rec_ledger.present(rid, "dsr_action:reorder:inventory", "ops", "home", title="Reorder the fries")
    out, st = _route(strategy_routes._do_outcome_record, _owner(rid),
                     {"source": "recommendation", "source_key": "dsr_action:reorder:inventory",
                      "title": "Reorder", "metric": "labor_pct"})
    assert st == 200 and out["tracker_refused"]["code"] == "no_metric" and "outcome" not in out
    rec_ledger.present(rid, "dsr_action:control_hours:labor", "labor", "home", title="Cut 12 hours")
    out, st = _route(strategy_routes._do_outcome_record, _owner(rid),
                     {"source": "recommendation", "source_key": "dsr_action:control_hours:labor",
                      "title": "Cut hours", "metric": "sales"})
    assert st == 200 and out["tracker"]["metric"] == "labor_pct"


def test_k2_a_key_never_shown_or_not_seen_is_404_on_post_outcomes():
    rid = _world_for_routes()
    out, st = _route(strategy_routes._do_outcome_record, _owner(rid),
                     {"source": "recommendation", "source_key": "trim_day:Nowhere", "title": "x", "metric": "labor_pct"})
    assert st == 404
    rec_ledger.present(rid, "reprice:Fries", "food", "food", title="Raise the fries")
    out, st = _route(strategy_routes._do_outcome_record, _manager(rid),
                     {"source": "recommendation", "source_key": "reprice:Fries", "title": "x", "metric": "labor_pct"})
    assert st == 404 and "outcome" not in out


def _comp_and_owner_only(rid):
    comp = _insert(rid, "Comp approvals need the GM", "comp_rate", 900.0, (TODAY - timedelta(days=70)).isoformat(),
                   source="manual", module="other")
    oo = _insert(rid, "Cut 12 hours (owner-only)", "labor_pct", 2000.0, (TODAY - timedelta(days=60)).isoformat(),
                 source="recommendation", key="dsr_action:control_hours:labor", module="labor")
    rec_ledger.present(rid, "dsr_action:control_hours:labor", "labor", "home", title="Cut 12 hours", owner_only=1)
    rec_ledger.link_tracker(rid, "dsr_action:control_hours:labor", oo)
    fine = _insert(rid, "Trim Tuesday", "overtime_hours", 300.0, (TODAY - timedelta(days=200)).isoformat(),
                   module="labor")
    for i in range(20):
        _vday(rid, comp, TODAY - timedelta(days=70 - i), 30.0, metric="comp_rate", family="comps", module="other")
        _vday(rid, oo, TODAY - timedelta(days=60 - i), 60.0)
        _vday(rid, fine, TODAY - timedelta(days=200 - i), 10.0, metric="overtime_hours")
    return comp, oo, fine


def test_a26_a_manager_never_reads_owner_only_or_comp_results_or_their_dollars():
    rid = _world_for_routes()
    comp, oo, fine = _comp_and_owner_only(rid)
    out, _ = _route(strategy_routes._do_outcomes_list, _manager(rid))
    assert {o["id"] for o in out["outcomes"]} == {fine}
    out, _ = _route(strategy_routes._do_outcomes_list, _owner(rid))
    assert {o["id"] for o in out["outcomes"]} == {comp, oo, fine}
    mgr, _ = _route(strategy_routes._do_value, _manager(rid))
    d = mgr["delivered"]
    assert d["monthly"] == 300.0 and d["biggest"]["title"] == "Trim Tuesday"
    assert d["cumulative"]["total"] == 200.0 and "other" not in d["cumulative"]["by_module"]
    own, _ = _route(strategy_routes._do_value, _owner(rid))
    assert own["delivered"]["monthly"] == 3200.0 and own["delivered"]["biggest"]["title"].startswith("Cut 12 hours")
    hl = value_delivered.headline(rid, user=_manager(rid))
    assert hl["monthly"] == 300 and hl["restaurant_wide"] is False


def test_a27_comps_need_loss_view_and_a_running_tracker_is_refused_not_returned():
    rid = _world_for_routes()
    assert strategy_routes._metric_visible(_manager(rid), "comp_rate") is False
    assert strategy_routes._metric_visible(dict(_manager(rid), grants=("loss.view",)), "void_rate") is True
    out, st = _route(strategy_routes._do_outcome_record, _manager(rid), {"title": "Voids", "metric": "void_rate"})
    assert st == 403
    rec_ledger.present(rid, "trim_day:Friday", "labor", "home", title="Trim Friday")
    outcomes.record(rid, "recommendation", "trim_day:Friday", "Trim Friday", "overtime_hours")
    out, st = _route(strategy_routes._do_outcome_record, _owner(rid),
                     {"source": "recommendation", "source_key": "trim_day:Friday", "title": "x", "metric": "labor_pct"})
    assert st == 409 and "outcome" not in out and "tracker" not in out
    # A body metric the login may not read is never started from its answer.
    assert outcomes.metric_for_rec(rid, "insight_labor:z", body_metric="food_cost_pct",
                                   viewer=_manager(rid)) == (None, False)


def test_a28_abandon_leaves_a_tracker_the_login_may_not_see():
    rid = _world_for_routes()
    rec_ledger.present(rid, "reprice:Fries", "food", "food", title="Raise the fries")
    t = outcomes.record(rid, "recommendation", "reprice:Fries", "Fries", "weekly_waste")
    out, st = _route(strategy_routes._do_outcome_abandon, _manager(rid), args=(t["id"],))
    assert (out, st) == ({"ok": True}, 200) and outcomes.get_outcome(t["id"])["status"] == "tracking"
    _route(strategy_routes._do_outcome_abandon, _owner(rid), args=(t["id"],))
    assert outcomes.get_outcome(t["id"])["status"] == "abandoned"


# ── A29 / A33 / A35 / A36 / K4 net everywhere ──────────────────────────────

def test_a29_k4_the_headline_carries_the_net_and_the_snapshot_is_net(monkeypatch):
    rid = _rid()
    _insert(rid, "Trim", "labor_pct", 400.0, "2026-06-01", module="labor")
    _insert(rid, "Cheaper cheese", "food_cost_pct", -150.0, "2026-06-01", verdict="worsened", module="inventory")
    _insert(rid, "Rating", "avg_rating", None, "2026-06-01", module="reviews")
    # A35: the headline does not build the best-ever or the rates.
    monkeypatch.setattr(outcomes, "best_ever", lambda *a, **k: pytest.fail("headline built best_ever"))
    monkeypatch.setattr(value_delivered, "rates", lambda: pytest.fail("headline built the rates"))
    hl = value_delivered.headline(rid, user=_owner(rid))
    assert hl["monthly"] == 400 and hl["net_monthly"] == 250
    assert hl["worsened"] == {"count": 1, "monthly": 150.0, "priced_count": 1}
    block = value_delivered.home_block(hl, [])
    assert {"total", "net_monthly", "worsened", "cumulative", "unpriced_wins", "sales_lift"} <= set(block)
    assert block["unpriced_wins"][0]["title"] == "Rating"
    assert value_delivered.compute_total_value_delivered(rid) == 250


def test_a29_a36_milestones_read_the_measured_sum_not_a_projection():
    rid = _rid()
    oid = _insert(rid, "Trim", "labor_pct", 500.0, (TODAY - timedelta(days=150)).isoformat(), module="labor")
    assert milestones.check_savings(rid) is None               # $6,000 a year projected, nothing summed yet
    for i in range(30):
        _vday(rid, oid, TODAY - timedelta(days=100 - i), 40.0)
    m = milestones.check_savings(rid)
    assert m and m["value"] == 1000 and "a year" not in m["body"] and "summed over the days" in m["body"]
    _x("UPDATE restaurants SET created_at=? WHERE id=?", ((TODAY - timedelta(days=95)).isoformat(), rid))
    a = milestones.check_anniversary(rid, restaurant=models.get_restaurant(rid))
    assert a and "Measured results in that time: $1,200" in a["body"]


def test_a29_the_email_value_lines_are_net_and_call_x12_a_projection():
    rid = _rid()
    _insert(rid, "Trim", "labor_pct", 400.0, "2026-06-01", module="labor")
    _insert(rid, "Cheaper cheese", "food_cost_pct", -150.0, "2026-06-01", verdict="worsened", module="inventory")
    _insert(rid, "Promo", "sales", 500.0, "2026-03-01", module="marketing")
    lines = value_delivered.value_lines(value_delivered.delivered(rid))
    assert lines[0].startswith("Measured results: $400/month from 1 change that improved, less $150/month from 1 "
                               "that got worse — $250/month net.")
    assert "a projection, not a measurement" in lines[0] and "$3,000" in lines[0]
    assert any("not profit" in x for x in lines)


def test_a33_worsened_names_its_priced_count():
    rid = _rid()
    _insert(rid, "Rating fell", "avg_rating", None, "2026-06-01", verdict="worsened", module="reviews")
    _insert(rid, "Cheaper cheese", "food_cost_pct", -150.0, "2026-06-01", verdict="worsened", module="inventory")
    w = outcomes.total_value(rid)["worsened"]
    assert w == {"count": 2, "monthly": 150.0, "priced_count": 1}


def test_a35_cumulative_is_summed_in_sql_by_module():
    src = open(outcomes.__file__, encoding="utf-8").read()
    body = src[src.index("def cumulative("):src.index("def apply_checkin(")]
    assert "GROUP BY sl, module" in body and "ROW_NUMBER() OVER" in body


# ── A31 the improved rate ───────────────────────────────────────────────────

def test_a31_the_improved_rate_is_over_clear_verdicts():
    from intelligence import features
    rid = _rid()
    # Updated for the ONE success definition (CA2 finding 4): the rate is
    # read through rec_learning.learned_verdict over CLEAR_VERDICTS — a
    # no-clear-change is measured (in the denominator), an unknown is not —
    # one result per number per window, and None below
    # MIN_MEASURED_FOR_RATE (5). Each result is on its own number here so
    # none overlaps another.
    metrics_ = ("labor_pct", "sales", "avg_rating", "weekly_waste", "response_hours", "overtime_hours",
                "comp_rate")
    verdicts = ("improved", "improved", "worsened", "unknown", "no_clear_change", "unknown", "improved")
    for i, (m, v) in enumerate(zip(metrics_, verdicts)):
        _insert(rid, f"t {i} {v}", m, None, (TODAY - timedelta(days=40)).isoformat(), verdict=v)
    f = features.compute(rid)
    assert f["outcomes_evaluated_90d"] == 7 and f["outcomes_improved_rate_90d"] == round(3 / 5, 3)
    # Below the floor there is no rate at all.
    rid2 = _rid(name="Group A Floor Co")
    for i, m in enumerate(metrics_[:4]):
        _insert(rid2, f"u {i}", m, None, (TODAY - timedelta(days=40)).isoformat(), verdict="improved")
    assert features.compute(rid2)["outcomes_improved_rate_90d"] is None


# ── A32 no direction from results that disagree ─────────────────────────────

def test_a32_an_average_inside_the_band_has_no_direction():
    c = {"metric": "labor_pct", "label": "Labor %", "unit": "%", "results": 2, "improved": 1, "worsened": 0,
         "no_clear_change": 1, "mean_delta": -0.2, "mean_delta_pct": -0.7, "consistent_direction": False}
    s = owner_report._change_sentence(c)
    assert "no consistent direction" in s and "reduction" not in s


# ── A34 dates an owner reads ────────────────────────────────────────────────

def test_a34_owner_facing_dates_are_mdy():
    import goals
    reply = outcomes.not_measurable_reply("food_cost_pct", "no counted inventory value within 10 days of 2026-09-01")
    assert "9/1/26" in reply["reason"] and "2026-09-01" not in reply["reason"]
    g = {"label": "Labor %", "unit": "%", "target": 25.0, "current": 27.0, "deadline": "2026-12-31", "state": "flat"}
    assert "by 12/31/26" in goals.summarise(g)
    import inspect
    import cogs
    import good_news
    assert "isoformat()}" not in inspect.getsource(cogs) and "{_mdy(start)}" in inspect.getsource(cogs)
    assert "ending {_mdy(current['end'])}" in inspect.getsource(good_news)
    import emails
    assert "Your audit, {_mdy(cmp['audit_date'])}" in inspect.getsource(emails)


# ── A37 bounded, sargable ───────────────────────────────────────────────────

def test_a37_the_evaluation_job_is_bounded_and_the_events_query_is_a_range(monkeypatch):
    src = open(outcomes.__file__, encoding="utf-8").read()
    fc = src[src.index("def find_concurrent("):src.index("def pre_trend(")]
    assert "AND date(e.at)" not in fc and "e.at>=? AND e.at<?" in fc
    seen = []
    monkeypatch.setattr(strategy_jobs, "_bounded_each", lambda job, fn, db_path, **k: seen.append(job) or 0)
    strategy_jobs.run_outcome_evaluations()
    assert seen == ["outcome_evaluations"]
    rid = _rid()
    _days(rid, TODAY - timedelta(days=60), 60)
    for k in ("a", "b"):
        outcomes.record(rid, "manual", f"manual:{k}", k, "labor_pct" if k == "a" else "avg_rating",
                        today=TODAY - timedelta(days=40))
    assert outcomes.evaluate_due(rid, max_seconds=-1) == []               # out of time: stops, never raises
    assert len(outcomes.evaluate_due(rid)) == 2
