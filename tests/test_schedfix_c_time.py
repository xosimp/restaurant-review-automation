"""Workstream C (scheduler fix round 10/10/26): one notion of time, real
hours from every writer, the one daypart rule where a pass looks a rule up,
the close cap a manager may stay to, a curve's small hours, aliases in the
manager plan and the restaurant's own date (BUGSGEN-6/13/14/15/16/19)."""
import inspect
import sys
from datetime import date, datetime, timedelta

import pytest

import models
import schedule_rules as sr
import schedule_time as st
import shift_quality as sq

TZ = "America/Chicago"
HALLOWEEN_WEEK = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31", "2026-11-01"]
SAT = HALLOWEEN_WEEK[5]                     # 10/31/26: 2:00am CDT -> 1:00am CST


def _c(week=HALLOWEEN_WEEK, **kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(week), week_days=list(sr.DAYS))
    c.compliance = dict(sr.DEFAULTS)
    c.managers = {"mia": "Manager"}
    c.roster_names = ["Mia", "Sam", "Sal"]
    c.active = {"mia", "sam", "sal"}
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(d, name, role, start, end, tz=None):
    r = {"date": d, "day": sr._weekday_of(d), "employee": name, "role": role, "shift_start": start,
         "shift_end": end, "notes": ""}
    r["scheduled_hours"] = sr.hours_text(sr.span_hours(r, tz))
    return r


# ── BUGSGEN-6: every writer of a row's times writes its real hours ──────────

def test_close_out_gaps_writes_the_halloween_hour():
    # the audit's repro t3: Sam's closer runs on to the 2am close
    c = _c(close_times={"Saturday": "2:00am"}, tz=TZ)
    c.closers_by_role = {"server": {"sam"}}
    rows = [_row(SAT, "Mia", "Manager", "5:00pm", "2:00am", TZ), _row(SAT, "Sam", "Server", "5:00pm", "1:00am", TZ),
            _row(SAT, "Sal", "Server", "5:00pm", "2:00am", TZ)]
    out = sr.close_out_gaps(rows, c)
    sam = next(r for r in out["rows"] if r["employee"] == "Sam")
    assert sam["shift_end"] == "2:00am"
    assert sam["scheduled_hours"] == "10" == sr.hours_text(sr.span_hours(sam, TZ))


def test_apply_role_times_writes_the_halloween_hour():
    c = _c(close_times={"Saturday": "2:00am"}, tz=TZ)
    c.role_times = {("server", "Saturday", "night"): {"end": 2 * 60}}
    rows = [_row(SAT, "Mia", "Manager", "5:00pm", "2:00am", TZ), _row(SAT, "Sam", "Server", "5:00pm", "11:00pm", TZ)]
    out = sr.apply_role_times(rows, c)
    sam = next(r for r in out["rows"] if r["employee"] == "Sam")
    assert sam["shift_end"] == "2:00am" and sam["scheduled_hours"] == "10"


def test_the_optimizer_economics_and_close_cap_write_real_hours():
    import schedule_optimizer as so
    import schedule_economics as se_econ
    import schedule_engine as se
    c = _c(close_times={"Saturday": "2:00am"}, tz=TZ)
    assert so._new_row(SAT, "Sam", "Server", "5:00pm", "2:00am", "x", c)["scheduled_hours"] == "10"
    assert se_econ._hours_text({"date": SAT, "shift_start": "5:00pm", "shift_end": "2:00am"}, c) == "10"
    row = {"date": SAT, "day": "Saturday", "employee": "Sam", "role": "Server", "shift_start": "5:00pm",
           "shift_end": "3:00am", "scheduled_hours": "10", "notes": ""}
    se._enforce_close_time(row, "Saturday", c.close_times, {}, constraints=c)
    assert row["shift_end"] == "2:00am" and row["scheduled_hours"] == "10"


def test_a_planned_manager_row_carries_real_hours():
    import schedule_skeleton as sk
    c = _c(close_times={"Saturday": "2:00am"}, open_times={"Saturday": "4:00pm"}, tz=TZ,
           standing_shifts={"mia": [{"day": "Saturday", "start": "4:00pm", "end": "2:00am"}]})
    plan = sk.plan_manager_coverage(c, [SAT], history=[])
    mia = [r for r in plan["rows"] if r["employee"] == "Mia"]
    assert mia and mia[0]["shift_end"] == "2:00am" and mia[0]["scheduled_hours"] == "11"


