"""Memory fix round (9/29/26), workstream M7 — "eligibility".

Internal, test and demo accounts leaked into learning: the internal account
counted as live, the schedule A/B read demo weeks, admin calibration mixed
seeded trackers with real ones, the admin Intelligence page's kind ranking
had no filter, test accounts were excluded only by a hand-set flag, and a
converted demo was quarantined for 90 days while its readers looked back a
year. One predicate now (models.learning_exclusion / learning_eligible):
automatic for internal billing, the admin's home and test-pattern names,
with the admin's learning_override; learning_since stamped at a demo's
conversion drops its demo era from every learner; the seed's rows carry
source 'seed' and a converted demo's own baselines drop them.
"""
import inspect
import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest
from flask import Flask

import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default, db_path) else path)
    monkeypatch.setattr(models, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    models._internal_homes_cache.clear()
    yield
    models._internal_homes_cache.clear()


def _x(sql, args=()):
    c = models.get_conn()
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _rid(name, **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{abs(hash(name)) % 10**6}@x.test"))
    for k, v in cols.items():
        _x(f"UPDATE restaurants SET {k}=? WHERE id=?", (v, rid))
    return rid


def _ago(days):
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


# ── the one predicate ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name,cols,why", [
    ("Bella's Bistro", {}, None),
    ("Testa Pizza", {}, None),
    ("Simple EJ's", {}, None),
    ("Simple EJ's Demo", {"is_demo": 1}, "demo"),
    ("Real Kitchen", {"exclude_from_learning": 1}, "excluded"),
    ("Real Kitchen", {"learning_override": "exclude"}, "excluded"),
    ("Cavnar AI Admin", {"billing_status": "internal"}, "internal"),
    ("Preview Test", {}, "test_name"),
    ("QA Tools", {}, "test_name"),
    ("Account Test Co", {}, "test_name"),
    ("Preview Test", {"learning_override": "include"}, None),
    ("Our Staff Account", {"billing_status": "internal", "learning_override": "include"}, None),
])
def test_one_predicate_decides_who_may_teach(name, cols, why):
    rid = _rid(name, **cols)
    r = models.get_restaurant(rid)
    assert models.learning_exclusion(r) == why
    assert models.learning_eligible(r) is (why is None)
    assert models.learning_eligible(rid) is (why is None)
    assert (rid in models.learning_ineligible_ids()) is (why is not None)
    st = models.learning_status(r)
    assert st["eligible"] is (why is None) and st["automatic"] is (why in ("internal", "admin_home", "test_name"))


def test_the_admins_own_home_is_out_automatically():
    import auth
    auth.init_auth(db_path=models.DB_PATH)
    rid = _rid("Will's Place")
    _x("INSERT INTO users (restaurant_id, username, email, password_hash, is_admin) VALUES (?, 'will', 'w@x.test', 'x', 1)",
       (rid,))
    assert models.learning_exclusion(rid) == "admin_home"
    _x("INSERT INTO users (restaurant_id, username, email, password_hash, role) VALUES (?, 'own', 'o@x.test', 'x', 'client')",
       (rid,))
    models._internal_homes_cache.clear()
    assert models.learning_exclusion(rid) is None, "a real owner login there: it is a customer's"


def test_data_recorded_before_learning_since_teaches_nothing():
    rid = _rid("Converted Co", learning_since="2026-09-01 12:00:00")
    assert models.learning_eligible(rid) is True
    assert models.learning_eligible(rid, since="2026-08-15") is False
    assert models.learning_eligible(rid, since="2026-09-01 12:00:00") is True
    assert models.learning_eligible(rid, since="2026-09-20T08:00:00") is True


# ── every cross-restaurant reader and admin check ──────────────────────────

def _answer(rid, key, at, outcome="improved", cohort="bar"):
    _x("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, cohort, action, outcome, event_at) "
       "VALUES (?,?,?,?,?,?,?)", (rid, "trim_day", key, cohort, "measured", outcome, at))


