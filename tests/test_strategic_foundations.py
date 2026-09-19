"""Audit #18 foundations: metrics, outcomes, goals, menu intelligence, demand,
loss detection and the issue accountability loop.

Most of these tests pin a refusal — a number that must stay unknown, a
pattern that must not be read into too little data, a text that must not go
to someone who never consented — because those are the failures that look
exactly like the feature working.
"""
import json
from datetime import date, datetime, timedelta

import pytest

import models
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    """Every module here binds get_conn at import time, so each is patched —
    the bound-import gotcha documented in CLAUDE.md."""
    import metrics, outcomes, goals, menu_intelligence, demand, loss_detection, issues
    import review_intelligence, food_cost_intelligence, business_intelligence
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for m in (models, metrics, outcomes, goals, menu_intelligence, demand, loss_detection,
              issues, review_intelligence, food_cost_intelligence, business_intelligence):
        monkeypatch.setattr(m, "get_conn", redirect, raising=False)


@pytest.fixture
def sms(monkeypatch):
    sent = []
    monkeypatch.setattr("notify.send_sms", lambda to, msg, use_case="alert": sent.append((to, msg)) or True)
    return sent


def _rid(db_path, **kw):
    return create_restaurant(Restaurant(name="Strat Co", owner_email="s@x.com", **kw), db_path=db_path)


def _day(offset):
    return (date.today() + timedelta(days=offset)).isoformat()


def _sales_day(db_path, rid, d, sales, labor_cost=None):
    conn = get_conn(db_path)
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_cost, "
                 "labor_pct) VALUES (?,?,?,?,?,?)",
                 (rid, d, date.fromisoformat(d).strftime("%A"), sales, labor_cost,
                  (labor_cost / sales * 100) if (labor_cost and sales) else None))
    conn.commit(); conn.close()


def _contact(db_path, rid, name="Maria", phone="+15555550100", consent=1):
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) VALUES (?,?,?,?)",
                       (rid, name, phone, consent))
    conn.commit(); cid = cur.lastrowid; conn.close()
    return cid


# ── metrics ────────────────────────────────────────────────────────────────

def test_a_window_with_no_data_is_unknown_not_zero(db_path):
    import metrics
    rid = _rid(db_path)
    value, why = metrics.measure(rid, "labor_pct", _day(-30), _day(-1))
    assert value is None and why


def test_labor_pct_is_cost_over_sales_across_the_window(db_path):
    import metrics
    rid = _rid(db_path)
    _sales_day(db_path, rid, _day(-2), 1000, 300)
    _sales_day(db_path, rid, _day(-1), 3000, 600)
    value, _ = metrics.measure(rid, "labor_pct", _day(-7), _day(-1))
    assert value == 22.5, "900/4000, not the mean of 30% and 20%"


def test_a_move_inside_the_noise_band_is_not_called_an_improvement():
    import metrics
    assert metrics.compare("labor_pct", 28.0, 27.8)["verdict"] == "no_clear_change"
    assert metrics.compare("labor_pct", 28.0, 26.0)["verdict"] == "improved"
    assert metrics.compare("avg_rating", 4.2, 4.5)["verdict"] == "improved"
    assert metrics.compare("labor_pct", None, 26.0)["verdict"] == "unknown"


def test_a_rating_on_a_handful_of_reviews_is_withheld(db_path):
    import metrics
    rid = _rid(db_path)
    value, why = metrics.measure(rid, "avg_rating", _day(-30), _day(0))
    assert value is None and "reviews" in why


# ── outcomes ───────────────────────────────────────────────────────────────

def test_committing_twice_to_the_same_fix_keeps_the_first_baseline(db_path):
    """Re-committing must not reset the baseline — that would quietly erase
    the improvement already made."""
    import outcomes
    rid = _rid(db_path)
    _sales_day(db_path, rid, _day(-3), 1000, 300)
    a = outcomes.record(rid, "brief", "trim:monday", "Trim Monday lunch", "labor_pct")
    _sales_day(db_path, rid, _day(-2), 1000, 100)
    b = outcomes.record(rid, "brief", "trim:monday", "Trim Monday lunch", "labor_pct")
    assert a["id"] == b["id"]
    assert b["baseline_value"] == a["baseline_value"]


