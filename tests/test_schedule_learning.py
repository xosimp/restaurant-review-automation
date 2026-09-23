"""schedule_learning — the schedule engine learning from what the manager
does to its drafts and from how published weeks went.

learned_patterns used to read two things (a person moved off / onto a
weekday), a recommendation was only ever 'accepted' through a button, the
record of what published weeks did was prompt text only, and reliability
was one number per person. These pin the retimes, headcount, role changes
and leader swaps the draft now learns; the implicit acceptance an edit
records; the draft-acceptance metric; outcome calibration; attendance by
weekday; and the overtime forecast.
"""
import json
import random

import pytest

import models
import schedule_intel as intel
import schedule_learning as sl
import schedule_rules as sr
import schedule_versions as sv
from models import create_restaurant, Restaurant, get_conn

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _week(i):
    """Seven ISO dates, Monday first, i weeks after Monday 10/5/26."""
    import datetime as dt
    start = dt.date(2026, 10, 5) + dt.timedelta(weeks=i)
    return [(start + dt.timedelta(days=k)).isoformat() for k in range(7)]


def _row(date, emp, start="4:00pm", end="10:00pm", role="Server", hours=6.0):
    import datetime as dt
    day = dt.date.fromisoformat(date).strftime("%A")
    return {"date": date, "day": day, "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(hours), "notes": ""}


def _csv(rows):
    return HEADER + "".join(",".join(str(r[c]) for c in sv.COLS) + "\n" for r in rows)


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, sv, intel):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    monkeypatch.setattr(models, "get_leader_flags", lambda r, db_path=None: {})
    monkeypatch.setattr(models, "get_operational_scores", lambda r, db_path=None: {})
    return create_restaurant(Restaurant(name="Learning Co", owner_email="l@x.com"), db_path=db_path)


def _history(db_path, rid, week_start, csv_text=HEADER, published=False, quality=None, target=30):
    conn = get_conn(db_path)
    cur = conn.execute(
        "INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, labor_target, "
        "schedule_csv, summary_json, quality_json, published_at) VALUES (?,?,?,?,?,?,?,'[]',?,?)",
        (rid, week_start, week_start, 0, 100, target, csv_text, json.dumps(quality) if quality else None,
         "2026-10-01 10:00:00" if published else None))
    conn.commit()
    hid = cur.lastrowid
    conn.close()
    return hid


def _edit_weeks(db_path, rid, pairs):
    """pairs: [(draft_rows, edited_rows)] one per week — a generated version then an edited one."""
    for i, (draft, edited) in enumerate(pairs):
        hid = _history(db_path, rid, _week(i)[0], _csv(draft))
        sv.append(rid, hid, "generated", _csv(draft), saved_by="Cavnar AI", db_path=db_path)
        sv.append(rid, hid, "edited", _csv(edited), saved_by="will", db_path=db_path)


# ── the diff sees what its key cannot ──────────────────────────────────

def test_diff_reports_an_end_only_retime_and_a_role_change():
    d0 = _week(0)[5]
    before = [_row(d0, "Ana"), _row(d0, "Bob")]
    after = [_row(d0, "Ana", end="11:00pm", hours=7.0), _row(d0, "Bob", role="Bartender")]
    d = sv.diff(before, after)
    assert [r["employee"] for r in d["retimed"]] == ["Ana"]
    assert d["retimed"][0]["old_end"] == "10:00pm" and d["retimed"][0]["new_end"] == "11:00pm"
    assert d["retimed"][0]["role"] == "Server" and d["retimed"][0]["hours_delta"] == 1.0
    assert d["role_changed"] == [{"date": d0, "day": "Saturday", "employee": "Bob", "shift_start": "4:00pm",
                                  "old_role": "Server", "new_role": "Bartender"}]
    assert d["changes"] == 2 and not d["added"] and not d["removed"]
    assert any("Server → Bartender" in l for l in sv.diff_lines(d))


