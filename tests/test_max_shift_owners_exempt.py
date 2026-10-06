"""The shift-length maximum holds the team, never an owner or a manager
(Simple EJ's, 10/6/26): "maximum hours should not affect owners/managers -
Erik and Jim WANT to work those longer shifts ... the schedule should
generate accordingly". Their 13-hour standing shifts were dropped."""
from datetime import date, timedelta

import schedule_rules as sr
import shift_quality as sq


def _c():
    d = date(2026, 10, 12)
    dates = [(d + timedelta(days=i)).isoformat() for i in range(7)]
    c = sr.Constraints(restaurant_id=1, week_dates=dates, week_days=list(sr.DAYS))
    c.compliance = dict(sr.DEFAULTS, max_shift_hours=12.0)
    c.managers = {"erik baylis": "Owner", "danny k": "Manager FOH"}
    c.acting_managers = {"sam lee": {"2026-10-13"}}
    return c


def _row(name, s, e, hrs):
    return {"employee": name, "role": "Owner", "date": "2026-10-13", "day": "Tuesday",
            "shift_start": s, "shift_end": e, "scheduled_hours": hrs}


def test_owners_and_managers_are_not_held_to_it_and_everyone_else_is():
    c = _c()
    assert not sr.max_shift_applies(c, "Erik Baylis") and not sr.max_shift_applies(c, "Danny K")
    assert sr.max_shift_applies(c, "Sam Lee"), "standing in as a manager for a day is not being one"
    assert sr.max_shift_applies(c, "Jose Quintana Morales")
    kinds = lambda name: [v["kind"] for v in sr.violations([_row(name, "9:00am", "10:00pm", 13)], c,  # noqa: E731
                                                          person_only=True)]
    assert "shift_too_long" not in kinds("Erik Baylis") and "shift_too_long" not in kinds("Danny K")
    assert "shift_too_long" in kinds("Jose Quintana Morales")


def test_the_prompt_and_the_quality_score_say_the_same():
    import inspect
    src = inspect.getsource(sr)
    assert "except an owner's or a manager's: how long they stay is their own call" in src
    qsrc = inspect.getsource(sq)
    assert 'str(name or "").strip().lower() not in ctx.max_shift_exempt' in qsrc
    import schedule_engine
    assert 'signals["max_shift_exempt"] = sorted(n.strip().lower() for n in (c.roster_names or [])' in \
        inspect.getsource(schedule_engine)


def test_the_summary_notes_clear_the_tiles_above_them():
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath("templates", "dashboard.html").read_text(encoding="utf-8")
    assert "#ss-summary .sfw1-notes{padding:0;margin:26px 0 22px;gap:10px}" in src


def _sat_close_sun_open(name):
    close = {"employee": name, "role": "Manager", "date": "2026-10-17", "day": "Saturday",
             "shift_start": "4:00pm", "shift_end": "2:00am", "scheduled_hours": 10}
    open_ = {"employee": name, "role": "Manager", "date": "2026-10-18", "day": "Sunday",
             "shift_start": "9:00am", "shift_end": "5:00pm", "scheduled_hours": 8}
    return close, open_


def test_owners_and_managers_keep_their_own_turnaround_and_overlaps_still_count():
    """Andrew Marola closes Saturday and opens Sunday by choice (owner,
    10/6/26: "exempt andrew from the rest rule too")."""
    c = _c()
    c.managers["andrew marola"] = "Manager"
    assert not sr.rest_rule_applies(c, "Andrew Marola") and sr.rest_rule_applies(c, "Jose Quintana Morales")
    kinds = lambda rows: [v["kind"] for v in sr.violations(rows, c, person_only=True)]  # noqa: E731
    assert "rest_gap" not in kinds(list(_sat_close_sun_open("Andrew Marola")))
    assert "rest_gap" in kinds(list(_sat_close_sun_open("Jose Quintana Morales")))
    close, open_ = _sat_close_sun_open("Andrew Marola")
    assert c.rest_ok("Andrew Marola", open_, [close, open_]) == (True, "")
    assert c.rest_ok("Jose Quintana Morales", dict(open_, employee="Jose Quintana Morales"),
                     [dict(close, employee="Jose Quintana Morales")])[0] is False
    overlap = dict(open_, date="2026-10-17", shift_start="6:00pm", shift_end="11:00pm")
    assert c.rest_ok("Andrew Marola", overlap, [close])[0] is False, "an overlap is never legal"


def test_the_schedule_call_runs_at_medium_effort():
    import labor
    assert labor.SCHEDULE_EFFORT == "medium"
