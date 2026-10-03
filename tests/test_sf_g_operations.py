"""Schedule fix round 10/3/26, workstream G — operations: the live clock-in
check, attendance and the one weighted attendance reader.

E-4   the coverage check runs on the BUSINESS date: after midnight on a 2am
      close a missing 10pm starter is one issue, closed by their 12:10am
      arrival, recorded late — never a Saturday duplicate or a no-show.
E-5   an approved drop nobody claimed is excused: never "hasn't clocked
      in", never a no-show.
E-31  a mass call-off is one issue per business date and role, each gap its
      own covers, the lunch server who could stay named first.
L-17  reliability, by-weekday, standby and the prompt's no-show block read
      one weighted reader.
L-18  a call-out carries its notice and weighs less than a no-show; standby
      reads the absence (call-out) rate in full.
D-44  a late rate over clocked shifts, with a floor, reaches who opens and
      closes (the scorer and the prompt).
L-32  RPOWER can be watched; a night counts as watched only when the check
      read its clock-ins.

Nothing is sent: SMS is a stub that records.
"""
import json
from datetime import date, datetime, timedelta

import pytest

import models
from models import Restaurant, create_restaurant, get_conn, update_restaurant

FRI = date(2026, 10, 9)
SAT = FRI + timedelta(days=1)
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import importlib
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for name in ("models", "intraday", "issues", "notify", "push", "auth", "strategy_jobs", "ops", "staff_settings",
                 "schedule_rules", "schedule_engine", "labor", "schedule_versions", "shift_requests", "time_off",
                 "attendance", "people", "shift_facts", "staff_comms", "labor_replacements", "schedule_learning",
                 "client_api", "mobile_api", "closeout", "dsr.store"):
        monkeypatch.setattr(importlib.import_module(name), "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import auth
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])


@pytest.fixture
def texts(monkeypatch):
    out = []
    monkeypatch.setattr("notify.send_sms", lambda to, msg, use_case="alert", **k: out.append((to, msg)) or True)
    import notify
    monkeypatch.setattr(notify, "send_sms_outcome",
                        lambda to, msg, **k: out.append((to, msg)) or type("R", (), {"ok": True, "error": None,
                                                                                   "status": None,
                                                                                   "permanent": False})())
    return out


def _rid(db_path, **kw):
    kw.setdefault("name", "Late Night Co")
    kw.setdefault("owner_email", "o@x.test")
    kw.setdefault("module_labor", 1)
    rid = create_restaurant(Restaurant(**kw), db_path=db_path)
    update_restaurant(rid, {"open_times_json": json.dumps({d: "11:00am" for d in
                                                            ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                                                             "Saturday", "Sunday")}),
                            "close_times_json": json.dumps({d: "2:00am" for d in
                                                             ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                                                              "Saturday", "Sunday")})}, db_path=db_path)
    return rid


def _routed(db_path, rid):
    import issues
    conn = get_conn(db_path)
    cid = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) "
                       "VALUES (?, 'GM', '+15555550100', 1)", (rid,)).lastrowid
    conn.commit(); conn.close()
    issues.set_routing(rid, "manager", cid, db_path=db_path)
    return cid


def _publish(db_path, rid, day, rows):
    """A published week holding `day`: rows are (employee, role, start, end, hours)."""
    from models import save_schedule_history
    csv = HEADER + "".join(f"{day.isoformat()},{day.strftime('%A')},{e},{r},{s},{end},{h},\n"
                           for e, r, s, end, h in rows)
    hid = save_schedule_history(rid, day.isoformat(), day.isoformat(), 40, 40, 30, csv, [], db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.commit(); conn.close()
    return hid


def _at(monkeypatch, when):
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r=None, naive=False: when)


def _clocked(monkeypatch, names):
    import pos
    monkeypatch.setattr(pos, "fetch_clock_ins_today", lambda rid_, d: ([{"employee": n} for n in names], "toast"))


def _coverage(db_path, rid):
    import issues
    return [i for i in issues.list_issues(rid, db_path=db_path, limit=50) if i["kind"] == "coverage"]


# ── E-4: the business date, after midnight ─────────────────────────────────