def test_no_writer_derives_hours_from_wall_clock_minutes():
    """Every pass that writes a row's times writes its hours from the real
    span (schedule_time.row_hours_text / schedule_rules.span_hours): none
    divides wall-clock minutes by 60 into scheduled_hours any more."""
    import schedule_dedicated
    import schedule_economics
    import schedule_engine
    import schedule_memory
    import schedule_optimizer
    import schedule_skeleton
    for mod in (sr, schedule_engine, schedule_optimizer, schedule_economics, schedule_skeleton, schedule_dedicated,
                schedule_memory):
        for n, line in enumerate(inspect.getsource(mod).splitlines(), 1):
            if "scheduled_hours" in line and ("/ 60" in line or "/60" in line or "round(" in line):
                pytest.fail(f"{mod.__name__}:{n} writes wall-clock hours: {line.strip()}")


# ── BUGSGEN-14: a time rule is looked up by the daypart the row serves ──────

def test_a_dinner_start_rule_finds_a_two_thirty_row():
    c = _c(week=["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"])
    c.role_times = {("server", "Friday", "night"): {"start": 14 * 60 + 30}}
    fri = "2026-10-16"
    assert sr.role_time_for(c, _row(fri, "Sam", "Server", "4:00pm", "10:00pm")) == {"start": 870}
    # the row the rule itself writes (and a model's 2:30pm dinner row) is still dinner's
    assert sr.role_time_for(c, _row(fri, "Sam", "Server", "2:30pm", "10:00pm")) == {"start": 870}
    # a lunch shift is not
    assert sr.role_time_for(c, _row(fri, "Sam", "Server", "10:30am", "3:00pm")) == {}


# ── BUGSGEN-15: the model's manager close is held to the sweep's cap ───────

def test_a_manager_may_stay_as_long_as_any_role_at_the_parse():
    import schedule_engine as se
    week = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
    c = _c(week=week, close_times={"Friday": "11:00pm"}, role_buffers={"Bartender": 60})
    mgr = {"date": "2026-10-16", "day": "Friday", "employee": "Mia", "role": "Manager", "shift_start": "4:00pm",
           "shift_end": "12:00am", "scheduled_hours": "8", "notes": ""}
    se._enforce_close_time(mgr, "Friday", c.close_times, c.role_buffers, constraints=c)
    assert mgr["shift_end"] == "12:00am" and "auto-capped" not in mgr["notes"]
    server = dict(mgr, employee="Sam", role="Server")
    se._enforce_close_time(server, "Friday", c.close_times, c.role_buffers, constraints=c)
    assert server["shift_end"] == "11:00pm" and server["scheduled_hours"] == "7"
    # the job's parse passes the week's rules to it
    src = inspect.getsource(se._run_schedule_job)
    assert 'constraints=result.get("constraints"))' in src


# ── BUGSGEN-19: a curve's small hours are the night's ───────────────────────

def test_a_two_am_bar_curve_ramps_past_midnight():
    import staffing_curve as sc
    curve = {"17": 10, "18": 20, "19": 30, "20": 30, "21": 25, "22": 20, "23": 30, "0": 40, "1": 30}
    shares = sc.half_hour_shares(curve)
    assert min(shares) == 17 * 60 and max(shares) == 25 * 60 + 30
    assert not any(m < 17 * 60 for m in shares), "1am sales sat at the start of the day"
    needs = sc.half_hour_needs(curve, {"Bartender": 2}, 15 * 60, 26 * 60)
    assert 24 * 60 in needs and 25 * 60 in needs, "the 12am-2am ramp asked for nobody"
    # the morning half of the day carries none of the bar's sales
    assert sc.half_hour_needs(curve, {"Bartender": 2}, 0, 15 * 60) == {}


# ── BUGSGEN-13: the manager plan reads a manager by every spelling ──────────

def test_the_manager_plan_reads_usual_days_through_aliases():
    import schedule_skeleton as sk
    c = _c(week=["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"])
    c.managers = {"gabriel huerta": "Manager"}
    c.roster_names = ["Gabriel Huerta"]
    c.active = {"gabriel huerta"}
    c.aliases = {"gabe huerta": "gabriel huerta"}
    history = []
    for wk in range(4):
        for wd in (0, 1, 2):                                      # Mon-Wed, as the POS spells him
            d = (date(2026, 9, 14) + timedelta(days=7 * wk + wd)).isoformat()
            history.append({"date": d, "employee": "Gabe Huerta", "role": "Manager", "shift_start": "10:00am",
                            "shift_end": "6:00pm", "_source": "punch"})
    usual = sk.usual_shifts(history, ["gabriel huerta"], key_of=c.key)
    assert set(usual["gabriel huerta"]["days"]) == {"Monday", "Tuesday", "Wednesday"}
    # without the Constraints' key, the punches were nobody's usual days
    assert not (sk.usual_shifts(history, ["gabriel huerta"]).get("gabriel huerta") or {}).get("days")
    src = inspect.getsource(sk.plan_manager_coverage)
    assert "key_of=c.key" in src and "c.key(r.get(\"employee\")) == key" in src


