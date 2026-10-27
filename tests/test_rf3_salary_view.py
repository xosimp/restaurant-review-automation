"""RF3 — the blind re-audit's salary-visibility and labor-basis findings
(10/26/26: B3, B4 "payload and record semantics", B8 "prime-cost
calibration"). The owner's labor % is all-in; a manager never sees salaries
in any surface, text, alert, cache or AI output.

  P0  job output read the owner's salary view by default — the daily issue
      scan titled the labor issue with the all-in % and texted it to the
      routed manager (models.viewer_sees_salaries, ops._job_context)
  P1  the Reviews read embedded labor % and the waste $ in a payload shared
      across roles (client_api.review_read_scope)
  P1  the labor-over alert and the weekly digest set hourly history against
      all-in figures (notify._labor_alert_figure, models.get_labor_history)
  P1  "food X% + labor Y%" did not add up to the prime cost
  P2  the labor-math ratchet missed reads of the stored hourly labor_pct
  P2  peer and location labor % beside the owner's all-in figure, unlabelled
  P2  outcome trackers not tied to the pay basis (a recost priced as value)
  P3  support acting through view-as saw salaries
  P3  the staff-knowledge plan gate had no cohort switch
  B4/B8  basis versions on the payload, the labor periods, the forecast record

Every "today" is the restaurant's own or fixed — never the process clock.
"""
import json
import sys
import types
from datetime import date, timedelta

import pytest
from flask import Flask, g

import models
from models import Restaurant, create_restaurant, get_conn, update_restaurant


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
    yield


def _x(sql, args=()):
    c = get_conn()
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _rid(name="RF3 Co", **kw):
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(name=name, owner_email="rf3@x.test", **kw))


def _today(rid):
    from time_utils import restaurant_now_by_id
    return restaurant_now_by_id(rid).date()


OWNER = {"id": 1, "role": "client", "is_admin": 0}
MANAGER = {"id": 2, "role": "manager", "is_admin": 0}
MEMBER = {"id": 3, "role": "member", "is_admin": 0}
SUPPORT_VIEW_AS = {"id": 1, "role": "client", "is_admin": 0, "acting_admin_id": 99, "acting_admin_role": "support",
                   "device_type": "admin-view-as"}
ADMIN_VIEW_AS = dict(SUPPORT_VIEW_AS, acting_admin_role="admin")


def _in_request(user, fn):
    with Flask(__name__).test_request_context("/"):
        g.cavnar_current_user = dict(user) if user else None
        return fn()


# A salaried manager at $300K over 52 weeks of 7 trading days (~$824 a day)
# beside $160 a day of hourly cooks on $5,000 of sales: hourly 3.2%, all-in
# ~19.7% — against a 10% target the owner is over and the hourly staff not.
SAL = json.dumps([{"name": "Erik Baylis", "annual": 300000}])


def _shift_world(target=10.0, salaried=True):
    rid = _rid("Shift Co", module_labor=1, labor_target_pct=target, hourly_rate=26.0)
    update_restaurant(rid, {"labor_target_source": "set", **({"salaried_staff_json": SAL} if salaried else {})})
    today = _today(rid)
    rows = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes,pay_rate"]
    for n in range(1, 8):
        d = (today - timedelta(days=n)).isoformat()
        rows.append(f"{d},X,{'Amy' if n % 2 else 'Bob'} Cook,Kitchen,08:00,16:00,8,8,5000,,20")
        rows.append(f"{d},X,Erik Baylis,Manager FOH,10:00,14:00,4,4,5000,,")
    models.save_client_data(rid, "shifts", "\n".join(rows) + "\n", source="rpower")
    return rid


# ── the helper itself ──────────────────────────────────────────────────────

def test_off_a_request_the_default_is_manager_safe():
    assert models.viewer_sees_salaries() is False
    with models.salary_view(True):
        assert models.viewer_sees_salaries() is True
    assert _in_request(None, models.viewer_sees_salaries) is False


