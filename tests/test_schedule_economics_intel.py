"""The money and the record: schedule_economics, schedule_intel,
compliance_packs, reservation_feeds, and the rules they feed.

The second audit's remaining list: a budget that is a ceiling in words
only, overtime priced straight, no rotation memory past seven days, no
break windows, a section cap on the literal word "server", ratings with
nobody to learn from, and staff who could only ask for one thing.
"""
import datetime as dt
import json

import pytest

import compliance_packs
import models
import reservation_feeds
import schedule_economics as econ
import schedule_intel as intel
import schedule_rules as sr
import shift_requests as srq
import schedule_versions as sv
import staff_settings as ss
from models import create_restaurant, Restaurant, get_conn

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


def _row(date, emp, start, end, role="Server", hours=None, notes=""):
    if hours is None:
        s, e = sr.parse_minutes(start), sr.parse_minutes(end)
        hours = round(((e - s) % (24 * 60)) / 60, 1)
    return {"date": date, "day": DAYS[WEEK.index(date)], "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(hours), "notes": notes}


def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ── economics ──────────────────────────────────────────────────────────

def test_overtime_is_priced_at_the_multiplier_past_the_ceiling():
    rows = [_row(d, "Ana", "9:00am", "5:00pm") for d in WEEK[:6]]      # 48h
    cost = econ.priced_cost(rows, {"Server": 20.0}, 20.0, ceiling=40)
    assert cost["overtime_hours"] == 8.0
    assert cost["straight"] == 48 * 20 and cost["overtime_premium"] == 8 * 20 * 0.5
    assert cost["total"] == 48 * 20 + 80
    # hours already published in the payroll week push this week into overtime sooner
    cost2 = econ.priced_cost(rows[:2], {"Server": 20.0}, 20.0, ceiling=40, base_hours={"ana": 36})
    assert cost2["overtime_hours"] == 12.0


def test_cost_delta_says_what_an_edit_moves():
    before = [_row(WEEK[0], "Ana", "9:00am", "5:00pm")]
    after = before + [_row(WEEK[1], "Bob", "4:00pm", "10:00pm")]
    d = econ.cost_delta(before, after, {"Server": 15.0}, 15.0)
    assert d["hours_delta"] == 6.0 and d["dollars_delta"] == 90.0


def test_trim_removes_discretionary_hours_first_and_reports_each():
    rows = [_row(d, n, "4:00pm", "10:00pm") for d in WEEK for n in ("Ana", "Bob", "Cy")]   # 126h
    rows.append(_row(WEEK[4], "Dee", "4:00pm", "10:00pm", notes="added — coverage top-up"))
    rows.append(_row(WEEK[5], "Ana", "11:00am", "3:00pm"))                                   # first leg; the 4pm is her second
    out, trimmed, removed = econ.trim_to_budget(rows, hours_budget=120.0, daily_targets={d: 17 for d in WEEK},
                                                constraints=_c(), floors={})
    assert removed >= 16 and sum(float(r["scheduled_hours"]) for r in out) <= 120.0
    reasons = [t["reason"] for t in trimmed]
    assert reasons[0].startswith("added by the top-up")
    assert any("second leg of a double" in r for r in reasons)
    assert all(t.get("employee") for t in trimmed)


def test_trim_never_goes_below_a_floor_or_removes_the_last_person_of_a_role():
    # Each cook also works another day (as the last of that role), so the
    # Monday cook is not their only shift of the week — which the trim never
    # takes (SCHED-23) — and only the floor and last-of-role rules decide.
    rows = [_row(WEEK[0], "Ana", "4:00pm", "10:00pm", role="Cook"), _row(WEEK[0], "Bob", "4:00pm", "10:00pm", role="Cook"),
            _row(WEEK[0], "Cy", "4:00pm", "10:00pm", role="Host"),
            _row(WEEK[1], "Ana", "4:00pm", "10:00pm", role="Prep"), _row(WEEK[1], "Bob", "4:00pm", "10:00pm", role="Dish")]
    floors = {"Cook": {"morning": 0, "night": 2, "days": {}}}
    out, trimmed, removed = econ.trim_to_budget(rows, hours_budget=6.0, daily_targets={WEEK[0]: 6}, constraints=_c(), floors=floors)
    assert removed == 0 and trimmed == []                      # the two cooks are the floor, the host is the last host
    out, trimmed, removed = econ.trim_to_budget(rows, hours_budget=6.0, daily_targets={WEEK[0]: 6}, constraints=_c(), floors={})
    assert removed == 6 and len(out) == len(rows) - 1 and trimmed[0]["role"] == "Cook"


def test_a_week_inside_the_tolerance_is_not_trimmed():
    rows = [_row(WEEK[0], "Ana", "4:00pm", "10:00pm"), _row(WEEK[0], "Bob", "4:00pm", "10:00pm")]
    assert econ.trim_to_budget(rows, 11.9, {}, constraints=_c())[2] == 0


def test_identical_starts_are_staggered_along_a_climbing_curve():
    rows = [_row(WEEK[5], n, "5:00pm", "10:00pm") for n in ("Ana", "Bob", "Cy", "Dee")]
    curve = {"Saturday": {17: 0.10, 18: 0.20, 19: 0.30, 20: 0.25, 21: 0.15}}
    out, changes = econ.stagger_same_starts(rows, curve)
    starts = [r["shift_start"] for r in out]
    assert starts == ["5:00pm", "5:30pm", "6:00pm", "6:30pm"]
    assert out[1]["scheduled_hours"] == "4.5" and "staggered" in out[1]["notes"]
    assert len(changes) == 3 and "climb until 19:00" in changes[0]["reason"]
    # past the peak, or with no measured curve, nothing moves
    assert econ.stagger_same_starts([dict(r) for r in rows], {"Saturday": {12: 0.5, 13: 0.5}})[1] == []
    assert econ.stagger_same_starts([dict(r) for r in rows], {})[1] == []


def test_holiday_dates_and_lift_from_the_restaurants_own_record(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(econ, "get_conn", lambda *a, **k: real(db_path))
    rid = create_restaurant(Restaurant(name="H", owner_email="h@x.com"), db_path=db_path)
    names = econ._holiday_dates(2026)
    assert names["2026-11-26"] == "Thanksgiving" and names["2026-04-05"] == "Easter" and names["2026-05-10"] == "Mother's Day"
    conn = get_conn(db_path)
    # Halloween 2025 (Friday) took $9,000; the surrounding Fridays took $6,000
    for d, sales in (("2025-10-31", 9000), ("2025-10-10", 6000), ("2025-10-17", 6000), ("2025-10-24", 6000), ("2025-11-07", 6000)):
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales) VALUES (?,?,?,?)",
                     (rid, d, dt.date.fromisoformat(d).strftime("%A"), sales))
    conn.commit(); conn.close()
    week = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31", "2026-11-01"]
    lift = econ.holiday_lift(rid, week, db_path=db_path)
    assert lift["2026-10-31"]["name"] == "Halloween" and lift["2026-10-31"]["lift_pct"] == 50
    assert "Halloween 2025" in lift["2026-10-31"]["based_on"]
    assert econ.holiday_lift(rid, ["2026-10-06"], db_path=db_path) == {}


