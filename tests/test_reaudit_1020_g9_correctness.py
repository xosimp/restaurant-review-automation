"""Blind re-audit 10/20/26, cross-cutting correctness (R9), refix agent G9.

R9-07  a labor-% tracker started by a manager kept an hourly baseline and
       was judged on an all-in "after" read in the nightly job.
"""
import json
from datetime import date, timedelta

import pytest

import metrics
import models
import outcomes
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, outcomes, metrics):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(outcomes, "_holidays_between", lambda s, e: {})


from time_utils import restaurant_now as _rnow
TODAY = _rnow(None, naive=True).date()


def _rid(db_path, salaried=True):
    kw = {m: 1 for m in ("module_labor", "module_inventory", "module_reviews", "module_marketing")}
    rid = create_restaurant(Restaurant(name="Basis Co", owner_email="b@x.test", **kw), db_path=db_path)
    if salaried:
        # $150K x 2 over 52 weeks of 7 trading days: ~$824 a day.
        models.update_restaurant(rid, {"salaried_staff_json": json.dumps(
            [{"name": "Erik", "annual": 150000}, {"name": "Jim", "annual": 150000}])}, db_path=db_path)
    return rid


def _days(db_path, rid, start, n, labor, sales=5000.0):
    conn = get_conn(db_path)
    for i in range(n):
        d = start + timedelta(days=i)
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_cost, sales, "
                     "labor_pct) VALUES (?,?,?,?,?,?)",
                     (rid, d.isoformat(), d.strftime("%A"), labor, sales, round(labor / sales * 100, 1)))
    conn.commit()
    conn.close()


def _flat_labor(db_path, rid, t0):
    """The same hourly labor every day, before and after: nothing changed."""
    _days(db_path, rid, t0 - timedelta(days=28), 28, labor=1250.0)
    _days(db_path, rid, t0, (TODAY - t0).days, labor=1250.0)


def test_r9_07_a_manager_started_tracker_is_judged_on_its_own_hourly_basis(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=28)
    _flat_labor(db_path, rid, t0)
    with models.salary_view(False):             # the manager's request
        o = outcomes.record(rid, "manual", "trim", "Trim Monday lunch", "labor_pct", today=t0, db_path=db_path)
    assert o["salary_basis"] == 0 and o["baseline_value"] == 25.0
    with models.salary_view(True):              # even a job in the owner's view
        done = outcomes.evaluate(o["id"], today=TODAY, db_path=db_path)
    assert done["after_value"] == 25.0, "judged on the basis it started on, not the job's all-in view"
    assert done["verdict"] == "no_change" or done["verdict"] not in ("improved", "worsened")


def test_r9_07_an_owner_started_tracker_reads_all_in_everywhere(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=28)
    _flat_labor(db_path, rid, t0)
    with models.salary_view(True):              # the owner's request
        o = outcomes.record(rid, "manual", "trim", "Trim Monday lunch", "labor_pct", today=t0, db_path=db_path)
    assert o["salary_basis"] == 1 and o["baseline_value"] > 25.0
    # A manager's progress view never shows the owner's all-in figure, and
    # never re-reads it hourly against an all-in baseline.
    with models.salary_view(False):
        row = next(r for r in outcomes.progress(rid, db_path=db_path, today=TODAY) if r["id"] == o["id"])
    assert row["interim"] is None
    with models.salary_view(True):
        row = next(r for r in outcomes.progress(rid, db_path=db_path, today=TODAY) if r["id"] == o["id"])
    assert row["interim"]["value"] == o["baseline_value"]
    with models.salary_view(False):             # evaluated from a manager's request
        done = outcomes.evaluate(o["id"], today=TODAY, db_path=db_path)
    assert done["after_value"] == o["baseline_value"]


def test_r9_07_without_salaries_a_manager_still_sees_progress(db_path):
    rid = _rid(db_path, salaried=False)
    t0 = TODAY - timedelta(days=14)
    _flat_labor(db_path, rid, t0)
    o = outcomes.record(rid, "manual", "trim", "Trim Monday lunch", "labor_pct", today=t0, db_path=db_path)
    with models.salary_view(False):
        row = next(r for r in outcomes.progress(rid, db_path=db_path, today=TODAY) if r["id"] == o["id"])
    assert row["interim"] and row["interim"]["value"] == 25.0


