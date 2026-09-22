"""Edge cases of the schedule's money side (SCHED audit, economics scope).

Overtime priced against the wrong payroll week, daily overtime never
priced, a trim that zeroes a part-timer or keeps a no-show row while it
removes a legal one, and the weekly revenue median on thin or stale sales.

The generation job runs for real with the model stubbed out
(_build_schedule_result returns a fixed draft), so these check the figure
the owner is actually shown, not a helper called the way the test hopes
the engine calls it.
"""
import datetime as dt
import json
import sys

import pytest

# Imported here, before any fixture patches models.get_conn, so no module is
# first imported mid-test and left holding a redirect to a deleted database.
import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import labor_replacements  # noqa: F401

import models
import schedule_economics as econ
import schedule_engine as se
import schedule_rules as sr
from models import create_restaurant, Restaurant

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
PREV = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]
DAYS = list(sr.DAYS)
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        # The real one, or a redirect some earlier test left bound in a
        # module it imported for the first time mid-test.
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    return db_path


def _restaurant(db_path, people=("Ana",), **cols):
    rid = create_restaurant(Restaurant(name="Money Co", owner_email="m@x.com"), db_path=db_path)
    if cols:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
        conn.commit()
        conn.close()
    for n in people:
        models.add_manual_team_member(rid, n, role="Server", db_path=db_path)
    return rid


def _text(rows):
    return HEADER + "\n" + "\n".join(",".join(str(x) for x in r) for r in rows)


def _publish(db_path, rid, dates, rows):
    hid = models.save_schedule_history(rid, dates[0], dates[-1], 0, 0, 30, _text(rows), [], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.commit()
    conn.close()


def _run_job(monkeypatch, rid, rows, **extra):
    base = {"ok": True, "schedule_csv": _text(rows), "week_dates": list(WEEK), "week_days": DAYS, "summary": [],
            "hours_budget": 0, "daily_target_hours": {}, "labor_target": 30, "blended_rate": 20.0}
    base.update(extra)
    monkeypatch.setattr(se, "_build_schedule_result", lambda r, week_start=None: dict(base))
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job",
                        lambda job_id, status, result: finished.update(status=status, result=result))
    se._run_schedule_job("edge-econ", rid)
    assert finished.get("status") == "done", finished
    return finished["result"]


def _six(d, emp="Ana", start="10:00am", end="4:00pm"):
    return (d, dt.date.fromisoformat(d).strftime("%A"), emp, "Server", start, end, 6, "")


# ── SCHED-7: overtime priced per payroll bucket ──────────────────────────

def test_overtime_is_priced_per_payroll_week_when_payroll_starts_on_wednesday(db, monkeypatch):
    rid = _restaurant(db, week_start_day=2)
    _publish(db, rid, PREV, [_six(d) for d in PREV[2:]])            # 30h Wed-Sun of the prior payroll week
    res = _run_job(monkeypatch, rid, [_six(d) for d in WEEK[:6]])  # Mon+Tue finish that week at 42h; Wed-Sat 24h
    assert res["projected_cost"]["overtime_hours"] == 2.0
    assert not any("26h of overtime" in line for line in res["review"]["lines"])


def test_overtime_on_a_monday_payroll_week_is_priced_past_forty(db, monkeypatch):
    rid = _restaurant(db)
    rows = [(d, dt.date.fromisoformat(d).strftime("%A"), "Ana", "Server", "9:00am", "5:00pm", 8, "") for d in WEEK[:6]]
    res = _run_job(monkeypatch, rid, rows)
    assert res["projected_cost"]["overtime_hours"] == 8.0
    assert any("8h of overtime" in line for line in res["review"]["lines"])


def test_daily_overtime_is_priced_where_the_rule_applies(db, monkeypatch):
    rid = _restaurant(db, compliance_json=json.dumps({"daily_ot_hours": 8}))
    rows = [(WEEK[0], "Monday", "Ana", "Server", "8:00am", "6:00pm", 10, "")]
    res = _run_job(monkeypatch, rid, rows)
    assert "daily_ot" in {v["kind"] for v in res["rule_violations"]}
    assert res["projected_cost"]["overtime_hours"] == 2.0


# ── SCHED-23: trim before the sweep ──────────────────────────────────────