def test_an_outcome_is_evaluated_only_after_its_window_and_says_what_moved(db_path):
    import outcomes
    rid = _rid(db_path)
    start = date.today() - timedelta(days=40)
    for i in range(28):              # baseline: 30% labour
        _sales_day(db_path, rid, (start - timedelta(days=28) + timedelta(days=i)).isoformat(), 1000, 300)
    for i in range(28):              # after: 25% labour
        _sales_day(db_path, rid, (start + timedelta(days=i)).isoformat(), 1000, 250)
    o = outcomes.record(rid, "ask", "k1", "Cut Tuesday close", "labor_pct", today=start)
    assert outcomes.evaluate(o["id"], today=start + timedelta(days=5)) is None, "too early"
    done = outcomes.evaluate(o["id"], today=start + timedelta(days=28))
    assert done["verdict"] == "improved"
    assert done["delta"] == -5.0
    assert "improved" in outcomes.summarise(done)


def test_an_outcome_with_no_after_data_is_unknown_not_a_failure(db_path):
    import outcomes
    rid = _rid(db_path)
    start = date.today() - timedelta(days=40)
    _sales_day(db_path, rid, (start - timedelta(days=3)).isoformat(), 1000, 300)
    o = outcomes.record(rid, "ask", "k2", "Something", "labor_pct", today=start)
    done = outcomes.evaluate(o["id"], today=start + timedelta(days=28))
    assert done["verdict"] == "unknown"
    assert done["dollars_monthly"] is None


def test_every_outcome_carries_the_causation_caveat():
    import outcomes
    assert "not proven cause" in outcomes.CAUSATION_CAVEAT


# ── goals ──────────────────────────────────────────────────────────────────

def test_a_goal_reports_met_moving_or_unknown(db_path):
    import goals
    rid = _rid(db_path)
    g = goals.set_goal(rid, "labor_pct", 26)
    assert g["state"] == "unknown", "no data yet is unknown, never 'off track'"
    for i in range(10):
        _sales_day(db_path, rid, _day(-1 - i), 1000, 240)
    g = goals.progress(rid)[0]
    assert g["state"] == "met" and g["current"] == 24.0


def test_one_active_goal_per_metric(db_path):
    """Two live targets for labour % are two answers to 'am I on track'."""
    import goals
    rid = _rid(db_path)
    goals.set_goal(rid, "labor_pct", 27)
    goals.set_goal(rid, "labor_pct", 25)
    active = goals.progress(rid)
    assert len(active) == 1 and active[0]["target"] == 25


def test_a_goal_on_an_unknown_metric_is_refused(db_path):
    import goals
    with pytest.raises(ValueError):
        goals.set_goal(_rid(db_path), "happiness", 10)


# ── menu intelligence ──────────────────────────────────────────────────────

def _dish(db_path, rid, name, price, ingredient_cost, qty=1.0, sold=None):
    conn = get_conn(db_path)
    ing = conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost) VALUES (?,?,?,?)",
                       (rid, f"{name} base", "ea", ingredient_cost)).lastrowid
    mid = conn.execute("INSERT INTO menu_items (restaurant_id, toast_guid, name, sell_price) VALUES (?,?,?,?)",
                       (rid, f"g-{name}", name, price)).lastrowid
    conn.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
                 (mid, ing, qty))
    if sold:
        conn.execute("INSERT INTO menu_item_sales (restaurant_id, menu_item_id, business_date, qty_sold) "
                     "VALUES (?,?,?,?)", (rid, mid, _day(-2), sold))
    conn.commit(); conn.close()
    return mid


def _review(db_path, rid, ext, rating, sentiment, dishes, complaint=None):
    models.save_reviews([models.Review(restaurant_id=rid, platform="google", external_id=ext,
                                       author="G", rating=rating, text="x",
                                       review_date=_day(-5) + " 12:00:00")], db_path=db_path)
    conn = get_conn(db_path)
    rv = conn.execute("SELECT id FROM reviews WHERE external_id=?", (ext,)).fetchone()["id"]
    conn.execute("UPDATE reviews SET processed=1, sentiment=?, entities=?, specific_complaint=? WHERE id=?",
                 (sentiment, json.dumps({"dishes": dishes}), complaint, rv))
    conn.commit(); conn.close()


def test_a_star_dish_drawing_complaints_is_urgent(db_path):
    import menu_intelligence as mi
    rid = _rid(db_path, module_inventory=1)
    _dish(db_path, rid, "Ribeye Steak", 40, 12, sold=200)       # popular, high margin
    _dish(db_path, rid, "House Salad", 12, 3, sold=210)
    _dish(db_path, rid, "Soup", 8, 5, sold=10)
    _dish(db_path, rid, "Tart", 9, 6, sold=12)
    for i in range(3):
        _review(db_path, rid, f"r{i}", 2, "negative", ["the ribeye"], "overcooked")
    card = mi.dish_scorecard(rid)
    top = card["dishes"][0]
    assert top["name"] == "Ribeye Steak" and top["action"] == "urgent"
    assert top["negative_mentions"] == 3 and "overcooked" in top["complaints"]


