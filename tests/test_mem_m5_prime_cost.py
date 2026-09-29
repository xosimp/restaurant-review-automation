"""Memory fix round M5, prime_cost (memory audit 9/29/26, CROSS-10) and the
owner's goals as the nightly report's targets (the lead's note: target_for).

The report said prime cost was 58% last night and the brief said 61.2% month
to date, and neither said why: the month-to-date projection applied the
shift analysis's labor SHARE to the month's sales while the report measured
each night's labor DOLLARS. When the report runs, the projection now sums
those measured dollars and falls back to the share only for nights it did
not measure, and both surfaces name their basis.
"""
from datetime import date, timedelta

import pytest

import cogs
import food_cost_intelligence as fci
import models
from models import Restaurant, create_restaurant

TODAY = date(2026, 9, 21)


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(fci, "_local_today", lambda rid: TODAY)
    monkeypatch.setattr(fci, "forecast_accuracy", lambda *a, **k: None)
    monkeypatch.setattr(cogs, "net_sales_in_window", lambda r, s, e: (40000.0, None))
    monkeypatch.setattr(cogs, "build_food_cost_pct", lambda r, days=28, db_path=None, today=None: {
        "ok": True, "cogs": 12000.0, "pct": 30.0, "target": 28.0})
    yield


def _rid():
    return create_restaurant(Restaurant(name="Prime Co", owner_email="prime@x.test", module_inventory=1))


def _analysis(monkeypatch, pct=30.0, live=True):
    end = (TODAY - timedelta(days=1)).isoformat()
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant", lambda r: (
        {"is_live": True, "total_sales": 40000.0, "overall_labor_pct": pct,
         "date_range": {"start": "2026-08-25", "end": end, "days": 28}} if live else None))
    monkeypatch.setattr(fci, "_labor_stale_why", lambda *a, **k: None)


def _night(rid, d, labor_cost, archive_sales):
    conn = models.get_conn()
    conn.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) VALUES (?,?,?,?,?)",
                 (rid, d.isoformat(), "labor.cost", labor_cost, "ready"))
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, final) VALUES (?,?,?,?,1)",
                 (rid, d.isoformat(), d.strftime("%A"), archive_sales))
    conn.commit()
    conn.close()


def test_measured_nightly_labor_replaces_the_share_where_the_report_ran(monkeypatch):
    rid = _rid()
    _analysis(monkeypatch, pct=30.0)
    # Twenty nights so far; the report measured ten: $1,000 of labor on
    # $2,000 of sales each (50%), well off the 30% share.
    for k in range(1, 11):
        _night(rid, TODAY - timedelta(days=k), 1000.0, 2000.0)
    out = fci.profitability_projection(rid)
    assert out["available"] and out["labor_basis"] == "mixed" and out["labor_measured_nights"] == 10
    # $10,000 measured + 30% of the other $20,000 of sales = $16,000.
    assert out["labor_cost_mtd"] == 16000.0 and out["prime_cost_pct"] == 70.0
    assert "labor measured on 10 nights by your nightly reports" in out["labor_basis_text"]
    assert "measured labor dollars on 10 nights" in out["basis"] and "only to the other nights" in out["basis"]
    assert "estimated recipe cost" in out["basis"]              # why the report's own figure differs


def test_every_night_measured_needs_no_labor_share(monkeypatch):
    rid = _rid()
    _analysis(monkeypatch, live=False)                          # no shift analysis at all
    for k in range(1, 21):
        _night(rid, TODAY - timedelta(days=k), 600.0, 2000.0)
    out = fci.profitability_projection(rid)
    assert out["available"] and out["labor_basis"] == "measured"
    assert out["labor_cost_mtd"] == 12000.0 and out["labor_pct_mtd"] == 30.0
    assert "labor measured every night" in out["labor_basis_text"]


def test_without_the_report_the_share_is_said_as_before(monkeypatch):
    rid = _rid()
    _analysis(monkeypatch, pct=30.0)
    out = fci.profitability_projection(rid)
    assert out["labor_basis"] == "analysis_share" and out["labor_cost_mtd"] == 12000.0
    assert "not a labor measurement of these specific days" in out["basis"]


def test_the_brief_names_the_basis_it_rests_on():
    import morning_brief
    stamp = morning_brief._prime_stamp({"labor_basis": "mixed", "labor_period": "8/25/26 to 9/20/26",
                                        "labor_basis_text": "labor measured on 10 nights by your nightly reports, "
                                                            "labor from 8/25/26–9/20/26 for the rest"}, {})
    assert "labor measured on 10 nights" in stamp and "labor share from" not in stamp
    assert "labor share from 8/25/26" in morning_brief._prime_stamp(
        {"labor_basis": "analysis_share", "labor_period": "8/25/26 to 9/20/26"}, {})


# ── the owner's goals as the report's targets ───────────────────────────────

def test_the_prime_cost_kpi_takes_the_owners_goal(monkeypatch):
    import owner_memory
    from dsr import kpis
    rid = _rid()
    r = models.get_restaurant(rid)
    assert kpis._target("prime_pct", r) is None                    # no goal: no target of Cavnar AI's own
    monkeypatch.setattr(owner_memory, "target_for", lambda rid_, metric, db_path=None: (
        {"value": 58.0, "source": "goal", "goal_id": 7, "until": date(2026, 12, 1)}
        if metric == "prime_cost_pct" else None))
    t = kpis._target("prime_pct", r)
    assert t["value"] == 58.0 and t["source"] == "goal" and t["label"] == "Your goal by 12/1/26"


def test_a_nightly_sales_goal_is_the_nights_target_when_no_budget_is_set(monkeypatch):
    import owner_memory
    from dsr import store
    rid = _rid()
    day = date(2026, 9, 22)
    assert store.night_budget(rid, day) == {}
    monkeypatch.setattr(owner_memory, "target_for", lambda rid_, metric, db_path=None: (
        {"value": 9000.0, "source": "goal", "goal_id": 3, "until": None} if metric == "nightly_sales" else None))
    b = store.night_budget(rid, day)
    assert b["net"] == 9000.0 and b["source"] == "goal" and b["label"] == "Your goal of $9,000 a night"
    store.set_budget(rid, day, gross=11000.0, net=10000.0)
    b = store.night_budget(rid, day)
    assert (b["net"], b["source"], b["label"]) == (10000.0, "budget", "Budget")    # the owner's budget wins