def test_in_a_request_only_the_account_holder_sees_salaries_never_support_through_view_as():
    import permissions
    assert _in_request(OWNER, models.viewer_sees_salaries) is True
    assert _in_request(MANAGER, models.viewer_sees_salaries) is False
    assert _in_request(MEMBER, models.viewer_sees_salaries) is False
    assert _in_request(SUPPORT_VIEW_AS, models.viewer_sees_salaries) is False
    assert _in_request(ADMIN_VIEW_AS, models.viewer_sees_salaries) is True
    assert permissions.is_principal(SUPPORT_VIEW_AS), "the view-as identity is the owner's"
    assert not permissions.sees_salaries(SUPPORT_VIEW_AS)


def test_a_wrapped_unit_carries_its_view_onto_a_pool_thread():
    import threading
    seen = []
    t = threading.Thread(target=models.in_salary_view(True, lambda: seen.append(models.viewer_sees_salaries())))
    t.start()
    t.join()
    bare = threading.Thread(target=lambda: seen.append(models.viewer_sees_salaries()))
    bare.start()
    bare.join()
    assert seen == [True, False]


# ── P0: every job is manager-safe unless it reports to the owner alone ─────

def test_every_registered_job_runs_manager_safe_unless_it_is_owner_only():
    import jobs_registry
    import ops
    declared = {n for n, s in jobs_registry.JOBS.items() if s.get("salary_view") == "owner"}
    assert declared == set(jobs_registry.OWNER_SALARY_JOBS)
    assert declared == {"labor_prewarm", "value_figures", "digest_precompute", "weekly_digests",
                        "monthly_summary", "quarterly_summaries"}, "a new owner-only job says why on its entry"
    for name in jobs_registry.JOBS:
        # Even inside work that carried the owner's view in (an admin's
        # request, a pool thread that copied one).
        with models.salary_view(True):
            with ops._job_context(name, None):
                assert models.viewer_sees_salaries() is (name in declared), name
    with models.salary_view(True):
        with ops._job_context("an_admin_task_not_in_the_registry", None):
            assert models.viewer_sees_salaries() is False


def test_the_owner_only_job_bodies_read_all_in_however_they_are_called(monkeypatch):
    import ai_batches
    import reporter
    import scheduler
    seen = []
    monkeypatch.setattr(models, "get_all_restaurants", lambda *a, **k: seen.append(models.viewer_sees_salaries()) or [])
    scheduler.run_monthly_summaries()
    scheduler.run_quarterly_summaries()
    monkeypatch.setattr(ai_batches, "enabled", lambda *a, **k: seen.append(models.viewer_sees_salaries()) or False)
    reporter.run_digest_precompute()
    assert seen == [True, True, True]


def test_a_job_built_labor_issue_texts_the_manager_the_hourly_figure_never_the_all_in_one(monkeypatch):
    import issues
    import labor
    import ops
    import strategy_jobs
    rid = _shift_world(target=10.0)
    cid = _x("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) VALUES (?, 'GM', '+15555550100', 1)",
             (rid,))
    issues.set_routing(rid, "manager", cid)
    texts = []
    monkeypatch.setattr(issues, "_text", lambda phone, msg, r: texts.append(msg) or types.SimpleNamespace(
        ok=True, uncertain=False, permanent=False, error=None, status="queued"))
    monkeypatch.setattr(models, "is_in_quiet_hours", lambda *a, **k: False)
    with models.salary_view(True):
        owner = labor.analyse_shifts_for_restaurant(rid)
    with models.salary_view(False):
        team = labor.analyse_shifts_for_restaurant(rid)
    all_in, hourly = owner["overall_labor_pct"], team["overall_labor_pct"]
    assert owner["includes_salaries"] and all_in > 10.0 > hourly == 3.2

    ops.run_job("issue_scan", lambda: strategy_jobs.run_issue_scan(db_path=models.DB_PATH))

    row = get_conn().execute("SELECT title, detail FROM ops_issues WHERE restaurant_id=? AND kind='labor'",
                             (rid,)).fetchone()
    assert row is not None, "over the target (all-in, the owner's rule), so the issue opens"
    assert f"{hourly:.1f}%" in row["title"] and "hourly" in row["title"].lower()
    for text in [row["title"], row["detail"]] + texts:
        assert f"{all_in:.1f}" not in text, text
    assert texts and f"{hourly:.1f}%" in texts[0]
    # The owner's own Labor read is still all-in.
    assert _in_request(dict(OWNER, restaurant_id=rid),
                       lambda: labor.analyse_shifts_for_restaurant(rid)["overall_labor_pct"]) == all_in