def test_after_midnight_the_same_night_is_one_issue_closed_by_the_late_arrival(db_path, monkeypatch, texts):
    import attendance, issues, labor_replacements, strategy_jobs
    from dsr import store as dsr_store
    rid = _rid(db_path)
    _routed(db_path, rid)
    _publish(db_path, rid, FRI, [("Lee Ray", "Server", "5:00pm", "11:00pm", 6),
                                 ("Dana K", "Server", "10:00pm", "2:00am", 4),
                                 ("Max Orr", "Server", "12:00am", "2:00am", 2)])
    asked = []
    monkeypatch.setattr(labor_replacements, "for_gap",
                        lambda rid_, role, weekday, **k: asked.append((weekday, k.get("on_date"))) or [])
    _clocked(monkeypatch, ["Lee Ray"])
    _at(monkeypatch, datetime(2026, 10, 9, 22, 20))                    # Friday 10:20pm
    assert strategy_jobs.run_coverage_check(db_path=db_path)["opened"] == 1
    [cov] = _coverage(db_path, rid)
    assert cov["source_key"] == f"coverage:{FRI.isoformat()}:@server" and cov["title"] == "Dana K hasn't clocked in"

    _at(monkeypatch, datetime(2026, 10, 10, 0, 5))                     # Saturday 12:05am — still Friday's service
    assert strategy_jobs.run_coverage_check(db_path=db_path)["opened"] == 0
    assert len(_coverage(db_path, rid)) == 1, "no second issue keyed to the calendar date"

    _at(monkeypatch, datetime(2026, 10, 10, 0, 20))                    # Max (a 12:00am start listed for Friday) is late
    strategy_jobs.run_coverage_check(db_path=db_path)
    [cov] = _coverage(db_path, rid)
    assert cov["title"] == "2 of 3 servers haven't clocked in"
    # The covers for Max's gap were read from FRIDAY's week, not Saturday's.
    assert ("Friday", FRI.isoformat()) in asked and all(d == FRI.isoformat() for _w, d in asked)

    _clocked(monkeypatch, ["Lee Ray", "Dana K", "Max Orr"])            # both arrive after midnight
    _at(monkeypatch, datetime(2026, 10, 10, 0, 30))
    strategy_jobs.run_coverage_check(db_path=db_path)
    [cov] = _coverage(db_path, rid)
    assert cov["status"] == "resolved" and cov["resolution_note"].startswith("Closed automatically: they clocked in")
    assert dsr_store.coverage_ran(rid, FRI.isoformat()) and not dsr_store.coverage_ran(rid, SAT.isoformat())

    assert attendance.from_coverage_issues(rid, today=SAT) == 2
    got = {e["employee_name"]: (e["outcome"], e["business_date"]) for e in attendance.events(rid)}
    assert got == {"Dana K": ("late", FRI.isoformat()), "Max Orr": ("late", FRI.isoformat())}


def test_a_night_still_in_service_is_not_over_for_the_attendance_read(db_path, monkeypatch, texts):
    """At 1am on a 2am close the issue is tonight's: nobody on it is a
    no-show yet (from_coverage_issues measures 'over' by the business date)."""
    import attendance, labor_replacements, strategy_jobs
    rid = _rid(db_path)
    _routed(db_path, rid)
    _publish(db_path, rid, FRI, [("Lee Ray", "Server", "5:00pm", "11:00pm", 6),
                                 ("Dana K", "Server", "10:00pm", "2:00am", 4)])
    monkeypatch.setattr(labor_replacements, "for_gap", lambda *a, **k: [])
    _clocked(monkeypatch, ["Lee Ray"])
    _at(monkeypatch, datetime(2026, 10, 9, 22, 20))
    strategy_jobs.run_coverage_check(db_path=db_path)
    _at(monkeypatch, datetime(2026, 10, 10, 1, 0))
    assert attendance.from_coverage_issues(rid) == 0
    assert attendance.from_coverage_issues(rid, today=SAT) == 1
    assert [e["outcome"] for e in attendance.events(rid)] == ["no_show"]


# ── E-5: an approved drop nobody claimed is excused ────────────────────────

def _pos_day(rid, day, workers):
    import shift_facts
    shift_facts.ingest(rid, [{"date": day.isoformat(), "day": day.strftime("%A"), "employee": w, "role": "Server",
                              "shift_start": "17:00", "shift_end": "23:00", "scheduled_hours": "6",
                              "actual_hours": "6", "sales": "3000", "schedule_known": "0"} for w in workers], "rpower")
    conn = models.get_conn()
    conn.execute("INSERT OR REPLACE INTO labor_daily_history (restaurant_id, date, day_of_week, labor_cost, sales, "
                 "total_hours, source, provider, final) VALUES (?,?,?,?,?,?,?,?,1)",
                 (rid, day.isoformat(), day.strftime("%A"), 500, 3000, 12, "rpower", "rpower"))
    conn.commit(); conn.close()


