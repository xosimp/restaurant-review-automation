"""Memory fix round, integration wave (9/29/26): learning eligibility across
the workstreams' readers.

  #20  a converted demo's demo era (before models.learning_since) teaches no
       cross-restaurant reader: the pooled recommendation rows (scoring,
       the confidence log, predict), the pooled feature weeks (bands,
       patterns, trends), the A/B readout — and a test or internal account
       (models.learning_eligible) is out of the pooled feature reads too.
  #21  a converted demo's seeded days (source 'seed') are no final day for
       any of its own learners (canonical_facts.FINAL_SQL); a demo keeps them.
  #22  a converted demo's own recommendation model and measured record start
       at its learning_since (rec_learning.effectiveness / kind_record).
"""
import inspect
import json
import sys
from datetime import date, datetime, timedelta

import pytest

import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    from intelligence import jobs
    jobs.invalidate_excluded()
    models._internal_homes_cache.clear()
    yield
    jobs.invalidate_excluded()


def _ago(days):
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _rid(name, **cols):
    """create_restaurant stores the profile; learning_since, is_demo and
    billing are set the way the admin and the conversion set them."""
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{abs(hash(name)) % 10**6}@x.test"))
    for k, v in cols.items():
        _x(f"UPDATE restaurants SET {k}=? WHERE id=?", (v, rid))
    from intelligence import jobs
    jobs.invalidate_excluded()
    models._internal_homes_cache.clear()
    return rid


def _x(sql, args=()):
    c = models.get_conn()
    try:
        c.execute("PRAGMA foreign_keys=OFF")
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


# ── #20 the cross-restaurant readers ─────────────────────────────────────────

def test_pooled_recommendation_rows_leave_a_converted_demos_demo_era_out():
    from intelligence import jobs, scoring
    conv = _rid("Converted Co", learning_since=_ago(30))
    real = _rid("Bella's Bistro")
    rows = [{"restaurant_id": conv, "event_at": (date.today() - timedelta(days=60)).isoformat(), "k": "before"},
            {"restaurant_id": conv, "event_at": date.today().isoformat(), "k": "after"},
            {"restaurant_id": real, "event_at": (date.today() - timedelta(days=60)).isoformat(), "k": "real"},
            {"restaurant_id": -7, "event_at": date.today().isoformat(), "k": "tombstoned"}]

    class Row(dict):
        def keys(self):
            return list(super().keys())
    kept = {r["k"] for r in scoring._eligible_rows([Row(r) for r in rows], None)}
    assert kept == {"after", "real", "tombstoned"}
    assert jobs.before_learning(conv, (date.today() - timedelta(days=60)).isoformat(), jobs.learning_since_by_id())
    assert not jobs.before_learning(real, "2020-01-01", jobs.learning_since_by_id())
    # Every pooled reader of intel_rec_events selects the stamp the rule reads.
    for fn in (scoring.kind_stats, scoring.rank_kinds, scoring.platform_totals):
        assert "event_at" in inspect.getsource(fn), fn.__name__
    assert "before_learning" in inspect.getsource(jobs.log_confidence)
    from intelligence import predict
    assert "before_learning" in inspect.getsource(predict._taken)


def _features(rid, week, value=1.0):
    from intelligence import features
    _x("INSERT INTO intel_features (restaurant_id, week, features_json, completeness, version) VALUES (?,?,?,?,?)",
       (rid, week, json.dumps({"labor_pct_28d": value}), 1.0, features.FEATURES_VERSION))


def test_pooled_feature_weeks_leave_out_demo_eras_and_every_account_that_may_not_teach():
    from intelligence import features
    conv = _rid("Converted Co", learning_since=_ago(21))
    real = _rid("Bella's Bistro")
    test = _rid("Preview Test")
    ours = _rid("Our Kitchen", billing_status="internal")
    old_week = features.iso_week(date.today() - timedelta(weeks=5))
    new_week = features.iso_week(date.today())
    for rid in (conv, real, test, ours):
        _features(rid, old_week)
        _features(rid, new_week)
    weekly = features.weekly_by_restaurant(weeks=8)
    assert set(weekly[old_week]) == {real}, "a demo-era week and an ineligible account teach no pooled figure"
    assert set(weekly[new_week]) == {real, conv}
    latest = features.latest_by_restaurant()
    assert set(latest) == {real, conv}
    # Each restaurant's own side of a comparison (the peer ledger) is its own.
    assert {conv, real} <= set(features.latest_by_restaurant(pooled=False))


