"""schedule_rules — the one legality question every scheduling pass asks.

The audit found the backstops adding shifts on approved time off, the
hours check ignoring what was already published in the same payroll
week, and no rule at all for rest, minors or days off. These pin the
Constraints object and the violation sweep, mostly without a database:
a rule that only holds for one rendered payload is not a rule.
"""
import pytest

import models
import schedule_rules as sr
from models import create_restaurant, Restaurant, get_conn

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(date, emp, start, end, role="Server", hours=None):
    from schedule_rules import parse_minutes
    if hours is None:
        s, e = parse_minutes(start), parse_minutes(end)
        hours = round(((e - s) % (24 * 60)) / 60, 1)
    day = DAYS[WEEK.index(date)] if date in WEEK else ""
    return {"date": date, "day": day, "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(hours), "notes": ""}


# ── can_work ────────────────────────────────────────────────────────────

def test_approved_time_off_blocks_the_date_with_its_reason():
    c = _c(blocked_dates={"ana": {"2026-10-07": sr.LABELS["approved_time_off"]}})
    ok, why = c.can_work("Ana", "2026-10-07")
    assert not ok and why == sr.LABELS["approved_time_off"]
    assert c.can_work("Ana", "2026-10-08")[0]


def test_a_deactivated_person_cannot_work_any_date():
    c = _c(inactive={"old hand"})
    assert c.can_work("Old Hand", WEEK[0]) == (False, sr.LABELS["inactive"])


def test_off_roster_is_refused_only_once_a_roster_exists():
    assert _c().can_work("Anyone", WEEK[0])[0]            # no roster on file → no opinion
    c = _c(active={"ana"})
    assert c.can_work("Ana", WEEK[0])[0]
    assert c.can_work("Bob", WEEK[0]) == (False, sr.LABELS["off_roster"])


def test_daypart_availability_is_read_per_day():
    c = _c(daypart_avail={"ana": {"Monday": "morning", "Tuesday": "off"}})
    assert c.can_work("Ana", WEEK[0], "morning")[0]
    assert c.can_work("Ana", WEEK[0], "night") == (False, sr.LABELS["unavailable_daypart"])
    assert c.can_work("Ana", WEEK[1], "morning") == (False, sr.LABELS["unavailable_day"])
    assert c.can_work("Ana", WEEK[2], "night")[0]


def test_daypart_of_splits_at_three_pm():
    assert sr.daypart_of("2:59pm") == "morning"
    assert sr.daypart_of("15:00") == "night"
    assert sr.daypart_of("") == "unknown"


# ── hours ceilings ──────────────────────────────────────────────────────

def test_max_hours_is_the_lower_of_the_persons_cap_and_the_ceiling():
    c = _c(hours_limits={"ana": (None, 30)}, compliance={**sr.DEFAULTS, "weekly_hours_ceiling": 40})
    assert c.max_hours("Ana") == 30
    assert c.max_hours("Bob") == 40
    c.hours_limits["bob"] = (None, 50)
    assert c.max_hours("Bob") == 40


def test_published_tail_counts_toward_the_payroll_week():
    """Twenty hours already published in this payroll bucket plus 24 new
    ones is over 40 — the old check only saw the 24."""
    c = _c(base_hours={"ana": {c_bucket: 20.0} for c_bucket in [_c().bucket(WEEK[0])]})
    rows = [_row(WEEK[0], "Ana", "9:00am", "5:00pm"), _row(WEEK[1], "Ana", "9:00am", "5:00pm"),
            _row(WEEK[2], "Ana", "9:00am", "5:00pm")]
    kinds = {v["kind"] for v in sr.violations(rows, c)}
    assert "over_max_hours" in kinds
    assert "over_max_hours" not in {v["kind"] for v in sr.violations(rows, _c())}


def test_under_min_hours_is_soft():
    c = _c(hours_limits={"ana": (20, None)})
    v = sr.violations([_row(WEEK[0], "Ana", "9:00am", "1:00pm")], c)
    assert [x["kind"] for x in v] == ["under_min_hours"]
    assert not v[0]["hard"]


# ── rest, overlap, double booking ──────────────────────────────────────

def test_close_then_open_breaks_the_rest_rule():
    c = _c()
    rows = [_row(WEEK[0], "Ana", "4:00pm", "11:30pm"), _row(WEEK[1], "Ana", "7:00am", "3:00pm")]
    v = sr.violations(rows, c)
    assert any(x["kind"] == "rest_gap" and x["hard"] for x in v)
    assert "7.5h" in next(x["detail"] for x in v if x["kind"] == "rest_gap")


def test_rest_is_measured_against_the_published_tail_too():
    tail = _row("2026-10-04", "Ana", "4:00pm", "11:30pm")
    tail["day"] = "Sunday"
    c = _c(base_rows={"ana": [tail]})
    v = sr.violations([_row(WEEK[0], "Ana", "7:00am", "3:00pm")], c)
    assert any(x["kind"] == "rest_gap" for x in v)


def test_rest_ok_helper_agrees_with_the_sweep():
    c = _c()
    other = [_row(WEEK[0], "Ana", "4:00pm", "11:30pm")]
    ok, why = c.rest_ok("Ana", _row(WEEK[1], "Ana", "7:00am", "3:00pm"), other)
    assert not ok and "rest" in why.lower()
    assert c.rest_ok("Ana", _row(WEEK[1], "Ana", "11:00am", "3:00pm"), other)[0]


def test_overlapping_shifts_and_double_bookings_are_hard():
    c = _c()
    rows = [_row(WEEK[0], "Ana", "9:00am", "5:00pm"), _row(WEEK[0], "Ana", "3:00pm", "10:00pm"),
            _row(WEEK[1], "Bob", "9:00am", "5:00pm"), _row(WEEK[1], "Bob", "9:00am", "5:00pm")]
    kinds = {v["kind"] for v in sr.violations(rows, c)}
    assert {"overlap", "double_booked"} <= kinds
    assert "overlap" in sr.HARD and "double_booked" in sr.HARD


# ── shift length, minors, days off, pending time off ───────────────────

def test_a_shift_past_the_maximum_is_named_with_its_length():
    c = _c(compliance={**sr.DEFAULTS, "max_shift_hours": 10})
    v = sr.violations([_row(WEEK[0], "Ana", "8:00am", "8:00pm")], c)
    assert v[0]["kind"] == "shift_too_long" and "12h" in v[0]["detail"]


def test_minor_rules_apply_only_to_minors():
    c = _c(minors={"kid"}, compliance={**sr.DEFAULTS, "minor_latest_end": "10:00pm", "minor_max_daily_hours": 8})
    rows = [_row(WEEK[0], "Kid", "4:00pm", "11:00pm"), _row(WEEK[1], "Kid", "8:00am", "6:00pm"),
            _row(WEEK[0], "Ana", "4:00pm", "11:00pm")]
    v = sr.violations(rows, c)
    by = {(x["employee"], x["kind"]) for x in v}
    assert ("Kid", "minor_late") in by and ("Kid", "minor_hours") in by
    assert not any(e == "Ana" for e, _ in by)


def test_no_two_consecutive_days_off_is_flagged_softly():
    c = _c(compliance={**sr.DEFAULTS, "min_consecutive_days_off": 2})
    # Off Tuesday and Friday only — six shifts but never two off together.
    worked = [d for d in WEEK if d not in (WEEK[1], WEEK[4])]
    rows = [_row(d, "Ana", "9:00am", "3:00pm") for d in worked]
    v = [x for x in sr.violations(rows, c) if x["kind"] == "days_off"]
    assert len(v) == 1 and not v[0]["hard"] and "2 together" in v[0]["detail"]


def test_pending_time_off_is_a_warning_not_a_block():
    c = _c(pending_off={"ana": {WEEK[2]}})
    assert c.can_work("Ana", WEEK[2])[0]
    v = sr.violations([_row(WEEK[2], "Ana", "9:00am", "3:00pm")], c)
    assert [x["kind"] for x in v] == ["pending_time_off"] and not v[0]["hard"]


def test_a_no_show_kind_is_one_the_engine_could_not_vouch_for():
    assert set(sr.NO_SHOW) <= set(sr.HARD)
    for k in sr.HARD | sr.SOFT:
        assert k in sr.LABELS, k


def test_summarize_puts_hard_breaches_first_and_names_their_rows():
    c = _c(blocked_dates={"ana": {WEEK[0]: sr.LABELS["approved_time_off"]}}, hours_limits={"bob": (30, None)})
    rows = [_row(WEEK[0], "Bob", "9:00am", "1:00pm"), _row(WEEK[0], "Ana", "9:00am", "5:00pm")]
    s = sr.summarize(sr.violations(rows, c))
    assert s["hard"] == 1 and s["soft"] == 1 and s["hard_rows"] == [1]
    assert s["lines"][0].startswith("⚠") and "Ana" in s["lines"][0]


# ── build_constraints against a real database ──────────────────────────

@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(sr, "get_conn", lambda *a, **k: real(db_path))
    import staff_settings, time_off, schedule_versions
    for mod in (staff_settings, time_off, schedule_versions):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    return create_restaurant(Restaurant(name="Rules Co", owner_email="r@x.com"), db_path=db_path)


def test_build_constraints_reads_time_off_roster_and_published_hours(db_path, rid):
    import time_off, staff_settings
    models.add_manual_team_member(rid, "Ana", role="Server", db_path=db_path)
    models.add_manual_team_member(rid, "Gone", role="Server", db_path=db_path)
    staff_settings.upsert(rid, "Gone", active=False, db_path=db_path)
    staff_settings.upsert(rid, "Ana", max_hours=32, is_minor=True,
                          daypart_availability={"Monday": "morning"}, db_path=db_path)
    row, err = time_off.request_time_off(rid, "Ana", WEEK[3], WEEK[3], db_path=db_path, today=__import__("datetime").date(2026, 9, 1))
    assert not err, err
    time_off.decide(rid, row["id"], True, db_path=db_path)
    row2, _ = time_off.request_time_off(rid, "Ana", WEEK[5], WEEK[5], db_path=db_path, today=__import__("datetime").date(2026, 9, 1))
    # 12 published hours in the same payroll bucket, the week before
    conn = get_conn(db_path)
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, "
                 "labor_target, schedule_csv, summary_json, published_at) VALUES (?,?,?,?,?,?,?,'[]',datetime('now'))",
                 (rid, "2026-09-28", "2026-10-04", 12, 40, 30,
                  "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                  "2026-10-04,Sunday,Ana,Server,4:00pm,10:00pm,6.0,\n"
                  "2026-10-03,Saturday,Ana,Server,4:00pm,10:00pm,6.0,\n"))
    conn.commit()
    conn.close()

    c = sr.build_constraints(rid, WEEK, DAYS, db_path=db_path)
    assert "ana" in c.active and "gone" in c.inactive and c.roster_names == ["Ana"]
    assert c.can_work("Ana", WEEK[3]) == (False, sr.LABELS["approved_time_off"])
    assert WEEK[5] in c.pending_off["ana"]
    assert c.max_hours("Ana") == 32 and "ana" in c.minors
    assert c.daypart_avail["ana"] == {"Monday": "morning"}
    # Sunday the 4th is in last week's bucket (Monday start), so it does
    # not count toward this week's ceiling, but it does count for rest.
    assert sum(c.base_hours.get("ana", {}).values()) == 0
    assert any(r["date"] == "2026-10-04" for r in c.base_rows["ana"])
    v = sr.violations([_row(WEEK[0], "Ana", "6:00am", "2:00pm")], c)
    assert any(x["kind"] == "rest_gap" for x in v)