def test_r9_07_a_tracker_from_before_the_column_is_settled_from_its_own_baseline(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=28)
    _flat_labor(db_path, rid, t0)
    with models.salary_view(False):
        hourly = outcomes.record(rid, "manual", "a", "A", "labor_pct", today=t0, db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE recommendation_outcomes SET salary_basis=NULL WHERE id=?", (hourly["id"],))
    conn.commit()
    conn.close()
    outcomes.init_outcomes(db_path=db_path)
    assert outcomes.get_outcome(hourly["id"], db_path=db_path)["salary_basis"] == 0


def test_r9_07_a_metric_the_salaries_do_not_touch_has_no_basis(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=28)
    _flat_labor(db_path, rid, t0)
    o = outcomes.record(rid, "manual", "s", "Sales", "sales", today=t0, db_path=db_path)
    assert o["salary_basis"] is None
    assert metrics.salary_sensitive("labor_pct_day:Friday") and not metrics.salary_sensitive("sales")


# ── R9-08: live pace reads the share at the minute the sales stand at ──────

def test_r9_08_the_share_done_is_read_at_the_same_moment_as_the_sales():
    import labor_live
    pts = [(17, 3000.0), (19, 6000.0), (20, 7000.0), (23, 10000.0)]
    assert labor_live._done_at(pts, 19.0) == 6000.0
    # 7:50pm: five sixths of the way from the 7pm capture to the 8pm one.
    assert labor_live._done_at(pts, 19 + 50 / 60.0) == pytest.approx(6833.33, abs=0.01)
    assert labor_live._done_at(pts, 16.5) is None
    assert labor_live._done_at(pts, 23.5) == 10000.0


def test_r9_08_a_typical_night_at_750pm_reads_on_pace(db_path, monkeypatch):
    import datetime as dt
    import demand
    import labor_live
    import pos
    rid = create_restaurant(Restaurant(name="Pace Co", owner_email="p@x.test", module_labor=1), db_path=db_path)
    friday = dt.date(2026, 10, 9)
    conn = get_conn(db_path)
    for w in range(1, 6):
        d = (friday - dt.timedelta(weeks=w)).isoformat()
        for hour, net in ((17, 3000.0), (19, 6000.0), (20, 7000.0), (23, 10000.0)):
            conn.execute("INSERT INTO pos_intraday (restaurant_id, business_date, captured_hour, weekday, net_sales, "
                         "provider) VALUES (?,?,?,?,?,?)", (rid, d, hour, "Friday", net, "rpower"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(pos, "fetch_sales_today", lambda r, d: (6833.33, "rpower"))
    monkeypatch.setattr(demand, "forecast_day", lambda *a, **k: {"available": True, "sales": 10000.0})
    s = labor_live._sales(models.get_restaurant(rid, db_path), friday, dt.datetime(2026, 10, 9, 19, 50),
                          db_path=db_path)
    # It read 0.6 (the 7pm share) and projected $11,389: +14% for a night
    # running exactly typical.
    assert s["share"] == pytest.approx(0.683, abs=0.001)
    assert s["projected"] == pytest.approx(10000.0, abs=1.0)


# ── R9-11: a night the POS missed is not $0 in "needs $X a night" ──────────

def test_r9_11_a_missing_night_withholds_what_the_rest_of_the_week_needs(monkeypatch, db_path):
    import types
    import demand
    from dsr import access, kpis
    from dsr import store as _store
    mon = date(2026, 10, 12)
    sun = mon + timedelta(days=6)
    thu = mon + timedelta(days=3)
    got = {mon: 5000.0, mon + timedelta(days=1): 9000.0, mon + timedelta(days=2): 10000.0}   # Thursday missing
    monkeypatch.setattr(_store, "baselines_net", lambda rid, days, **k: {
        (d.isoformat() if hasattr(d, "isoformat") else str(d)): (got.get(d), "dsr") for d in days})
    monkeypatch.setattr(_store, "night_budget", lambda rid, d, **k: {"net": 7000.0, "source": "budget"})
    monkeypatch.setattr(_store, "last_year_day", lambda rest, d: None)
    monkeypatch.setattr(demand, "forecast_net", lambda rid, d, **k: {"available": True, "typical_sales": 7000.0})
    p = kpis._span_pace(types.SimpleNamespace(id=1), mon, sun, thu, db_path)
    assert p["net"] == 24000.0 and p["budget_vs"] == 3000.0 and p["nights_unmeasured"] == 1
    # It said "needs $8,333 a night" beside "+$3,000 ahead": Thursday read as $0.
    assert "budget_per_night_needed" not in p and "budget_left" not in p
    tile = kpis._pace_tile({"week": p}, access.OWNER)
    assert "1 night not read yet" in tile["sub"] and not any("Needs" in s for s in tile["sub"])
    # Every done night measured: the need is back.
    got[thu] = 7000.0
    p = kpis._span_pace(types.SimpleNamespace(id=1), mon, sun, thu, db_path)
    assert "nights_unmeasured" not in p and p["budget_per_night_needed"] == round((49000 - 31000) / 3, 2)


# ── R9-12: the morning batch claims each restaurant before its delivery ────

def test_r9_12_a_kill_mid_flush_never_resends_who_was_already_delivered(db_path, monkeypatch):
    import datetime as dt
    import notify
    import ops
    import time_utils
    monkeypatch.setattr(ops, "get_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda r, naive=False: dt.datetime(2026, 9, 21, 10, 5))
    r1 = create_restaurant(Restaurant(name="One", owner_email="1@x.test"), db_path=db_path)
    r2 = create_restaurant(Restaurant(name="Two", owner_email="2@x.test"), db_path=db_path)
    r3 = create_restaurant(Restaurant(name="Quiet", owner_email="3@x.test"), db_path=db_path)
    notify.begin_daily_batch()
    try:
        for rid in (r1, r2, r3):
            assert notify._gated_out(rid, 10, "g9_check") is False
        for rid in (r1, r2):
            notify._open_batch()[rid] = [notify._Pending("labor_over", "t", "s", [], None, recs=[{}])]
        sent = []

        def deliver(rid, item, db):
            sent.append(rid)
            if len(sent) == 2:
                raise KeyboardInterrupt("the deploy's SIGTERM")
        monkeypatch.setattr(notify, "_deliver_pending", deliver)
        with pytest.raises(KeyboardInterrupt):
            notify.flush_daily_batch(db_path)
    finally:
        notify.discard_daily_batch()
    day = "2026-09-21"
    # Delivered, in flight and checked-with-nothing are all claimed: the next
    # hourly pass sends none of them again (it re-sent r1 before).
    for rid in (r1, r2, r3):
        assert ops.period_claimed(f"g9_check:{rid}", day)
        assert notify._gated_out(rid, 10, "g9_check") is True


# ── R9-19: a salaried person's long week is not overtime ───────────────────

_OT_SHIFTS = """date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes
2026-06-01,Monday,Ana,Server,09:00,18:00,9,9,,
2026-06-02,Tuesday,Ana,Server,09:00,18:00,9,9,,
2026-06-03,Wednesday,Ana,Server,09:00,18:00,9,9,,
2026-06-04,Thursday,Ana,Server,09:00,18:00,9,9,,
2026-06-05,Friday,Ana,Server,09:00,18:00,9,9,,
2026-06-01,Monday,Gabe  Huerta,Manager,08:00,20:00,12,12,,
2026-06-02,Tuesday,Gabe  Huerta,Manager,08:00,20:00,12,12,,
2026-06-03,Wednesday,Gabe  Huerta,Manager,08:00,20:00,12,12,,
2026-06-04,Thursday,Gabe  Huerta,Manager,08:00,20:00,12,12,,
2026-06-05,Friday,Gabe  Huerta,Manager,08:00,20:00,12,12,,
"""


def test_r9_19_overtime_leaves_the_salaried_out(db_path):
    rid = create_restaurant(Restaurant(name="OT Co", owner_email="o@x.test", hourly_rate=20.0), db_path=db_path)
    models.save_client_data(rid, "shifts", _OT_SHIFTS, db_path=db_path)
    v, _ = metrics.measure(rid, "overtime_hours", "2026-06-01", "2026-06-07", db_path)
    assert v == 25.0                     # Ana 5 + the GM's 20, before
    models.update_restaurant(rid, {"salaried_staff_json": json.dumps([{"name": "Gabe Huerta", "annual": 70000}])},
                             db_path=db_path)
    v, _ = metrics.measure(rid, "overtime_hours", "2026-06-01", "2026-06-07", db_path)
    assert v == 5.0
    assert metrics.overtime_premium_per_hour(rid, db_path) == 10.0


# ── R9-20: a 4:30am prep shift is the morning's, not after midnight ────────

def test_r9_20_the_dsr_evening_mix_places_shifts_as_the_schedule_does():
    from dsr import block_labor
    assert block_labor._span({"shift_start": "4:30am", "shift_end": "12:30pm"}) == (270, 750)
    assert block_labor._span({"shift_start": "12:30am", "shift_end": "2:00am"}) == (1470, 1560)
    assert block_labor._span({"shift_start": "5:00pm", "shift_end": "1:00am"}) == (1020, 1500)
    rows = [{"shift_start": "4:30am", "shift_end": "12:30pm", "actual_hours": 8},
            {"shift_start": "6:00pm", "shift_end": "11:00pm", "actual_hours": 5}]
    worked, after = block_labor._hours_after(rows)
    assert worked == 13 and after == pytest.approx(5.0)


# ── R9-23: stuck sends are dropped, guarded and shown ──────────────────────

def test_r9_23_a_held_text_past_a_day_late_is_dropped_not_sent(db_path, monkeypatch):
    import datetime as dt
    import people
    import staff_reminders
    staff_reminders.init_staff_reminders(db_path)
    rid = create_restaurant(Restaurant(name="Held Co", owner_email="h@x.test"), db_path=db_path)
    now = dt.datetime(2026, 10, 20, 15, 0)
    assert staff_reminders.hold_text(rid, "Dana", "schedule", "t", "Your shift tonight", ["l"],
                                     now - dt.timedelta(hours=30), db_path=db_path)
    sent = []
    monkeypatch.setattr(people, "reach", lambda *a, **k: {"Dana": {"sms": "+16305550100"}})
    monkeypatch.setattr(people, "_send_staff_text", lambda rid, phone, body: sent.append(body) or True)
    out = staff_reminders.release_held(now_utc=now, db_path=db_path)
    assert sent == [] and out["skipped"] == 1
    conn = get_conn(db_path)
    row = conn.execute("SELECT state, error FROM staff_notices WHERE restaurant_id=?", (rid,)).fetchone()
    conn.close()
    assert row["state"] == "dropped" and "not sent late" in row["error"]


def test_r9_23_a_slow_deliverer_never_overwrites_a_newer_claims_outcome(db_path, monkeypatch):
    import schedule_publish
    monkeypatch.setattr(schedule_publish, "get_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    rid = create_restaurant(Restaurant(name="Notice Co", owner_email="n@x.test"), db_path=db_path)
    conn = get_conn(db_path)
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start) VALUES (?,?)",
                       (rid, "2026-10-19")).lastrowid
    nid = conn.execute("INSERT INTO schedule_notices (restaurant_id, history_id, week_start, batch, employee_name, "
                       "kind, status, claimed_at) VALUES (?,?,?,?,?,?,?,?)",
                       (rid, hid, "2026-10-19", "b1", "Dana", "published", "sending", "NEW-TOKEN")).lastrowid
    conn.commit()
    conn.close()
    # The first deliverer (lease lost, token OLD) answers late with a failure.
    schedule_publish._finish_notice(nid, "failed", item={"error": "slow"}, db_path=db_path, token="OLD-TOKEN")
    conn = get_conn(db_path)
    row = conn.execute("SELECT status, claimed_at FROM schedule_notices WHERE id=?", (nid,)).fetchone()
    conn.close()
    assert row["status"] == "sending" and row["claimed_at"] == "NEW-TOKEN"
    schedule_publish._finish_notice(nid, "unreachable", item={"reason": "x"}, db_path=db_path, token="NEW-TOKEN")
    conn = get_conn(db_path)
    assert conn.execute("SELECT status FROM schedule_notices WHERE id=?", (nid,)).fetchone()["status"] == "unreachable"
    conn.close()


def test_r9_23_the_console_shows_stuck_dsr_emails_and_staff_notices():
    import admin_ops
    keys = {q["key"] for q in admin_ops._QUEUES}
    assert {"dsr_emails", "staff_notices", "schedule_notices"} <= keys


# ── R9-02 found: a query that failed on both databases, swallowed ──────────

def test_r9_02_the_recommendation_confidence_impact_reads_its_recommendations(db_path, monkeypatch):
    """data_health.module_impact ordered rec_instances BY id — a column it
    does not have — so it raised on SQLite and Postgres alike and the line
    was never shown. The recorded-statement PREPARE found it."""
    import data_health
    monkeypatch.setattr(data_health, "get_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    monkeypatch.setattr(data_health.ce, "freshness", lambda states: {"pct": 30, "stalest": "inventory",
                                                                      "basis": "Counts from 9/2/26", "errors": []})
    rid = create_restaurant(Restaurant(name="Impact Co", owner_email="i@x.test"), db_path=db_path)
    conn = get_conn(db_path)
    for n in range(3):
        conn.execute("INSERT INTO rec_instances (rec_id, restaurant_id, key, module, status, evidence_pct, "
                     "accuracy_pct, created_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
                     (f"r{n}", rid, f"k{n}", "inventory", "open", 80, None))
    conn.commit()
    conn.close()
    imp = data_health.module_impact(rid, "inventory", {}, db_path=db_path)
    assert imp and imp["delta"] > 0 and "3 open recommendations" in imp["basis"]


def test_r9_02_a_parenthesised_predicate_is_summed_as_sqlite_counts_it():
    """ops._reclaim_dead_run's SUM((finished_at IS NULL AND …)) raised
    'function sum(boolean) does not exist' on every call on Postgres, so a
    period a dead process held was never reclaimed."""
    import db_dialect
    import ops
    tr = db_dialect.Translator(lambda t: ["id"], lambda t: None)
    out = tr.translate("SELECT SUM((a IS NULL AND b IS NOT NULL)), SUM(x = 1) FROM t")
    assert out.count("CAST(") == 2
    src = __import__("inspect").getsource(ops._reclaim_dead_run)
    assert "SUM(CASE WHEN" in src and "SUM(finished_at" not in src


def test_r9_02_an_insert_into_a_partitioned_fact_table_answers_its_id(db_path):
    """On Postgres (CAVNAR_TEST_PG_URL: the template is pg_copy'd, its fact
    tables partitioned as production's) the key gained its partition
    column and no RETURNING was added: lastrowid was None for every
    ai_usage and schedule_model_calls insert, so ai_calls.usage_id and a
    recorded model call's id were lost. On SQLite it is the rowid."""
    conn = get_conn(db_path)
    try:
        a = conn.execute("INSERT INTO schedule_model_calls (restaurant_id, generation_id) VALUES (1, 'g')").lastrowid
        b = conn.execute("INSERT INTO ai_usage (restaurant_id, action) VALUES (1, 'x')").lastrowid
        conn.commit()
        assert a and conn.execute("SELECT generation_id FROM schedule_model_calls WHERE id=?", (a,)).fetchone()[0] == "g"
        assert b and conn.execute("SELECT action FROM ai_usage WHERE id=?", (b,)).fetchone()[0] == "x"
    finally:
        conn.close()


def test_r9_02_a_prefix_range_compares_bytes_as_sqlite_does():
    """metric >= 'sales.cat:' AND metric < 'sales.cat;' was EMPTY on a
    Postgres whose collation is a non-C locale (punctuation ignored at the
    first level): the Bar's and Food Cost's sales by category read
    nothing. A text range against a non-date literal compares under "C";
    a date literal and a numeric column are left alone."""
    import db_dialect
    types = {"dsr_metrics": {"metric": "TEXT", "business_date": "TEXT", "value": "REAL"}}
    tr = db_dialect.Translator(lambda t: ["id"], lambda t: list(types.get(t, {})), None, None,
                               lambda t: types.get(t))
    out = tr.translate("SELECT value FROM dsr_metrics WHERE metric >= 'sales.cat:' AND metric < 'sales.cat;' "
                       "AND business_date >= '2026-10-01' AND value > '5'")
    assert out.count('COLLATE "C"') == 2
    assert "'2026-10-01' COLLATE" not in out and "'5' COLLATE" not in out


def test_r9_02_a_number_standing_as_a_condition_and_a_julian_day_translate():
    """`WHERE 0` (a fragment builder's "match nothing") was refused by
    Postgres - the console's Recommendations and the learning calibration
    read nothing; datetime(MAX(julianday(x))) read a Julian day as text -
    every owner's last sign-in NULL in the console."""
    import db_dialect
    tr = db_dialect.Translator(lambda t: ["id"], lambda t: None)
    assert tr.translate("SELECT id FROM restaurants WHERE 0") == "SELECT id FROM restaurants WHERE (1 = 0)"
    assert "AND (1 = 1)" in tr.translate("SELECT 1 FROM t WHERE a = 1 AND 1")
    simple = tr.translate("SELECT CASE k WHEN 1 THEN 2 WHEN 0 THEN 3 END FROM t")
    assert "WHEN 1 THEN" in simple and "WHEN 0 THEN" in simple, "a simple CASE's values stay values"
    assert "BETWEEN 0 AND 1" in tr.translate("SELECT 1 FROM t WHERE x BETWEEN 0 AND 1")
    out = tr.translate("SELECT datetime(MAX(julianday(created_at))) FROM t")
    assert "to_timestamp((" in out and "- 2440587.5) * 86400.0" in out


def test_r9_02_now_at_run_time_is_the_current_time_on_postgres():
    """julianday(COALESCE(?, 'now')): 'now' arriving as a value read NULL in
    cavnar_ts, so ops.jobs_overdue found no job overdue on Postgres."""
    import os
    sql = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations", "postgres",
                            "0000_sqlite_compat.sql")).read()
    body = sql[sql.index("CREATE OR REPLACE FUNCTION cavnar_ts"):]
    assert "lower(btrim(v)) = 'now' THEN timezone('UTC', now())" in body.split("$$;")[0]


