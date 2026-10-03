"""What the week can see around itself (schedule audit 10/3/26 D-20, D-21,
D-22, E-11, E-10, E-26, D-40).

D-20: the ledger and the rotation counted any shift ending after 1:30am as a
close on a 2am-close night (raw minutes: "2:00am" is 120), and every shift
on a midnight-close Thursday.
D-21: the ledger and the rotation read published weeks only, so Simple
EJ's — no Cavnar AI week published, months of RPOWER punches — drafted with
no fairness or rotation memory.
D-22 / E-11 / E-10: the tail the rest rule, the run of days and the payroll
hours read was "the two newest published weeks by id" — with this week and
next published, last week dropped out; a draft of last week counted for
nothing; with nothing published, last week's real shifts were invisible.
E-26 / D-40: siblings joined only on the same owner email, by raw name,
and any shift at a sibling blocked the whole date.
"""
import datetime as dt
import sys

import pytest

import models
import schedule_engine as se
import schedule_intel as si
import schedule_rules as sr
import shift_facts

WEEK = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27"]
LAST = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19", "2026-09-20"]
NEXT = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


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
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now_by_id",
                        lambda *a, **k: dt.datetime(2026, 10, 3, 9, 0))
    yield


def _rid(name="Tail Co", email="t@x.test", staff=("Ana", "Ben", "Cy")):
    rid = models.create_restaurant(models.Restaurant(name=name, owner_email=email, module_labor=1))
    for n in staff:
        models.add_manual_team_member(rid, n, role="Server")
    return rid


def _day(d):
    return dt.date.fromisoformat(d).strftime("%A")


def _week(rid, dates, rows, published=True):
    csv = HEADER + "\n" + "\n".join(f"{d},{_day(d)},{n},Server,{s},{e},{h}," for d, n, s, e, h in rows) + "\n"
    hid = models.save_schedule_history(rid, dates[0], dates[-1], 0, 0, 0, csv, [])
    if published:
        conn = models.get_conn()
        try:
            conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
            conn.commit()
        finally:
            conn.close()
    return hid


def _punches(rid, rows):
    facts = [shift_facts._fact({"date": d, "employee": n, "role": role, "shift_start": s, "shift_end": e,
                                "actual_hours": h, "schedule_known": "0"}, "rpower") for d, n, role, s, e, h in rows]
    conn = models.get_conn()
    try:
        shift_facts._write_facts(conn, rid, facts, min(f["business_date"] for f in facts),
                                 max(f["business_date"] for f in facts))
        conn.commit()
    finally:
        conn.close()


def _row(name, start, end, d):
    s, e = sr.parse_minutes(start), sr.parse_minutes(end)
    return {"date": d, "day": _day(d), "employee": name, "role": "Server", "shift_start": start, "shift_end": end,
            "scheduled_hours": str(round(((e - s) % 1440) / 60, 1)), "notes": ""}


# ── D-20: a close across midnight ───────────────────────────────────────────

def test_a_close_is_read_across_midnight():
    close = {"Friday": "2:00am", "Thursday": "12:00am"}
    assert sr.parse_minutes("2:00am") == 120
    assert not si._closes(_row("Ana", "4:00pm", "10:00pm", "2026-09-25"), close)     # Friday dinner
    assert si._closes(_row("Ana", "6:00pm", "2:00am", "2026-09-25"), close)
    assert not si._closes(_row("Ana", "11:00am", "9:00pm", "2026-09-24"), close)    # Thursday
    assert si._closes(_row("Ana", "5:00pm", "12:00am", "2026-09-24"), close)
    assert si._closes(_row("Ana", "6:00pm", "1:00am", "2026-09-23"), {})            # no close set: late is a close


def test_the_ledger_counts_only_real_closes_on_a_late_night(monkeypatch):
    rid = _rid()
    models.save_close_times(rid, {"Friday": "2:00am"}) if hasattr(models, "save_close_times") else None
    monkeypatch.setattr(models, "get_close_times", lambda *a, **k: {"Friday": "2:00am"})
    _week(rid, LAST, [(LAST[4], "Ana", "4:00pm", "10:00pm", 6), (LAST[4], "Ben", "6:00pm", "2:00am", 8)])
    led = si.fairness_ledger(rid, today=dt.date(2026, 9, 23))
    assert led["Ana"]["closing"] == 0 and led["Ben"]["closing"] == 1


# ── D-21: the record from punches until weeks are published ─────────────────

def _three_weeks_of_punches(rid):
    rows = []
    for w in range(3):
        base = dt.date(2026, 9, 21) - dt.timedelta(weeks=w + 1)
        for i, n in enumerate(("Ana", "Ben", "Cy")):
            for k in range(4):
                d = (base + dt.timedelta(days=(i + k) % 7)).isoformat()
                rows.append((d, n, "Server", "5:00pm", "11:00pm", 6.0))
    _punches(rid, rows)