def test_with_nobody_salaried_the_labor_issue_reads_as_before():
    import issues
    rid = _shift_world(target=2.0, salaried=False)
    cid = _x("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) VALUES (?, 'GM', '+15555550101', 1)",
             (rid,))
    issues.set_routing(rid, "manager", cid)
    fig = issues.labor_issue_figures(rid)
    # Nobody salaried: the manager's punches are hourly labor too, and the
    # figure the issue shows is the labor itself.
    assert fig["hourly_only"] is False and fig["shown_pct"] == fig["all_in_pct"] > 3.2


def test_the_morning_brief_still_builds_the_owner_all_in_and_the_manager_hourly(monkeypatch):
    import morning_brief
    rid = _rid("Brief Co")
    people = [dict(OWNER, id=11, restaurant_id=rid, email="o@x.test"),
              dict(MANAGER, id=12, restaurant_id=rid, email="m@x.test")]
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: people)
    built = []
    monkeypatch.setattr(morning_brief, "build", lambda r, viewer=None, **k: built.append(
        (viewer["id"], models.viewer_sees_salaries())) or {"lines": []})
    morning_brief.deliver(rid, today=_today(rid))
    assert sorted(built) == [(11, True), (12, False)]


# ── P1: the labor-over alert compares like with like ───────────────────────

def _labor_week(rid, hourly_pct, sales=10000.0):
    today = _today(rid)
    end = today - timedelta(days=2)
    start = end - timedelta(days=6)
    models.save_labor_snapshot(rid, start.isoformat(), end.isoformat(), hourly_pct, sales * hourly_pct / 100.0,
                               sales, days=7)


def _alert_world(salaried):
    rid = _rid("Alert Co")
    update_restaurant(rid, {"urgent_via_sms": 1, "urgent_via_email": 1, "owner_email": "a@x.test",
                            "alert_labor_over": 1, "labor_target_pct": 30.0, "labor_target_source": "set"})
    if salaried:
        # $52,000 a year: $142.86 a trading day, $1,000 over the week's seven.
        update_restaurant(rid, {"salaried_staff_json": json.dumps([{"name": "Pat Manager", "annual": 52000}])})
    return rid


def test_the_labor_alert_fires_on_the_all_in_week_and_says_only_the_hourly_figure(monkeypatch):
    import notify
    rid = _alert_world(salaried=True)
    _labor_week(rid, 25.0)                     # hourly 25% (under 30%); with salaries 35% (over)
    fired = []
    monkeypatch.setattr(notify, "raise_alert", lambda r, t, sms, subject, **k: fired.append((t, sms, k)) or True)
    notify.check_daily_alerts()
    labor = [f for f in fired if f[0] == "labor_over"]
    assert labor, "the stored hourly week read against the all-in target never fired"
    _t, sms, kw = labor[0]
    words = " ".join([sms] + list(kw.get("lines") or []) + [r.get("title") or "" for r in kw.get("recs") or []])
    assert "25.0%" in words and "salar" in words
    assert "35.0" not in words, "an all-in figure (or hourly + the points over) reaches managers' phones"


def test_with_nobody_salaried_the_labor_alert_names_the_labor_and_the_points_over(monkeypatch):
    import notify
    rid = _alert_world(salaried=False)
    _labor_week(rid, 35.0)
    fired = []
    monkeypatch.setattr(notify, "raise_alert", lambda r, t, sms, subject, **k: fired.append((t, sms)) or True)
    notify.check_daily_alerts()
    sms = [s for t, s in fired if t == "labor_over"][0]
    assert "Labor at 35.0%" in sms and "5.0pts over your 30% target" in sms