def test_weekly_revenue_is_the_median_of_complete_weeks(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(econ, "get_conn", lambda *a, **k: real(db_path))
    rid = create_restaurant(Restaurant(name="R", owner_email="r@x.com"), db_path=db_path)
    assert econ.projected_weekly_revenue(rid, db_path=db_path)["value"] is None
    conn = get_conn(db_path)
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    for w in range(1, 6):                       # five complete weeks, $7k a day, one week at $14k
        for i in range(7):
            d = monday - dt.timedelta(weeks=w) + dt.timedelta(days=i)
            conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales) VALUES (?,?,?,?)",
                         (rid, d.isoformat(), d.strftime("%A"), 14000 if w == 3 else 7000))
    conn.commit(); conn.close()
    out = econ.projected_weekly_revenue(rid, db_path=db_path)
    assert out["value"] == 49000 and "median of the last 5" in out["source"]


# ── compliance packs ───────────────────────────────────────────────────

def test_a_pack_sits_under_the_owners_own_values():
    base = {**sr.DEFAULTS, "meal_break_after_hours": 6}          # the owner's own value, already in force
    merged = compliance_packs.apply(base, "ca", owner_set={"meal_break_after_hours": 6})
    assert merged["daily_ot_hours"] == 8 and merged["meal_break_after_hours"] == 6      # owner's value kept
    assert merged["_pack"]["code"] == "CA" and "meal_break_after_hours" not in merged["_pack"]["applied"]
    assert compliance_packs.apply(base, "ZZ") == base and "_pack" not in compliance_packs.apply(base, "ZZ")
    assert {p["code"] for p in compliance_packs.available()} >= {"CA", "NY", "OR"}


