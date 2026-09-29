"""Memory fix round 9/29/26, workstream M8:

  platform_history  what the platform believed, week by week: an
                    append-only pattern history, cohort-series points
                    written once their week is complete (and "emerging" read
                    from them), the confidence log per trust version, counted
                    per recommendation and by organisation (PLATFORM-14).
  ab_verdict        the schedule A/B verdict clustered by restaurant, at most
                    MAX_WEEKS_PER_RESTAURANT weeks each, the published
                    version's quality, eligible restaurants only, stored
                    weekly (PLATFORM-13).
"""
import datetime as dt
import json
import random
import sqlite3
import sys
from datetime import date, timedelta

import pytest

import models
from models import Restaurant, create_restaurant, update_restaurant

import intelligence  # noqa: E402
from intelligence import features, jobs, patterns, trends, scoring  # noqa: E402
import schedule_versions  # noqa: E402,F401
import schedule_experiments as sx  # noqa: E402
import confidence_engine as ce  # noqa: E402


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    monkeypatch.delenv(sx.PIN_ENV, raising=False)
    import auth
    auth.init_auth(db_path=db_path)
    scoring.invalidate_org_map()
    jobs.invalidate_excluded()
    yield db_path
    scoring.invalidate_org_map()
    jobs.invalidate_excluded()


THIS_WEEK = features.iso_week(date.today())


def _rid(db, name, **kw):
    kw.setdefault("created_at", (date.today() - timedelta(days=120)).isoformat() + "T00:00:00")
    kw.setdefault("hourly_rate", 18.0)
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.replace(' ', '').lower()}@x.test", **kw),
                             db_path=db)


def _x(db, sql, args=()):
    c = models.get_conn(db)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _q(db, sql, args=()):
    c = models.get_conn(db)
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _seed(db, rid, week, f):
    _x(db, "INSERT INTO intel_features (restaurant_id, week, features_json, completeness) VALUES (?,?,?,0.8) "
           "ON CONFLICT(restaurant_id, week) DO UPDATE SET features_json=excluded.features_json",
       (rid, week, json.dumps(f)))


def _confirm(rid, sm="counter", concept="pizza"):
    update_restaurant(rid, {"service_model": sm, "concept": concept, "category": concept, "profile_source": "set",
                            "profile_confirmed_at": "2026-09-01T00:00:00"})


# ══ the pattern history ══════════════════════════════════════════════════════

def _planted(db, n=16, seed=1):
    rng = random.Random(seed)
    rids = [_rid(db, f"Hist Pie {seed} {i}") for i in range(n)]
    for i, r in enumerate(rids):
        fast = i % 2 == 0
        _seed(db, r, THIS_WEEK, {
            "response_24h_rate_30d": 0.8 if fast else 0.1,
            "avg_rating_delta": round((0.35 if fast else -0.05) + rng.uniform(-0.05, 0.05), 3),
            "avg_rating_30d": round(4.4 + rng.uniform(-0.2, 0.2), 2), "reply_rate_30d": 0.75 if fast else 0.6})
        _confirm(r)
    return rids


def test_a_pattern_leaves_one_row_a_week_and_its_retirement_is_kept(db):
    _planted(db)
    patterns.discover(db_path=db, shuffles=500)
    key = "sm:counter:reply_fast_rating"
    h = patterns.history(key, db_path=db)
    assert [(r["week"], r["status"]) for r in h] == [(THIS_WEEK, "active")]
    assert h[0]["n_with"] == 8 and h[0]["p_value"] <= 0.05 and "restaurant_id" not in h[0]
    patterns.discover(db_path=db, shuffles=500)                    # the same week again: written once
    assert len(patterns.history(key, db_path=db)) == 1
    # noise: the pattern is retired, and its past stays readable
    rng = random.Random(3)
    for r in _q(db, "SELECT id, features_json FROM intel_features"):
        f = json.loads(r["features_json"])
        f["avg_rating_delta"] = round(rng.uniform(-0.3, 0.3), 3)
        _x(db, "UPDATE intel_features SET features_json=? WHERE id=?", (json.dumps(f), r["id"]))
    patterns.discover(db_path=db, shuffles=500)
    assert [r["status"] for r in patterns.history(key, db_path=db)] == ["active", "retired"]
    held = {p["key"]: p["weeks_held"] for p in patterns.all_patterns(db_path=db)}
    assert held[key] == 1
    with pytest.raises(sqlite3.DatabaseError):
        _x(db, "UPDATE intel_pattern_history SET effect=9 WHERE key=?", (key,))