def test_labor_history_says_its_pay_basis_and_two_bases_are_never_compared():
    rid = _alert_world(salaried=True)
    _labor_week(rid, 25.0)
    with models.salary_view(True):
        owner = models.get_labor_history(rid, limit=2)[0]
    with models.salary_view(False):
        team = models.get_labor_history(rid, limit=2)[0]
    assert owner["pay_basis"] == "all_in" and owner["labor_pct"] == 35.0 and owner["hourly_labor_pct"] == 25.0
    assert team["pay_basis"] == "hourly" and team["labor_pct"] == 25.0 and "salaried_cost" not in team
    a = dict(owner, complete=True, basis="b", period_end="2026-10-11", period_start="2026-10-05")
    b = dict(owner, complete=True, basis="b", period_end="2026-10-04", period_start="2026-09-28")
    assert models._periods_comparable(a, b)
    assert not models._periods_comparable(a, dict(b, pay_basis="hourly"))


# ── P1: prime cost on one denominator ──────────────────────────────────────

def _prime_world(monkeypatch, net=15000.0, food_sales=10500.0):
    """$4,500 of COGS on $10,500 of food sales (42.9% food cost) inside
    $15,000 of net (drinks are the rest); $3,000 of hourly labor on three
    measured nights, and a salaried manager at $300 a trading day."""
    import cogs
    import food_cost_intelligence as fci
    rid = _rid("Prime Co", module_inventory=1)
    update_restaurant(rid, {"salaried_staff_json": json.dumps([{"name": "Erin Manager", "annual": 109200}])})
    nights = [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)]
    for d in nights:
        for metric, v in (("labor.cost", 1000.0), ("sales.net", net / 3)):
            _x("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) VALUES (?,?,?,?,?)",
               (rid, d.isoformat(), metric, v, "ready"))
        _x("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, final) VALUES (?,?,?,?,1)",
           (rid, d.isoformat(), d.strftime("%A"), net / 3))
    monkeypatch.setattr(fci, "_local_today", lambda r: date(2026, 10, 9))
    monkeypatch.setattr(fci, "forecast_accuracy", lambda *a, **k: None)
    monkeypatch.setattr(cogs, "net_sales_in_window", lambda r, s, e: (net, None))
    monkeypatch.setattr(cogs, "build_food_cost_pct", lambda r, days=28, db_path=None, today=None: {
        "ok": True, "cogs": 4500.0, "pct": round(4500.0 / food_sales * 100, 1), "target": 28.0, "window_days": days,
        "food_sales": food_sales, "net_sales": net,
        "start": (today - timedelta(days=days - 1)).isoformat(), "end": today.isoformat()})
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant", lambda r: None)
    return rid


@pytest.mark.parametrize("sees", [True, False])
def test_food_share_and_labor_add_up_to_the_prime_cost_when_food_sales_are_not_net(monkeypatch, sees):
    import food_cost_intelligence as fci
    rid = _prime_world(monkeypatch)
    with models.salary_view(sees):
        out = fci.profitability_projection(rid)
    assert out["available"] and out["food_cost_pct"] == 42.9, "the food cost % proper, over food sales"
    assert out["food_share_pct"] == 30.0 and out["pct_denominator"] == "net_sales"
    assert abs(out["food_share_pct"] + out["labor_pct"] - out["prime_cost_pct"]) <= 0.1
    assert out["figures_version"] == fci.PROFITABILITY_FIGURES_VERSION == 2
    block = fci._profit_block(out)
    assert f"food 30.0% + labor {out['labor_pct']}%" in block and "42.9%" in block
    assert "food 42.9%" not in block, "the food cost % over food sales is never set as a part of the prime cost"


def test_the_closed_month_prime_cost_is_one_share_of_net_sales(monkeypatch):
    import cogs
    import metrics
    rid = _rid("Closed Co")
    monkeypatch.setattr(cogs, "build_food_cost_pct", lambda r, days=28, db_path=None, today=None: {
        "ok": True, "cogs": 3000.0, "pct": 40.0, "net_sales": 10000.0, "food_sales": 7500.0, "window_days": days,
        "start": (today - timedelta(days=days - 1)).isoformat(), "end": today.isoformat()})
    monkeypatch.setattr(metrics, "labor_figures", lambda *a, **k: {"days": 30, "pct": 25.0, "basis_text": "x"})
    v, detail = metrics.prime_cost_pct(rid, "2026-09-01", "2026-09-30")
    assert v == 55.0 and "food 30.0% + labor 25.0% of net sales" in detail     # not 40 + 25