def test_r9_02_the_console_ai_table_groups_by_everything_it_selects():
    import inspect
    import admin_ops
    assert "GROUP BY action, model, 3" in inspect.getsource(admin_ops._by_action)


# ── R9-14 / R9-22: one collapse id per alert, on the restaurant's date ─────

def test_r9_14_two_distinct_alerts_of_a_type_on_one_day_do_not_collapse(monkeypatch):
    import datetime as dt
    import push
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda r, naive=False: dt.datetime(2026, 10, 20, 20, 30))
    a = push._collapse_id(5, "platform_alert", {"alert_id": 101})
    b = push._collapse_id(5, "platform_alert", {"alert_id": 102})
    assert a != b
    # No alert row behind it: the restaurant's own date (8:30pm Central is
    # already 10/21 in UTC).
    assert push._collapse_id(5, "milestone", {}) == "5-milestone-20261020"


def test_r9_22_the_collapse_id_is_fixed_once_and_survives_a_redrive(db_path, monkeypatch):
    import push
    got = []

    class _Now:
        def submit(self, fn, token_row, alert_type, title, body, data, db, *rest):
            got.append(data)
    monkeypatch.setattr(push, "_push_executor", lambda: _Now())
    monkeypatch.setattr(push, "get_device_tokens", lambda *a, **k: [{"id": 1, "user_id": 1, "restaurant_id": 9},
                                                                    {"id": 2, "user_id": 2, "restaurant_id": 9}])
    monkeypatch.setattr(push, "token_may_receive", lambda *a, **k: True)
    push.fire_push(9, "login", "t", "b", db_path=db_path)
    ids = {d["collapse_id"] for d in got}
    assert len(ids) == 1, "one id for the push on every device"
    # What the outbox carries back after a restart is the same id.
    assert push._collapse_id(9, "login", dict(got[0])) == ids.pop()