def test_compliance_reads_the_jurisdiction_from_the_restaurant(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    rid = create_restaurant(Restaurant(name="J", owner_email="j@x.com"), db_path=db_path)
    models.update_restaurant(rid, {"jurisdiction": "CA", "compliance_json": json.dumps({"meal_break_after_hours": 6})}, db_path=db_path)
    comp = sr.compliance(models.get_restaurant(rid, db_path))
    assert comp["daily_ot_hours"] == 8 and comp["meal_break_after_hours"] == 6 and comp["_pack"]["label"] == "California"


# ── rules: windows, certifications, manager on duty, section cap, days off ──

def test_time_windows_block_a_shift_outside_them():
    c = _c(time_windows={"ana": {"Monday": (sr.parse_minutes("10:00am"), sr.parse_minutes("9:00pm"))}})
    assert c.window_ok("Ana", WEEK[0], "11:00am", "3:00pm")[0]
    ok, why = c.window_ok("Ana", WEEK[0], "8:00am", "3:00pm")
    assert not ok and "not before 10:00am" in why
    v = sr.violations([_row(WEEK[0], "Ana", "4:00pm", "10:00pm")], c)
    assert [x["kind"] for x in v] == ["outside_window"] and v[0]["no_show"]
    assert sr.violations([_row(WEEK[1], "Ana", "4:00pm", "10:00pm")], c) == []       # no window on Tuesday


def test_a_role_that_needs_a_certification_refuses_people_without_it():
    c = _c(role_requirements={"bartender": {"alcohol"}}, certifications={"ana": {"alcohol"}})
    assert c.cert_ok("Ana", "Bartender")[0]
    ok, why = c.cert_ok("Bob", "Bartender")
    assert not ok and "alcohol" in why
    v = sr.violations([_row(WEEK[0], "Bob", "4:00pm", "10:00pm", role="Bartender")], c)
    assert [x["kind"] for x in v] == ["missing_cert"]
    assert "Bartender needs alcohol" in sr.prompt_block(c) or "bartender needs alcohol" in sr.prompt_block(c)


def test_manager_on_duty_needs_a_keyholder_on_every_open_daypart():
    c = _c(keyholders={"ana"}, compliance={**sr.DEFAULTS, "manager_on_duty": True})
    rows = [_row(WEEK[0], "Ana", "9:00am", "3:00pm"), _row(WEEK[0], "Bob", "4:00pm", "10:00pm")]
    v = [x for x in sr.violations(rows, c) if x["kind"] == "no_manager_on_duty"]
    assert len(v) == 1 and v[0]["hard"] and "night" in v[0]["detail"]
    c.compliance["manager_on_duty"] = False
    assert not [x for x in sr.violations(rows, c) if x["kind"] == "no_manager_on_duty"]


def test_the_section_cap_counts_the_configured_front_of_house_roles():
    c = _c(section_cap=2, foh_roles={"server", "bartender"})
    rows = [_row(WEEK[0], "Ana", "4:00pm", "10:00pm"), _row(WEEK[0], "Bob", "4:00pm", "10:00pm"),
            _row(WEEK[0], "Cy", "5:00pm", "10:00pm", role="Bartender"), _row(WEEK[0], "Dee", "4:00pm", "10:00pm", role="Cook")]
    v = [x for x in sr.violations(rows, c) if x["kind"] == "over_section_cap"]
    assert len(v) == 1 and not v[0]["hard"] and v[0]["employee"] == "Cy" and "3 on the floor" in v[0]["detail"]


def test_monday_wednesday_friday_has_no_two_days_off_together():
    c = _c(compliance={**sr.DEFAULTS, "min_consecutive_days_off": 2})
    rows = [_row(d, "Ana", "9:00am", "3:00pm") for d in (WEEK[0], WEEK[2], WEEK[4], WEEK[6])]   # off Tue, Thu, Sat: never two together
    assert any(x["kind"] == "days_off" for x in sr.violations(rows, c))
    rows = [_row(d, "Ana", "9:00am", "3:00pm") for d in (WEEK[0], WEEK[2], WEEK[4])]            # Sat+Sun off together
    assert not any(x["kind"] == "days_off" for x in sr.violations(rows, c))


def test_arrival_time_by_role_is_a_soft_flag_and_a_backstop():
    import schedule_engine
    c = _c(open_times={"Monday": "11:00am"}, arrivals={"line cook": -90})
    v = sr.violations([_row(WEEK[0], "Ana", "7:00am", "3:00pm", role="Line Cook")], c)
    assert [x["kind"] for x in v] == ["before_arrival"] and not v[0]["hard"]
    row = _row(WEEK[0], "Ana", "7:00am", "3:00pm", role="Line Cook")
    schedule_engine._enforce_arrival_time(row, "Monday", c.open_times, c.arrivals)
    assert row["shift_start"] == "9:30am" and row["scheduled_hours"] == "5.5" and "arrival" in row["notes"]


# ── staff settings: windows, certifications, preferences ───────────────

@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    import time_off, schedule_engine
    for mod in (models, ss, sr, sv, srq, intel, econ, time_off, schedule_engine):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    rid = create_restaurant(Restaurant(name="Intel Co", owner_email="i@x.com"), db_path=db_path)
    for n in ("Ana", "Bob", "Cy"):
        models.add_manual_team_member(rid, n, role="Server", db_path=db_path)
    return rid


def test_settings_carry_windows_certifications_and_wishes_into_the_constraints(db_path, rid):
    ss.upsert(rid, "Ana", time_windows={"Monday": {"earliest": "10:00am", "latest": "9:00pm"}},
              certifications=["Alcohol", "manager"], preferred_dayparts=["night"], desired_hours=30, db_path=db_path)
    with pytest.raises(ss.StaffSettingsError):
        ss.upsert(rid, "Ana", time_windows={"Monday": {"earliest": "9:00pm", "latest": "10:00am"}}, db_path=db_path)
    with pytest.raises(ss.StaffSettingsError):
        ss.upsert(rid, "Ana", desired_hours=99, db_path=db_path)
    row = ss.get_all(rid, db_path=db_path)["Ana"]
    assert row["certifications"] == ["alcohol", "manager"] and row["preferred_dayparts"] == ["night"] and row["desired_hours"] == 30
    c = sr.build_constraints(rid, WEEK, DAYS, db_path=db_path)
    assert "ana" in c.keyholders and c.certifications["ana"] == {"alcohol", "manager"}
    assert c.time_windows["ana"]["Monday"] == (sr.parse_minutes("10:00am"), sr.parse_minutes("9:00pm"))
    assert ss.stated_preferences(rid, db_path=db_path)["Ana"]["desired_hours"] == 30
    block = intel.preferences_block({}, ss.stated_preferences(rid, db_path=db_path))
    assert "Ana: prefers night; would like about 30h a week" in block


# ── intel: outcomes, ledger, behaviour, mentoring, ledger-aware fairness ──

def _publish(db_path, rid, week_start, week_end, csv_text):
    conn = get_conn(db_path)
    models._ensure_history_columns(conn)
    cur = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, labor_target, "
                       "schedule_csv, summary_json, published_at) VALUES (?,?,?,?,?,?,?,'[]','2026-09-01 10:00:00')",
                       (rid, week_start, week_end, 12, 40, 30, csv_text))
    conn.commit(); conn.close()
    return cur.lastrowid