def test_an_approved_drop_nobody_claimed_is_excused_not_a_no_show(db_path, monkeypatch, texts):
    import attendance, intraday, shift_requests, staff_settings, strategy_jobs
    rid = _rid(db_path)
    _routed(db_path, rid)
    _publish(db_path, rid, FRI, [("Ana Bell", "Server", "5:00pm", "11:00pm", 6),
                                 ("Lee Ray", "Server", "5:00pm", "11:00pm", 6)])
    before = datetime(2026, 10, 7, 12, 0)
    req = shift_requests.request_drop(rid, "Ana Bell", FRI.isoformat(), "5:00pm", reason="sick", now=before)
    assert shift_requests.decide(rid, req["id"], True, decided_by="GM", now=before)["status"] == "open"

    _clocked(monkeypatch, ["Lee Ray"])
    out = intraday.coverage_gaps(rid, now_local=datetime(2026, 10, 9, 17, 30), db_path=db_path)
    assert out["missing"] == []
    assert [(r["employee"], r["status"]) for r in out["released"]] == [("Ana Bell", "open (dropped)")]
    _at(monkeypatch, datetime(2026, 10, 9, 17, 30))
    assert strategy_jobs.run_coverage_check(db_path=db_path)["opened"] == 0
    assert _coverage(db_path, rid) == [] and texts == []

    # The shift passes unclaimed; the nightly join reads the approval, not a no-show.
    shift_requests.expire_past(rid, now=datetime(2026, 10, 10, 0, 30))
    _pos_day(rid, FRI, ["Lee Ray"])
    attendance.join_published(rid, FRI.isoformat())
    got = {e["employee_name"]: e["outcome"] for e in attendance.events(rid)}
    assert got == {"Ana Bell": "excused", "Lee Ray": "on_time"}
    # Excused is not a shift she owed: nothing counts it, worked or missed.
    evs = attendance.reliability_events(rid, detail=True) + [("Ana Bell", (FRI - timedelta(days=7 * i)).isoformat(),
                                                              "worked") for i in range(1, 7)]
    rel = staff_settings.weighted_attendance(evs, today=SAT)
    assert rel["Ana Bell"]["shifts"] == 6 and rel["Ana Bell"]["no_shows"] == 0


def test_a_drop_asked_and_never_approved_is_a_call_out_with_its_notice(db_path, monkeypatch, texts):
    import attendance, intraday, shift_requests
    rid = _rid(db_path)
    _publish(db_path, rid, FRI, [("Ana Bell", "Server", "5:00pm", "11:00pm", 6),
                                 ("Lee Ray", "Server", "5:00pm", "11:00pm", 6)])
    req = shift_requests.request_drop(rid, "Ana Bell", FRI.isoformat(), "5:00pm", now=datetime(2026, 10, 8, 12, 0))
    conn = get_conn(db_path)                     # asked at 4pm Chicago the day before: 25 hours' notice
    conn.execute("UPDATE shift_change_requests SET created_at='2026-10-08 21:00:00' WHERE id=?", (req["id"],))
    conn.commit(); conn.close()
    _clocked(monkeypatch, ["Lee Ray"])
    out = intraday.coverage_gaps(rid, now_local=datetime(2026, 10, 9, 17, 30), db_path=db_path)
    [m] = out["missing"]
    assert m["employee"] == "Ana Bell" and m["asked_off"] is True and m["notice_minutes"] == 25 * 60
    shift_requests.expire_past(rid, now=datetime(2026, 10, 10, 0, 30))
    _at(monkeypatch, datetime(2026, 10, 10, 5, 0))             # the morning after: Friday's punches are final
    _pos_day(rid, FRI, ["Lee Ray"])
    attendance.join_published(rid, FRI.isoformat())
    ev = next(e for e in attendance.events(rid) if e["employee_name"] == "Ana Bell")
    assert ev["outcome"] == "called_out" and ev["notice_minutes"] == 25 * 60


# ── E-31: a mass call-off ──────────────────────────────────────────────────