def test_one_complaint_is_an_anecdote_not_a_flag(db_path):
    import menu_intelligence as mi
    rid = _rid(db_path, module_inventory=1)
    _dish(db_path, rid, "Ribeye Steak", 40, 12, sold=200)
    _dish(db_path, rid, "House Salad", 12, 3, sold=210)
    _dish(db_path, rid, "Soup", 8, 5, sold=10)
    _dish(db_path, rid, "Tart", 9, 6, sold=12)
    _review(db_path, rid, "only", 2, "negative", ["ribeye"], "cold")
    ribeye = next(d for d in mi.dish_scorecard(rid)["dishes"] if d["name"] == "Ribeye Steak")
    assert ribeye["action"] != "urgent"


def test_a_mention_matching_several_dishes_counts_for_none(db_path):
    """'chicken' against three chicken dishes is not evidence about any one."""
    import menu_intelligence as mi
    rid = _rid(db_path, module_inventory=1)
    _dish(db_path, rid, "Chicken Parm", 20, 6)
    _dish(db_path, rid, "Chicken Wings", 14, 5)
    for i in range(3):
        _review(db_path, rid, f"c{i}", 2, "negative", ["chicken"])
    card = mi.dish_scorecard(rid)
    assert all(d["negative_mentions"] == 0 for d in card["dishes"])


def test_reprice_restores_the_food_cost_percentage(db_path, monkeypatch):
    import menu_intelligence as mi
    rid = _rid(db_path, module_inventory=1)
    mid = _dish(db_path, rid, "Wings", 12.0, 3.30, qty=1.0, sold=300)   # plate cost now $3.30
    monkeypatch.setattr("inventory.load_inventory_for_restaurant", lambda r: ([{"item": "x"}], True))
    monkeypatch.setattr("inventory.compute_item_trends", lambda r, items: {})
    monkeypatch.setattr("inventory.build_price_watch", lambda t: [
        {"item": "Wings base", "kind": "trend", "change_pct": 10.0,
         "old_price": 3.00, "new_price": 3.30}])
    out = mi.reprice_suggestions(rid)
    s = out["suggestions"][0]
    assert s["dish"] == "Wings"
    assert s["increase_per_plate"] == 0.30
    assert s["food_cost_pct_before"] == 25.0          # 3.00 / 12
    assert s["suggested_price"] == 13.25              # 3.30 / 0.25 = 13.20 -> next quarter
    assert s["monthly_margin_lost"] == 90.0           # 0.30 x 300
    assert "assumes" in out["assumption"].lower()


def test_reprice_refuses_on_sample_inventory(db_path, monkeypatch):
    import menu_intelligence as mi
    monkeypatch.setattr("inventory.load_inventory_for_restaurant", lambda r: ([], False))
    assert mi.reprice_suggestions(_rid(db_path))["available"] is False


# ── demand ─────────────────────────────────────────────────────────────────

def _history(db_path, rid, weekday, values, before=None):
    """Seed sales for past occurrences of `weekday`."""
    before = before or date.today()
    d = before - timedelta(days=1)
    placed = 0
    while placed < len(values):
        if d.strftime("%A") == weekday:
            _sales_day(db_path, rid, d.isoformat(), values[placed])
            placed += 1
        d -= timedelta(days=1)


def test_yesterday_is_compared_with_its_own_weekday_excluding_itself(db_path):
    import demand
    rid = _rid(db_path)
    y = date.today() - timedelta(days=1)
    _history(db_path, rid, y.strftime("%A"), [3000, 3100, 2900], before=y)
    _sales_day(db_path, rid, y.isoformat(), 2000)
    out = demand.yesterday_vs_typical(rid)
    assert out["typical"] == 3000 and out["off"] and out["direction"] == "below"


def test_yesterday_with_no_sales_is_unknown(db_path):
    import demand
    assert demand.yesterday_vs_typical(_rid(db_path))["available"] is False


def test_a_forecast_on_too_few_days_is_withheld(db_path):
    import demand
    rid = _rid(db_path)
    today = date.today()
    _history(db_path, rid, today.strftime("%A"), [2000, 2100], before=today)
    assert demand.forecast_day(rid, today)["available"] is False