def test_outcomes_are_recorded_per_published_daypart_and_read_back(db_path, rid):
    csv_text = HEADER + ("2026-09-05,Saturday,Ana,Server,11:00am,3:00pm,4.0,\n"
                         "2026-09-05,Saturday,Bob,Server,4:00pm,10:00pm,6.0,\n"
                         "2026-09-05,Saturday,Cy,Server,4:00pm,10:00pm,6.0,\n")
    hid = _publish(db_path, rid, "2026-08-31", "2026-09-06", csv_text)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_pct) VALUES (?,?,?,?,?)",
                 (rid, "2026-09-05", "Saturday", 10000, 28.5))
    conn.execute("INSERT INTO ops_issues (restaurant_id, kind, title, created_at) VALUES (?,?,?,?)",
                 (rid, "coverage", "Short a server", "2026-09-05 18:30:00"))
    conn.commit(); conn.close()
    out = intel.record_outcomes(rid, db_path=db_path, today=dt.date(2026, 9, 22))
    assert out["written"] == 2
    intel.record_outcomes(rid, db_path=db_path, today=dt.date(2026, 9, 22))     # idempotent
    conn = get_conn(db_path)
    rows = {r["daypart"]: dict(r) for r in conn.execute("SELECT * FROM schedule_outcomes WHERE history_id=?", (hid,)).fetchall()}
    conn.close()
    assert rows["night"]["hours"] == 12.0 and rows["night"]["people"] == 2 and rows["night"]["issues"] == 1
    assert rows["morning"]["sales"] == 4000 and rows["night"]["sales"] == 6000        # the 40/60 default split
    # a second recorded week makes it readable
    _publish(db_path, rid, "2026-09-07", "2026-09-13", csv_text.replace("2026-09-05", "2026-09-12"))
    intel.record_outcomes(rid, db_path=db_path, today=dt.date(2026, 9, 22))
    by = intel.outcomes_by_daypart(rid, db_path=db_path)
    assert by["Saturday"]["night"]["weeks"] == 2 and by["Saturday"]["night"]["avg_hours"] == 12.0
    assert "Saturday dinner/night" in intel.outcome_block(by, DAYS)