def test_a_sibling_sites_published_shift_blocks_the_date_here(db_path, rid):
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET location_group='Group', owner_email='r@x.com' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    sib = create_restaurant(Restaurant(name="Rules Co North", owner_email="r@x.com"), db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET location_group='Group', location_name='North' WHERE id=?", (sib,))
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, "
                 "labor_target, schedule_csv, summary_json, published_at) VALUES (?,?,?,?,?,?,?,'[]',datetime('now'))",
                 (sib, WEEK[0], WEEK[6], 8, 40, 30,
                  "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                  f"{WEEK[2]},Wednesday,Ana,Server,4:00pm,10:00pm,6.0,\n"))
    conn.commit()
    conn.close()
    c = sr.build_constraints(rid, WEEK, DAYS, db_path=db_path)
    ok, why = c.can_work("Ana", WEEK[2])
    assert not ok and "North" in why
    assert c.base_hours["ana"][c.bucket(WEEK[2])] == 6.0


def test_compliance_and_role_floors_round_trip(db_path, rid):
    saved = sr.save_compliance(rid, {"min_rest_hours": 11, "max_shift_hours": "9", "minor_latest_end": "9:30pm",
                                     "weekly_hours_ceiling": 38, "nonsense": 1}, db_path=db_path)
    assert saved["min_rest_hours"] == 11 and saved["max_shift_hours"] == 9 and saved["weekly_hours_ceiling"] == 38
    assert "nonsense" not in saved
    floors = sr.save_role_floors(rid, {"Line Cook": {"morning": 1, "night": 2, "days": {"saturday": {"night": 3}}},
                                       "": {"night": 9}}, db_path=db_path)
    assert list(floors) == ["Line Cook"]
    assert sr.floor_for(floors, "line cook", "Saturday", "night") == 3
    assert sr.floor_for(floors, "Line Cook", "Tuesday", "night") == 2
    assert sr.floor_for(floors, "Line Cook", "Tuesday", "morning") == 1
    assert sr.floor_for(floors, "Server", "Tuesday", "morning") == 0
    r = models.get_restaurant(rid, db_path)
    assert sr.compliance(r)["min_rest_hours"] == 11
