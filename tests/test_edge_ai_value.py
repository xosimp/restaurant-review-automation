"""Edge cases in "value delivered": one piece of work is one win.

CLAUDE.md's rule for every value figure is that it "counts distinct work,
never rows". Two trackers on the same metric whose windows overlap measure
the same before/after move, so their dollars are one result, not two
(AI-18). Ask Cavnar's track_outcome tool mints trackers from a title the
model writes, so two conversations naming the same change differently are
the easiest way to get that pair (AI-18, second half).

The passing tests pin the other side of the line: separate work on the
same metric at different times, and overlapping work on different metrics,
are both real and both count.
"""
import json
from datetime import date

import pytest

import models
# Module scope on purpose: outcomes and metrics bind `from models import
# get_conn` at import (CLAUDE.md "Bound imports").
import ask_cavnar_tools as tools
import metrics
import outcomes
import value_delivered
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, outcomes, metrics, tools):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)


def _rid(db_path, **kw):
    kw.setdefault("module_labor", 1)
    kw.setdefault("module_inventory", 1)
    return create_restaurant(Restaurant(name="Value Co", owner_email="v@x.test", **kw), db_path=db_path)


def _win(db_path, rid, title, metric, dollars, started_on, evaluate_on, source="ask", verdict="improved"):
    """An evaluated tracker exactly as outcomes.evaluate leaves it: the
    baseline window ends the day before it started, the after window runs
    from the start to the day before evaluation."""
    s, e = date.fromisoformat(started_on), date.fromisoformat(evaluate_on)
    window = (e - s).days
    base_end = date.fromordinal(s.toordinal() - 1)
    base_start = date.fromordinal(base_end.toordinal() - window + 1)
    after_end = date.fromordinal(e.toordinal() - 1)
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, "
        "baseline_value, baseline_start, baseline_end, started_on, evaluate_on, after_value, "
        "after_start, after_end, verdict, delta, dollars_monthly, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'evaluated')",
        (rid, source, "%s:%s" % (source, title.lower()), title, metric, 32.0,
         base_start.isoformat(), base_end.isoformat(), started_on, evaluate_on, 29.0,
         started_on, after_end.isoformat(), verdict, -3.0, dollars))
    conn.commit()
    conn.close()


def _in_flight(db_path, rid, metric):
    conn = get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM recommendation_outcomes WHERE restaurant_id=? AND metric=? "
                     "AND status='tracking'", (rid, metric)).fetchone()[0]
    conn.close()
    return n


# ── overlapping trackers on the same metric ────────────────────────────────

def test_two_overlapping_labor_wins_count_once_in_total_value(db_path):
    """Both windows cover June 10-28; the same drop in labor % is the whole
    of both results. The larger reading stands, the other adds nothing."""
    rid = _rid(db_path)
    _win(db_path, rid, "Cut one server on Monday lunch", "labor_pct", 400.0, "2026-06-01", "2026-06-29")
    _win(db_path, rid, "Trim Monday lunch staffing", "labor_pct", 300.0, "2026-06-10", "2026-07-08")
    v = outcomes.total_value(rid, db_path=db_path)
    assert v["monthly"] == 400.0
    assert v["annual"] == 4800.0
    assert v["by_module"].get("labor") == 400.0


def test_two_overlapping_labor_wins_count_once_in_the_value_delivered_headline(db_path):
    rid = _rid(db_path)
    _win(db_path, rid, "Cut one server on Monday lunch", "labor_pct", 400.0, "2026-06-01", "2026-06-29")
    _win(db_path, rid, "Trim Monday lunch staffing", "labor_pct", 300.0, "2026-06-10", "2026-07-08")
    assert value_delivered.delivered(rid, db_path=db_path)["monthly"] == 400.0
    assert value_delivered.compute_total_value_delivered(rid, db_path=db_path) == 400


def test_two_labor_wins_in_separate_windows_are_distinct_work_and_both_count(db_path):
    """The fix for the overlap must not merge a June change with a
    September one just because they moved the same number."""
    rid = _rid(db_path)
    _win(db_path, rid, "Cut one server on Monday lunch", "labor_pct", 400.0, "2026-06-01", "2026-06-29")
    _win(db_path, rid, "Moved the prep shift later", "labor_pct", 300.0, "2026-08-01", "2026-08-29")
    v = outcomes.total_value(rid, db_path=db_path)
    assert v["monthly"] == 700.0 and v["wins"] == 2