# ── retimes ────────────────────────────────────────────────────────────

def test_a_start_the_manager_keeps_moving_to_is_learned_per_role_day_and_daypart(db_path, rid):
    pairs = []
    for i in range(2):
        fri = _week(i)[4]
        draft = [_row(fri, "Ana"), _row(fri, "Bob"), _row(fri, "Cy", role="Cook", start="3:00pm")]
        edited = [_row(fri, "Ana", start="4:30pm", hours=5.5), _row(fri, "Bob", start="4:30pm", hours=5.5),
                  _row(fri, "Cy", role="Cook", start="3:00pm")]
        pairs.append((draft, edited))
    _edit_weeks(db_path, rid, pairs)
    learned = sv.learned_patterns(rid, db_path=db_path)
    retimes = [p for p in learned if p["kind"] == "retime_start"]
    assert len(retimes) == 1
    p = retimes[0]
    assert (p["role"], p["day"], p["daypart"], p["time"], p["times"]) == ("Server", "Friday", "night", "4:30pm", 2)
    assert "keeps starting Servers on Friday dinner/night at 4:30pm" in p["text"]
    assert "keeps starting Servers on Friday dinner/night at 4:30pm" in sv.prompt_block(learned)
    # two servers retimed in one week is one week of evidence
    assert not [p for p in sv.learned_patterns(rid, min_repeats=3, db_path=db_path) if p["kind"] == "retime_start"]


def test_one_week_of_retimes_is_not_a_pattern(db_path, rid):
    fri = _week(0)[4]
    _edit_weeks(db_path, rid, [([_row(fri, "Ana")], [_row(fri, "Ana", start="4:30pm", hours=5.5)])])
    assert not [p for p in sv.learned_patterns(rid, db_path=db_path) if p["kind"].startswith("retime")]


# ── headcount ──────────────────────────────────────────────────────────

def test_headcount_the_manager_keeps_adding_is_a_fact_and_a_structured_adjustment(db_path, rid):
    pairs = []
    for i in range(3):
        sat, mon = _week(i)[5], _week(i)[0]
        draft = [_row(sat, "Ana"), _row(sat, "Bob"), _row(mon, "Cy", start="10:00am", end="3:00pm", hours=5)]
        edited = draft + [_row(sat, "Dee")]
        if i == 0:
            edited = [r for r in edited if r["employee"] != "Cy"]     # one Monday cut: not a pattern
        pairs.append((draft, edited))
    _edit_weeks(db_path, rid, pairs)
    pats = [p for p in sv.learned_patterns(rid, db_path=db_path) if p["kind"].startswith("headcount")]
    assert [(p["kind"], p["role"], p["day"], p["daypart"], p["delta"], p["times"]) for p in pats] == \
        [("headcount_add", "Server", "Saturday", "night", 1, 3)]
    assert "added 1 Server to Saturday dinner/night in 3 recent weeks" in pats[0]["text"]
    assert sl.learned_headcount_adjustments(rid, db_path=db_path) == {("Saturday", "night"): {"Server": 1}}
    # the owner's dismissal reaches the structured feed too
    intel.dismiss_pattern(rid, intel.pattern_key(pats[0]), db_path=db_path)
    assert sl.learned_headcount_adjustments(rid, db_path=db_path) == {}


def test_headcount_needs_more_weeks_one_way_than_the_other(db_path, rid):
    pairs = []
    for i, extra in enumerate((1, -1)):
        sat = _week(i)[5]
        draft = [_row(sat, "Ana"), _row(sat, "Bob")]
        edited = draft + [_row(sat, "Dee")] if extra > 0 else draft[:1]
        pairs.append((draft, edited))
    _edit_weeks(db_path, rid, pairs)
    assert sl.learned_headcount_adjustments(rid, min_weeks=1, db_path=db_path) == {}