def _call_off_world(db_path, monkeypatch):
    rid = _rid(db_path)
    _routed(db_path, rid)
    for n in ("Pat Off", "Quinn Off", "Ana Bell", "Bo Chen", "Cy Diaz", "Di Eng", "Lu Fox", "Mo Gray", "Ed Hart"):
        models.add_manual_team_member(rid, n, role="Server", db_path=db_path)
    _publish(db_path, rid, FRI, [("Ana Bell", "Server", "5:00pm", "9:00pm", 4), ("Bo Chen", "Server", "5:00pm", "9:00pm", 4),
                                 ("Cy Diaz", "Server", "5:00pm", "9:00pm", 4), ("Di Eng", "Server", "5:00pm", "9:00pm", 4),
                                 ("Ed Hart", "Server", "5:30pm", "9:30pm", 4),
                                 ("Lu Fox", "Server", "11:00am", "5:00pm", 6),       # ends as dinner starts
                                 ("Mo Gray", "Server", "11:00am", "4:30pm", 5.5)])  # ends half an hour before
    _clocked(monkeypatch, ["Di Eng", "Lu Fox", "Mo Gray"])
    return rid


def test_a_mass_call_off_is_one_issue_with_its_own_cover_for_each_gap(db_path, monkeypatch, texts):
    import issues, strategy_jobs
    rid = _call_off_world(db_path, monkeypatch)
    _at(monkeypatch, datetime(2026, 10, 9, 17, 20))
    assert strategy_jobs.run_coverage_check(db_path=db_path)["opened"] == 1
    [cov] = _coverage(db_path, rid)
    assert cov["title"] == "3 of 7 servers haven't clocked in"
    assert len([t for t in texts if "haven't clocked in" in t[1]]) == 1, "one text, not one per person"
    covers = cov["meta"]["covers"]
    names = [c["name"] for c in covers]
    assert len(names) == len(set(names)), "the same people were suggested for every gap"
    assert {c["for"] for c in covers} == {"Ana Bell", "Bo Chen", "Cy Diaz"}
    # The lunch servers whose shifts end as dinner starts are named, first.
    stay = [c for c in covers if c["kind"] == "stay"]
    assert {c["name"] for c in stay} == {"Lu Fox", "Mo Gray"}
    assert covers[0]["kind"] == "stay" and "could stay on" in covers[0]["how"]
    for c in covers:
        assert c["name"] not in ("Ana Bell", "Bo Chen", "Cy Diaz", "Di Eng"), c
    assert "Lu Fox" in cov["detail"] and "Free today and best placed to cover" in cov["detail"]

    # Ed (5:30pm) goes missing later the same night: he joins the open issue
    # and the manager is told once more, with the new count.
    _at(monkeypatch, datetime(2026, 10, 9, 17, 50))
    sent = len(texts)
    assert strategy_jobs.run_coverage_check(db_path=db_path)["opened"] == 0
    [cov] = _coverage(db_path, rid)
    assert cov["title"] == "4 of 7 servers haven't clocked in" and len(texts) == sent + 1
    assert sorted(p["employee"] for p in issues.coverage_people(cov)) == ["Ana Bell", "Bo Chen", "Cy Diaz", "Ed Hart"]


def test_covering_one_gap_leaves_the_others_open_and_arrivals_close_it(db_path, monkeypatch, texts):
    import issues, strategy_jobs
    rid = _call_off_world(db_path, monkeypatch)
    _at(monkeypatch, datetime(2026, 10, 9, 17, 20))
    strategy_jobs.run_coverage_check(db_path=db_path)
    [cov] = _coverage(db_path, rid)
    issues.mark_coverage_covered(rid, cov["id"], "Ana Bell", "Lu Fox", db_path=db_path)
    [cov] = _coverage(db_path, rid)
    assert cov["status"] != "resolved" and cov["title"] == "2 of 7 servers haven't clocked in"
    assert "Covered: Ana Bell's 5:00pm by Lu Fox." in cov["detail"]
    _clocked(monkeypatch, ["Di Eng", "Lu Fox", "Mo Gray", "Bo Chen", "Cy Diaz"])
    _at(monkeypatch, datetime(2026, 10, 9, 17, 25))
    strategy_jobs.run_coverage_check(db_path=db_path)
    [cov] = _coverage(db_path, rid)
    assert cov["status"] == "resolved"
    status = {p["employee"]: p["status"] for p in issues.coverage_people(cov)}
    assert status == {"Ana Bell": "covered", "Bo Chen": "arrived", "Cy Diaz": "arrived"}