def test_the_rotation_ledger_reads_published_weeks(db_path, rid):
    for w in range(3):
        sat = dt.date(2026, 9, 5) + dt.timedelta(weeks=w)
        csv_text = HEADER + (f"{sat.isoformat()},Saturday,Ana,Server,4:00pm,10:00pm,6.0,\n"
                             f"{sat.isoformat()},Saturday,Bob,Server,11:00am,3:00pm,4.0,\n"
                             f"{(sat - dt.timedelta(days=3)).isoformat()},Wednesday,Cy,Server,4:00pm,10:00pm,6.0,\n")
        _publish(db_path, rid, (sat - dt.timedelta(days=5)).isoformat(), (sat + dt.timedelta(days=1)).isoformat(), csv_text)
    ledger = intel.fairness_ledger(rid, db_path=db_path, today=dt.date(2026, 9, 22))
    assert ledger["Ana"]["weekend"] == 3 and ledger["Ana"]["closing"] == 3 and ledger["Cy"]["weekend"] == 0
    assert ledger["Bob"]["closing"] == 0 and ledger["Ana"]["weeks"] == 3
    assert "Most weekend shifts" in intel.ledger_block(ledger)


def test_fairness_reads_the_ledger_when_this_week_adds_to_a_pattern():
    import shift_quality as sq
    rows = [_row(WEEK[5], n, "4:00pm", "10:00pm") for n in ("Ana", "Bob", "Cy", "Dee")]
    rows += [_row(WEEK[0], n, "4:00pm", "10:00pm") for n in ("Bob", "Cy", "Dee")]
    ledger = {"Ana": {"weekend": 8, "closing": 8, "holiday": 0, "shifts": 20, "weeks": 8},
              "Bob": {"weekend": 1, "closing": 1, "holiday": 0, "shifts": 20, "weeks": 8},
              "Cy": {"weekend": 2, "closing": 2, "holiday": 0, "shifts": 20, "weeks": 8},
              "Dee": {"weekend": 2, "closing": 2, "holiday": 0, "shifts": 20, "weeks": 8}}
    res = sq.score_rows(rows, scores={"Ana": 3}, typical_headcount={"Server": 4}, ledger=ledger)
    sat = next(s for s in res["shifts"] if s["date"] == WEEK[5])
    fair = next(d for d in sat["dimensions"] if d["key"] == "fairness")
    assert any("Ana already has the most weekend shifts of the last 8 published weeks" in w for w in fair["weaknesses"])


def test_behaviour_preferences_need_two_of_the_same(db_path, rid):
    hid = _publish(db_path, rid, WEEK[0], WEEK[6], HEADER + f"{WEEK[6]},Sunday,Ana,Server,4:00pm,10:00pm,6.0,\n")
    conn = get_conn(db_path)
    for d in ("2026-09-06", "2026-09-13"):
        conn.execute("INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, status, replacement_name) "
                     "VALUES (?,?,?,?,?,?,?)", (rid, hid, "Ana", d, "4:00pm", "covered", "Bob"))
    conn.execute("INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, status) "
                 "VALUES (?,?,?,?,?,?)", (rid, hid, "Cy", "2026-09-06", "4:00pm", "withdrawn"))
    conn.commit(); conn.close()
    prefs = intel.behaviour_preferences(rid, db_path=db_path)
    assert prefs["Ana"]["avoids"] == ["Sunday night"] and prefs["Bob"]["prefers"] == ["Sunday night"]
    assert "Cy" not in prefs