def test_the_ab_readout_reads_no_week_before_learning_since():
    import schedule_experiments as sx
    conv = _rid("Converted Co", learning_since=_ago(30))
    since = sx._learning_since(None)
    assert sx._before_learning(conv, (date.today() - timedelta(days=60)).isoformat(), since)
    assert not sx._before_learning(conv, date.today().isoformat(), since)
    src = inspect.getsource(sx.readout)
    assert "_before_learning(r[\"restaurant_id\"], r[\"week_start\"], since)" in src


# ── #21 the seed filter in the final-day rule ───────────────────────────────

def _day(rid, d, sales, source=None):
    _x("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_cost, source) "
       "VALUES (?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), sales, sales * 0.3, source))


def test_a_converted_demos_seeded_days_are_no_final_day_and_a_demo_keeps_them():
    import canonical_facts as cf
    import demand
    conv = _rid("Converted Co", learning_since=_ago(20))
    demo = _rid("Demo Grill", is_demo=1)
    start = date.today() - timedelta(days=70)
    for i in range(70):
        d = start + timedelta(days=i)
        seeded = i < 50
        for rid in (conv, demo):
            _day(rid, d, 9000.0 if seeded else 1000.0, "seed" if seeded else "pos")
    end = date.today()
    conv_days = cf.final_days(conv, start, end)
    assert conv_days and all(r["source"] != "seed" for r in conv_days)
    assert any(r["source"] == "seed" for r in cf.final_days(demo, start, end))
    # An own learner (the weekday forecast) reads only the real nights.
    fc = demand.forecast_day(conv, date.today() + timedelta(days=1), effects=False)
    assert fc["available"] and fc["typical_sales"] == 1000.0 and fc["samples"] == 3
    assert demand.forecast_day(demo, date.today() + timedelta(days=1), effects=False)["samples"] > 3
    assert "own_history_sql" in inspect.getsource(cf) and "seed" in cf.FINAL_SQL
    assert "seed" in cf.final_sql("l") and "l.source" in cf.final_sql("l")


# ── #22 the restaurant's own model ──────────────────────────────────────────

_n = [0]


def _episode(rid, key, created, verdict="improved", status="completed"):
    import rec_ledger
    _n[0] += 1
    rec_id = f"int{_n[0]}"
    start = (datetime.strptime(created, "%Y-%m-%d %H:%M:%S").date() + timedelta(days=1 + _n[0] % 5))
    tid = _x("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, baseline_value, "
             "started_on, evaluate_on, status, verdict, after_start, after_end, concurrent) "
             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (rid, "recommendation", key, "t", "weekday_sales:Monday", 10.0, start.isoformat(),
              (start + timedelta(days=7)).isoformat(), "evaluated", verdict, start.isoformat(),
              (start + timedelta(days=6)).isoformat(), "[]"))
    kind = rec_ledger.kind_of(key)
    _x("INSERT INTO rec_instances (rec_id, restaurant_id, key, module, kind, title, status, tags, tracker_id, "
       "created_at, last_event_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
       (rec_id, rid, key, "labor", kind, key, status, json.dumps(rec_ledger.tags_for(key, "labor", kind)), tid,
        created, created, created))
    _x("INSERT INTO rec_events (rec_id, restaurant_id, key, event, surface, dedupe, at) VALUES (?,?,?,?,?,?,?)",
       (rec_id, rid, key, "shown", "home", f"shown:{rec_id}", created))


def test_a_converted_demos_own_record_starts_at_learning_since():
    import rec_learning
    conv = _rid("Converted Co", learning_since=_ago(60))
    plain = _rid("Bella's Bistro")
    for rid in (conv, plain):
        for i, days in enumerate((200, 170, 140, 110)):          # the demo era for conv
            _episode(rid, f"trim_day:Monday{i}", _ago(days))
        _episode(rid, "trim_day:Friday", _ago(20))              # after conv's learning_since
    assert rec_learning.kind_record(plain, "trim_day")["measured"] == 5
    assert rec_learning.kind_record(conv, "trim_day")["measured"] == 1
    # The same floor when a caller hands in the ledger it loaded itself.
    c = models.get_conn()
    try:
        eps = rec_learning._load(c, conv, lean=True)
    finally:
        c.close()
    assert rec_learning.kind_record(conv, "trim_day", episodes=eps)["measured"] == 1
    # The model reads the ledger from learning_since on (the later floor).
    seen = {}
    real_load = rec_learning._load

    def spy(conn, rid, since=None, **kw):
        seen[rid] = since
        return real_load(conn, rid, since=since, **kw)
    rec_learning._load = spy
    try:
        rec_learning.effectiveness(conv)
        rec_learning.effectiveness(plain)
    finally:
        rec_learning._load = real_load
    assert seen[conv][:10] == _ago(60)[:10]
    assert seen[plain][:10] < _ago(60)[:10], "a restaurant never a demo reads its whole window"