# ── role changes and leader swaps ──────────────────────────────────────

def test_a_role_change_the_manager_keeps_making_is_learned(db_path, rid):
    pairs = []
    for i in range(2):
        sat = _week(i)[5]
        pairs.append(([_row(sat, "Ana"), _row(sat, "Bob")], [_row(sat, "Ana", role="Bartender"), _row(sat, "Bob")]))
    _edit_weeks(db_path, rid, pairs)
    rc = [p for p in sv.learned_patterns(rid, db_path=db_path) if p["kind"] == "role_change"]
    assert len(rc) == 1
    assert (rc[0]["employee"], rc[0]["was_role"], rc[0]["role"], rc[0]["day"], rc[0]["daypart"]) == \
        ("Ana", "Server", "Bartender", "Saturday", "night")
    assert "switching Ana from Server to Bartender on Saturday dinner/night" in rc[0]["text"]


def test_a_leader_swapped_onto_a_busy_night_is_learned(db_path, rid, monkeypatch):
    pairs = []
    for i in range(2):
        fri = _week(i)[4]
        pairs.append(([_row(fri, "Ana"), _row(fri, "Bob")], [_row(fri, "Cy"), _row(fri, "Bob")]))
    _edit_weeks(db_path, rid, pairs)
    # nobody flagged or rated: a move is only a move
    assert not [p for p in sv.learned_patterns(rid, db_path=db_path) if p["kind"] == "leader_swap"]
    monkeypatch.setattr(models, "get_leader_flags", lambda r, db_path=None: {"Cy": True})
    ls = [p for p in sv.learned_patterns(rid, db_path=db_path) if p["kind"] == "leader_swap"]
    assert len(ls) == 1 and (ls[0]["day"], ls[0]["daypart"], ls[0]["role"], ls[0]["times"]) == ("Friday", "night", "Server", 2)
    assert ls[0]["names"] == ["Cy"] and "stronger hand" in ls[0]["text"]
    # a higher Operational Score counts too
    monkeypatch.setattr(models, "get_leader_flags", lambda r, db_path=None: {})
    monkeypatch.setattr(models, "get_operational_scores", lambda r, db_path=None: {"Cy": 5, "Ana": 3})
    assert [p for p in sv.learned_patterns(rid, db_path=db_path) if p["kind"] == "leader_swap"]


def test_a_weeknight_move_is_not_a_leader_swap(db_path, rid, monkeypatch):
    monkeypatch.setattr(models, "get_leader_flags", lambda r, db_path=None: {"Cy": True})
    pairs = []
    for i in range(2):
        tue = _week(i)[1]
        pairs.append(([_row(tue, "Ana")], [_row(tue, "Cy")]))
    _edit_weeks(db_path, rid, pairs)
    assert not [p for p in sv.learned_patterns(rid, db_path=db_path) if p["kind"] == "leader_swap"]


# ── dismissal keys ─────────────────────────────────────────────────────

def test_pattern_keys_keep_the_old_shape_and_separate_roles_for_the_new_kinds():
    assert intel.pattern_key({"kind": "moved_off", "employee": "Ana", "day": "Saturday", "daypart": "night"}) == \
        "moved_off|ana|Saturday|night"
    a = intel.pattern_key({"kind": "headcount_add", "employee": "", "role": "Server", "day": "Saturday", "daypart": "night"})
    b = intel.pattern_key({"kind": "headcount_add", "employee": "", "role": "Host", "day": "Saturday", "daypart": "night"})
    c = intel.pattern_key({"kind": "retime_start", "employee": "", "role": "Server", "day": "Friday",
                           "daypart": "night", "time": "4:30pm"})
    assert a != b and a == "headcount_add||Saturday|night|server||" and c.endswith("|server||4:30pm")