def test_the_ledger_and_the_rotation_are_seeded_from_punches():
    rid = _rid()
    _three_weeks_of_punches(rid)
    led = si.fairness_ledger(rid, today=dt.date(2026, 9, 23))
    assert set(led) == {"Ana", "Ben", "Cy"} and led["Ana"]["weeks"] == 3 and led["Ana"]["from_punches"] == 3
    assert "from punches" in si.ledger_block(led)
    plan = si.rotation_plan(rid, today=dt.date(2026, 9, 23), roster_roles={"Ana": "Server", "Ben": "Server",
                                                                          "Cy": "Server"})
    assert plan and plan["source"] == "punches" and plan["from_punches"] == 3
    assert "from punches" in si.rotation_block(plan)


def test_a_published_week_takes_its_week_over_from_the_punches():
    rid = _rid()
    _three_weeks_of_punches(rid)
    _week(rid, LAST, [(LAST[0], "Ana", "9:00am", "3:00pm", 6)])
    hist = si.record_weeks(rid, today=dt.date(2026, 9, 23))
    assert [(ws, src) for ws, _r, src in hist] == [(LAST[0], "published"), ("2026-09-07", "punches"),
                                                    ("2026-08-31", "punches")]


# ── D-22 / E-11 / E-10: the tail around the week ────────────────────────────

def test_with_nothing_published_last_weeks_punches_are_the_seam():
    rid = _rid()
    _punches(rid, [(LAST[6], "Ana", "Server", "6:00pm", "2:00am", 8.0)] +
                  [(d, "Ben", "Server", "5:00pm", "10:00pm", 5.0) for d in LAST[1:]])
    c = sr.build_constraints(rid, WEEK, list(sr.DAYS))
    assert any(r.get("_source") == "punches" for r in c.base_rows["ana"])
    rest = [v for v in sr.violations([_row("Ana", "9:00am", "3:00pm", WEEK[0])], c, person_only=True)
            if v["kind"] == "rest_gap"]
    assert rest and "from punches" in rest[0]["detail"]
    run = [v for v in sr.violations([_row("Ben", "5:00pm", "10:00pm", WEEK[0])], c, person_only=True)
           if v["kind"] == "long_run"]
    assert run                                  # six clocked days + Monday is seven in a row
    seam = se._prior_week_assignments(rid, before=WEEK[0])
    assert seam["Ana"][-1]["date"] == LAST[6] and seam["Ana"][-1]["source"] == "punches"


def test_with_this_week_and_next_published_last_week_still_counts():
    rid = _rid()
    _week(rid, LAST, [(LAST[6], "Ana", "6:00pm", "2:00am", 8)])
    _week(rid, WEEK, [(WEEK[2], "Ben", "5:00pm", "10:00pm", 5)])
    _week(rid, NEXT, [(NEXT[0], "Cy", "9:00am", "3:00pm", 6)])
    c = sr.build_constraints(rid, WEEK, list(sr.DAYS))          # regenerating this week
    assert [r["date"] for r in c.base_rows.get("ana", [])] == [LAST[6]]
    assert [r["date"] for r in c.base_rows.get("cy", [])] == [NEXT[0]]
    assert "ben" not in c.base_rows                              # this week's own rows are never the tail
    assert any(v["kind"] == "rest_gap" for v in
               sr.violations([_row("Ana", "9:00am", "3:00pm", WEEK[0])], c, person_only=True))


def test_an_unsent_draft_of_last_week_is_a_labelled_tail(monkeypatch):
    rid = _rid()
    models.update_restaurant(rid, {"week_start_day": 2})        # a Wednesday payroll week
    _week(rid, LAST, [(d, "Ana", "9:00am", "5:00pm", 8) for d in LAST[2:]], published=False)
    _week(rid, WEEK, [(WEEK[0], "Ben", "9:00am", "1:00pm", 4)], published=False)   # this week's own draft
    c = sr.build_constraints(rid, WEEK, list(sr.DAYS))
    assert {r.get("_source") for r in c.base_rows["ana"]} == {"draft"}
    assert "ben" not in c.base_rows
    # 40h Wed-Sun in the draft: Monday and Tuesday are past the overtime line.
    assert sum(c.base_hours["ana"].values()) == 40.0
    ok, why = c.can_add(_row("Ana", "9:00am", "3:00pm", WEEK[0]), [])
    assert not ok and "40h" in why


# ── E-26 / D-40: the organisation's other sites ─────────────────────────────

def _org(*rids):
    conn = models.get_conn()
    try:
        for r in rids:
            conn.execute("UPDATE restaurants SET organization_id=77, location_group='Group' WHERE id=?", (r,))
        conn.commit()
    finally:
        conn.close()