# ── B8/B4: the prime-cost projection's record is read on one pay basis ─────

def _scored_month(rid, predicted, actual, pay_basis, months_back):
    import forecast_log
    end = forecast_log.month_bounds(date(2026, 9, 15) - timedelta(days=31 * months_back))[1]
    err, signed = forecast_log.errors(predicted, actual)
    _x("INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, actual, error_pct, signed_error_pct, "
       "scored_at, created_at, pay_basis) VALUES (?,?,?,?,?,?,?,datetime('now'),?,?)",
       (rid, "profitability_month", end.isoformat(), predicted, actual, err, signed,
        f"{(end - timedelta(days=10)).isoformat()} 09:00:00", pay_basis))


def test_hourly_era_rows_never_correct_an_all_in_projection():
    import forecast_log
    rid = _rid("Calib Co")
    for i in range(4):                                         # predicted hourly, scored all-in: a 15% low lean
        _scored_month(rid, 51.0, 60.0, None, i)
    with models.salary_view(True):
        rec = forecast_log.shown(rid, "profitability_month", 60.0)
    assert rec["corrected"] is False and rec["shown"] == 60.0
    for i in range(4, 8):
        _scored_month(rid, 66.0, 60.0, "all_in", i)            # the current basis, 10% high
    with models.salary_view(True):
        assert forecast_log.shown(rid, "profitability_month", 66.0)["corrected"] is True
    with models.salary_view(False):                            # a manager's projection: the owner's record never
        assert forecast_log.shown(rid, "profitability_month", 66.0)["corrected"] is False


def test_a_frozen_forecast_keeps_its_pay_basis_and_is_scored_on_it(monkeypatch):
    import forecast_log
    import metrics
    rid = _rid("Freeze Co")
    with models.salary_view(False):
        forecast_log.record(rid, "labor_week", 22.0, period_of=date(2026, 9, 14))
    forecast_log.record(rid, "profitability_month", 58.0, period_of=date(2026, 9, 14), pay_basis="all_in")
    rows = {r["kind"]: r["pay_basis"] for r in get_conn().execute(
        "SELECT kind, pay_basis FROM forecast_log WHERE restaurant_id=?", (rid,)).fetchall()}
    assert rows == {"labor_week": "hourly", "profitability_month": "all_in"}
    seen = []
    monkeypatch.setattr(metrics, "prime_cost_pct",
                        lambda *a, **k: seen.append(("prime", models.viewer_sees_salaries())) or (57.0, ""))
    monkeypatch.setattr(metrics, "measure",
                        lambda *a, **k: seen.append(("labor", models.viewer_sees_salaries())) or (21.0, ""))
    forecast_log.score_due(rid, today=date(2026, 10, 20))
    # Each actual (and its naive forecasts) on its own row's basis.
    assert {v for k, v in seen if k == "prime"} == {True}
    assert {v for k, v in seen if k == "labor"} == {False}


# ── P2: the labor-math ratchet sees reads of the stored hourly % ───────────

def test_the_ratchet_counts_a_read_of_the_stored_hourly_labor_pct():
    sys.path.insert(0, "scripts")
    import check_labor_math as clm
    snippet = ('def f(conn, rid):\n'
               '    return conn.execute("""SELECT labor_pct, period_start FROM labor_history\n'
               '                           WHERE restaurant_id=?""", (rid,)).fetchone()\n'
               'def g(conn, rid):\n'
               '    return conn.execute(f"SELECT date, labor_pct FROM labor_daily_history WHERE x={rid}")\n'
               'def h(conn):\n'
               '    return conn.execute("SELECT sales FROM labor_daily_history")\n')
    assert clm.count_stored_reads(snippet) == 2
    assert clm.compare(clm.scan_stored(), clm.load_baseline(clm.STORED_BASELINE)) == []
    assert "notify.py" not in clm.scan_stored() and "reporter.py" not in clm.scan_stored()