def test_ask_to_cover_offers_the_gap_the_suggestion_was_for(db_path, monkeypatch, texts):
    import intraday, issues, strategy_jobs
    rid = _call_off_world(db_path, monkeypatch)
    _at(monkeypatch, datetime(2026, 10, 9, 17, 20))
    strategy_jobs.run_coverage_check(db_path=db_path)
    [cov] = _coverage(db_path, rid)
    pick = next(c for c in cov["meta"]["covers"] if c["for"] == "Bo Chen")
    conn = get_conn(db_path)
    conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) VALUES (?,?,?,1)",
                 (rid, pick["name"], "+15555550177"))
    conn.execute("INSERT INTO staff_contacts (restaurant_id, employee_name, phone) VALUES (?,?,?)",
                 (rid, pick["name"], "+15555550177"))
    conn.commit(); conn.close()
    out = intraday.ask_to_cover(rid, cov["id"], pick["name"], db_path=db_path)
    assert out["ok"], out
    assert "Bo Chen can't make today's Server shift (5:00pm)" in texts[-1][1]
    [cov] = _coverage(db_path, rid)
    assert [(a["name"], a["for"]) for a in cov["meta"]["asked"]] == [(pick["name"], "Bo Chen")]


# ── L-17 / L-18 / D-44: the one weighted reader ───────────────────────────

def test_a_call_out_weighs_by_its_notice_and_standby_counts_it_in_full():
    import staff_settings as ss
    assert ss.miss_weight("no_show") == 1.0
    assert ss.miss_weight("called_out", 25 * 60) == 0.25
    assert ss.miss_weight("called_out", 3 * 60) == 0.5
    assert ss.miss_weight("called_out", 30) == ss.miss_weight("called_out", None) == 0.75
    assert ss.miss_weight("late") == ss.miss_weight("excused") == 0.0
    today = date(2026, 10, 3)
    days = [(today - timedelta(days=7 * i)).isoformat() for i in range(10)]
    evs = [("Nora", d, "no_show" if i < 2 else "worked") for i, d in enumerate(days)]
    evs += [("Cal", d, "called_out", {"notice_minutes": 2 * 24 * 60, "timed": False}) if i < 2 else ("Cal", d, "worked")
            for i, d in enumerate(days)]
    rel = ss.weighted_attendance(evs, today=today)
    assert rel["Cal"]["no_show_rate"] < rel["Nora"]["no_show_rate"], "a day's notice is not a no-show"
    assert rel["Cal"]["absence_rate"] == rel["Nora"]["absence_rate"], "either way the shift was short a person"
    assert rel["Cal"]["call_out_rate"] > 0 and rel["Nora"]["call_out_rate"] < rel["Cal"]["call_out_rate"]
    assert rel["Cal"]["no_shows"] == 2 and rel["Cal"]["called_out"] == 2


def test_one_window_and_decay_for_reliability_by_weekday_and_the_no_show_block():
    """Old misses fade the same way in every reader (L-17): reliability, the
    per-weekday read standby and the review use, and the prompt's weekday
    risk."""
    import staff_settings as ss
    today = date(2026, 10, 3)                                   # a Saturday
    sats = [(today - timedelta(days=7 * i)).isoformat() for i in range(1, 9)]
    old_misses = [("Ann", d, "no_show" if i >= 5 else "worked") for i, d in enumerate(sats)]
    new_misses = [("Ben", d, "no_show" if i < 3 else "worked") for i, d in enumerate(sats)]
    rel = ss.weighted_attendance(old_misses + new_misses, today=today)
    wd = ss.weekday_attendance(old_misses + new_misses, today=today)
    assert rel["Ann"]["no_shows"] == rel["Ben"]["no_shows"] == 3
    assert rel["Ann"]["no_show_rate"] < rel["Ben"]["no_show_rate"]
    assert wd["Ann"]["Saturday"]["no_show_rate"] < wd["Ben"]["Saturday"]["no_show_rate"]
    assert ss.weekday_absence(new_misses, today=today)["Saturday"]["rate"] > \
        ss.weekday_absence(old_misses, today=today)["Saturday"]["rate"]
    # Past the window nothing counts, in any of them.
    ancient = [("Cy", (today - timedelta(days=400 + 7 * i)).isoformat(), "no_show") for i in range(8)]
    assert ss.weighted_attendance(ancient, today=today) == {} and ss.weekday_attendance(ancient, today=today) == {}
    assert ss.weekday_absence(ancient, today=today) == {}


