"""Audit #25 workstream 3 — financial correctness (release-safety fix round).

One test (or a few) per finding, each citing its ID:
  A03-01 (F1/R3)  month-to-date prime cost carries the salaries the owner's
                  other labor figures carry; a manager's stays hourly
  A03-04 (F4/R24) the Bar payload on the restaurant's own date
  A03-08 (F8)     value delivered: the four figures, numerically, never summed
  A03-13 (F13)    the Labor (and Food) read kept per salary view
  A03-14 (F14)    the sales-consistency window on local dates; the same-basis
                  limit said in Data Health
  A03-09 (F9)     RPOWER's overtime pay readings recorded per store, raised
                  when unreadable; a synthetic payroll fixture's shape
Every "today" is fixed or the restaurant's own (never the process clock).
"""
import json
import sys
import threading
from datetime import date, datetime, timedelta

import pytest

import cogs
import food_cost_intelligence as fci
import models
from models import Restaurant, create_restaurant, get_conn, update_restaurant

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract


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


def _rid(name="WS3 Co", **kw):
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(name=name, owner_email="ws3@x.test", **kw))


# ── A03-01 (F1/R3): prime cost carries the salaries for the owner ──────────

PRIME_TODAY = date(2026, 10, 9)
NIGHTS = [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)]


def _prime_world(monkeypatch):
    """Three nights the nightly report measured — $1,000 of hourly labor on
    $5,000 of net each — and one salaried manager whose trading-day share
    is $300 (109,200 / 52 / 7)."""
    rid = _rid("Prime Salaries Co", module_inventory=1)
    update_restaurant(rid, {"salaried_staff_json": json.dumps([{"name": "Erin Manager", "annual": 109200}])})
    for d in NIGHTS:
        for metric, v in (("labor.cost", 1000.0), ("sales.net", 5000.0)):
            _x("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) VALUES (?,?,?,?,?)",
               (rid, d.isoformat(), metric, v, "ready"))
        _x("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, final) VALUES (?,?,?,?,1)",
           (rid, d.isoformat(), d.strftime("%A"), 5000.0))
    monkeypatch.setattr(fci, "_local_today", lambda r: PRIME_TODAY)
    monkeypatch.setattr(fci, "forecast_accuracy", lambda *a, **k: None)
    monkeypatch.setattr(cogs, "net_sales_in_window", lambda r, s, e: (15000.0, None))
    monkeypatch.setattr(cogs, "build_food_cost_pct", lambda r, days=28, db_path=None, today=None: {
        "ok": True, "cogs": 4500.0, "pct": 30.0, "target": 28.0, "window_days": days,
        "start": (today - timedelta(days=days - 1)).isoformat(), "end": today.isoformat()})
    # No shift analysis: the three measured nights are the whole month's labor.
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant", lambda r: None)
    return rid


def test_a03_01_owner_prime_cost_carries_the_salaried_day_share(monkeypatch):
    rid = _prime_world(monkeypatch)
    assert round(models.salaried_day_share(models.get_restaurant(rid)), 2) == 300.0
    with models.salary_view(True):
        out = fci.profitability_projection(rid)
    assert out["available"] and out["labor_basis"] == "measured" and out["labor_measured_nights"] == 3
    assert out["labor_cost_mtd"] == 3900.0                    # 3 x ($1,000 + $300)
    assert out["prime_cost_pct"] == round((4500.0 + 3900.0) / 15000.0 * 100, 1)
    assert out["labor_pct_mtd"] == out["labor_pct"] == 26.0
    assert out["labor_includes_salaries"] and out["labor_salary_basis"] == "all_in"
    assert "salaries included" in out["labor_basis_text"] and "salaries included" in out["basis"]
    # The nightly report's owner labor % for the same nights, all-in.
    from dsr import kpis
    hist = kpis._history("labor_pct", rid, NIGHTS[-1] + timedelta(days=1), None, salaried_share=300.0)
    assert all(hist[d.isoformat()] == 26.0 for d in NIGHTS)
    # The model's prompt names the figure it adds up from and its basis.
    block = fci._profit_block(out)
    assert "labor 26.0%, salaries included" in block


def test_a03_01_a_managers_prime_cost_is_hourly_and_names_no_salary(monkeypatch):
    rid = _prime_world(monkeypatch)
    with models.salary_view(False):
        out = fci.profitability_projection(rid)
    assert out["labor_cost_mtd"] == 3000.0 and out["labor_pct_mtd"] == out["labor_pct"] == 20.0
    assert not out["labor_includes_salaries"] and out["labor_salary_basis"] == "hourly"
    assert "hourly staff only" in out["labor_basis_text"]
    blob = json.dumps(out)
    assert "3900" not in blob and "salaries included" not in blob


