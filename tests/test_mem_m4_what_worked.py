"""what_worked: measured results only reordered cards; no generator learned
what worked for this restaurant (memory audit 9/29/26, M4). The record by
kind and tag reaches every model call through memory_context, is kept by
month forever, weighs the food cost drivers, and holds back a schedule cut
on a weekday where cuts measured worse here."""
import json
from datetime import date, datetime, timedelta

import pytest

import models
import rec_learning
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(name="Worked Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


_n = [0]


def _episode(rid, key, module, status="completed", verdict=None, title=None, days_ago=40, tags=None):
    """One shown episode; with a verdict, an evaluated tracker on its own
    non-overlapping window."""
    import rec_ledger
    _n[0] += 1
    rec_id = f"r{_n[0]}"
    created = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn = models.get_conn()
    try:
        tid = None
        if verdict:
            start = date.today() - timedelta(days=days_ago + 30 * _n[0])
            cur = conn.execute(
                "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, baseline_value, "
                "started_on, evaluate_on, status, verdict, after_start, after_end, concurrent) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, "recommendation", key, "t", f"weekday_sales:Monday", 10.0, start.isoformat(),
                 (start + timedelta(days=7)).isoformat(), "evaluated", verdict, start.isoformat(),
                 (start + timedelta(days=6)).isoformat(), "[]"))
            tid = cur.lastrowid
        kind = rec_ledger.kind_of(key)
        conn.execute("INSERT INTO rec_instances (rec_id, restaurant_id, key, module, kind, title, status, tags, "
                     "tracker_id, created_at, last_event_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (rec_id, rid, key, module, kind, title or key, status,
                      json.dumps(tags if tags is not None else rec_ledger.tags_for(key, module, kind)), tid,
                      created, created, created))
        conn.execute("INSERT INTO rec_events (rec_id, restaurant_id, key, event, surface, dedupe, at) "
                     "VALUES (?,?,?,?,?,?,?)", (rec_id, rid, key, "shown", "home", f"shown:{rec_id}", created))
        conn.commit()
    finally:
        conn.close()


def _labor_record(rid):
    for v in ("improved", "improved", "improved", "improved", "no_clear_change"):
        _episode(rid, "trim_day:Tuesday", "labor", verdict=v)
    for _ in range(4):
        _episode(rid, "post_this_week", "marketing", status="expired")


def test_the_record_counts_taken_measured_and_ignored_by_kind_and_tag():
    rid = _rid()
    _labor_record(rid)
    rec = rec_learning.what_worked(rid)
    k = rec["kinds"]["trim_day"]
    assert (k["taken"], k["measured"], k["improved"], k["module"]) == (5, 5, 4, "labor")
    assert rec["tags"]["day:tuesday"]["measured"] == 5
    assert rec["kinds"]["post_this_week"]["ignored"] == 4 and rec["kinds"]["post_this_week"]["taken"] == 0


def test_the_provider_serves_the_surfaces_own_levers_with_measured_counts():
    import memory_context
    rid = _rid()
    _labor_record(rid)
    req = memory_context.MemoryRequest(rid, "labor_read", subjects=("labor:day:tuesday",))
    lines = rec_learning.what_worked_lines(req)
    text = "\n".join(ln["text"] for ln in lines)
    assert "improved 4 of 5 measured results (before and after, not proof)" in text
    assert "post this week" not in text, "marketing advice is not the labor read's"
    assert all(ln["trusted"] for ln in lines)
    assert lines[0]["subject"] in ("day:tuesday", "trim_day"), "the subject in play leads"
    mk = rec_learning.what_worked_lines(memory_context.MemoryRequest(rid, "marketing"))
    assert any("ignored 4 times and never taken" in ln["text"] for ln in mk)
    block = memory_context.memory_context(rid, "labor_read")
    import memory_context
    assert memory_context.SECTION_TITLES["what_worked"] + ":" in block.text