def test_sites_of_one_organisation_see_each_other_whoever_onboarded_them():
    a = _rid("Site A", "owner@x.test")
    b = _rid("Site B", "gm@x.test")                             # onboarded under the GM's email
    models.update_restaurant(b, {"location_name": "Site B"})
    _org(a, b)
    _week(b, WEEK, [(WEEK[2], "Ana", "11:00am", "3:00pm", 4)])
    c = sr.build_constraints(a, WEEK, list(sr.DAYS))
    rows = c.base_rows.get("ana") or []
    assert rows and rows[0]["_site"] == "Site B"
    # D-40: dinner here after lunch there is a double, not a block.
    assert c.can_work("Ana", WEEK[2])[0]
    assert not sr.violations([_row("Ana", "5:00pm", "10:00pm", WEEK[2])], c, person_only=True)
    over = [v for v in sr.violations([_row("Ana", "2:00pm", "8:00pm", WEEK[2])], c, person_only=True)
            if v["kind"] == "overlap"]
    assert over and "at Site B" in over[0]["detail"]
    assert not c.can_add(_row("Ana", "2:00pm", "8:00pm", WEEK[2]), [])[0]
    # The scorer's sibling check is by time too.
    sib = models.sibling_location_shifts(a, WEEK)
    assert sib["Ana"][0]["shift_start"] == "11:00am"


def test_a_person_is_matched_across_sites_through_their_identity():
    import people
    a = _rid("Site A", "owner@x.test", staff=("Michael Smith",))
    b = _rid("Site B", "gm@x.test", staff=("Mike Smith",))
    _org(a, b)
    people.stamp_person_ids(a)
    conn = people._conn(None)
    try:
        idx = people._Index(conn, a)
        pid = next(iter(idx.for_key(people._nk("Michael Smith"))))
        people._alias(conn, idx, pid, "manual", "Mike Smith")
        conn.commit()
    finally:
        conn.close()
    _week(b, WEEK, [(WEEK[3], "Mike Smith", "5:00pm", "11:00pm", 6)])
    c = sr.build_constraints(a, WEEK, list(sr.DAYS))
    assert [r["date"] for r in c.base_rows.get("michael smith", [])] == [WEEK[3]]
    assert models.sibling_location_shifts(a, WEEK).get("Michael Smith")


def test_an_unrelated_restaurant_with_the_same_group_name_is_never_a_sibling():
    a = _rid("Syrup", "one@x.test")
    b = _rid("Syrup Two", "two@x.test")
    conn = models.get_conn()
    try:
        conn.execute("UPDATE restaurants SET location_group='Syrup' WHERE id IN (?,?)", (a, b))
        conn.commit()
    finally:
        conn.close()
    _week(b, WEEK, [(WEEK[2], "Ana", "11:00am", "3:00pm", 4)])
    assert "ana" not in sr.build_constraints(a, WEEK, list(sr.DAYS)).base_rows


def test_a_close_this_week_before_next_weeks_published_open_is_a_rest_breach():
    # The tail after the week was read and never compared: a row of this
    # week was only checked against a tail row that came BEFORE it.
    rid = _rid()
    _week(rid, NEXT, [(NEXT[0], "Ana", "8:00am", "2:00pm", 6)])
    c = sr.build_constraints(rid, WEEK, list(sr.DAYS))
    sunday_close = _row("Ana", "6:00pm", "2:00am", WEEK[6])
    rest = [v for v in sr.violations([sunday_close], c, person_only=True) if v["kind"] == "rest_gap"]
    assert rest and rest[0]["date"] == WEEK[6] and "before their next shift" in rest[0]["detail"]


def test_an_overlap_inside_a_long_shift_is_found():
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=list(sr.DAYS))
    rows = [_row("Ana", "9:00am", "9:00pm", WEEK[1]), _row("Ana", "10:00am", "11:00am", WEEK[1]),
            _row("Ana", "1:00pm", "3:00pm", WEEK[1])]
    over = [v for v in sr.violations(rows, c, person_only=True) if v["kind"] == "overlap"]
    assert sorted(v["index"] for v in over) == [1, 2]


def test_the_week_being_rescored_is_never_its_own_tail():
    # A live rescore builds constraints from the edited rows' dates; the
    # stored copy of the same week (its Sunday removed in the edit) must not
    # come back as "last week".
    rid = _rid()
    _week(rid, WEEK, [(WEEK[0], "Ana", "9:00am", "3:00pm", 6), (WEEK[6], "Ana", "6:00pm", "11:00pm", 5)])
    c = sr.build_constraints(rid, WEEK[:6], [_day(d) for d in WEEK[:6]])
    assert "ana" not in c.base_rows and "ana" not in c.base_hours