def _r(date, emp, start, end, hours, role="Server", notes=""):
    return {"date": date, "day": dt.date.fromisoformat(date).strftime("%A"), "employee": emp, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": str(hours), "notes": notes}


def test_trim_never_removes_somebodys_only_shift_of_the_week():
    rows = []
    for d in WEEK[:5]:
        rows += [_r(d, "Ana", "11:00am", "3:00pm", 4), _r(d, "Ben", "11:00am", "3:00pm", 4)]
    rows.append(_r(WEEK[1], "Maya", "5:00pm", "9:00pm", 4))             # Maya's one shift, latest starter on a heavy day
    rows.append(_r(WEEK[1], "Cy", "4:00pm", "9:00pm", 5))
    targets = {d: 8.0 for d in WEEK}
    out, trimmed, removed = econ.trim_to_budget(rows, hours_budget=44.0, daily_targets=targets)
    assert trimmed, "the week is over budget, so something is trimmed"
    assert any(r["employee"] == "Maya" for r in out), trimmed


def test_trim_never_takes_somebody_under_their_minimum_hours():
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    c.hours_limits = {"maya": (8.0, None)}
    rows = [_r(d, n, "11:00am", "3:00pm", 4) for d in WEEK[:5] for n in ("Ana", "Ben")]
    rows += [_r(WEEK[1], "Maya", "5:00pm", "9:00pm", 4), _r(WEEK[3], "Maya", "5:00pm", "9:00pm", 4),
             _r(WEEK[1], "Cy", "4:00pm", "9:00pm", 5), _r(WEEK[3], "Cy", "4:00pm", "9:00pm", 5)]
    out, trimmed, removed = econ.trim_to_budget(rows, hours_budget=50.0, daily_targets={d: 8.0 for d in WEEK},
                                                constraints=c)
    assert trimmed
    assert sum(float(r["scheduled_hours"]) for r in out if r["employee"] == "Maya") >= 8.0, trimmed


def test_trim_never_removes_a_legal_row_while_a_no_show_row_remains():
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    c.blocked_dates = {"zed": {WEEK[2]: sr.LABELS["approved_time_off"]}}
    rows = [_r(WEEK[2], n, "4:00pm", "10:00pm", 6) for n in ("Ana", "Ben", "Cy")]
    rows.append(_r(WEEK[2], "Zed", "3:00pm", "10:00pm", 7))              # on approved time off: will not come in
    out, trimmed, removed = econ.trim_to_budget(rows, hours_budget=18.0, daily_targets={WEEK[2]: 18.0}, constraints=c)
    removed_names = {t["employee"] for t in trimmed}
    assert removed_names <= {"Zed"}, removed_names


def test_trim_leaves_a_week_inside_its_budget_alone():
    rows = [_r(d, "Ana", "11:00am", "3:00pm", 4) for d in WEEK[:5]]
    out, trimmed, removed = econ.trim_to_budget(rows, hours_budget=20.0, daily_targets={})
    assert trimmed == [] and removed == 0.0 and len(out) == 5


# ── weekly revenue median on thin or stale sales (Low risk, no finding) ──

def _sales(db_path, rid, days):
    conn = models.get_conn(db_path)
    for d, s in days:
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales) VALUES (?,?,?,?)",
                     (rid, d.isoformat(), d.strftime("%A"), s))
    conn.commit()
    conn.close()


def _complete_weeks(weeks_ago, per_day):
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    out = []
    for w in weeks_ago:
        start = monday - dt.timedelta(weeks=w)
        out += [(start + dt.timedelta(days=i), per_day) for i in range(7)]
    return out


def test_weekly_revenue_needs_three_complete_weeks_and_says_so(db):
    rid = _restaurant(db)
    _sales(db, rid, _complete_weeks([1, 2], 1000.0))
    out = econ.projected_weekly_revenue(rid)
    assert out["value"] is None and "fewer than three" in out["source"]


def test_a_partial_week_never_enters_the_weekly_revenue_median(db):
    rid = _restaurant(db)
    days = _complete_weeks([1, 2, 3], 1000.0)
    monday4 = dt.date.today() - dt.timedelta(days=dt.date.today().weekday()) - dt.timedelta(weeks=4)
    days += [(monday4 + dt.timedelta(days=i), 90000.0) for i in range(3)]      # three days only: not a week
    _sales(db, rid, days)
    out = econ.projected_weekly_revenue(rid)
    assert out["value"] == 7000.0 and out["weeks"] == 3


def test_stale_weeks_outside_the_window_never_set_the_budget(db):
    """Sales that stopped arriving two months ago must not keep setting the
    ceiling: only weeks inside the 63-day window are read."""
    rid = _restaurant(db)
    _sales(db, rid, _complete_weeks([10, 11, 12], 9000.0))
    out = econ.projected_weekly_revenue(rid)
    assert out["value"] is None