# ══ the cohort series ════════════════════════════════════════════════════════

def test_a_cohort_series_point_is_written_once_its_week_is_complete_and_never_again(db):
    rids = [_rid(db, f"Series Pie {i}") for i in range(9)]
    for r in rids:
        _confirm(r)
    for w in range(8):
        wk = features.iso_week(date.today() - timedelta(weeks=7 - w))
        for r in rids:
            _seed(db, r, wk, {"labor_pct_28d": 30.0})
    members = jobs.member_info(db_path=db)
    parts = jobs.peer_partitions(members, db_path=db)
    out = trends.persist(cohorts=parts, members=members, db_path=db)
    weeks = [r["week"] for r in _q(db, "SELECT week FROM intel_cohort_series WHERE metric='labor_pct_28d' "
                                       "ORDER BY week")]
    assert out["written"] == len(weeks) == 7 and THIS_WEEK not in weeks    # the week in progress waits
    past = weeks[-2]
    for r in rids:
        _seed(db, r, past, {"labor_pct_28d": 20.0})                 # the members' figures move later
    assert trends.persist(cohorts=parts, members=members, db_path=db)["written"] == 0
    assert _q(db, "SELECT p50 FROM intel_cohort_series WHERE week=? AND metric='labor_pct_28d'",
              (past,))[0]["p50"] == 30.0                            # what was measured then
    frozen = trends.stored_series(db_path=db)["sm:counter"]["labor_pct_28d"]
    assert [p["p50"] for p in frozen] == [30.0] * 7


# ══ the confidence log ═══════════════════════════════════════════════════════

def test_the_confidence_log_averages_one_trust_version_and_counts_recommendations_by_organisation(db):
    rids = [_rid(db, f"Log Co {i}") for i in range(5)]
    for i, r in enumerate(rids):
        for k in range(2):
            key = f"trim_day:D{k}#e{'%032x' % (r * 10 + k)}"
            # two rows for one episode: the Track and the Done — one recommendation
            for action in ("tracking", "done"):
                _x(db, "INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, event_at, "
                       "confidence_at, trust_version) VALUES (?, 'trim_day', ?, ?, datetime('now'), ?, ?)",
                   (r, key, action, 0.7 if k == 0 else 0.2, ce.VERSION if k == 0 else 1))
    jobs.log_confidence(db_path=db)
    row = _q(db, "SELECT n, mean_confidence, trust_version, orgs FROM intel_confidence_log "
                 "WHERE cohort='platform' AND rec_kind='trim_day'")[0]
    assert row == {"n": 10, "mean_confidence": 0.7, "trust_version": ce.VERSION, "orgs": 5}


# ══ ab_verdict ═══════════════════════════════════════════════════════════════

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
EXP = sx.EXPERIMENTS[0]


def _csv(rows):
    return HEADER + "\n" + "\n".join(",".join(r) for r in rows)


def _week(db, rid, week_start, arm, unchanged, total=10, score=80, publish=True):
    gen = [(week_start, "Monday", f"P{k}", "Server", "5:00pm", "10:00pm", "5", "") for k in range(total)]
    pub = [r if k < unchanged else (r[0], r[1], f"Q{k}", *r[3:]) for k, r in enumerate(gen)]
    hid = models.save_schedule_history(rid, week_start, week_start, 50, 50, 30, _csv(gen), [], db_path=db)
    c = models.get_conn(db)
    if publish:
        c.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
        c.execute("INSERT INTO schedule_versions (restaurant_id, history_id, version, reason, schedule_csv) "
                  "VALUES (?,?,?,?,?)", (rid, hid, 1, "generated", _csv(gen)))
        c.execute("INSERT INTO schedule_versions (restaurant_id, history_id, version, reason, schedule_csv) "
                  "VALUES (?,?,?,?,?)", (rid, hid, 2, "published", _csv(pub)))
        c.execute("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, issues, labor_pct) "
                  "VALUES (?,?,?,?,?,?,?)", (rid, hid, week_start, "night", 50, 0, 30.0))
    c.commit()
    c.close()
    sx.record(rid, hid, [{"experiment": EXP["key"], "arm": arm, "pinned": False}], score, db_path=db)
    return hid