def test_mentoring_counts_shifts_beside_a_closer_in_another_role(db_path, rid, monkeypatch):
    shifts = []
    for i in range(10):
        d = f"2026-08-{i + 1:02d}"
        shifts.append({"employee": "Lead", "role": "Bartender", "date": d, "shift_start": "4:00pm"})
        shifts.append({"employee": "Ana", "role": "Bartender" if i < 9 else "Server", "date": d, "shift_start": "4:00pm"})
    shifts += [{"employee": "Ana", "role": "Server", "date": f"2026-07-{i + 1:02d}", "shift_start": "4:00pm"} for i in range(20)]
    monkeypatch.setattr(models, "_cached_shifts", lambda r: shifts)
    monkeypatch.setattr(models, "get_leader_flags", lambda r, db_path=None: {"Lead": True})
    m = intel.mentoring(rid, db_path=db_path)
    assert m["Ana"]["Bartender"] == 9
    assert intel.could_hold(m) == {"Ana": ["Bartender"]}


def test_recommendation_kinds_are_suppressed_after_ten_ignored_showings(db_path, rid):
    import shift_quality as sq
    assert sq.recommendation_kind("Trim about 12h from Friday night to get back under target.") == "hours"
    # Ten separate showings (ten weeks' drafts). The same one re-shown in a
    # single sitting counts once (SCHED-26), so the fixture varies the key.
    for week in range(10):
        intel.record_recommendation(rid, "hours", f"Trim about 12h (week {week})", "shown", db_path=db_path)
    intel.record_recommendation(rid, "hours", "Trim about 12h (week 9)", "shown", db_path=db_path)   # a rescore
    assert intel.suppressed_kinds(rid, db_path=db_path) == {"hours"}
    intel.record_recommendation(rid, "hours", "Trim about 12h", "accepted", db_path=db_path)
    assert intel.suppressed_kinds(rid, db_path=db_path) == set()


def test_learned_patterns_can_be_dismissed_and_restored(db_path, rid):
    key = intel.pattern_key({"kind": "moved_off", "employee": "Ana", "day": "Saturday", "daypart": "night"})
    intel.dismiss_pattern(rid, key, actor="will", db_path=db_path)
    assert key in intel.dismissed_patterns(rid, db_path=db_path)
    intel.restore_pattern(rid, key, db_path=db_path)
    assert key not in intel.dismissed_patterns(rid, db_path=db_path)


def test_tenure_survives_the_upload_window(db_path, rid):
    intel.remember_tenure(rid, [{"employee": "Ana", "date": "2026-03-01"}] * 40 + [{"employee": "Bob", "date": "2026-09-01"}], db_path=db_path)
    out = intel.tenure(rid, {"Ana": 12, "Bob": 1}, db_path=db_path)
    assert out["Ana"] >= 40 and out["Bob"] == 1
    intel.remember_tenure(rid, [{"employee": "Ana", "date": "2026-09-15"}] * 5, db_path=db_path)
    assert intel.tenure(rid, {"Ana": 5}, db_path=db_path)["Ana"] >= 40      # never counts down


# ── swaps and the reservation frame ────────────────────────────────────

