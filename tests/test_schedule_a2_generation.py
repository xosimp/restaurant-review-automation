"""The A2 repairs inside a real generation (schedule audit 10/3/26): the job
runs with the model stubbed out (_build_schedule_result returns a fixed
draft), so these check what the owner is shown — a day-level manager gap on
the day, never on a server's row, with why nobody could cover it (E-13,
E-12); a full-timer under their minimum given shifts (P-4); a minor's week
over the cap repaired (P-5); and the Halloween close counted in real hours
(E-23)."""
import datetime as dt
import sys

import pytest

# Imported before any fixture patches models.get_conn (see
# tests/test_edge_sched_economics.py).
import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import labor_replacements  # noqa: F401

import models
import schedule_engine as se
import schedule_rules as sr
from models import create_restaurant, Restaurant

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    return db_path


def _restaurant(db_path, people, **cols):
    rid = create_restaurant(Restaurant(name="A2 Co", owner_email="a2@x.com"), db_path=db_path)
    if cols:
        c = models.get_conn(db_path)
        c.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
        c.commit()
        c.close()
    for name, role in people:
        models.add_manual_team_member(rid, name, role=role, db_path=db_path)
    return rid


def _line(d, emp, role, start, end, hours):
    return f"{d},{dt.date.fromisoformat(d).strftime('%A')},{emp},{role},{start},{end},{hours},"


def _run(monkeypatch, rid, lines, week=WEEK, roles=None, **extra):
    base = {"ok": True, "schedule_csv": HEADER + "\n" + "\n".join(lines), "week_dates": list(week),
            "week_days": [dt.date.fromisoformat(d).strftime("%A") for d in week], "summary": [],
            "hours_budget": 0, "daily_target_hours": {}, "labor_target": 30, "blended_rate": 20.0,
            "roster_roles": roles or {}}
    base.update(extra)
    monkeypatch.setattr(se, "_build_schedule_result", lambda r, week_start=None: dict(base))
    done = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda job_id, status, result: done.update(status=status, result=result))
    se._run_schedule_job("a2-job", rid)
    assert done.get("status") == "done", done
    return done["result"]


def test_the_only_manager_off_leaves_the_gap_on_the_day_with_why(db, monkeypatch):
    rid = _restaurant(db, [("Max", "General Manager"), ("Ana", "Server"), ("Bo", "Server")])
    row, err = time_off.request_time_off(rid, "Max", WEEK[2], WEEK[2], db_path=db, today=dt.date(2026, 9, 1))
    assert not err, err
    time_off.decide(rid, row["id"], True, db_path=db)
    lines = [_line(WEEK[2], "Ana", "Server", "11:00am", "5:00pm", 6), _line(WEEK[2], "Bo", "Server", "5:00pm", "10:00pm", 5)]
    res = _run(monkeypatch, rid, lines, roles={"Max": "General Manager", "Ana": "Server", "Bo": "Server"})
    gaps = [v for v in res["rule_violations"] if v["kind"] == "no_manager"]
    assert gaps and all(v["hard"] and v["day_level"] for v in gaps)
    # never "needs review" on Ana or Bo, never their row in hard_rows
    assert not any(r.get("needs_review") for r in res["preview_rows"])
    assert res["review"]["hard_rows"] == [] and res["review"]["hard_days"][0]["date"] == WEEK[2]
    assert any(line.startswith("⚠ 10/7/26 — no manager on Wednesday") for line in res["review"]["lines"])
    assert not any(u.get("kind") == "no_manager" for u in res["review"]["unfixed"])
    # why, once for the week, and per manager
    left = res["manager_coverage"]["left"]
    assert left and any(r["employee"] == "Max" and "time off" in r["why"] for r in left[0]["reasons"])
    assert any("has no manager on" in line and "acting manager" in line for line in res["review"]["lines"])
    # the publish gate names the day, never a server
    import client_api
    gate = client_api.publish_review(rid, schedule_id=res["history_id"], today=dt.date(2026, 9, 20))
    texts = [b["text"] for b in gate["blockers"]]
    assert any(t.startswith("10/7/26 — no manager on Wednesday") for t in texts), texts
    assert not any(t.startswith(("Ana —", "Bo —")) and "no manager" in t for t in texts), texts
    assert gate["blockers"][0]["key"] != "needs_review"


def test_a_full_timer_under_their_minimum_is_given_shifts(db, monkeypatch):
    rid = _restaurant(db, [("Gm", "General Manager"), ("Cook", "Line Cook"), ("Lee", "Line Cook")])
    staff_settings.upsert(rid, "Cook", min_hours=40, max_hours=45, db_path=db)
    lines = [_line(WEEK[0], "Cook", "Line Cook", "11:00am", "6:00pm", 7)]
    lines += [_line(d, "Lee", "Line Cook", "2:00pm", "10:00pm", 8) for d in WEEK[1:6]]
    lines += [_line(d, "Gm", "General Manager", "11:00am", "10:00pm", 11) for d in WEEK[:6]]
    res = _run(monkeypatch, rid, lines, roles={"Gm": "General Manager", "Cook": "Line Cook", "Lee": "Line Cook"})
    cook = sum(float(r["scheduled_hours"]) for r in res["preview_rows"] if r["employee"] == "Cook")
    assert cook > 7 and res["min_hours"]["moved"] >= 1
    assert any(f.get("kind") == "min_hours" for f in res["review"]["fixes"])


def test_a_school_week_minor_over_the_cap_is_repaired(db, monkeypatch):
    rid = _restaurant(db, [("Gm", "General Manager"), ("Kid", "Host"), ("Ana", "Host")])
    staff_settings.upsert(rid, "Kid", minor_age_band="14-15", db_path=db)
    lines = [_line(d, "Kid", "Host", "4:00pm", "7:00pm", 3) for d in WEEK[:5]]
    lines += [_line(WEEK[5], "Kid", "Host", "10:00am", "3:00pm", 5), _line(WEEK[5], "Ana", "Host", "3:00pm", "9:00pm", 6)]
    lines += [_line(d, "Gm", "General Manager", "9:00am", "9:00pm", 12) for d in WEEK[:6] if d != WEEK[3]]
    lines += [_line(WEEK[3], "Gm", "General Manager", "2:00pm", "8:00pm", 6)]
    res = _run(monkeypatch, rid, lines, roles={"Gm": "General Manager", "Kid": "Host", "Ana": "Host"})
    assert "minor_week_hours" not in {v["kind"] for v in res["rule_violations"]}
    assert sum(float(r["scheduled_hours"]) for r in res["preview_rows"] if r["employee"] == "Kid") <= 18


def test_the_halloween_close_is_counted_in_real_hours(db, monkeypatch):
    week = [(dt.date(2026, 10, 26) + dt.timedelta(days=i)).isoformat() for i in range(7)]
    rid = _restaurant(db, [("Gm", "General Manager"), ("Ana", "Bartender")], timezone="America/Chicago")
    lines = [_line("2026-10-31", "Ana", "Bartender", "5:00pm", "2:00am", 9),
             _line("2026-10-31", "Gm", "General Manager", "5:00pm", "2:00am", 9)]
    res = _run(monkeypatch, rid, lines, week=week, roles={"Gm": "General Manager", "Ana": "Bartender"})
    ana = next(r for r in res["preview_rows"] if r["employee"] == "Ana")
    assert ana["scheduled_hours"] == "10.0" and ana.get("dst_hours") == 1.0
    assert "extra hour when the clocks go back" in ana["notes"]