def test_every_attendance_reader_goes_through_the_one_reader():
    """Read at the source (a rule that must hold everywhere): no reader keeps
    a window of its own."""
    import inspect
    import labor, schedule_learning
    src = inspect.getsource(labor.generate_optimized_schedule)
    block = src[src.index("No-show risk per weekday"):src.index("_noshows_block = (")]
    assert "attendance_events(" in block and "weekday_absence(" in block and "weeks=26" not in block
    assert "reliability_events" not in block
    for fn in (schedule_learning.attendance_by_weekday, schedule_learning.standby_days):
        body = inspect.getsource(fn)
        assert "staff_settings" in body and "reliability_events" not in body
    assert not hasattr(schedule_learning, "_attendance_tally")


def test_a_late_rate_needs_clocked_shifts_and_reaches_who_opens(monkeypatch):
    import schedule_engine
    import shift_quality as sq
    import staff_settings as ss
    today = date(2026, 10, 3)
    days = [(today - timedelta(days=i)).isoformat() for i in range(1, 9)]
    timed = [("Ana", d, "late" if i % 2 == 0 else "on_time", {"notice_minutes": None, "timed": True})
             for i, d in enumerate(days)]
    rel = ss.weighted_attendance(timed, today=today)
    assert rel["Ana"]["late_shifts"] == 8 and rel["Ana"]["late"] == 4
    assert rel["Ana"]["late_rate"] >= ss.LATE_RISK_RATE and rel["Ana"]["late_risk"] is True
    # Below the floor the rate is not said; an upload's worked shift is not clocked.
    few = timed[:5] + [("Ana", (today - timedelta(days=20 + i)).isoformat(), "worked") for i in range(4)]
    rel_few = ss.weighted_attendance(few, today=today)
    assert rel_few["Ana"]["late_shifts"] == 5 and rel_few["Ana"]["late_rate"] is None
    assert rel_few["Ana"]["late_risk"] is False

    # The prompt names them for the open and the close.
    block = schedule_engine._reliability_block({"Ana": rel["Ana"]})
    assert "LATENESS" in block and "Ana: late to 4 of 8 clocked shifts" in block and "opens or closes" in block

    # The scorer: Ana alone opening the bar costs the shift; with Bo opening
    # beside her it does not.
    day = "2026-10-09"
    rows = [{"date": day, "employee": "Ana", "role": "Bartender", "shift_start": "4:00pm", "shift_end": "10:00pm"},
            {"date": day, "employee": "Bo", "role": "Bartender", "shift_start": "6:00pm", "shift_end": "11:00pm"}]
    reliab = {"Ana": rel["Ana"], "Bo": {"no_show_rate": 0.0, "shifts": 9}}

    def _ctx(rs):
        return sq.ShiftContext(date=day, daypart="dinner", rows=rs, day_rows=rs, reliability=reliab,
                               profile=sq.ShiftProfile())
    alone = sq.dim_reliability(_ctx(rows))
    assert alone.facts["late_exposed"] == [{"name": "Ana", "role": "Bartender", "edge": "opens",
                                           "late_rate": rel["Ana"]["late_rate"]}]
    assert alone.score < 100 and any("only bartender opening" in w for w in alone.weaknesses)
    paired = [dict(rows[0]), dict(rows[1], shift_start="4:00pm")]
    beside = sq.dim_reliability(_ctx(paired))
    assert beside.facts["late_exposed"] == [] and beside.score == 100


# ── L-32: RPOWER is watchable; a night is watched when its clock-ins were read

def test_an_rpower_restaurant_with_a_routed_manager_can_be_watched(db_path, monkeypatch):
    import inspect
    import pos, rpower
    import schedule_intel as si
    rid = _rid(db_path)
    _routed(db_path, rid)
    monkeypatch.setattr(pos, "connected_provider", lambda rid_: ("rpower", rpower))
    assert si.coverage_watch_missing(rid, db_path=db_path) == []
    src = inspect.getsource(si)
    assert "RPOWER, month-at-a-time,\n#      does not" not in src and "rpower.fetch_clock_ins_today" in src