def test_cohort_and_platform_rates_leave_test_internal_and_demo_era_rows_out():
    from intelligence import scoring
    real = _rid("Bella's Bistro")
    test = _rid("Preview Test")
    ours = _rid("Our Kitchen", billing_status="internal")
    conv = _rid("Converted Co", learning_since=_ago(30))
    today = date.today().isoformat()
    for rid in (real, test, ours):
        _answer(rid, f"k{rid}", today)
    _answer(conv, "before", (date.today() - timedelta(days=60)).isoformat())
    _answer(conv, "after", today)
    stats = scoring.kind_stats("trim_day")
    assert stats["measured"] == 2 and stats["restaurants"] == 2          # real + conv's post-conversion row
    assert scoring.kind_stats("trim_day", restaurant_id=conv)["measured"] == 1   # its own record too
    ranked = {k["rec_kind"]: k for k in scoring.rank_kinds()}
    assert ranked["trim_day"]["restaurants"] == 2
    assert scoring.platform_totals()["restaurants"] == 2
    from intelligence import jobs
    assert {real, conv} <= jobs.real_restaurant_ids() and not {test, ours} & jobs.real_restaurant_ids()


def test_feature_weeks_from_a_demo_era_never_join_a_cohort():
    from intelligence import features
    conv = _rid("Converted Co", learning_since=_ago(0))
    real = _rid("Bella's Bistro")
    old_week = features.iso_week(date.today() - timedelta(days=14))
    new_week = features.iso_week(date.today())
    for rid in (conv, real):
        _x("INSERT INTO intel_features (restaurant_id, week, features_json, completeness) VALUES (?,?,?,?)",
           (rid, old_week, "{}", 0.5))
    latest = features.latest_by_restaurant()
    assert real in latest and conv not in latest
    _x("INSERT INTO intel_features (restaurant_id, week, features_json, completeness) VALUES (?,?,?,?)",
       (conv, new_week, "{}", 0.5))
    assert conv in features.latest_by_restaurant()
    weekly = features.weekly_by_restaurant(weeks=4)
    assert conv not in weekly.get(old_week, {}) and conv in weekly.get(new_week, {})


def _episode(rid, rec_id, created, kind="trim_day"):
    _x("INSERT INTO rec_instances (rec_id, restaurant_id, key, kind, status, created_at) VALUES (?,?,?,?,?,?)",
       (rec_id, rid, f"{kind}:x", kind, "open", created))
    _x("INSERT INTO rec_events (rec_id, restaurant_id, key, event, dedupe, at) VALUES (?,?,?,?,?,?)",
       (rec_id, rid, f"{kind}:x", "shown", f"shown:{rec_id}", created))


def test_the_admin_acceptance_view_leaves_test_accounts_and_demo_eras_out():
    import admin_ops
    real = _rid("Bella's Bistro")
    test = _rid("QA Tools")
    conv = _rid("Converted Co", learning_since=_ago(5))
    _episode(real, "r1", _ago(2))
    _episode(test, "t1", _ago(2))
    _episode(conv, "c-before", _ago(10))
    _episode(conv, "c-after", _ago(1))
    out = admin_ops.recommendation_acceptance(days=30)
    shown = {r["restaurant_id"]: r for r in out.get("by_restaurant", [])} if out.get("by_restaurant") else None
    conn = models.get_conn()
    try:
        eps = admin_ops._episodes(conn, _ago(30))
    finally:
        conn.close()
    assert sorted(e["rec_id"] for e in eps) == ["c-after", "r1"]
    assert shown is None or test not in shown


def test_the_schedule_ab_readout_reads_no_demo_or_test_weeks():
    import schedule_experiments as se
    real = _rid("Bella's Bistro")
    demo = _rid("Simple EJ's Demo", is_demo=1)
    conv = _rid("Converted Co", learning_since=_ago(3))
    arm = se.EXPERIMENTS[0]["arms"][0]["key"] if se.EXPERIMENTS else "control"
    exp = se.EXPERIMENTS[0]["key"] if se.EXPERIMENTS else "exp"
    hid = 0
    for rid, week in ((real, date.today().isoformat()), (demo, date.today().isoformat()),
                      (conv, (date.today() - timedelta(days=14)).isoformat())):
        hid = _x("INSERT INTO schedule_history (restaurant_id, week_start) VALUES (?,?)", (rid, week))
        _x("INSERT INTO schedule_experiment_weeks (history_id, restaurant_id, experiment, arm, week_start, quality_score) "
           "VALUES (?,?,?,?,?,?)", (hid, rid, exp, arm, week, 80))
    out = se.readout()
    arms = [a for e in out["experiments"] for a in e["arms"] if e["key"] == exp]
    assert sum(a["generated"] for a in arms) == 1, "only the real restaurant's week counts"