def test_overlapping_wins_on_different_metrics_both_count(db_path):
    """Labor and food cost can both improve in the same weeks — two moves."""
    rid = _rid(db_path)
    _win(db_path, rid, "Cut one server on Monday lunch", "labor_pct", 400.0, "2026-06-01", "2026-06-29")
    _win(db_path, rid, "Switched mozzarella supplier", "food_cost_pct", 300.0, "2026-06-10", "2026-07-08")
    v = outcomes.total_value(rid, db_path=db_path)
    assert v["monthly"] == 700.0
    assert v["by_module"] == {"labor": 400.0, "inventory": 300.0}


def test_an_overlapping_tracker_that_did_not_improve_adds_nothing(db_path):
    """Already true today, and the reason the overlap bug needs two WINS."""
    rid = _rid(db_path)
    _win(db_path, rid, "Cut one server on Monday lunch", "labor_pct", 400.0, "2026-06-01", "2026-06-29")
    _win(db_path, rid, "Trim Monday lunch staffing", "labor_pct", 300.0, "2026-06-10", "2026-07-08",
         verdict="no_clear_change")
    assert outcomes.total_value(rid, db_path=db_path)["monthly"] == 400.0


# ── Ask's track_outcome: the model's title is not the identity of the work ──

def _ask_track(rid, db_path, **tool_input):
    """Through the dispatcher Ask itself uses for action tools."""
    restaurant = models.get_restaurant(rid, db_path=db_path)
    return json.loads(tools.run_read_tool("track_outcome", rid, tool_input, restaurant=restaurant))


def test_ask_naming_the_same_change_two_ways_keeps_one_labor_tracker_in_flight(db_path):
    rid = _rid(db_path)
    first = _ask_track(rid, db_path, title="Cut one server on Monday lunch", metric="labor_pct")
    assert first.get("ok"), first
    _ask_track(rid, db_path, title="Trim Monday lunch staffing by one", metric="labor_pct")
    assert _in_flight(db_path, rid, "labor_pct") == 1


def test_ask_passing_its_own_source_key_does_not_mint_a_second_tracker_on_the_metric(db_path):
    rid = _rid(db_path)
    _ask_track(rid, db_path, title="Cut one server on Monday lunch", metric="labor_pct", source_key="conv-1")
    _ask_track(rid, db_path, title="Cut one server on Monday lunch", metric="labor_pct", source_key="conv-2")
    assert _in_flight(db_path, rid, "labor_pct") == 1


def test_ask_does_not_start_a_labor_tracker_beside_one_a_published_schedule_already_started(db_path):
    rid = _rid(db_path)
    observed = outcomes.observe(rid, "schedule_published", db_path=db_path, today=date.today())
    assert observed and observed["metric"] == "labor_pct"
    _ask_track(rid, db_path, title="Publish a leaner schedule", metric="labor_pct")
    assert _in_flight(db_path, rid, "labor_pct") == 1


def test_ask_repeating_the_same_title_in_different_case_keeps_one_tracker(db_path):
    """The dedupe that exists today: the lowercased title is the key."""
    rid = _rid(db_path)
    a = _ask_track(rid, db_path, title="Cut one server on Monday lunch", metric="labor_pct")
    b = _ask_track(rid, db_path, title="CUT ONE SERVER ON MONDAY LUNCH", metric="labor_pct")
    assert a["outcome"]["id"] == b["outcome"]["id"]
    assert _in_flight(db_path, rid, "labor_pct") == 1


def test_ask_can_track_two_different_metrics_at_once(db_path):
    """One tracker per METRIC, not one per restaurant."""
    rid = _rid(db_path)
    assert _ask_track(rid, db_path, title="Cut one server on Monday lunch", metric="labor_pct").get("ok")
    assert _ask_track(rid, db_path, title="Switched mozzarella supplier", metric="food_cost_pct").get("ok")
    assert _in_flight(db_path, rid, "labor_pct") == 1
    assert _in_flight(db_path, rid, "food_cost_pct") == 1


def test_observe_already_refuses_a_second_tracker_on_a_metric_in_flight(db_path):
    """The rule track_outcome should share: observe() has it."""
    rid = _rid(db_path)
    assert outcomes.observe(rid, "schedule_published", db_path=db_path, today=date(2026, 9, 21))
    assert outcomes.observe(rid, "alert_labor_over", db_path=db_path, today=date(2026, 9, 21)) is None
    assert _in_flight(db_path, rid, "labor_pct") == 1