def test_the_prompt_leaves_out_dismissed_new_kinds(db_path, rid):
    """schedule_engine filters learned_patterns by pattern_key; a dismissed
    retime must drop out that way while a legacy kind stays."""
    pairs = []
    for i in range(2):
        fri = _week(i)[4]
        pairs.append(([_row(fri, "Ana"), _row(fri, "Bob")], [_row(fri, "Ana", start="4:30pm", hours=5.5), _row(fri, "Dee", start="5:00pm", hours=5)]))
    _edit_weeks(db_path, rid, pairs)
    learned = sv.learned_patterns(rid, db_path=db_path)
    retime = next(p for p in learned if p["kind"] == "retime_start")
    intel.dismiss_pattern(rid, intel.pattern_key(retime), db_path=db_path)
    dismissed = intel.dismissed_patterns(rid, db_path=db_path)
    kept = [p for p in learned if intel.pattern_key(p) not in dismissed]
    assert not [p for p in kept if p["kind"] == "retime_start"]
    assert [p for p in kept if p["kind"] == "moved_off" and p["employee"] == "Bob"]


# ── draft acceptance ───────────────────────────────────────────────────

def test_acceptance_counts_what_survived_from_draft_to_published_and_the_trend(db_path, rid):
    for i, kept in enumerate((2, 3, 4, 4)):       # of four drafted rows, how many went out untouched
        days = _week(i)
        draft = [_row(days[k], "Ana") for k in range(4)]
        published = draft[:kept] + [_row(days[k], "Bob") for k in range(kept, 4)]
        hid = _history(db_path, rid, days[0], _csv(published), published=True)
        sv.append(rid, hid, "generated", _csv(draft), db_path=db_path)
        sv.append(rid, hid, "edited", _csv(published), db_path=db_path)
        sv.append(rid, hid, "published", _csv(published), db_path=db_path)
    # an uploaded week (no generated version) is not judged
    _history(db_path, rid, _week(9)[0], _csv([_row(_week(9)[0], "Ana")]), published=True)
    a = sv.acceptance(rid, db_path=db_path)
    assert a["available"] and [w["week_start"] for w in a["weeks"]] == [_week(i)[0] for i in (3, 2, 1, 0)]
    assert [w["unchanged_share"] for w in a["weeks"]] == [1.0, 1.0, 0.75, 0.5]
    assert [w["changes"] for w in a["weeks"]] == [0, 0, 1, 2]
    assert a["trend"]["direction"] == "rising" and a["trend"]["delta"] == 0.375
    assert sv.acceptance(rid + 99, db_path=db_path) == {"available": False, "weeks": [], "trend": None,
                                                        "mean_unchanged_share": None, "mean_changes": None}


# ── implicit recommendation acceptance ─────────────────────────────────

RECS = ["Fill the gap on Saturday night: Server short 1 of 3.",
        "Trim about 6h from Monday morning to get back under target.",
        "Give Ana a day off — 6 in a row this week.",
        "Move somebody who clears \"a closer on Friday night\" onto Friday night."]


def _accepted(db_path, rid):
    conn = get_conn(db_path)
    try:
        return [(r["kind"], r["key"], r["actor"]) for r in conn.execute(
            "SELECT kind, key, actor FROM schedule_recommendation_events WHERE restaurant_id=? AND action='accepted' ORDER BY id",
            (rid,)).fetchall()]
    finally:
        conn.close()


