"""Labor period history is calendar payroll weeks derived from the daily
history, replaced in place, compared only back to back on the same costing
(memory audit 9/29/26, labor_periods).

Production: Simple EJ's 9/1 period was saved three times on its first day,
the demo's 8/31 period 38 times; two periods were recosted by a point or
more and five windows overlapped. The same 14 days recosted from 31.1% to
29.5% came back three seconds later as "Labor's down 1.6 points from last
upload", and each daily slide of the window opened a new labor_over episode.
"""
from datetime import date, timedelta

import pytest

import ask_cavnar_tools
import models
from models import Restaurant, create_restaurant

TODAY = date(2026, 9, 29)          # a Tuesday


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_restaurant_today", lambda rid: TODAY)
    yield


def _rid(**kw):
    fields = dict(name="Periods Co", owner_email="p@x.test", module_labor=1, week_start_day=0)
    fields.update(kw)
    return create_restaurant(Restaurant(**fields))


def _days(start, n, labor=300.0, sales=1000.0):
    return {(start + timedelta(days=i)).isoformat(): {"sales": sales, "labor_cost": labor, "actual": 20}
            for i in range(n)}


def _rows(rid, where=""):
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM labor_history WHERE restaurant_id=? {where} ORDER BY period_start", (rid,)).fetchall()]
    finally:
        conn.close()


def test_periods_are_calendar_weeks_that_never_overlap():
    rid = _rid()
    models.save_labor_daily_history(rid, _days(date(2026, 9, 7), 23))       # Mon 9/7 .. Tue 9/29
    assert models.refresh_labor_periods(rid, today=TODAY) == 4
    weeks = models.get_labor_history(rid, limit=8)
    assert [w["period_start"] for w in weeks] == ["2026-09-28", "2026-09-21", "2026-09-14", "2026-09-07"]
    assert all(w["kind"] == models.LABOR_PERIOD_WEEK for w in weeks)
    # Back to back, no overlap; the week in progress runs to its last day with figures.
    assert [w["period_end"] for w in weeks] == ["2026-09-29", "2026-09-27", "2026-09-20", "2026-09-13"]
    assert [w["complete"] for w in weeks] == [False, True, True, True]
    assert weeks[1]["labor_pct"] == 30.0 and weeks[1]["days"] == 7


def test_the_owners_payroll_week_sets_the_boundaries():
    rid = _rid()
    models.update_restaurant(rid, {"week_start_day": 2})                     # Wednesday
    models.save_labor_daily_history(rid, _days(date(2026, 9, 9), 14))
    models.refresh_labor_periods(rid, today=TODAY)
    assert [w["period_start"] for w in models.get_labor_history(rid, limit=3)] == ["2026-09-16", "2026-09-09"]


def test_a_recost_replaces_the_week_in_place_and_says_what_it_was():
    rid = _rid()
    models.save_labor_daily_history(rid, _days(date(2026, 9, 14), 14, labor=311.0))
    models.refresh_labor_periods(rid, today=TODAY)
    before = _rows(rid)
    # A costing input changes (a role rate), and the same days are re-costed.
    models.update_restaurant(rid, {"hourly_rate": 24.0})
    models.save_labor_daily_history(rid, _days(date(2026, 9, 14), 14, labor=295.0))
    models.refresh_labor_periods(rid, today=TODAY)
    after = _rows(rid)
    assert len(after) == len(before) == 2, "a recost never appends a period"
    assert after[0]["labor_pct"] == 29.5 and after[0]["recosted_from"] == 31.1
    # Both weeks are on the new costing now, so they compare with each other.
    assert models.labor_period_change(rid)["comparable"] is True


def test_weeks_costed_on_different_bases_are_recosted_not_comparable():
    rid = _rid()
    models.save_labor_daily_history(rid, _days(date(2026, 9, 14), 14))
    conn = models.get_conn()
    conn.execute("UPDATE labor_daily_history SET basis='old:upload' WHERE restaurant_id=? AND date < '2026-09-21'",
                 (rid,))
    conn.commit()
    conn.close()
    models.refresh_labor_periods(rid, today=TODAY)
    change = models.labor_period_change(rid)
    assert change["comparable"] is False and change["reason"] == "recosted" and change["delta"] is None


def test_the_week_in_progress_is_never_compared():
    rid = _rid()
    models.save_labor_daily_history(rid, _days(date(2026, 9, 21), 9))      # one full week + two days
    models.refresh_labor_periods(rid, today=TODAY)
    change = models.labor_period_change(rid)
    assert change["reason"] == "partial" and change["delta"] is None


def test_provisional_pos_days_are_left_out():
    rid = _rid()
    models.save_labor_daily_history(rid, _days(date(2026, 9, 21), 7))
    conn = models.get_conn()
    conn.execute("UPDATE labor_daily_history SET final=0, labor_cost=900 WHERE restaurant_id=? AND date='2026-09-27'",
                 (rid,))
    conn.commit()
    conn.close()
    models.refresh_labor_periods(rid, today=TODAY)
    week = models.get_labor_history(rid, limit=1)[0]
    assert week["days"] == 6 and week["labor_pct"] == 30.0