def test_fewer_than_the_floor_is_never_a_rate():
    rid = _rid()
    _episode(rid, "trim_day:Friday", "labor", verdict="improved")
    lines = rec_learning.what_worked_lines(type("R", (), {"restaurant_id": rid, "surface": "labor_read",
                                                          "subjects": (), "db_path": None})())
    text = "\n".join(ln["text"] for ln in lines)
    assert "1 measured so far — too few to say" in text and "improved 1 of 1" not in text


def test_the_nightly_snapshot_is_kept_by_month_and_read_first():
    rid = _rid()
    _labor_record(rid)
    n = rec_learning.snapshot_what_worked(rid)
    assert n > 0
    conn = models.get_conn()
    try:
        conn.execute("INSERT INTO rec_learning_summaries (restaurant_id, month, scope, name, taken, measured, improved) "
                     "VALUES (?,?,?,?,?,?,?)", (rid, "2025-01", "kind", "trim_day", 1, 1, 1))
        conn.commit()
    finally:
        conn.close()
    rec_learning.snapshot_what_worked(rid)
    conn = models.get_conn()
    try:
        months = [r[0] for r in conn.execute("SELECT DISTINCT month FROM rec_learning_summaries WHERE restaurant_id=? "
                                             "ORDER BY month", (rid,)).fetchall()]
    finally:
        conn.close()
    assert months[0] == "2025-01" and len(months) == 2, "a past month is never rewritten"
    stored = rec_learning._stored_what_worked(rid)
    assert stored["kinds"]["trim_day"]["measured"] == 5
    import learning_memory
    out = learning_memory.nightly(rid)
    assert out["steps"]["what_worked"]["written"] > 0


def test_the_food_drivers_are_weighed_by_what_that_kind_of_fix_measured_here(monkeypatch):
    import food_cost_intelligence as fci

    class Learned:
        def weight(self, key, kind=None, tags=None):
            return (1.25, ["waste fixes worked here"]) if "topic:waste" in (tags or []) else (0.75, ["price moves did not"])
    monkeypatch.setattr(rec_learning, "effectiveness", lambda rid, db_path=None: Learned())
    drivers = [{"kind": "price", "label": "Beef price up 9%", "item": "Beef", "dollars_monthly": 200.0,
                "confidence": "high", "difficulty": "low"},
               {"kind": "waste", "label": "Salmon waste above tolerance", "item": "Salmon", "dollars_monthly": 180.0,
                "confidence": "high", "difficulty": "low"}]
    fci._learned_driver_weights(1, drivers)
    assert drivers[1]["learned"]["weight"] == 1.25 and drivers[0]["learned"]["weight"] == 0.75
    import inspect
    assert '(d.get("learned") or {}).get("weight")' in inspect.getsource(fci.cost_drivers)


def test_a_cut_on_a_day_whose_cuts_measured_worse_pays_for_it():
    import schedule_optimizer as opt
    rid = _rid()
    for v in ("worsened", "worsened", "improved"):
        _episode(rid, "trim_day:Friday", "labor", verdict=v, title="Trim Friday dinner by one server")
    _episode(rid, "coverage:Monday", "schedule", verdict="worsened", title="Add a Monday opener")
    levers = rec_learning.worsened_levers(rid)
    assert levers == {"friday": {"worsened": 2, "measured": 3, "label": "staffing cuts on Fridays"}}
    worse = opt.learned_worse_days({"learned_worse": levers})
    fri = "2026-10-02"                                           # a Friday
    before = [{"date": fri, "scheduled_hours": "6", "shift_start": "5:00pm", "shift_end": "11:00pm"}] * 2
    pen, why = opt.learned_penalty(worse, before, before[:1])
    assert pen == 1.0 and "staffing cuts on Fridays measured worse here 2 times" in why
    assert opt.learned_penalty(worse, before, before) == (0.0, None)          # no cut, no cost
    mon = [{"date": "2026-10-05", "scheduled_hours": "6"}] * 2
    assert opt.learned_penalty(worse, mon, mon[:1]) == (0.0, None)            # another day
    import schedule_engine
    assert schedule_engine.learned_worse_levers(rid) == levers