def test_addressed_recommendations_reads_each_kind_conservatively():
    w = _week(0)
    before = [_row(w[5], "Ana"), _row(w[5], "Bob"),
              _row(w[0], "Cy", start="9:00am", end="3:00pm"), _row(w[0], "Dee", start="9:00am", end="3:00pm"),
              _row(w[4], "Bob")] + [_row(w[k], "Ana") for k in range(6)]
    assert sl.addressed_recommendations(RECS, before, before) == []
    filled = before + [_row(w[5], "Eve")]
    assert sl.addressed_recommendations(RECS, before, filled) == [RECS[0]]
    host = before + [_row(w[5], "Eve", role="Host")]                 # the wrong role does not fill a Server gap
    assert sl.addressed_recommendations(RECS[:1], before, host) == []
    trimmed = [r for r in before if not (r["employee"] == "Dee")]    # −6h on Monday morning
    assert sl.addressed_recommendations(RECS, before, trimmed) == [RECS[1]]
    rested = [r for r in before if not (r["employee"] == "Ana" and r["date"] == w[2])]
    assert RECS[2] in sl.addressed_recommendations(RECS, before, rested)
    led = [r if not (r["employee"] == "Bob" and r["date"] == w[4]) else _row(w[4], "Zed") for r in before]
    assert sl.addressed_recommendations(RECS, before, led) == [RECS[3]]
    assert sl.addressed_recommendations(["Something unparseable."], before, filled) == []


def test_an_edit_that_carries_out_a_recommendation_records_it_accepted(db_path, rid):
    w = _week(0)
    draft = [_row(w[5], "Ana"), _row(w[5], "Bob"), _row(w[0], "Cy", start="9:00am", end="3:00pm")]
    hid = _history(db_path, rid, w[0], _csv(draft))
    sv.append(rid, hid, "generated", _csv(draft), quality={"score": 70, "recommendations": RECS}, db_path=db_path)
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        sv.write_on(conn, rid, hid, "edited", _csv(draft + [_row(w[5], "Eve")]), saved_by="will", expected_version=1)
        conn.commit()
    finally:
        conn.close()
    assert _accepted(db_path, rid) == [("coverage", RECS[0][:200], "will")]
    assert intel.suppressed_kinds(rid, db_path=db_path) == set()
    # a second save that changes nothing else records nothing more
    sv.append(rid, hid, "edited", _csv(draft + [_row(w[5], "Eve"), _row(w[1], "Fay")]), saved_by="will", db_path=db_path)
    assert len(_accepted(db_path, rid)) == 1


def test_implied_acceptance_rolls_back_with_the_save_it_rode_on(db_path, rid):
    """write_on runs inside the caller's transaction; a save that is rolled
    back must not leave an 'accepted' behind."""
    w = _week(0)
    draft = [_row(w[5], "Ana")]
    hid = _history(db_path, rid, w[0], _csv(draft))
    sv.append(rid, hid, "generated", _csv(draft), quality={"recommendations": RECS[:1]}, db_path=db_path)
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        sv.write_on(conn, rid, hid, "edited", _csv(draft + [_row(w[5], "Eve")]), saved_by="will")
        conn.rollback()
    finally:
        conn.close()
    assert _accepted(db_path, rid) == []
    assert [v["version"] for v in sv.list_versions(rid, hid, db_path=db_path)] == [1]


def test_a_generated_or_published_version_never_implies_acceptance(db_path, rid):
    w = _week(0)
    draft = [_row(w[5], "Ana")]
    hid = _history(db_path, rid, w[0], _csv(draft))
    sv.append(rid, hid, "generated", _csv(draft), quality={"recommendations": RECS[:1]}, db_path=db_path)
    sv.append(rid, hid, "published", _csv(draft + [_row(w[5], "Eve")]), db_path=db_path)
    assert _accepted(db_path, rid) == []


# ── outcome calibration ────────────────────────────────────────────────