def test_a03_01_the_nightly_job_reads_the_owners_all_in_figure(monkeypatch):
    # Off a request (the nightly forecast freeze, the morning brief's
    # owner build): the owner's basis, as every other job's labor read.
    rid = _prime_world(monkeypatch)
    assert fci.profitability_projection(rid, withhold=False)["labor_cost_mtd"] == 3900.0


def test_a03_01_the_shared_food_diagnosis_reads_the_team_basis(monkeypatch):
    # One diagnosis per restaurant, shown to every Food Cost login: its
    # evidence is read without the salaries.
    rid = _prime_world(monkeypatch)
    seen = {}

    def fake_evidence(r, db_path=None):
        seen["sees"] = models.viewer_sees_salaries()
        return {"drivers": {"drivers": [], "reason": "none"}}
    monkeypatch.setattr(fci, "build_evidence", fake_evidence)
    monkeypatch.setattr(fci, "retire_diagnosis", lambda *a, **k: None)
    with models.salary_view(True):                          # even when the owner's job asks
        plan = fci.diagnosis_plan(rid)
    assert plan["kind"] == "done"
    assert seen["sees"] is False


# ── A03-04 (F4/R24): the Bar payload on the restaurant's own date ──────────

def test_a03_04_home_and_the_bar_page_build_one_payload_on_the_local_date(monkeypatch):
    import bar.cache as bcache
    import bar.build
    import bar_signals
    import mobile_api
    import time_utils
    rid = _rid("Bar Date Co", module_inventory=1)
    local = datetime(2026, 10, 20, 20, 30)                  # 8:30pm Chicago: 1:30am UTC on 10/21

    class _UtcHostDate(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 21)                       # what a UTC host's clock says
    for mod in ("bar.cache", "bar.build", "bar.feed", "bar.mapping", "bar.variance", "bar.counts", "bar.waste"):
        monkeypatch.setattr(sys.modules[mod] if mod in sys.modules else __import__(mod, fromlist=["x"]),
                            "date", _UtcHostDate, raising=False)
    real_now = time_utils.restaurant_now_by_id
    monkeypatch.setattr(time_utils, "restaurant_now_by_id",
                        lambda r, naive=False: local if int(r) == rid else real_now(r, naive=naive))
    builds = []

    def fake_build(restaurant_id, db_path=None, today=None):
        builds.append(today)
        return {"ok": True, "available": False, "today": str(today)}
    monkeypatch.setattr(bar.build, "build", fake_build)
    bcache.clear()
    home = bar_signals.payload(rid)                         # Home asks with no date
    page = bcache.full(rid, today=mobile_api._bar_count_today(rid))
    assert builds == [date(2026, 10, 20)], "one build, on the restaurant's date — never UTC tomorrow"
    assert home == page
    assert bcache.stamp(rid) != bcache.stamp(rid, today=date(2026, 10, 21))
    assert bcache.stamp(rid, today=date(2026, 10, 20)) == bcache.stamp(rid, today=mobile_api._bar_count_today(rid))
    # The other Bar reads default to the same local day.
    from bar import feed
    assert feed._today(None, rid) == date(2026, 10, 20)
    bcache.clear()


# ── A03-08 (F8): value delivered, numerically ───────────────────────────────