# ── BUGSGEN-16: the restaurant's date, never the server's UTC one ───────────

@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return models.create_restaurant(models.Restaurant(name="EJ Co", owner_email="e@x.com", timezone=TZ),
                                    db_path=db_path)


def _local(monkeypatch, when):
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda rid, naive=False: when)


def test_a_note_hold_ending_today_holds_until_the_restaurants_day_ends(rid, db_path, monkeypatch):
    import person_note_holds as pnh
    pnh.add_hold(rid, "Ana", "off 10/9", start="2026-10-09", end="2026-10-09", db_path=db_path)
    _local(monkeypatch, datetime(2026, 10, 9, 21, 0))            # 9pm Chicago is 2am UTC on 10/10
    assert [h["part_text"] for h in pnh.holds(rid, db_path=db_path)] == ["off 10/9"]


def test_certificates_and_crew_sales_read_the_restaurants_date(rid, monkeypatch):
    import crew_matrix
    import staff_knowledge
    seen = {}
    monkeypatch.setattr(staff_knowledge, "expired_certs", lambda r, today=None, **k: seen.setdefault("on", today) and {})
    _local(monkeypatch, datetime(2026, 10, 9, 21, 0))
    sr._expired_certs(rid, ["2026-10-05", "2026-10-06"])
    assert seen["on"] == date(2026, 10, 9)
    # the crew grid's sales window starts SALES_WEEKS before the local date
    calls = []

    class _Conn:
        def execute(self, sql, args=()):
            calls.append(args)

            class _R:
                def fetchall(self):
                    return []
            return _R()

        def close(self):
            pass
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: _Conn())
    crew_matrix._shift_sales(rid)
    assert calls and calls[0][1] == (date(2026, 10, 9) - timedelta(days=7 * crew_matrix.SALES_WEEKS)).isoformat()


# ── BUGSGEN-20: hours are written in the one spelling, an unreadable one said ─

def test_an_unreadable_stored_time_is_named_and_holds_the_publish():
    c = _c(close_times={"Friday": "9", "Saturday": "2:00am"}, open_times={"Friday": "noon"})
    sr._unreadable_hours(c)
    assert [p["hours"] for p in c.input_problems] == [{"day": "Friday", "which": "closing", "value": "9"}]
    text = sr.input_problem_text(c.input_problems[0])
    assert "Friday's closing time “9” isn't a time Cavnar AI can read" in text
    # build_constraints reads them every time
    assert "_unreadable_hours(c)" in inspect.getsource(sr.build_constraints)


def test_the_admin_close_times_box_stores_the_one_spelling():
    import admin_routes
    got = admin_routes._settings_close_times({"close_times_json": '{"friday": "21:30", "Saturday": "2am"}'},
                                             "close_times_json")
    assert got == '{"Friday": "9:30pm", "Saturday": "2:00am"}'
    with pytest.raises(admin_routes._SettingsError):
        admin_routes._settings_close_times({"close_times_json": '{"Friday": "9"}'}, "close_times_json")


def test_the_web_hours_grid_reads_a_twelve_hour_time():
    import os
    import re
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    page = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates",
                             "dashboard.html"), encoding="utf-8").read()
    fn = re.search(r"function cavTimeValue\(v\) \{.*?\n\}", page, re.S).group(0)
    js = fn + ("\nconsole.log(JSON.stringify(['9:00pm','21:00','9:00am','12:00am','noon','9:30 p.m.','21:00:00','9','',"
               "'24:00','13pm'].map(cavTimeValue)));")
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=30).stdout.strip()
    assert out == '["21:00","21:00","09:00","00:00","12:00","21:30","21:00","","","00:00",""]'


# ── ARCH-24: the floors' week is the restaurant's own week ──────────────────

def test_effective_floors_build_the_restaurants_own_week(monkeypatch):
    import schedule_note_rules
    import staff_settings
    seen = {}
    monkeypatch.setattr(schedule_note_rules, "apply_note_rules",
                        lambda c, rid, db_path=None: seen.setdefault("dates", list(c.week_dates)))
    monkeypatch.setattr(sr, "apply_owner_rules", lambda *a, **k: None)
    monkeypatch.setattr(staff_settings, "roster", lambda *a, **k: [])

    class _R:
        id, role_floors_json, open_times_json, week_start_day = 1, None, None, 2       # Wednesday to Tuesday
    sr.effective_role_floors(_R(), day=date(2026, 10, 12))                             # a Monday
    assert seen["dates"][0] == "2026-10-07" and seen["dates"][-1] == "2026-10-13"