def _calibration_world(db_path, rid, weeks, shifts_per_week=6):
    rng = random.Random(7)
    conn = get_conn(db_path)
    for i in range(weeks):
        days = _week(i)
        shifts, outcomes = [], []
        for k in range(shifts_per_week):
            d, part = days[k % 7], ("night" if k % 2 else "morning")
            cov = rng.choice([50, 60, 70, 80, 90, 100])
            fair = rng.choice([40, 60, 80, 100])                      # unrelated to anything
            shifts.append({"date": d, "day": DAYS[k % 7], "daypart": part, "scored": True, "score": cov,
                           "dimensions": [{"key": "coverage", "score": cov}, {"key": "fairness", "score": fair},
                                          {"key": "stability", "score": 80}]})
            issues = 0 if cov >= 80 else (1 if cov >= 60 else 2)          # thin coverage goes wrong
            outcomes.append((d, part, issues, 4.8 if cov >= 80 else 3.9, 30 + (100 - cov) / 20))
        cur = conn.execute(
            "INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, labor_target, "
            "schedule_csv, summary_json, quality_json, published_at) VALUES (?,?,?,0,0,30,?,'[]',?,'2026-10-01')",
            (rid, days[0], days[6], HEADER, json.dumps({"score": 80, "shifts": shifts})))
        for d, part, issues, rating, labor in outcomes:
            conn.execute("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, people, issues, "
                         "review_rating, labor_pct) VALUES (?,?,?,?,10,2,?,?,?)", (rid, cur.lastrowid, d, part, issues, rating, labor))
    conn.commit()
    conn.close()


def test_calibration_refuses_on_too_little_record(db_path, rid):
    _calibration_world(db_path, rid, weeks=5)
    out = sl.calibrate_weights(rid, db_path=db_path)
    assert out["ready"] is False and out["weeks"] == 5 and out["shifts"] == 30
    assert "at least 8 weeks and 40 shifts" in out["reason"]


def test_calibration_suggests_bounded_nudges_and_applies_nothing(db_path, rid, monkeypatch):
    """Updated for #39: issues count only on watched nights (every night is
    watched here), and one suggestion moves a weight at most 10% of its
    default toward the fitted 30% — it used to jump the whole 30% at once."""
    import shift_quality
    before = dict(shift_quality.DEFAULT_WEIGHTS)
    monkeypatch.setattr(intel, "watched_dates", lambda rid, a, b, db_path=None: {d for w in range(9) for d in _week(w)})
    _calibration_world(db_path, rid, weeks=9)
    out = sl.calibrate_weights(rid, db_path=db_path, current_weights={})
    assert out["ready"] is True and out["applied"] is False and out["weeks"] == 9 and out["shifts"] == 54
    cov = out["dimensions"]["coverage"]
    assert cov["correlation"]["issues"] < -0.5 and cov["correlation"]["review_rating"] > 0.5
    assert cov["correlation"]["labor_vs_target"] < -0.5
    assert cov["target"] == round(before["coverage"] * 1.3, 1)
    assert cov["nudge_pct"] == 10 and cov["suggested"] == round(before["coverage"] * 1.1, 1)
    # a dimension that never varied says nothing, and one with no data is left alone
    assert out["dimensions"]["stability"]["nudge_pct"] == 0 and out["dimensions"]["stability"]["evidence"] is None
    assert out["dimensions"]["leadership"]["suggested"] == before["leadership"]
    assert all(abs(d["nudge_pct"]) <= 30 for d in out["dimensions"].values())
    assert shift_quality.DEFAULT_WEIGHTS == before
    assert sl.calibrate_weights(rid, db_path=db_path) == out            # deterministic


# ── attendance by weekday ──────────────────────────────────────────────

def _clocked(monkeypatch):
    shifts = []
    for i in range(4):
        w = _week(i)
        for name in ("Ana", "Bob"):
            for k in (0, 5):
                missed = name == "Ana" and k == 5 and i < 2          # Ana misses two of four Saturdays
                shifts.append({"employee": name, "date": w[k], "scheduled_hours": "6", "actual_hours": "0" if missed else "6"})
    shifts.append({"employee": "Cy", "date": _week(0)[5], "scheduled_hours": "6", "actual_hours": ""})   # no clock reading
    monkeypatch.setattr(models, "_cached_shifts", lambda r: shifts)