# ── loss detection ─────────────────────────────────────────────────────────

def test_loss_detection_says_so_when_the_pos_cannot_report(db_path):
    import loss_detection, pos

    def _cannot(*a, **k):
        raise pos.POSCapabilityError("toast does not report comps and voids yet")
    import pos as _pos
    _pos.fetch_loss_lines, orig = _cannot, _pos.fetch_loss_lines
    try:
        out = loss_detection.sync(_rid(db_path))
    finally:
        _pos.fetch_loss_lines = orig
    assert out["ok"] is False and "does not report" in out["reason"]


def _seed_loss(db_path, rid, day, kind, amount, events, by):
    conn = get_conn(db_path)
    conn.execute("INSERT INTO pos_loss_daily (restaurant_id, business_date, kind, amount, events, by_approver) "
                 "VALUES (?,?,?,?,?,?)", (rid, day, kind, amount, events, json.dumps(by)))
    conn.commit(); conn.close()


def _synced_baseline(db_path, rid, kind, weekly_amount, events):
    """Eight weeks as sync() writes them: a row for EVERY day asked about,
    the week's comps on one day and zeros on the rest."""
    for w in range(1, 9):
        for dd in range(7):
            day = _day(-7 - 7 * w + dd)
            amt, ev = (weekly_amount, events) if dd == 0 else (0, 0)
            _seed_loss(db_path, rid, day, kind, amt, ev, {"11": {"amount": amt, "events": ev}} if ev else {})


def test_a_comp_spike_needs_both_a_multiple_and_a_dollar_floor(db_path):
    import loss_detection as ld
    rid = _rid(db_path)
    _synced_baseline(db_path, rid, "comp", 50, 3)       # $50/week
    _seed_loss(db_path, rid, _day(-2), "comp", 260, 9,
               {"11": {"amount": 130, "events": 5}, "12": {"amount": 130, "events": 4}})
    sig = ld.signals(rid)
    comp = next(k for k in sig["kinds"] if k["kind"] == "comp")
    assert any(f["type"] == "spike" for f in comp["flags"])
    assert "not a finding of wrongdoing" in sig["note"]


def test_a_small_week_is_not_an_alarm_whatever_its_multiple(db_path):
    import loss_detection as ld
    rid = _rid(db_path)
    _synced_baseline(db_path, rid, "void", 10, 1)
    _seed_loss(db_path, rid, _day(-2), "void", 40, 5, {"11": {"amount": 40, "events": 5}})
    void = next(k for k in ld.signals(rid)["kinds"] if k["kind"] == "void")
    assert void["flags"] == []


def test_one_approver_concentration_is_flagged_with_an_alternative(db_path):
    import loss_detection as ld
    rid = _rid(db_path)
    _seed_loss(db_path, rid, _day(-2), "comp", 300, 10,
               {"11": {"amount": 250, "events": 8}, "12": {"amount": 50, "events": 2}})
    comp = next(k for k in ld.signals(rid)["kinds"] if k["kind"] == "comp")
    conc = [f for f in comp["flags"] if f["type"] == "concentration"]
    assert conc and conc[0]["approver"] == "11" and conc[0]["alternative"]


# ── issues ─────────────────────────────────────────────────────────────────

def test_routing_refuses_a_contact_who_never_consented_to_texts(db_path):
    """Admin-added contacts never carry consent. Texting them would be the
    exact thing notify.py's sms_consent_only filter exists to prevent."""
    import issues
    rid = _rid(db_path)
    cid = _contact(db_path, rid, consent=0)
    with pytest.raises(ValueError):
        issues.set_routing(rid, "manager", cid)


def test_routing_refuses_another_restaurants_contact(db_path):
    import issues
    a, b = _rid(db_path), _rid(db_path)
    theirs = _contact(db_path, b)
    with pytest.raises(ValueError):
        issues.set_routing(a, "manager", theirs)


def test_an_issue_texts_its_assignee_a_working_link(db_path, sms):
    import issues
    rid = _rid(db_path)
    issues.set_routing(rid, "manager", _contact(db_path, rid))
    issue, token = issues.create_issue(rid, "manual", "Walk-in fridge at 45F")
    assert sms and token in sms[0][1]
    assert issues.by_token(token)["id"] == issue["id"]
    assert issues.acknowledge(token)["status"] == "acknowledged"
    assert issues.resolve_by_token(token, "Tech fixed the compressor")["status"] == "resolved"


