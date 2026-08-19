"""Deterministic schedule backstops (_top_up_hours_gap,
_ensure_pizza_cook_coverage, _extend_shifts_to_close_gap) vs. freeform
availability notes.

staff_availability only has whole-day granularity in its structured
fields (available_days/unavailable_days) -- a client who writes "only
mornings" or "no closes" has nowhere to put that except the freeform
notes field. The AI prompt does read notes (see labor.py's _avail_block),
but these deterministic passes previously didn't -- they could add a
brand-new shift, or extend an existing one, for someone whose notes
explicitly ruled it out, entirely unaware the note existed. Fixed by
excluding anyone with any notes text at all from automatic additions/
extensions -- conservative (a missed top-up beats a deterministic
constraint violation), and simple/reliable in a way keyword-matching
arbitrary prose never would be.
"""
import client_api
import models


def test_pizza_cook_coverage_skips_the_only_candidate_when_they_have_notes(monkeypatch):
    # Alex T. is the *only* Pizza Cook anyone has worked this week (Tuesday
    # morning, in the CSV below) -- Tuesday still needs 1 for night
    # (target 1, current 0; Tuesday isn't a "busy day" in _PIZZA_BUSY_DAYS).
    # Without the notes exclusion, Alex T. is the only real candidate and
    # would get added to Tuesday night despite "only mornings".
    preview_rows = [{
        "date": "2026-08-25", "day": "Tuesday", "employee": "Alex T.",
        "role": "Pizza Cook", "shift_start": "8:00am", "shift_end": "2:00pm",
        "scheduled_hours": "6", "notes": "morning",
    }]
    monkeypatch.setattr(models, "get_staff_availability", lambda restaurant_id: [
        {"employee_name": "Alex T.", "available_days": '["Tuesday"]',
         "unavailable_days": "[]", "notes": "only mornings"},
    ])

    result_rows, rows_added, added_dates = client_api._ensure_pizza_cook_coverage(
        preview_rows, ["2026-08-25"], ["Tuesday"], restaurant_id=1,
        close_times={}, role_buffers={},
    )

    assert rows_added == 0
    assert added_dates == {}
    night_rows = [r for r in result_rows if r["role"] == "Pizza Cook"
                  and client_api._window_overlap(r, client_api._PIZZA_NIGHT_WINDOW)]
    assert night_rows == []


def test_pizza_cook_coverage_still_uses_a_candidate_without_notes(monkeypatch):
    # Two Pizza Cooks on the roster this week, both only working Monday
    # (so neither is already "working" the Tuesday date being filled --
    # _ensure_pizza_cook_coverage correctly never double-books the same
    # date regardless of daypart, so a candidate already on Tuesday
    # wouldn't be a fair test of the notes exclusion specifically). Alex
    # T. has "only mornings" and must be skipped; Casey R. has no notes
    # and should get picked for the missing Tuesday coverage instead --
    # proving the exclusion is specifically about notes, not some other
    # bug hiding the failure in the test above.
    preview_rows = [{
        "date": "2026-08-24", "day": "Monday", "employee": "Alex T.",
        "role": "Pizza Cook", "shift_start": "8:00am", "shift_end": "2:00pm",
        "scheduled_hours": "6", "notes": "morning",
    }, {
        "date": "2026-08-24", "day": "Monday", "employee": "Casey R.",
        "role": "Pizza Cook", "shift_start": "4:00pm", "shift_end": "9:00pm",
        "scheduled_hours": "5", "notes": "night",
    }]
    monkeypatch.setattr(models, "get_staff_availability", lambda restaurant_id: [
        {"employee_name": "Alex T.", "available_days": '["Monday", "Tuesday"]',
         "unavailable_days": "[]", "notes": "only mornings"},
        {"employee_name": "Casey R.", "available_days": '["Monday", "Tuesday"]',
         "unavailable_days": "[]", "notes": ""},
    ])

    result_rows, rows_added, added_dates = client_api._ensure_pizza_cook_coverage(
        preview_rows, ["2026-08-24", "2026-08-25"], ["Monday", "Tuesday"], restaurant_id=1,
        close_times={}, role_buffers={},
    )

    assert rows_added > 0
    assert added_dates.get("2026-08-25", 0) > 0
    tuesday_employees = {r["employee"] for r in result_rows if r["date"] == "2026-08-25"}
    assert "Casey R." in tuesday_employees
    assert "Alex T." not in tuesday_employees


def test_top_up_hours_gap_skips_the_only_candidate_when_they_have_notes(monkeypatch):
    # Jamie L. is the only Server with hours left this week; Wednesday is
    # under its target and needs another server. With a "closes only,
    # never mornings" note on file, the top-up must not add them rather
    # than silently ignoring the note.
    preview_rows = [{
        "date": "2026-08-26", "day": "Wednesday", "employee": "Jamie L.",
        "role": "Server", "shift_start": "5:00pm", "shift_end": "10:00pm",
        "scheduled_hours": "5", "notes": "closer",
    }, {
        "date": "2026-08-27", "day": "Thursday", "employee": "Jamie L.",
        "role": "Server", "shift_start": "5:00pm", "shift_end": "10:00pm",
        "scheduled_hours": "5", "notes": "closer",
    }]
    monkeypatch.setattr(models, "get_staff_availability", lambda restaurant_id: [
        {"employee_name": "Jamie L.", "available_days": '["Wednesday", "Thursday"]',
         "unavailable_days": "[]", "notes": "closes only, never mornings"},
    ])

    result_rows, hours_added, added_dates = client_api._top_up_hours_gap(
        preview_rows, daily_target_hours={"2026-08-26": 20.0, "2026-08-27": 5.0},
        hours_budget=100.0, hours_scheduled=10.0, restaurant_id=1,
        close_times={}, role_buffers={},
    )

    assert hours_added == 0.0
    assert added_dates == {}
    assert len(result_rows) == 2  # nothing new added