# ── a demo turned real ─────────────────────────────────────────────────────

def test_turning_demo_off_stamps_learning_since(monkeypatch):
    import admin_routes
    rid = _rid("Bella's Bistro", is_demo=1)
    app = Flask(__name__)
    app.secret_key = "x"
    fn = inspect.unwrap(admin_routes.admin_api_set_demo)
    with app.test_request_context(json={"is_demo": 0}):
        fn(rid, current_user={"username": "will"})
    r = models.get_restaurant(rid)
    assert r.is_demo == 0 and r.learning_since and r.demo_cleared_at == r.learning_since
    assert models.learning_eligible(r) is True
    assert models.learning_eligible(r, since="2020-01-01") is False


def test_the_admin_override_is_saved_and_reported(monkeypatch):
    import admin_routes
    rid = _rid("Preview Test")
    app = Flask(__name__)
    fn = inspect.unwrap(admin_routes.admin_set_brand)
    with app.test_request_context(json={"learning_override": "include"}):
        resp = fn(rid, current_user={"username": "will", "is_admin": 1})
    r = models.get_restaurant(rid)
    assert r.learning_override == "include" and r.learning_since
    assert models.learning_eligible(r) is True
    assert admin_routes._brand_payload(rid)["learning"]["eligible"] is True
    with app.test_request_context(json={"learning_override": "sometimes"}):
        bad = fn(rid, current_user={"username": "will", "is_admin": 1})
    assert bad[1] == 400


# ── the seed says it is the seed ────────────────────────────────────────────

def test_the_seed_stamps_its_labor_days_and_old_ones_are_stamped_once():
    import demo_seed
    rid = _rid("Simple EJ's Demo", is_demo=1)
    demo_seed._seed_ejs_history(rid, models.DB_PATH)
    c = models.get_conn()
    try:
        assert {r[0] for r in c.execute("SELECT DISTINCT source FROM labor_daily_history WHERE restaurant_id=?",
                                        (rid,)).fetchall()} == {"seed"}
    finally:
        c.close()
    # A database seeded before the stamp: the seed's figures, no source.
    _x("UPDATE labor_daily_history SET source=NULL WHERE restaurant_id=?", (rid,))
    monday = date(2025, 6, 2)
    _x("INSERT OR REPLACE INTO labor_daily_history (restaurant_id, date, sales, total_hours) VALUES (?,?,?,?)",
       (rid, "2025-06-09", 4321.0, 99.0))                              # not the seed's figures: a real day
    _x("DELETE FROM data_migrations WHERE name=?", (models.SEED_PROVENANCE_MIGRATION,))
    stamped = models.stamp_seed_provenance()
    assert stamped > 0 and models.stamp_seed_provenance() == 0
    c = models.get_conn()
    try:
        assert c.execute("SELECT source FROM labor_daily_history WHERE restaurant_id=? AND date=?",
                         (rid, monday.isoformat())).fetchone()[0] == "seed"
        assert c.execute("SELECT source FROM labor_daily_history WHERE restaurant_id=? AND date='2025-06-09'",
                         (rid,)).fetchone()[0] is None
    finally:
        c.close()


def test_a_converted_demos_own_baselines_drop_the_seed_and_a_demo_keeps_it():
    import metrics
    from intelligence import memory
    import schedule_economics
    rid = _rid("Bella's Bistro", is_demo=1)
    for i in range(40):
        d = (date(2025, 6, 1) + timedelta(days=i)).isoformat()
        _x("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_cost, source) "
           "VALUES (?,?,?,?,?,?)", (rid, d, date.fromisoformat(d).strftime("%A"), 5000.0, 1000.0, "seed"))
    v, _ = metrics.measure(rid, "sales", "2025-06-01", "2025-06-30")
    assert v == 5000.0, "a demo's own screens show the demo"
    _x("UPDATE restaurants SET is_demo=0, learning_since=datetime('now') WHERE id=?", (rid,))
    v, why = metrics.measure(rid, "sales", "2025-06-01", "2025-06-30")
    assert v is None, "a converted demo's year-over-year never reads synthetic sales"
    assert memory.seasonality(rid)["available"] is False
    for src in (inspect.getsource(schedule_economics),):
        assert src.count("_own_sql()") >= 2, "the holiday lift reads through the own-history filter"