def test_a03_08_the_headline_is_the_measured_figure_alone(monkeypatch):
    import data_freshness
    import inventory
    import value_delivered as vd
    rid = _rid("Value Golden Co", module_reviews=1, module_inventory=1, module_labor=0, module_marketing=0)
    # One measured win: $400 a month.
    _x("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, baseline_value, "
       "started_on, evaluate_on, after_value, verdict, dollars_monthly, status) "
       "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'evaluated')",
       (rid, "test", "k:prep", "Cut prep waste", "weekly_waste", 300.0, "2026-08-01", "2026-09-01", 200.0,
        "improved", 400.0))
    # Avoided: thirty replies posted at the stated $5 = $150.
    from models import Review, save_reviews
    save_reviews([Review(restaurant_id=rid, platform="google", external_id=f"r{i}", author="A", rating=5,
                         text="Great") for i in range(30)])
    _x("UPDATE reviews SET response_status='approved', processed=1 WHERE restaurant_id=?", (rid,))
    # Opportunity: $900 a month of waste above tolerance, on current data.
    monkeypatch.setattr(inventory, "analysis_for", lambda r, **k: ([], True, {"recoverable_monthly": 900.0}))
    monkeypatch.setattr(data_freshness, "source_state", lambda *a, **k: {
        "state": "current", "key": "inventory", "as_of": "10/1/26", "as_of_iso": "2026-10-01", "pct": 100})
    # Surfaced: one waste alert carrying $200.
    _x("INSERT INTO alert_log (restaurant_id, alert_type, value) VALUES (?,?,?)", (rid, "food_waste", 200.0))

    assert vd.compute_total_value_delivered(rid) == 400
    b = vd.breakdown(rid)
    assert (b["delivered"]["monthly"], b["delivered"]["net_monthly"]) == (400.0, 400.0)
    assert b["avoided"]["dollars"] == 150.0
    assert b["opportunity"]["monthly"] == 900.0
    assert b["surfaced"]["dollars"] == 200.0
    h = vd.headline(rid)
    assert (h["monthly"], h["net_monthly"]) == (400, 400)
    assert vd.home_block(h)["total"] == 400
    # Never one figure folded into another: not 400+150, 400+900, or all four.
    assert vd.compute_total_value_delivered(rid) not in (550, 1300, 1650)


# ── A03-13 (F13): the Labor read per salary view ────────────────────────────

def test_a03_13_owner_and_manager_labor_reads_do_not_evict_each_other(monkeypatch):
    import labor
    calls = []
    monkeypatch.setattr(labor, "get_claude_insights", lambda a, **k: calls.append(a["overall_labor_pct"]) or
                        f"read at {a['overall_labor_pct']}%")
    monkeypatch.setattr(labor, "_note_local_day", lambda rid: "2026-10-20")
    owner_a = {"period": "p", "date_range": {"end": "2026-10-19"}, "overall_labor_pct": 41.0}
    mgr_a = dict(owner_a, overall_labor_pct=28.0)
    for _ in range(3):
        with models.salary_view(True):
            assert labor.labor_note(11, owner_a) == "read at 41.0%"
        with models.salary_view(False):
            assert labor.labor_note(11, mgr_a) == "read at 28.0%"
    assert calls == [41.0, 28.0], "one read per view, not one per alternate open"


def test_a03_13_the_route_cache_and_the_stale_read_are_per_view():
    import client_api
    import insight_store
    a = {"period": "p", "date_range": {"end": "2026-10-19"}, "overall_labor_pct": 41.0}
    with models.salary_view(True):
        client_api.labor_cache_put(12, a, "owner read: labor with salaries 41%")
        assert client_api.labor_cached_read(12, a).startswith("owner read")
        assert client_api.labor_refresh_key() is None
    with models.salary_view(False):
        assert client_api.labor_cached_read(12, a) is None, "a manager never gets the owner's cached read"
        assert client_api.labor_stale_read(12) is None
        assert client_api.labor_refresh_key() == ":hourly"
        client_api.labor_cache_put(12, dict(a, overall_labor_pct=28.0), "manager read: 28%")
        assert client_api.labor_stale_read(12)["text"] == "manager read: 28%"
    with models.salary_view(True):
        assert client_api.labor_stale_read(12)["text"].startswith("owner read")
    assert insight_store.salary_kind("reviews") == "reviews"
    with models.salary_view(False):
        assert insight_store.salary_kind("labor") == "labor:hourly"
        assert insight_store.salary_kind("food") == "food:hourly"
    client_api._insight_cache.clear()


def test_a03_13_ask_never_serves_a_manager_the_owners_stored_reads():
    import ai_reads
    import ask_cavnar_tools
    import insight_store
    rid = _rid("Ask Reads Co")
    insight_store.put(rid, "labor", "fp-o", "OWNER labor read: 41% with salaries")
    insight_store.put(rid, "labor:hourly", "fp-m", "TEAM labor read: 28% hourly")
    insight_store.put(rid, "food", "fp-of", "OWNER food read: prime cost 64%")
    with models.salary_view(False):
        out = json.dumps(ask_cavnar_tools._read_recent_reads(rid))
    assert "TEAM labor read" in out
    assert "OWNER" not in out, "neither the owner's labor read nor its food read (prime cost) reaches a manager"
    with models.salary_view(True):
        out = json.dumps(ask_cavnar_tools._read_recent_reads(rid))
    assert "OWNER labor read" in out and "OWNER food read" in out
    assert ai_reads.STORE_SURFACE["labor:hourly"] == "labor_read_hourly"
    assert ai_reads.SURFACE_MODULE["labor_read_hourly"] == "labor"


