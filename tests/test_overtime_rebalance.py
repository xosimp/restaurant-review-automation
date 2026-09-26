"""schedule_rules.rebalance_overtime (owner, 9/26/26: overtime is "VERY
critical" — most owners never want to pay it when a teammate has room)."""
from datetime import date, timedelta

import schedule_rules as sr

WEEK = [(date(2026, 9, 28) + timedelta(days=i)).isoformat() for i in range(7)]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=DAYS)
    c.compliance = dict(sr.DEFAULTS)
    c.compliance["max_consecutive_days"] = 7
    c.compliance["min_consecutive_days_off"] = 0
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(i, emp, start="2:00pm", end="10:00pm", hours=8, role="Line Cook"):
    return {"date": WEEK[i], "day": DAYS[i], "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(hours), "notes": ""}


def _hours(rows, name):
    return sum(float(r["scheduled_hours"]) for r in rows if r["employee"] == name)


def test_an_over_forty_week_hands_a_shift_to_the_teammate_with_room():
    rows = [_row(i, "Vince") for i in range(6)] + [_row(0, "Bea"), _row(1, "Bea")]
    out = sr.rebalance_overtime(rows, _c(), roster_roles={"Vince": "Line Cook", "Bea": "Line Cook"})
    assert _hours(out["rows"], "Vince") <= 40 and _hours(out["rows"], "Bea") <= 40
    assert out["moves"] and out["moves"][0]["from"] == "Vince" and out["moves"][0]["to"] == "Bea"
    assert out["moves"][0]["index"] == 5, "the latest shift of the payroll week goes first"
    assert not out["left"]
    assert not [v for v in sr.violations(out["rows"], _c()) if v["kind"] == "over_max_hours"]


def test_never_moves_a_shift_to_another_role_or_someone_already_on_that_day():
    rows = [_row(i, "Vince") for i in range(6)] + [_row(5, "Bea"), _row(0, "Ana", role="Server")]
    out = sr.rebalance_overtime(rows, _c(), roster_roles={"Vince": "Line Cook", "Bea": "Line Cook", "Ana": "Server"})
    assert all(m["to"] != "Ana" for m in out["moves"])
    for m in out["moves"]:
        assert out["rows"][m["index"]]["date"] != WEEK[5] or m["to"] != "Bea"


def test_a_short_excess_is_trimmed_where_the_role_stays_covered():
    # 42h, and the only teammate would go over too: trim 2h off the last
    # shift, where Bea is on until close.
    rows = [_row(i, "Vince", hours=8) for i in range(5)] + [_row(5, "Vince", "2:00pm", "4:00pm", 2)]
    rows += [_row(i, "Bea", "2:00pm", "10:00pm", 8) for i in range(5)]
    out = sr.rebalance_overtime(rows, _c(), roster_roles={"Vince": "Line Cook", "Bea": "Line Cook"})
    assert _hours(out["rows"], "Vince") <= 40
    assert out["trims"] and not out["left"]


def test_nothing_can_absorb_it_so_it_is_named_not_hidden():
    rows = [_row(i, "Vince", hours=8) for i in range(6)]
    out = sr.rebalance_overtime(rows, _c(), roster_roles={"Vince": "Line Cook"})
    assert out["rows"] == rows and not out["moves"]
    assert out["left"] and out["left"][0]["employee"] == "Vince" and out["left"][0]["hours"] == 48


def test_an_owner_who_set_someone_above_forty_keeps_their_overtime():
    c = _c(hours_limits={"vince": (None, 48)})
    c.compliance["weekly_hours_ceiling"] = 50
    rows = [_row(i, "Vince") for i in range(6)] + [_row(0, "Bea")]
    out = sr.rebalance_overtime(rows, c, roster_roles={"Vince": "Line Cook", "Bea": "Line Cook"})
    assert not out["moves"] and not out["trims"] and not out["left"]


def test_a_redo_of_some_days_only_touches_those_days():
    rows = [_row(i, "Vince") for i in range(6)] + [_row(0, "Bea")]
    out = sr.rebalance_overtime(rows, _c(), roster_roles={"Vince": "Line Cook", "Bea": "Line Cook"},
                                editable={WEEK[2]})
    assert all(m["index"] == 2 for m in out["moves"])


def test_the_engine_runs_it_before_the_fix_pass_and_reports_each_move():
    import inspect
    import schedule_engine
    src = inspect.getsource(schedule_engine._run_schedule_job)
    assert src.index("_rules.rebalance_overtime(") < src.index("_sqf.apply_fixes(")
    assert "_fixes, _unfixed = list(_ot_fixes), []" in src and "_fixes = _ot_fixes + _out[\"fixes\"]" in src


def test_the_prompt_says_overtime_is_a_cost_the_owner_does_not_want():
    import labor
    src = open(labor.__file__, encoding="utf-8").read()
    assert "nobody goes past \"\n        \"40 hours in the payroll week while a teammate in the same role has room" in src


def test_the_week_is_priced_after_the_overtime_pass_and_again_on_its_final_rows():
    import inspect
    import schedule_engine
    src = inspect.getsource(schedule_engine._run_schedule_job)
    assert src.index("_rules.rebalance_overtime(") < src.index("_price_week(preview_rows)")
    assert "_price_week(preview_rows)\n                _lines_out" in src


def test_a_night_nobody_closes_keeps_a_keyholder_on_to_close():
    c = _c(keyholders={"jade"}, close_times={"Thursday": "12:00am"})
    rows = [_row(3, "Jade", "3:00pm", "11:30pm", 8.5, role="Bartender"),
            _row(3, "Ray", "3:00pm", "11:30pm", 8.5)]
    assert {v["kind"] for v in sr.violations(rows, c)} >= {"nobody_at_close", "keyholder_until_close"}
    out = sr.close_out_gaps(rows, c)
    assert out["extended"] and out["extended"][0]["employee"] == "Jade"
    assert out["rows"][0]["shift_end"] == "12:00am" and out["rows"][0]["scheduled_hours"] == "9"
    assert not {v["kind"] for v in sr.violations(out["rows"], c)} & {"nobody_at_close", "keyholder_until_close"}


def test_close_out_never_pushes_a_keyholder_into_overtime():
    c = _c(keyholders={"jade"}, close_times={"Thursday": "12:00am"})
    rows = [_row(i, "Jade", "3:00pm", "11:30pm", 8, role="Bartender") for i in range(5)]
    rows[3]["scheduled_hours"] = "8"
    out = sr.close_out_gaps(rows, c)
    assert not out["extended"], "40h already: the half hour would be overtime"