def test_the_same_review_never_opens_two_issues(db_path, sms):
    import issues
    rid = _rid(db_path)
    issues.set_routing(rid, "manager", _contact(db_path, rid))
    a, t1 = issues.create_issue(rid, "review", "1★", source_key="review:9")
    b, t2 = issues.create_issue(rid, "review", "1★", source_key="review:9")
    assert a["id"] == b["id"] and t2 is None and len(sms) == 1


def test_escalation_gives_the_regional_their_own_link_and_keeps_the_managers(db_path, sms):
    """The bug this design exists to avoid: with one token per issue,
    escalating replaced it and the local manager's link stopped working."""
    import issues
    rid = _rid(db_path)
    issues.set_routing(rid, "manager", _contact(db_path, rid, "Local", "+15555550101"))
    issues.set_routing(rid, "escalation", _contact(db_path, rid, "Regional", "+15555550102"),
                       escalate_after_minutes=30)
    issue, manager_token = issues.create_issue(rid, "manual", "Health inspector tomorrow")
    out = issues.tick(now=datetime.utcnow() + timedelta(minutes=45))
    assert out["escalated"] == 1
    assert sms[-1][0] == "+15555550102"
    assert issues.by_token(manager_token) is not None, "the manager's link must survive escalation"
    assert issues.tick(now=datetime.utcnow() + timedelta(minutes=90))["escalated"] == 0, "once only"


def test_an_acknowledged_issue_does_not_escalate(db_path, sms):
    import issues
    rid = _rid(db_path)
    issues.set_routing(rid, "manager", _contact(db_path, rid, "Local", "+15555550101"))
    issues.set_routing(rid, "escalation", _contact(db_path, rid, "Regional", "+15555550102"),
                       escalate_after_minutes=30)
    _issue, token = issues.create_issue(rid, "manual", "Short a cook tonight")
    issues.acknowledge(token)
    assert issues.tick(now=datetime.utcnow() + timedelta(minutes=45))["escalated"] == 0


def test_quiet_hours_hold_the_text_rather_than_drop_it(db_path, sms, monkeypatch):
    import issues
    rid = _rid(db_path)
    issues.set_routing(rid, "manager", _contact(db_path, rid))
    monkeypatch.setattr("models.is_in_quiet_hours", lambda *a, **k: True)
    issue, _ = issues.create_issue(rid, "manual", "Overnight: freezer alarm")
    assert sms == [] and issues.get_issue(rid, issue["id"])["notified_at"] is None
    monkeypatch.setattr("models.is_in_quiet_hours", lambda *a, **k: False)
    assert issues.tick()["held_sent"] == 1 and len(sms) == 1


def test_no_routing_means_reviews_open_no_issues(db_path, sms):
    """Opt-in by configuring who owns them — nothing starts texting phones
    on its own."""
    import issues
    rid = _rid(db_path)
    assert issues.open_from_reviews(rid) == [] and sms == []


def test_an_issue_cannot_be_read_or_closed_from_another_restaurant(db_path, sms):
    import issues
    a, b = _rid(db_path), _rid(db_path)
    issue, _ = issues.create_issue(a, "manual", "Mine", notify=False)
    assert issues.get_issue(b, issue["id"]) is None
    assert issues.resolve(b, issue["id"]) is None


def test_sparse_comp_days_do_not_inflate_the_baseline(db_path):
    """The bug this pins: counting only days that HAD comps turned eight weeks
    of $50 into a 'weekly' baseline of ~$350, and a real $260 spike vanished."""
    import loss_detection as ld
    rid = _rid(db_path)
    _synced_baseline(db_path, rid, "comp", 50, 3)
    comp = next(k for k in ld.signals(rid)["kinds"] if k["kind"] == "comp")
    assert comp["weekly_baseline"] == 50.0


def test_sync_writes_a_zero_row_for_every_day_it_asked_about(db_path, monkeypatch):
    import loss_detection as ld, pos
    rid = _rid(db_path)
    monkeypatch.setattr(pos, "fetch_loss_lines", lambda r, s, e: ([
        {"business_date": _day(-3), "kind": "comp", "amount": 20.0, "approver": "11"}], "rpower"))
    ld.sync(rid, days=7)
    conn = get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) AS n FROM pos_loss_daily WHERE restaurant_id=?", (rid,)).fetchone()["n"]
    conn.close()
    assert n == 7 * len(ld.KINDS), "asked about 7 days x 3 kinds — every one recorded, zeros included"