# ── A03-14 (F14): local-date window; the same-basis limit in Data Health ───

def _final_night(rid, day, dsr_net, pos_net):
    rep = _x("INSERT INTO dsr_reports (restaurant_id, business_date, version, status) VALUES (?,?,1,'final')",
             (rid, day.isoformat()))
    _x("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, report_id) "
       "VALUES (?,?,?,?,?,?)", (rid, day.isoformat(), "sales.net", dsr_net, "ready", rep))
    _x("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, final) VALUES (?,?,?,?,1)",
       (rid, day.isoformat(), day.strftime("%A"), pos_net))


def test_a03_14_the_window_is_business_dates_ending_on_the_restaurants_date():
    import data_freshness as df
    rid = _rid("Consistency Co")
    today = date(2026, 10, 20)
    _final_night(rid, today - timedelta(days=3), 5000.0, 4200.0)      # inside 14 days
    _final_night(rid, today - timedelta(days=20), 5000.0, 4200.0)     # outside
    out = df.sales_consistency(rid, today=today)
    assert out["checked"] == 1 and [m["date"] for m in out["mismatches"]] == ["2026-10-17"]
    # With no date given, the restaurant's own (never the server's UTC day).
    from time_utils import restaurant_now_by_id
    local = restaurant_now_by_id(rid).date()
    _final_night(rid, local - timedelta(days=1), 3000.0, 2000.0)
    assert (local - timedelta(days=1)).isoformat() in [m["date"] for m in df.sales_consistency(rid)["mismatches"]]


def test_a03_14_data_health_says_a_non_same_basis_pos_is_reported_not_rerun(monkeypatch):
    import data_freshness as df
    import pos
    rid = _rid("Basis Co")
    r = models.get_restaurant(rid)
    monkeypatch.setattr(pos, "connected_provider", lambda x: ("toast", object()))
    assert df.same_basis_recheck(r) == "reported_only"
    from time_utils import restaurant_now_by_id
    local = restaurant_now_by_id(rid).date()
    _x("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, final) VALUES (?,?,?,?,1)",
       (rid, (local - timedelta(days=1)).isoformat(), "x", 4000.0))
    st = df.source_state(r, "sales")
    assert st.get("recheck") == "reported_only" and df.SALES_RECHECK_REPORTED_ONLY in st["basis"]
    monkeypatch.setattr(pos, "connected_provider", lambda x: ("rpower", object()))
    assert df.same_basis_recheck(r) == "rerun"
    assert df.SALES_RECHECK_REPORTED_ONLY not in df.source_state(r, "sales")["basis"]
    monkeypatch.setattr(pos, "connected_provider", lambda x: (None, None))
    assert df.same_basis_recheck(r) is None


# ── A03-09 (F9): RPOWER's overtime pay, read and recorded ──────────────────

def _punch(emp, day, reg=8.0, ot=2.0, reg_rate=20.0, **pay):
    return dict({"emp_mid": emp, "job_mid": "J1", "in_dttm": f"{day}T08:00:00", "out_dttm": f"{day}T18:00:00",
                 "reg_hours": reg, "ot_hours": ot, "dt_hours": 0, "reg_rate": reg_rate}, **pay)


def test_a03_09_each_punch_says_how_its_overtime_pay_was_read():
    import rpower
    assert rpower._ot_reading(_punch("E", "2026-09-14", ot=0)) == (0.0, "no_ot", None)
    assert rpower._ot_reading(_punch("E", "2026-09-14", reg_rate=0)) == (None, "no_rate", None)
    assert rpower._ot_reading(_punch("E", "2026-09-14", ot_rate=30.0)) == (20.0, "rate", None)
    assert rpower._ot_reading(_punch("E", "2026-09-14", ot_pay=60.0)) == (20.0, "whole_pay", 1.5)
    assert rpower._ot_reading(_punch("E", "2026-09-14", ot_pay=20.0)) == (20.0, "premium_only", 0.5)
    p, band, ratio = rpower._ot_reading(_punch("E", "2026-09-14", ot_pay=40.0))
    assert (p, band, ratio) == (None, "unreadable", 1.0)
    assert rpower._ot_reading(_punch("E", "2026-09-14"))[1] == "no_pay"
    # _ot_premium is the reading's premium, unchanged.
    for e in (_punch("E", "2026-09-14", ot_pay=60.0), _punch("E", "2026-09-14", ot_pay=40.0)):
        assert rpower._ot_premium(e) == rpower._ot_reading(e)[0]