def _ws(k):
    return (dt.date(2025, 1, 6) + dt.timedelta(weeks=k)).isoformat()


def test_one_busy_restaurant_can_no_longer_call_a_winner_for_everyone(db):
    big = _rid(db, "Busy Grill")
    k = 0
    for w in range(15):                          # 15 weeks an arm, the solver far ahead there
        _week(db, big, _ws(k), "solver", 9); k += 1
        _week(db, big, _ws(k), "model", 5); k += 1
    for i in range(4):                           # four others, no difference at all
        r = _rid(db, f"Small Grill {i}")
        for w in range(4):
            _week(db, r, _ws(k), "solver", 6); k += 1
            _week(db, r, _ws(k), "model", 6); k += 1
    read = sx.readout(db_path=db)
    exp = read["experiments"][0]
    solver = next(a for a in exp["arms"] if a["arm"] == "solver")
    assert solver["acceptance"]["n"] == sx.MAX_WEEKS_PER_RESTAURANT + 16      # capped at 8 for the busy one
    assert solver["restaurants"] == 5 and solver["vs_control"]["acceptance"]["clusters"] == 5
    assert exp["verdict"]["state"] != "winner" and exp["verdict"]["call"] is None
    # the old reading — weeks as independent, uncapped — would have called it
    def vals(arm):
        c = models.get_conn(db)
        try:
            rows = c.execute("SELECT history_id FROM schedule_experiment_weeks WHERE arm=?", (arm,)).fetchall()
        finally:
            c.close()
        acc = {}
        for r in (big,) + tuple(range(big + 1, big + 5)):
            for w in schedule_versions.acceptance(r, weeks=60, db_path=db)["weeks"]:
                acc[w["history_id"]] = w["unchanged_share"]
        return [acc[r["history_id"]] for r in rows if r["history_id"] in acc]
    old = sx.diff_ci(sx.mean_ci(vals("solver")), sx.mean_ci(vals("model")))
    assert old["ci90"][0] > 0                                               # a false winner, before


def test_quality_is_the_published_versions_and_a_demo_account_is_not_read(db):
    rid = _rid(db, "Quality Grill")
    _week(db, rid, _ws(0), "solver", 8, score=10, publish=False)          # a regeneration, never published
    _week(db, rid, _ws(0), "solver", 8, score=80)                         # the published version
    demo = _rid(db, "Demo Grill", is_demo=1)
    for w in range(6):
        _week(db, demo, _ws(10 + w), "solver", 10, score=99)
    jobs.invalidate_excluded()
    solver = next(a for a in sx.readout(db_path=db)["experiments"][0]["arms"] if a["arm"] == "solver")
    assert solver["quality"]["mean"] == 80 and solver["restaurants"] == 1 and solver["acceptance"]["n"] == 1


def test_the_verdict_is_stored_once_a_week(db):
    rid = _rid(db, "Stored Grill")
    _week(db, rid, _ws(0), "solver", 8)
    out = sx.record_verdicts(db_path=db, today=dt.date(2026, 10, 5))
    assert out["written"] == 1 and {"attempted", "ok", "failed", "skipped", "hit_bound"} <= set(out)
    assert sx.record_verdicts(db_path=db, today=dt.date(2026, 10, 7))["written"] == 0     # the same ISO week
    assert sx.record_verdicts(db_path=db, today=dt.date(2026, 10, 12))["written"] == 1
    rows = sx.verdicts(EXP["key"], db_path=db)
    assert [r["week"] for r in rows] == ["2026-W42", "2026-W41"] and rows[0]["method"] == sx.VERDICT_METHOD
    assert rows[0]["state"] == "insufficient" and rows[0]["arms"]
