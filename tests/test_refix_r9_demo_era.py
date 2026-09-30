"""Memory re-audit fix round (9/29/26), R9 — "demo_era" (PLATFORM-7, -13, -17).

  * Ask's own record (intelligence.memory.own_record) and its slopes
    (features.series, own by default) start at a converted demo's
    learning_since, as the ranker and the confidence % do.
  * The neighbour prediction's control arm drops a neighbour's demo era, as
    its taken arm does.
  * learning_since comparisons normalise ISO "T" stamps on both sides.
"""
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


def _ago(days, t=" "):
    return (datetime.utcnow() - timedelta(days=days)).strftime(f"%Y-%m-%d{t}%H:%M:%S")


def _x(sql, args=()):
    c = models.get_conn()
    try:
        c.execute("PRAGMA foreign_keys=OFF")
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _rid(name, **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{abs(hash(name)) % 10**6}@x.test"))
    for k, v in cols.items():
        _x(f"UPDATE restaurants SET {k}=? WHERE id=?", (v, rid))
    from intelligence import jobs
    jobs.invalidate_excluded()
    return rid


def test_asks_own_record_starts_at_learning_since():
    from intelligence import memory
    rid = _rid("Converted Co", learning_since=_ago(20))
    for i in range(5):              # seeded "trim_day worked" results, before conversion
        _x("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, outcome, event_at) "
           "VALUES (?, 'trim_day', ?, 'measured', 'improved', ?)", (rid, f"trim_day:x#o{i}", _ago(60)))
    rec = memory.own_record(rid)
    assert rec["worked"] == [] and "trim_day" not in rec["by_kind"], "demo-era results are not this restaurant's record"
    for i in range(5):
        _x("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, outcome, event_at) "
           "VALUES (?, 'trim_day', ?, 'measured', 'improved', ?)", (rid, f"trim_day:y#o{i}", _ago(5)))
    rec = memory.own_record(rid)
    assert rec["by_kind"]["trim_day"]["measured"] == 5


def test_the_own_feature_series_drops_demo_weeks():
    from intelligence import features
    rid = _rid("Slope Co", learning_since=(date.today() - timedelta(weeks=3)).isoformat() + " 12:00:00")
    for n in range(10):
        wk = features.iso_week(date.today() - timedelta(weeks=n))
        _x("INSERT INTO intel_features (restaurant_id, week, version, features_json, completeness) "
           "VALUES (?, ?, ?, ?, 1.0)", (rid, wk, features.FEATURES_VERSION, json.dumps({"labor_pct_28d": 30 + n})))
    own = features.series(rid, weeks=12)
    assert len(own) == 4, "the conversion week and the three after it"
    assert len(features.series(rid, weeks=12, own=False)) == 10
    from intelligence import memory
    assert memory.metric_slopes(rid)["labor_pct_28d"]["weeks"] == 4


def test_before_learning_normalises_iso_stamps():
    from intelligence import jobs
    since = {7: "2026-09-29 12:00:00"}
    assert jobs.before_learning(7, "2026-09-29T08:00:00", since) is True
    assert jobs.before_learning(7, "2026-09-29T13:00:00", since) is False
    assert jobs.before_learning(7, "2026-09-29 08:00:00", {7: "2026-09-29T12:00:00"}) is True


def test_learning_rows_sql_normalises_iso_stamps():
    rid = _rid("T Co", learning_since="2026-09-29 12:00:00")
    _x("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, event_at) "
       "VALUES (?, 'k', 'k:a', 'accepted', '2026-09-29T08:00:00')", (rid,))
    _x("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, event_at) "
       "VALUES (?, 'k', 'k:b', 'accepted', '2026-09-29T13:00:00')", (rid,))
    c = models.get_conn()
    try:
        keys = [r[0] for r in c.execute(
            "SELECT source_key FROM intel_rec_events WHERE " + models.learning_rows_sql("restaurant_id", "event_at"))]
    finally:
        c.close()
    assert keys == ["k:b"]
    assert models.learning_eligible({"id": rid, "name": "T Co", "learning_since": "2026-09-29T12:00:00"},
                                    since="2026-09-29 08:00:00") is False


def test_the_control_arm_drops_a_neighbours_demo_era(monkeypatch):
    import rec_learning
    from intelligence import feedback, predict
    rid = _rid("Neighbour Co", learning_since=_ago(100))
    monkeypatch.setattr(rec_learning, "_confounded", lambda r: False)
    monkeypatch.setattr(feedback, "effect_of", lambda r: {"effect_pct": 5.0})
    for i, started in enumerate((200, 30)):       # one seeded (before conversion), one real
        rec_id = f"{i:032x}"
        _x("INSERT INTO rec_instances (rec_id, restaurant_id, key) VALUES (?, ?, 'trim_day:tue')", (rec_id, rid))
        d0 = (date.today() - timedelta(days=started)).isoformat()
        d1 = (date.today() - timedelta(days=started - 14)).isoformat()
        _x("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, started_on, "
           "evaluate_on, after_start, after_end, verdict, status) VALUES (?, 'untaken', ?, 't', 'labor_pct', ?, ?, ?, ?, "
           "'improved', 'evaluated')", (rid, f"observed:untaken:{rec_id}", d0, d1, d0, d1))
    c = models.get_conn()
    try:
        span = ((date.today() - timedelta(days=400)).isoformat(), date.today().isoformat())
        rows = predict._untaken(c, [rid], "trim_day", "labor_pct", "2000-01-01", span)
    finally:
        c.close()
    assert len(rows) == 1 and rows[0]["started_on"] == (date.today() - timedelta(days=30)).isoformat()