def test_attendance_is_read_per_weekday_with_a_sample_floor(db_path, rid, monkeypatch):
    _clocked(monkeypatch)
    att = sl.attendance_by_weekday(rid, db_path=db_path)
    assert att["Ana"]["Saturday"] == {"shifts": 4, "no_shows": 2, "no_show_rate": 0.5}
    assert att["Ana"]["Monday"]["no_show_rate"] == 0.0 and att["Bob"]["Saturday"]["no_show_rate"] == 0.0
    assert "Cy" not in att
    assert sl.attendance_by_weekday(rid, min_shifts=5, db_path=db_path) == {}


def test_standby_days_names_the_riskiest_dates(db_path, rid, monkeypatch):
    _clocked(monkeypatch)
    w = _week(6)
    rows = [_row(w[0], "Ana"), _row(w[0], "Bob"), _row(w[5], "Ana"), _row(w[5], "Bob"), _row(w[5], "Cy")]
    out = sl.standby_days(rid, w, rows=rows, db_path=db_path)
    assert [d["date"] for d in out] == [w[5]]
    sat = out[0]
    assert sat["chance_of_a_no_show"] == 0.5 and sat["no_record"] == 1 and sat["scheduled"] == 3
    assert sat["people"] == [{"employee": "Ana", "no_show_rate": 0.5, "basis": "Saturdays"}]
    # without rows it reads the stored week
    _history(db_path, rid, w[0], _csv(rows))
    assert [d["date"] for d in sl.standby_days(rid, w, db_path=db_path)] == [w[5]]


# ── overtime forecast ──────────────────────────────────────────────────

def test_overtime_forecast_counts_published_hours_and_finds_a_same_role_person_with_room():
    w = _week(0)
    rows = [_row(w[k], "Ana", start="9:00am", end="5:00pm", hours=8) for k in range(5)]        # 40h
    rows += [_row(w[k], "Bob", start="9:00am", end="5:00pm", hours=8) for k in range(2)]       # 16h
    rows += [_row(w[k], "Cook Cy", role="Cook", start="9:00am", end="5:00pm", hours=8) for k in range(5, 7)]
    out = sl.overtime_forecast(rows, base_hours={"ana": 6})
    assert len(out) == 1
    f = out[0]
    assert (f["employee"], f["hours"], f["published_hours"], f["over"], f["ceiling"]) == ("Ana", 46.0, 6.0, 6.0, 40.0)
    assert f["candidate"]["employee"] == "Bob" and f["candidate"]["headroom"] == 24.0
    assert f["candidate"]["date"] not in (w[0], w[1])                 # Bob already works those days
    assert "46h" in f["text"] and "Bob" in f["text"] and "10/7/26" in f["text"] and w[2] not in f["text"]
    assert sl.overtime_forecast(rows) == []                           # 40h is not over 40h


def test_overtime_forecast_reads_payroll_weeks_and_caps_from_constraints():
    w = _week(0)
    c = sr.Constraints(restaurant_id=1, week_dates=w, week_days=DAYS)
    c.week_start_day = 3                                              # payroll weeks start Thursday
    c.base_hours = {"ana": {c.bucket(w[0]): 30.0}}                    # Mon-Wed belong to a week already carrying 30h
    c.hours_limits = {"bob": (None, 20)}
    rows = [_row(w[k], "Ana", start="9:00am", end="5:00pm", hours=8) for k in range(3)]       # 24h Mon-Wed
    rows += [_row(w[3], "Bob", start="9:00am", end="5:00pm", hours=8)]
    rows += [_row(w[4], "Dee", start="9:00am", end="5:00pm", hours=8)]
    out = sl.overtime_forecast(rows, constraints=c)
    assert [(f["employee"], f["hours"], f["over"]) for f in out] == [("Ana", 54.0, 14.0)]
    assert out[0]["candidate"]["employee"] == "Dee"                 # the most room in Ana's payroll week
    assert "payroll week of 10/1/26" in out[0]["text"]              # M/D/YY, never ISO