# ── P2: peer labor % says its basis ────────────────────────────────────────

def test_the_dsr_peer_labor_line_is_labelled_hourly_and_sets_the_owners_hourly_figure_beside_it(monkeypatch):
    from dsr import kpis
    from intelligence import engine
    monkeypatch.setattr(engine, "compare", lambda *a, **k: {"comparisons": [
        {"kind": "peers", "available": True, "p50": 21.0, "n": 9, "cohort_label": "pubs"}]})
    r = types.SimpleNamespace(id=1)
    owner = kpis._peers("labor_pct", r, None, own_hourly=24.5)
    team = kpis._peers("labor_pct", r, None)
    assert owner["label"] == "Restaurants like yours, hourly staff (yours 24.5%)" and owner["basis"] == "hourly"
    assert team["label"] == "Restaurants like yours, hourly staff" and "salaries left out" in team["detail"]
    import intelligence.metrics_registry as reg
    assert "hourly staff" in reg.meta("labor_pct_28d")["label"]


# ── P2: a labor tracker is tied to the pay it was costed on ────────────────

def _tracker_world():
    rid = _rid("Tracker Co", module_labor=1, module_inventory=1, module_reviews=1, module_marketing=1)
    today = _today(rid)
    t0 = today - timedelta(days=28)
    c = get_conn()
    for i in range(56):
        d = t0 - timedelta(days=28) + timedelta(days=i)
        if d >= today:
            break
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_cost, sales, labor_pct) "
                  "VALUES (?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), 1250.0, 5000.0, 25.0))
    c.commit()
    c.close()
    return rid, t0, today


def test_a_recost_since_the_baseline_is_not_comparable_and_never_priced(monkeypatch):
    import outcomes
    monkeypatch.setattr(outcomes, "_holidays_between", lambda s, e: {})
    rid, t0, today = _tracker_world()
    with models.salary_view(False):
        o = outcomes.record(rid, "manual", "trim", "Trim Monday lunch", "labor_pct", today=t0)
    assert o["pay_key"] == models.labor_rates_key(rid)
    update_restaurant(rid, {"salaried_staff_json": json.dumps([{"name": "New Salaried", "annual": 90000}])})
    assert outcomes._pay_recosted(o)
    done = outcomes.evaluate(o["id"], today=today)
    assert done["verdict"] == "unknown" and done["after_detail"] == outcomes.PAY_RECOST_DETAIL
    assert not outcomes.counts_in_delivered(done) and not outcomes.result_counts(done)
    assert any(c.get("kind") == "pay_recost" for c in done["concurrent"])


def test_without_a_recost_the_tracker_is_judged_as_before(monkeypatch):
    import outcomes
    monkeypatch.setattr(outcomes, "_holidays_between", lambda s, e: {})
    rid, t0, today = _tracker_world()
    with models.salary_view(False):
        o = outcomes.record(rid, "manual", "trim", "Trim Monday lunch", "labor_pct", today=t0)
    done = outcomes.evaluate(o["id"], today=today)
    assert done["verdict"] not in ("unknown",) and done["after_detail"] != outcomes.PAY_RECOST_DETAIL
    sales = outcomes.record(rid, "manual", "sales", "Push the patio", "sales", today=t0, gate=None)
    assert sales["pay_key"] is None, "pay does not cost sales"


# ── P1: the Reviews read is kept per salary view and Food Cost view ────────

def test_the_reviews_read_scope_follows_the_login():
    import client_api
    import insight_store
    assert _in_request(OWNER, lambda: client_api.review_read_scope(OWNER)) == ""
    assert _in_request(MANAGER, lambda: client_api.review_read_scope(MANAGER)) == ":hourly:nofood"
    assert _in_request(MEMBER, lambda: client_api.review_read_scope(MEMBER)) == ":hourly"
    assert _in_request(SUPPORT_VIEW_AS, lambda: client_api.review_read_scope(SUPPORT_VIEW_AS)) == ":hourly"
    assert client_api.review_read_scope(None) == ":hourly:nofood", "no login named, off a request: the least"
    for kind in ("reviews:hourly", "reviews:hourly:nofood"):
        assert kind in insight_store._KIND_ACTIONS
    import ai_reads
    assert ai_reads.SURFACE_MODULE["review_read_team"] == "reviews"
    assert ai_reads.SURFACE_MODULE["review_read_hourly"] == "inventory", "it carries the waste dollars"
    assert "review_read" in ai_reads.SALARY_VIEW_SURFACES