def test_a_swap_moves_both_shifts_only_when_both_are_legal(db_path, rid):
    csv_text = HEADER + (f"{WEEK[0]},Monday,Ana,Server,4:00pm,10:00pm,6.0,\n"
                         f"{WEEK[2]},Wednesday,Bob,Server,4:00pm,10:00pm,6.0,\n")
    hid = _publish(db_path, rid, WEEK[0], WEEK[6], csv_text)
    sv.append(rid, hid, "published", csv_text, db_path=db_path)
    today = dt.date(2026, 9, 28)
    with pytest.raises(srq.ShiftRequestError):
        srq.request_swap(rid, "Ana", WEEK[0], "4:00pm", "Ana", WEEK[2], "4:00pm", db_path=db_path, today=today)
    with pytest.raises(srq.ShiftRequestError):
        srq.request_swap(rid, "Ana", WEEK[0], "4:00pm", "Bob", WEEK[3], "4:00pm", db_path=db_path, today=today)   # Bob has no Thursday
    req = srq.request_swap(rid, "Ana", WEEK[0], "4:00pm", "Bob", WEEK[2], "4:00pm", db_path=db_path, today=today)
    assert req["kind"] == "swap" and req["target_name"] == "Bob"
    assert [r["kind"] for r in srq.for_manager(rid, db_path=db_path)] == ["swap"]
    # The manager's yes alone moves nothing: Bob's shift is his (SCHED-21).
    approved = srq.decide(rid, req["id"], True, decided_by="will", db_path=db_path)
    assert approved["status"] == "approved"
    conn = get_conn(db_path)
    assert conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (hid,)).fetchone()["schedule_csv"] == csv_text
    conn.close()
    assert [a["id"] for a in srq.asked_of_me(rid, "bob", db_path=db_path)] == [req["id"]]
    done = srq.respond_swap(rid, req["id"], "Bob", True, db_path=db_path)
    assert done["status"] == "covered"
    conn = get_conn(db_path)
    rows = sv.rows_from_csv(conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (hid,)).fetchone()["schedule_csv"])
    conn.close()
    assert {(r["date"], r["employee"]) for r in rows} == {(WEEK[0], "Bob"), (WEEK[2], "Ana")}
    assert all("swapped with" in r["notes"] for r in rows)


def test_reservation_feeds_are_honest_about_not_being_live(db_path, rid):
    r = models.get_restaurant(rid, db_path)
    st = reservation_feeds.status(r)
    assert st["provider"] is None and not st["live"]
    models.update_restaurant(rid, {"reservation_provider": "tock", "reservation_api_key": "x"}, db_path=db_path)
    st = reservation_feeds.status(models.get_restaurant(rid, db_path))
    assert st["provider"] == "tock" and st["configured"] and not st["live"] and "partner key" in st["message"]
    out = reservation_feeds.sync(rid, db_path=db_path)
    assert out["written"] == 0 and out["error"]
    assert all(not p["live"] for p in reservation_feeds.available())


def test_break_windows_sit_in_the_middle_of_a_long_shift():
    import labor
    assert labor.break_window("11:00am", "9:00pm", 5) == "4:00pm–4:30pm"
    assert labor.break_window("11:00am", "8:30pm", 5) == "3:45pm–4:15pm"
    assert labor.break_window("4:00pm", "8:00pm", 5) == ""
    assert labor.break_window("4:00pm", "11:00pm", None) == ""
    shifts = labor.employee_shifts_from_csv(HEADER + f"{WEEK[0]},Monday,Ana,Server,11:00am,9:00pm,10.0,\n", "Ana", meal_break_after_hours=6)
    assert shifts[0]["break"] == "4:00pm–4:30pm"


def test_the_history_list_reads_the_stored_headline_and_supersedes_drafts(db_path, rid):
    a = models.save_schedule_history(rid, WEEK[0], WEEK[6], 10, 40, 30, HEADER, [], quality={"score": 61, "band": "fair", "confidence": {"level": "high"}},
                                     what_if={"ran": True}, db_path=db_path)
    b = models.save_schedule_history(rid, WEEK[0], WEEK[6], 10, 40, 30, HEADER, [], quality={"score": 70, "band": "fair", "confidence": {"level": "high"}}, db_path=db_path)
    lst = {r["id"]: r for r in models.get_schedule_history(rid, db_path=db_path)}
    assert lst[b]["quality_score"] == 70 and lst[b]["confidence"] == "high"
    assert lst[a]["superseded_by"] == b and lst[b]["superseded_by"] is None
    detail = models.get_schedule_history_detail(a, rid, db_path)
    assert json.loads(detail["what_if_json"])["ran"] is True


def test_demand_blind_notice_reads_the_last_sales_date(db_path, rid):
    import schedule_engine
    assert schedule_engine._demand_data_through(rid) == {"date": None, "days_ago": None, "blind": True}
    conn = get_conn(db_path)
    old = (dt.date.today() - dt.timedelta(days=40)).isoformat()
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales) VALUES (?,?,?,?)", (rid, old, "Monday", 5000))
    conn.commit(); conn.close()
    out = schedule_engine._demand_data_through(rid)
    assert out["date"] == old and out["days_ago"] == 40 and out["blind"]