def test_legacy_rolling_windows_are_marked_at_boot_and_never_read():
    rid = _rid()
    conn = models.get_conn()
    for start, end, pct in (("2026-09-11", "2026-09-24", 31.0), ("2026-09-12", "2026-09-25", 18.4),
                            ("2026-09-14", "2026-09-27", 31.1)):
        conn.execute("INSERT INTO labor_history (restaurant_id, period_start, period_end, labor_pct, total_labor, "
                     "total_sales) VALUES (?,?,?,?,?,?)", (rid, start, end, pct, pct * 100, 10000))
    conn.commit()
    conn.close()
    models.save_labor_daily_history(rid, _days(date(2026, 9, 14), 14))
    models.backfill_labor_periods()
    assert {r["kind"] for r in _rows(rid, "AND period_start IN ('2026-09-11','2026-09-12')")} == {"rolling_window"}
    assert [w["period_start"] for w in models.get_labor_history(rid, limit=8)] == ["2026-09-21", "2026-09-14"]


def test_an_explicitly_stored_period_is_replaced_in_place_too():
    rid = _rid()
    models.save_labor_snapshot(rid, "2026-09-14", "2026-09-27", 31.1, 3371.7, 10840.0, basis="a:toast")
    models.save_labor_snapshot(rid, "2026-09-14", "2026-09-27", 29.5, 3198.9, 10840.0, basis="b:toast")
    rows = _rows(rid)
    assert len(rows) == 1 and rows[0]["labor_pct"] == 29.5 and rows[0]["recosted_from"] == 31.1


def test_asks_labor_detail_reads_the_fields_the_history_has():
    rid = _rid()
    models.save_labor_daily_history(rid, _days(date(2026, 9, 14), 14))
    models.refresh_labor_periods(rid, today=TODAY)
    out = ask_cavnar_tools._read_labor_detail(rid, weeks=4)
    last = out["weekly_trend"][-1]
    assert last["labor_cost"] == 2100.0 and last["sales"] == 7000.0
    assert last["complete"] is True


def test_the_rpower_sync_archives_the_days_once_and_writes_no_window(monkeypatch):
    import pos
    import rpower
    rid = _rid()
    csv_text = "date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes,pay_rate\n" + \
        "".join(f"2026-09-{d:02d},X,Ana B.,Server,16:00,22:00,6,6,1000,,20\n" for d in range(14, 28))
    monkeypatch.setattr(rpower, "build_shifts_csv", lambda r, days=60: csv_text)
    monkeypatch.setattr(pos, "_complete_through_for", lambda r: date(2026, 9, 28))
    calls = []
    real = models.save_labor_daily_history
    monkeypatch.setattr(models, "save_labor_daily_history", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    import client_api
    monkeypatch.setattr(client_api, "invalidate_insight_cache", lambda r: None)
    out = rpower.sync_to_db(rid)
    assert out["ok"] is True
    assert len(calls) == 1, "the per-day archive is written once, with the pull's provenance"
    kinds = {r["kind"] for r in _rows(rid)}
    assert kinds == {models.LABOR_PERIOD_WEEK}


def test_the_labor_read_states_a_trend_only_week_on_week(monkeypatch):
    import labor
    import types
    rid = _rid()
    models.save_labor_daily_history(rid, {**_days(date(2026, 9, 14), 7, labor=300.0),
                                          **_days(date(2026, 9, 21), 7, labor=330.0)})
    models.refresh_labor_periods(rid, today=TODAY)
    seen = {}

    def fake(*a, **kw):
        seen["prompt"] = kw["messages"][0]["content"]
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="Hi, labor ran 33%.")],
                                     stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(models, "other_tenant_names", lambda rid, db_path=None: set())
    a = {"is_live": True, "total_sales": 14000.0, "total_labor_cost": 4410.0, "overall_labor_pct": 31.5,
         "labor_target": 30, "period_days": 14, "potential_savings": 210.0, "potential_savings_weekly": 105.0,
         "potential_savings_monthly": 455.0, "dow_summary": {}, "overstaffed_days": [], "understaffed_days": [],
         "overtime_risk": [], "role_summary": {},
         "date_range": {"start": "2026-09-14", "end": "2026-09-27", "days": 14}}
    labor.get_claude_insights(a, restaurant_name="R", owner_name="Sam", restaurant_id=rid)
    assert "TREND: Labor % is UP 3.0 points week on week" in seen["prompt"]
    assert "from last upload" not in seen["prompt"]
    assert models.get_labor_history(rid, limit=5)[0]["kind"] == models.LABOR_PERIOD_WEEK
    assert len(_rows(rid)) == 2, "building the note writes no period"
