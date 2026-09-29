"""positive_volume: positive learning was starved — one tracker per metric
family at a time, trackers only on Track or Done, and a do-nothing
comparison on only two surfaces (memory audit 9/29/26, M4). Finer slices
that do not overlap run side by side; a change that was made starts its own
tracker; every surface carries the number it would be measured on; and a
kind below its floor borrows its lever's results."""
import json
from datetime import date, datetime, timedelta

import pytest

import metrics
import models
import outcomes
import rec_ledger
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


TODAY = date.today()


def _rid(name="Volume Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


def _exec(sql, args=()):
    conn = models.get_conn()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _days(rid, weeks=16, labor_share=0.3, sales=1000.0):
    start = TODAY - timedelta(days=weeks * 7)
    for i in range(weeks * 7):
        d = start + timedelta(days=i)
        share = labor_share + (0.1 if d.strftime("%A") == "Friday" else 0.0)
        _exec("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_cost) VALUES (?,?,?,?,?)",
              (rid, d.isoformat(), d.strftime("%A"), sales, sales * share))


# ── the finer grains ─────────────────────────────────────────────────────────

def test_one_weekdays_labor_is_its_own_number():
    rid = _rid()
    _days(rid)
    s, e = (TODAY - timedelta(days=56)).isoformat(), (TODAY - timedelta(days=1)).isoformat()
    assert metrics.measure(rid, "labor_pct_day:Friday", s, e)[0] == 40.0
    assert metrics.measure(rid, "labor_pct_day:tuesday", s, e)[0] == 30.0
    assert metrics.known("labor_pct_day:Friday") and not metrics.known("labor_pct_day:Funday")
    assert metrics.normalize("labor_pct_day: friday") == "labor_pct_day:Friday"
    assert metrics.describe("labor_pct_day:Friday")["label"] == "Labor % on Friday"
    # A point of Friday's labor is worth a point of a Friday's sales, 4.33 times a month.
    assert metrics.monthly_dollars(rid, "labor_pct_day:Friday", -1.0) == pytest.approx(1000 * 0.01 * 52 / 12, abs=0.1)


def test_an_items_waste_is_a_measured_zero_when_waste_was_logged_but_not_it():
    rid = _rid()
    salmon = _exec("INSERT INTO ingredients (restaurant_id, name, unit_cost) VALUES (?,?,?)", (rid, "Salmon", 10.0))
    kale = _exec("INSERT INTO ingredients (restaurant_id, name, unit_cost) VALUES (?,?,?)", (rid, "Kale", 2.0))
    s, e = (TODAY - timedelta(days=13)).isoformat(), TODAY.isoformat()
    assert metrics.measure(rid, "item_waste:Salmon", s, e)[0] is None       # nothing logged: unknown
    _exec("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source) "
          "VALUES (?,?,?,?,?,?)", (rid, kale, "waste", 3, TODAY.isoformat(), "count"))
    assert metrics.measure(rid, "item_waste:Salmon", s, e)[0] == 0.0          # logged, none of it: $0
    _exec("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source) "
          "VALUES (?,?,?,?,?,?)", (rid, salmon, "waste", 2, TODAY.isoformat(), "count"))
    assert metrics.measure(rid, "item_waste:salmon", s, e)[0] == 10.0         # $20 over 2 weeks
    assert metrics.measure(rid, "item_waste:Tofu", s, e)[0] is None


def test_a_daypart_is_measured_only_on_a_measured_sales_split():
    rid = _rid()
    fri = TODAY - timedelta(days=(TODAY.weekday() - 4) % 7 or 7)
    hid = _exec("INSERT INTO schedule_history (restaurant_id, week_start) VALUES (?,?)", (rid, fri.isoformat()))
    for w in range(3):
        d = fri - timedelta(weeks=w)
        _exec("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_cost) VALUES (?,?,?,?,?)",
              (rid, d.isoformat(), "Friday", 1000.0, 300.0))
        _exec("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours) VALUES (?,?,?,?,?)",
              (rid, hid, d.isoformat(), "morning", 10.0))
        _exec("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours) VALUES (?,?,?,?,?)",
              (rid, hid, d.isoformat(), "night", 20.0))
    s, e = (fri - timedelta(days=30)).isoformat(), fri.isoformat()
    v, why = metrics.measure(rid, "labor_pct_part:Friday night", s, e)
    assert v is None and "never used" in why, "the assumed 40/60 split is never read"
    for w in range(3):
        d = fri - timedelta(weeks=w)
        _exec("INSERT INTO pos_intraday (restaurant_id, business_date, captured_hour, weekday, net_sales) "
              "VALUES (?,?,?,?,?)", (rid, d.isoformat(), 15, "Friday", 250.0))
        _exec("INSERT INTO pos_intraday (restaurant_id, business_date, captured_hour, weekday, net_sales) "
              "VALUES (?,?,?,?,?)", (rid, d.isoformat(), 22, "Friday", 1000.0))
    v, why = metrics.measure(rid, "labor_pct_part:friday NIGHT", s, e)
    # Night: 750 of sales, 200 of the 300 labor (two thirds of the hours).
    assert v == pytest.approx(26.7, abs=0.1) and "labor split by the published schedule's hours" in why
    assert metrics.monthly_dollars(rid, "labor_pct_part:Friday night", -1.0) is None


def test_slices_that_do_not_overlap_do_not_collide():
    c = metrics.slices_collide
    assert c("labor_pct", "labor_pct_day:Friday") and c("overtime_hours", "labor_pct_day:Friday")
    assert not c("labor_pct_day:Tuesday", "labor_pct_day:Friday")
    assert c("labor_pct_day:Friday", "labor_pct_part:Friday night")
    assert not c("labor_pct_part:Friday night", "labor_pct_part:Friday morning")
    assert not c("item_waste:Salmon", "item_waste:Kale") and c("weekly_waste", "item_waste:Salmon")
    assert not c("complaints:service", "complaints:food_quality") and c("avg_rating", "complaints:service")
    assert not c("weekday_sales:Tuesday", "weekday_sales:Friday") and c("sales", "weekday_sales:Friday")
    assert not c("labor_pct_day:Friday", "weekday_sales:Friday"), "different families"


# ── trackers side by side ────────────────────────────────────────────────────

def test_tuesday_and_friday_labor_trackers_run_side_by_side():
    rid = _rid()
    _days(rid)
    a = outcomes.start(rid, "recommendation", "trim_day:Tuesday", "Trim Tuesday", "labor_pct_day:Tuesday",
                       gate="family")
    b = outcomes.start(rid, "recommendation", "trim_day:Friday", "Trim Friday", "labor_pct_day:Friday",
                       gate="family")
    assert a["ok"] and b["ok"], "two weekdays are two numbers"
    c = outcomes.start(rid, "observed", "observed:schedule_published:x", "Published", "labor_pct", gate="family")
    assert c["ok"] is False and c["tracker_refused"]["code"] == "in_flight", "the whole week overlaps both"
    row = outcomes.get_outcome(a["outcome"]["id"])
    conc = outcomes.find_concurrent(row, row["started_on"], row["evaluate_on"])
    assert not any(x["kind"] == "tracker" for x in conc), "Friday's tracker is no change on Tuesday's number"


# ── a change that was made starts its tracker ────────────────────────────────

def test_an_implemented_recommendation_starts_the_tracker_its_number_carries():
    rid = _rid()
    _days(rid)
    rec_ledger.present_many(rid, [{"key": "trim_day:Tuesday", "module": "labor", "title": "Trim Tuesday",
                                   "expected_metric": "labor_pct_day:Tuesday"}], "home")
    assert rec_ledger.implemented(rid, "trim_day:Tuesday", "schedule", source_ref="edit:1") == 1
    conn = models.get_conn()
    try:
        ep = conn.execute("SELECT tracker_id FROM rec_instances WHERE restaurant_id=? AND key=?",
                          (rid, "trim_day:Tuesday")).fetchone()
        tr = conn.execute("SELECT metric, source FROM recommendation_outcomes WHERE id=?", (ep["tracker_id"],)).fetchone()
    finally:
        conn.close()
    assert ep["tracker_id"] and tr["metric"] == "labor_pct_day:Tuesday"


def test_kinds_with_their_own_tracker_and_numberless_advice_start_nothing():
    rid = _rid()
    rec_ledger.present_many(rid, [{"key": "reprice:Burger", "module": "food", "title": "Reprice",
                                   "expected_metric": "food_cost_pct"},
                                  {"key": "first_post", "module": "marketing", "title": "Post"}], "home")
    assert outcomes.autostart_implemented(rid, "reprice:Burger") is None
    assert outcomes.autostart_implemented(rid, "first_post") is None


def test_the_nightly_pass_catches_up_changes_made_inside_a_transaction():
    import learning_memory
    rid = _rid()
    _days(rid)
    rec_ledger.present_many(rid, [{"key": "trim_day:Friday", "module": "labor", "title": "Trim Friday",
                                   "expected_metric": "labor_pct_day:Friday"}], "home")
    conn = models.get_conn()
    try:
        rec_ledger.implemented_on(conn, rid, "trim_day:Friday", "schedule", source_ref="save:1")
        conn.commit()
    finally:
        conn.close()
    out = learning_memory.nightly(rid)
    assert out["steps"]["implemented_trackers"]["started"] == 1


# ── the number every surface carries ─────────────────────────────────────────

def test_each_surface_carries_the_finest_honest_number():
    rid = _rid()
    _exec("INSERT INTO ingredients (restaurant_id, name, unit_cost) VALUES (?,?,?)", (rid, "Chicken Breast", 4.0))
    f = outcomes.expected_metric_for
    assert f("insight_labor:abc", "Cut a server from Tuesday dinner", module="labor") == "labor_pct_day:Tuesday"
    assert f("insight_labor:abc", "Tighten the whole schedule", module="labor") == "labor_pct"
    assert f("insight_food:abc", "Cut the chicken breast order by a case", module="food",
             restaurant_id=rid) == "item_waste:chicken breast"
    assert f("insight_food:abc", "Raise the burger price", module="food", restaurant_id=rid) == "food_cost_pct"
    assert f("insight_review:x", "Coach servers on check-backs", module="reviews") in ("avg_rating",) or \
        f("insight_review:x", "Coach servers on check-backs", module="reviews").startswith("complaints:")
    assert f("insight_marketing:abc", "Post the patio on Thursday", module="marketing") is None
    assert f("dsr_action:control_hours:labor", "Send a server home early") == "labor_pct"
    assert f("dsr_action:reorder:food/salmon", "Order salmon") is None
    assert f("slow_day:Tuesday", "Fill Tuesday") == "weekday_sales:Tuesday"
    import schedule_engine
    assert schedule_engine.schedule_item_metric({"kind": "hours", "key": "k", "text": "Trim about 6h from "
                                                 "Tuesday night"}) == "labor_pct_day:Tuesday"
    assert schedule_engine.schedule_item_metric({"kind": "coverage", "text": "Fill the gap Friday"}) is None
    import marketing_opportunities as mo
    assert mo.card_expected_metric({"key": "slow_day:Monday", "kind": "slow_day"}) == "weekday_sales:Monday"
    assert mo.card_expected_metric({"key": "dish_promote:Burger", "kind": "dish_promote"}) is None


def test_a_nightly_report_action_carries_tomorrows_slice():
    import types
    from dsr import narrative
    rid = _rid()
    _exec("INSERT INTO ingredients (restaurant_id, name, unit_cost) VALUES (?,?,?)", (rid, "Salmon", 10.0))
    ctx = types.SimpleNamespace(restaurant_id=rid, business_date=date(2026, 9, 24), db_path=None)   # a Thursday
    m = narrative.action_expected_metric
    assert m({"kind": "control_hours", "urgency": "before_service", "key": "dsr_action:control_hours:labor"},
             ctx) == "labor_pct_day:Friday"
    assert m({"kind": "control_hours", "urgency": "next_schedule", "key": "dsr_action:control_hours:labor"},
             ctx) == "labor_pct"
    assert m({"kind": "reduce_waste", "key": "dsr_action:reduce_waste:food/salmon"}, ctx) == "item_waste:salmon"
    assert m({"kind": "reorder", "key": "dsr_action:reorder:food/salmon"}, ctx) is None
    items = narrative.ledger_items([{"key": "dsr_action:control_hours:labor", "kind": "control_hours",
                                     "text": "t", "cites": ["labor.pct"], "expected_metric": "labor_pct_day:Friday"}])
    assert items[0]["expected_metric"] == "labor_pct_day:Friday"
    # Track then starts on the finer slice, the kind still deciding there is one.
    rec_ledger.present_many(rid, items, "dsr")
    assert outcomes.metric_for_rec(rid, "dsr_action:control_hours:labor") == ("labor_pct_day:Friday", True)


def test_advice_left_unanswered_gets_its_do_nothing_comparison_from_its_kind():
    rid = _rid()
    _days(rid)
    rec_ledger.present_many(rid, [{"key": "slow_day:Tuesday", "module": "marketing", "title": "Fill Tuesday"}],
                            "home")
    rec_ledger.record(rid, "slow_day:Tuesday", "dismissed", surface="home", meta={"kind": "hide"})
    assert outcomes.observe_untaken(rid) == 1


# ── a kind below its floor borrows its lever ─────────────────────────────────

def test_a_kind_below_its_floor_reads_its_levers_pooled_record():
    import confidence_engine as ce
    import rec_learning
    rid = _rid()
    now = datetime.utcnow()
    n = [0]

    def ep(key, verdict):
        n[0] += 1
        created = (now - timedelta(days=30 + n[0])).strftime("%Y-%m-%d %H:%M:%S")
        start = TODAY - timedelta(days=200 - 10 * n[0])
        tid = _exec("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, "
                    "baseline_value, started_on, evaluate_on, status, verdict, after_start, after_end, concurrent) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, "recommendation", key, "t", f"labor_pct_day:{['Monday','Tuesday','Wednesday'][n[0] % 3]}",
                     30.0, start.isoformat(), (start + timedelta(days=7)).isoformat(), "evaluated", verdict,
                     start.isoformat(), (start + timedelta(days=6)).isoformat(), "[]"))
        _exec("INSERT INTO rec_instances (rec_id, restaurant_id, key, module, kind, title, status, tags, tracker_id, "
              "created_at, last_event_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (f"p{n[0]}", rid, key, "labor", rec_ledger.kind_of(key), key, "completed",
               json.dumps(rec_ledger.tags_for(key)), tid, created, created))
        _exec("INSERT INTO rec_events (rec_id, restaurant_id, key, event, dedupe, at) VALUES (?,?,?,?,?,?)",
              (f"p{n[0]}", rid, key, "shown", f"s{n[0]}", created))
    ep("trim_day:Tuesday", "improved")
    ep("trim_day:Friday", "improved")
    for i in range(4):
        ep(f"dsr_action:adjust_staffing:labor/x{i}", "improved" if i < 3 else "no_clear_change")
    rec = rec_learning.kind_record(rid, "trim_day")
    assert rec["measured"] == 2 and rec["pooled"]["measured"] == 6 and rec["pooled"]["topic"] == "staffing"
    acc = ce.accuracy(rec)
    assert acc["source"] == "pooled" and acc["pct"] is not None
    assert "across your staffing advice here" in acc["basis"] and "this kind alone: 2 of 2" in acc["basis"]