def test_the_reviews_operational_context_is_hourly_and_food_free_for_a_manager():
    import review_intelligence as ri
    rid = _shift_world(target=10.0)
    with models.salary_view(False):
        team = ri.operational_context(rid, include_food=False)
    with models.salary_view(True):
        owner = ri.operational_context(rid)
    assert team["labor"]["hourly_only"] and team["labor"]["labor_pct"] == 3.2
    assert team["food_cost"] is None and team["waste"] is None
    line = str(ri._operational_lines(team)["labor"])
    assert "hourly staff only" in line and "target" in line and "against a" not in line
    assert not owner["labor"]["hourly_only"] and owner["labor"]["labor_pct"] > 10.0
    assert "against a 10.0% target" in str(ri._operational_lines(owner)["labor"])


def test_the_stored_review_diagnoses_read_hourly_and_no_food_even_in_the_owners_job(monkeypatch):
    import review_intelligence as ri
    rid = _rid("Diag Co")
    seen = {}

    def ctx(r, db_path=None, include_food=True):
        seen.update(sees=models.viewer_sees_salaries(), food=include_food)
        return {}
    monkeypatch.setattr(ri, "complaint_clusters", lambda *a, **k: [{"category": "service", "mentions": 4,
                                                                    "review_ids": [1, 2, 3, 4]}])
    monkeypatch.setattr(ri, "operational_context", ctx)
    import data_health
    monkeypatch.setattr(data_health, "unattended_readiness", lambda *a, **k: {"decision": "hold", "why": "test"})
    with models.salary_view(True):
        ri.diagnose(rid)
    assert seen == {"sees": False, "food": False}


def test_a_manager_and_the_owner_get_separate_reviews_reads_and_neither_carries_the_evidence(monkeypatch):
    """Owner GET then manager GET: the owner's all-in labor and the waste
    dollars never reach the manager's read, the two are kept under separate
    keys, and the operational evidence is never in either payload."""
    import ai_utils
    import auth
    import client_api
    import review_intelligence as ri
    from tests.test_edge_ai_reviews import _above_the_floor, _review
    db = models.DB_PATH
    rid = _rid("Read Co", module_reviews=1)
    _review(db, rid, processed=1, sentiment="negative")
    _above_the_floor(db, rid)
    calls = []

    def ctx(r, db_path=None, include_food=True):
        sees = models.viewer_sees_salaries()
        calls.append((sees, include_food))
        return {"labor": {"labor_pct": 13.0 if sees else 9.5, "hourly_only": not sees, "target_pct": 8.0,
                          "total_sales": 1, "period_days": 7, "overstaffed_days": 0, "understaffed_days": 1,
                          "covers_from": None, "covers_to": None, "age_days": None},
                "food_cost": ({"waste_cost_week": 987.65, "top_waste_item": "Salmon", "critical_low": 0}
                              if include_food else None),
                "waste": None, "marketing": None, "guests": None, "games": None, "notes": []}
    monkeypatch.setattr(ri, "operational_context", ctx)
    prompts = []

    class _Msg:
        content = [types.SimpleNamespace(type="text", text="Reviews are steady this week.")]
        stop_reason = "end_turn"
        usage = None

    def fake(*a, **k):
        prompts.append(json.dumps(k.get("messages") or a, default=str))
        return _Msg()
    monkeypatch.setattr(ai_utils, "create_with_retry", fake)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    web = app.test_client()
    client_api._insight_cache.clear()
    who = {}
    monkeypatch.setattr(auth, "get_current_user", lambda: dict(who))
    who.update(id=7, restaurant_id=rid, is_admin=0, username="owner", email="o@x.test", role="client")
    owner = web.get("/api/review-insight").get_data(as_text=True)
    who.update(id=8, username="mgr", email="m@x.test", role="manager")
    manager = web.get("/api/review-insight").get_data(as_text=True)
    assert calls == [(True, True), (False, False)], calls
    assert "operational_context" not in owner and "operational_context" not in manager
    assert "13.0" not in manager and "987.65" not in manager
    keys = [k for k in client_api._insight_cache if k.startswith(f"review-insight:{rid}")]
    assert sorted(keys) == sorted([f"review-insight:{rid}", f"review-insight:{rid}:hourly:nofood"]), keys
    assert len(prompts) == 2, "each view's read is its own model call"
    assert "13.0%" in prompts[0] and "987.65" in prompts[0]
    assert "13.0%" not in prompts[1] and "987.65" not in prompts[1] and "9.5%" in prompts[1]