def test_a03_09_the_distribution_is_kept_and_an_unreadable_store_is_raised_once():
    import ops
    import rpower
    rid = _rid("OT Bands Co")
    entries = [_punch(f"E{i}", "2026-09-14", ot_pay=60.0) for i in range(4)] + \
              [_punch("U1", "2026-09-14", ot_pay=40.0), _punch("U2", "2026-09-15", ot_pay=44.0)] + \
              [_punch("R", "2026-09-14", ot_rate=30.0), _punch("N", "2026-09-14", ot=0)]
    bands = rpower.ot_pay_bands(entries)
    assert bands["punches"] == 7 and bands["pay_read"] == 6
    assert bands["bands"] == {"rate": 1, "unreadable": 2, "whole_pay": 4}
    assert bands["unreadable_share"] == round(2 / 6, 3) and bands["ratios"] == [1.0, 1.1, 1.5, 1.5, 1.5, 1.5]
    ops.init_ops()
    assert rpower.record_ot_pay_bands(rid, bands, date(2026, 9, 15)) is True
    assert rpower.record_ot_pay_bands(rid, bands, date(2026, 9, 15)) is False, "once a day"
    c = get_conn()
    try:
        kept = json.loads(c.execute("SELECT value FROM job_cursors WHERE key=?",
                                    (rpower.OT_BANDS_CURSOR + str(rid),)).fetchone()[0])
        found = c.execute("SELECT kind, error FROM job_failures WHERE job='rpower_ot_pay' AND restaurant_id=?",
                          (rid,)).fetchall()
    finally:
        c.close()
    assert kept["unreadable_share"] == bands["unreadable_share"] and kept["day"] == "2026-09-15"
    assert len(found) == 1 and found[0][0] == "data_quality" and "unreadable on 2 of 6" in found[0][1]
    # A store whose pay reads cleanly is kept, never raised.
    clean = rpower.ot_pay_bands([_punch(f"E{i}", "2026-09-14", ot_pay=60.0) for i in range(6)])
    assert clean["unreadable_share"] == 0.0 and rpower.record_ot_pay_bands(rid, clean, date(2026, 9, 16)) is False


# A golden payroll fixture's SHAPE, with synthetic figures (the repo is
# public: a real week's punches and Erik's payroll report never go in it).
# A real check fills the same shape privately once RPOWER confirms what
# ot_pay means (WILL-STEP, audit #25 F9).
PAYROLL_WEEK = {
    "store": "synthetic", "week": ["2026-09-14", "2026-09-20"],
    "people": {"employees": {"E1": "Ana Line", "E2": "Ben Grill", "E3": "Cal Prep"}, "jobs": {"J1": "Line Cook"}},
    "punches": [
        _punch("E1", "2026-09-18", reg=8, ot=2, reg_rate=18.0, ot_rate=27.0),      # the rates say it
        _punch("E2", "2026-09-18", reg=8, ot=2, reg_rate=20.0, ot_pay=60.0),       # ot_pay = the whole OT pay
        _punch("E3", "2026-09-18", reg=8, ot=2, reg_rate=16.0, ot_pay=16.0),       # ot_pay = the premium only
    ],
    # What payroll paid: straight time on every hour, 1.5x on the OT hours.
    "payroll": {"ot_premium": 2 * 9.0 + 2 * 10.0 + 2 * 8.0,
                "gross": 10 * 18.0 + 2 * 9.0 + 10 * 20.0 + 2 * 10.0 + 10 * 16.0 + 2 * 8.0},
}


def test_a03_09_the_payroll_fixture_reconciles_within_a_dollar():
    import labor
    import rpower
    w = PAYROLL_WEEK
    rows = rpower.normalise_entries(w["punches"], {}, w["people"])
    a = labor.analyse_shifts(rows, hourly_rate=15.0, labor_target=30.0)
    assert abs(a["overtime_premium"] - w["payroll"]["ot_premium"]) <= 1.0
    assert abs(a["total_labor_cost"] - w["payroll"]["gross"]) <= 1.0
    assert rpower.ot_pay_bands(w["punches"])["unreadable_share"] == 0.0
