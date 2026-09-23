"""Regressions from the independent re-audit of the schedule optimizer work.
Each test names the defect it pins."""
import shift_quality as sq
import schedule_optimizer as so

SAT, FRI, MON, TUE = "2026-09-12", "2026-09-11", "2026-09-07", "2026-09-08"


def row(date, name, role, start="5:00pm", end="10:00pm", hours="5"):
    return {"date": date, "day": sq._day_name(date), "employee": name, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": hours, "notes": ""}


def test_a_2pm_to_close_shift_is_dinner_not_lunch():
    assert sq.present_dayparts({"shift_start": "2:00pm", "shift_end": "11:30pm"}) == ["night"]
    assert sq.present_dayparts({"shift_start": "11:30am", "shift_end": "7:00pm"}) == ["morning", "night"]
    assert sq.present_dayparts({"shift_start": "6:00am", "shift_end": "10:30am"}) == ["morning"]


def test_mornings_only_is_checked_against_the_whole_shift():
    assert not sq.works_daypart_ok({"shift_start": "2:30pm", "shift_end": "11:30pm"}, "morning")
    assert sq.works_daypart_ok({"shift_start": "9:00am", "shift_end": "2:00pm"}, "morning")


def test_a_role_nobody_rated_is_unknown_strength_not_zero():
    rows = [row(SAT, "S1", "Server"), row(SAT, "S2", "Server"), row(SAT, "B1", "Bartender"), row(SAT, "B2", "Bartender")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(min_strength={"Server": 8, "Bartender": 4})],
                        scores={"S1": 5, "S2": 5}, typical_headcount={("Saturday", "night"): {"Server": 2, "Bartender": 2}})
    shift = out["shifts"][0]
    assert shift["capped_by"] != "operational_strength" and shift["score"] > 50


def test_a_close_rule_nobody_in_the_role_holds_is_set_aside_not_scored_zero(monkeypatch):
    import schedule_engine as se, models
    monkeypatch.setattr(models, "get_leader_flags", lambda rid: {"Cook1": True})
    rule = {"role": "Bartender", "count": 1, "attribute": "can_close", "closing": True}
    result = {"leader_rules": [rule], "operational_scores": {"Bar1": 3},
              "roster_roles": {"Bar1": "Bartender", "Cook1": "Cook"}, "roster": ["Bar1", "Cook1"]}
    monkeypatch.setattr(se, "_hourly_profile", lambda rid: {})
    sig, _w = se._quality_signals(1, result)
    assert rule in sig["unmeetable_leader_rules"]


def test_the_fix_pass_does_not_swap_people_for_a_shift_that_is_too_long():
    rows = [row(SAT, "Ann", "Server", "8:00am", "11:00pm", "15"), row(FRI, "Bob", "Server")]
    v = [{"hard": True, "kind": "shift_too_long", "index": 0, "employee": "Ann", "label": "too long"}]
    out = sq.apply_fixes(rows, v, profiles=[sq.ShiftProfile()], roster=["Ann", "Bob"],
                         typical_headcount={("Saturday", "night"): {"Server": 1}})
    assert out["fixes"] == [] and out["rows"][0]["employee"] == "Ann"
    assert "not something a different person" in out["unfixed"][0]["reason"]


def test_a_shift_the_draft_left_out_is_scored_as_empty():
    rows = [row(SAT, "A", "Server", "11:00am", "3:00pm", "4")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile()],
                        typical_headcount={("Saturday", "morning"): {"Server": 1}, ("Saturday", "night"): {"Server": 6}})
    night = [s for s in out["shifts"] if s["daypart"] == "night"]
    assert night and night[0]["score"] == 0 and night[0]["capped_by"] == "coverage"


def test_prompt_and_score_share_one_requirement():
    req = sq.shift_role_requirements({"Server": 6, "Line Cook": 1}, {"line cook": 2}, {"Host": 1, "Bartender": 2},
                                     {"Server": 4})
    # usual 6 servers beats the profile's 4; the floor's 2 beats usual 1; a
    # whole-day minimum binds only a role that works this daypart.
    assert req["Server"][0] == 6 and req["line cook"][0] == 2
    assert "Host" not in req and "Bartender" not in req
    rows = [row(SAT, "C1", "Line Cook"), row(SAT, "S1", "Server")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile()], role_minimums={"Line Cook": 1},
                        typical_headcount={("Saturday", "night"): {"Server": 6, "Line Cook": 1}})
    cov = next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "coverage")
    assert cov["facts"]["short"] == {"Server": 5}


def test_the_optimizer_writes_hours_as_text():
    rows = [row(SAT, "Ann", "Server"), row(FRI, "Bob", "Server"), row(FRI, "Cat", "Server")]
    res = so.optimize(rows, {}, signals={"roster": ["Ann", "Bob", "Cat"], "typical_headcount": {("Saturday", "night"): {"Server": 3}}})
    assert res["changes"] and all(isinstance(r["scheduled_hours"], str) for r in res["rows"])


def test_cavnar_notes_never_reach_an_employee():
    import labor
    assert labor.staff_facing_note("Cavnar: Server strength was under target (swapped with Hana W.)") == ""
    assert labor.staff_facing_note("opener — Cavnar: added to cover Server") == "opener"


def test_a_rewrite_that_drops_a_shift_is_not_better():
    import schedule_engine as se
    before = {"shifts": [{"date": SAT, "daypart": "night", "scored": True, "score": 42, "profile": {"demand": "peak"}}]}
    after_dropped = {"shifts": [{"date": SAT, "daypart": "night", "scored": True, "score": 0, "profile": {"demand": "peak"}}]}
    assert se._gate_local(after_dropped, [SAT]) < se._gate_local(before, [SAT])


def test_the_budget_trim_stops_asking_the_score_when_time_runs_out():
    import schedule_economics as econ
    rows = [row(SAT, n, "Server") for n in ("A", "B", "C")] + [row(FRI, n, "Server") for n in ("A", "B", "C")]
    calls = []
    out, trimmed, _h = econ.trim_to_budget([dict(r) for r in rows], 22, {}, score_fn=lambda rs: calls.append(1) or 0.0,
                                           score_seconds=0.0)
    assert trimmed and calls == []