# ── P3: support through view-as reads the manager's view ───────────────────

def test_support_through_view_as_gets_the_manager_dsr_and_no_salaried_staff():
    import strategy_routes
    from dsr import access
    rid = _rid("Support Co", module_labor=1)
    update_restaurant(rid, {"salaried_staff_json": SAL})
    support = dict(SUPPORT_VIEW_AS, restaurant_id=rid)
    owner = dict(OWNER, restaurant_id=rid)
    assert access.view_for(support) == access.MANAGER and access.view_for(owner) == access.OWNER
    t_support, _ = strategy_routes._do_targets_get(support)
    t_owner, _ = strategy_routes._do_targets_get(owner)
    assert t_owner["targets"]["salaried"] and not t_support["targets"]["salaried"]
    assert "300000" not in json.dumps(t_support)


def test_the_live_floor_read_carries_no_salary_for_a_manager():
    import labor_live
    rd = {"available": True, "sales": {"so_far": 1000.0, "projected": 5000.0},
          "labor": {"to_date": 200.0, "projected": 900.0, "salaried_day": 824.18},
          "pct": {"hourly_so_far": 20.0, "hourly_projected": 18.0, "all_in_projected": 34.5, "target": 30.0,
                  "target_label": "your target", "over_by": 4.5}}
    out = labor_live.redact_salaries(rd)
    blob = json.dumps(out)
    assert "824" not in blob and "34.5" not in blob and "4.5" not in blob
    assert out["pct"]["hourly_projected"] == 18.0 and "18% for hourly staff" in out["sentence"]


# ── P3: the staff-knowledge plan gate is a cohort ──────────────────────────

def test_the_staff_knowledge_plan_gate_can_be_turned_off_without_a_deploy(monkeypatch):
    import auth
    import rollout
    rid = _rid("Plan Co", module_labor=0, module_reviews=1)
    user = dict(OWNER, restaurant_id=rid)
    with Flask(__name__).test_request_context("/api/house-rules"):
        assert auth._module_blocked(user) is not None, "on by default: the Labor plan gates it"
        monkeypatch.setenv(rollout.env_name(auth.STAFF_KNOWLEDGE_GATE_FEATURE), "off")
        rollout._flag_cache.clear()
        assert auth._module_blocked(user) is None
        assert auth._module_blocked(user, path="/api/labor") is not None, "only the staff-knowledge paths"


# ── B4: the stored reads from before the per-view split are retired ────────

def test_the_reads_stored_before_the_split_are_renamed_once_never_served():
    import insight_store
    rid = _rid("Split Co")
    c = get_conn()
    c.execute("DELETE FROM data_migrations WHERE name=?", (insight_store.PRE_SPLIT_MIGRATION,))
    for kind in ("labor", "reviews", "marketing"):
        c.execute("INSERT INTO insight_cache (restaurant_id, kind, fingerprint, payload) VALUES (?,?,?,?)",
                  (rid, kind, "fp", json.dumps({"insight": kind})))
    c.commit()
    c.close()
    insight_store.init_insight_store()
    kinds = {r[0] for r in get_conn().execute("SELECT kind FROM insight_cache WHERE restaurant_id=?", (rid,))}
    assert kinds == {"labor:pre_split", "reviews:pre_split", "marketing"}
    assert insight_store.latest(rid, "labor")[0] is None
