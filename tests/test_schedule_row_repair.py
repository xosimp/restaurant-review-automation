"""client_api.py's _run_schedule_job — day/employee column-repair.

Live generations occasionally come back with one CSV row (out of 60-100+)
mis-written — always a non-routine addition that doesn't follow the same
repeating pattern as the rest of the week. `date` was correct in every
malformed row observed across live testing, so `day` is always re-derived
from it server-side. Four distinct failure shapes have actually been
captured from live generations (not guessed) and each has a targeted
repair:
  1. Clean day/employee swap — full recovery.
  2. `day` field omitted entirely, shifting every field after `date` one
     position left (the tell: `role` ends up holding a clock time, which a
     real role name never does) — full recovery by un-shifting.
  3. `day` text just garbled/misspelled ("Thursson") with everything else
     in its correct position — day corrected, nothing else touched.
  4. Employee name duplicated into the day slot instead of a weekday
     ("Piper A.,Piper A.,Runner,...") — same as #3, only day needs fixing.
For #3 and #4, a sanity check confirms employee/role/times/hours all
already look individually well-formed before skipping the flag — anything
that still can't be confirmed sane gets its day corrected AND an explicit
`needs_review` flag, rather than silently passing a possibly-wrong row
through unmarked.
"""
import client_api
import models


def _fake_build_schedule_result(csv_text):
    def fake(restaurant_id):
        return {
            "schedule_csv": csv_text,
            "summary": ["- ok"],
            "week_dates": ["2026-08-17"],
            "week_days": ["Monday"],
            "projected_revenue": 84296,
            "hours_budget": 745.7,
            "labor_budget_dollars": 19388,
            "labor_target": 23,
            "restaurant_name": "Test Bistro",
        }
    return fake


def _run(monkeypatch, csv_text, close_times=None, role_close_buffers=None):
    monkeypatch.setattr(client_api, "_build_schedule_result", _fake_build_schedule_result(csv_text))
    monkeypatch.setattr(models, "get_staff_notes", lambda restaurant_id: [])
    # Unconfigured (the default, matching a restaurant that never set
    # close_times_json) means no enforcement at all — every existing test
    # above relies on that opt-in behavior to stay unaffected by the
    # close-time feature below.
    monkeypatch.setattr(models, "get_close_times", lambda restaurant_id: close_times or {})
    monkeypatch.setattr(models, "get_role_close_buffers", lambda restaurant_id: role_close_buffers or {})
    client_api._schedule_jobs.pop("test-job", None)
    client_api._run_schedule_job("test-job", 1)
    job = client_api._schedule_jobs.pop("test-job")
    assert job["status"] == "done"
    return job["result"]


HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


def test_clean_day_employee_swap_is_fully_repaired(monkeypatch):
    # 2026-08-21 is a real Friday — day and employee are transposed.
    csv_text = HEADER + "\n" + "2026-08-21,Jamie L.,Friday,Server,5:00pm,10:00pm,5.0,closer"

    result = _run(monkeypatch, csv_text)

    assert len(result["preview_rows"]) == 1
    row = result["preview_rows"][0]
    assert row["day"] == "Friday"
    assert row["employee"] == "Jamie L."
    assert row["role"] == "Server"
    assert "needs_review" not in row
    # The exported/shared CSV must reflect the repair too, not the raw text.
    assert "Friday,Jamie L.,Server" in result["schedule_csv"]


def test_day_field_omitted_shift_is_fully_repaired(monkeypatch):
    # Day column omitted entirely — every field after date shifts left,
    # landing a clock time in the `role` slot. Captured live (Farah A.,
    # Sam V. examples); fully recoverable by un-shifting one position.
    csv_text = HEADER + "\n" + "2026-08-21,Jamie L.,Server,11:00am,6:00pm,7,second server staggered"

    result = _run(monkeypatch, csv_text)

    assert len(result["preview_rows"]) == 1
    row = result["preview_rows"][0]
    assert row["day"] == "Friday"
    assert row["employee"] == "Jamie L."
    assert row["role"] == "Server"
    assert row["shift_start"] == "11:00am"
    assert row["shift_end"] == "6:00pm"
    assert row["scheduled_hours"] == "7"
    assert row["notes"] == "second server staggered"
    assert "needs_review" not in row


def test_garbled_day_text_is_corrected_without_flagging(monkeypatch):
    # The model sometimes just misspells the day word itself while every
    # other field is already in its correct position — captured live as
    # "Thursson" instead of "Thursday". Nothing needs a human look here.
    csv_text = HEADER + "\n" + "2026-08-21,Thursson,Jamie L.,Server,5:00pm,10:00pm,5.0,closer"

    result = _run(monkeypatch, csv_text)

    assert len(result["preview_rows"]) == 1
    row = result["preview_rows"][0]
    assert row["day"] == "Friday"
    assert row["employee"] == "Jamie L."
    assert row["role"] == "Server"
    assert "needs_review" not in row


def test_duplicated_employee_in_day_slot_is_corrected_without_flagging(monkeypatch):
    # The model duplicates the employee name into the day slot instead of
    # writing a weekday, rather than reordering anything — captured live
    # as "Piper A.,Piper A.,Runner,...". Employee/role/times were already
    # correct, so only the day label needs fixing.
    csv_text = HEADER + "\n" + "2026-08-21,Piper A.,Piper A.,Runner,3:00pm,10:00pm,7,night second runner"

    result = _run(monkeypatch, csv_text)

    assert len(result["preview_rows"]) == 1
    row = result["preview_rows"][0]
    assert row["day"] == "Friday"
    assert row["employee"] == "Piper A."
    assert row["role"] == "Runner"
    assert "needs_review" not in row


def test_genuinely_unrecoverable_row_is_still_flagged(monkeypatch):
    # A missing role (not just a wrong day) means nothing here can be
    # confirmed sane — this must still fall back to an explicit flag
    # rather than guessing.
    csv_text = HEADER + "\n" + "2026-08-21,Weird,Jamie L.,,11:00am,6:00pm,7,note"

    result = _run(monkeypatch, csv_text)

    assert len(result["preview_rows"]) == 1
    row = result["preview_rows"][0]
    assert row["day"] == "Friday"
    assert row.get("needs_review") is True


def test_well_formed_row_passes_through_unflagged(monkeypatch):
    csv_text = HEADER + "\n" + "2026-08-17,Monday,Sofia R.,Server,10:00am,5:00pm,7.0,opener"

    result = _run(monkeypatch, csv_text)

    assert len(result["preview_rows"]) == 1
    row = result["preview_rows"][0]
    assert row["day"] == "Monday"
    assert row["employee"] == "Sofia R."
    assert "needs_review" not in row


def test_unparseable_date_is_flagged_rather_than_dropped_or_trusted(monkeypatch):
    csv_text = HEADER + "\n" + "not-a-date,Monday,Sofia R.,Server,10:00am,5:00pm,7.0,opener"

    result = _run(monkeypatch, csv_text)

    assert len(result["preview_rows"]) == 1
    assert result["preview_rows"][0].get("needs_review") is True


# ── Close-time enforcement — a deterministic backstop, not a prompt request ─
#
# Live testing showed servers occasionally scheduled to 9:30-10pm on a night
# that closes at 9pm, even after the prompt was made more explicit about it
# (plausibly borrowing a busier day's later close from a "staff this like a
# busy Friday" volume note). Prose alone plateaued below 100% compliance, so
# this hard-caps shift_end at the restaurant's own configured close time —
# opt-in per restaurant (get_close_times returns {} until an owner sets
# close_times_json), so a restaurant that hasn't configured this is
# completely unaffected.

def test_shift_scheduled_past_close_is_trimmed_not_just_flagged(monkeypatch):
    # 2026-08-21 is a Friday; configured close is 9pm, no role buffer.
    csv_text = HEADER + "\n" + "2026-08-21,Friday,Sofia R.,Server,4:00pm,9:30pm,5.5,dinner closer"

    result = _run(monkeypatch, csv_text, close_times={"Friday": "9:00pm"})

    assert len(result["preview_rows"]) == 1
    row = result["preview_rows"][0]
    assert row["shift_end"] == "9:00pm"
    assert row["scheduled_hours"] == "5.0"
    assert "auto-capped to close time" in row["notes"]
    assert "needs_review" not in row
    # The shared/exported CSV must reflect the trim too.
    assert "9:00pm,5.0" in result["schedule_csv"]


def test_role_with_configured_after_close_buffer_is_left_alone(monkeypatch):
    # Bartender has an explicit "stay 1h after close" allowance — 10pm on a
    # 9pm-close night is within that buffer and shouldn't be touched.
    csv_text = HEADER + "\n" + "2026-08-21,Friday,Marco D.,Bartender,4:00pm,10:00pm,6.0,closer"

    result = _run(
        monkeypatch, csv_text,
        close_times={"Friday": "9:00pm"}, role_close_buffers={"Bartender": 60},
    )

    row = result["preview_rows"][0]
    assert row["shift_end"] == "10:00pm"
    assert row["scheduled_hours"] == "6.0"
    assert "auto-capped" not in row.get("notes", "")


def test_role_buffer_still_enforced_once_exceeded(monkeypatch):
    # Same bartender buffer, but 10:30pm blows past even the 1h allowance
    # (9pm close + 60min = 10pm ceiling) — still gets trimmed.
    csv_text = HEADER + "\n" + "2026-08-21,Friday,Marco D.,Bartender,4:00pm,10:30pm,6.5,closer"

    result = _run(
        monkeypatch, csv_text,
        close_times={"Friday": "9:00pm"}, role_close_buffers={"Bartender": 60},
    )

    row = result["preview_rows"][0]
    assert row["shift_end"] == "10:00pm"


def test_unconfigured_restaurant_is_not_enforced_at_all(monkeypatch):
    # No close_times passed — matches a restaurant that never set
    # close_times_json. Even a wildly-late shift_end must pass through
    # completely untouched; this feature must never suddenly start
    # rewriting schedules for restaurants that haven't opted in.
    csv_text = HEADER + "\n" + "2026-08-21,Friday,Sofia R.,Server,4:00pm,11:45pm,7.75,closer"

    result = _run(monkeypatch, csv_text)

    row = result["preview_rows"][0]
    assert row["shift_end"] == "11:45pm"
    assert "needs_review" not in row


def test_malformed_scheduled_hours_does_not_crash_the_backstop_passes(monkeypatch):
    # A row whose day already matches (so the day-repair sanity check
    # never runs on it) but whose scheduled_hours holds a stray time
    # string instead of a number -- reproduces a real live crash: the
    # deterministic backstop passes (_ensure_pizza_cook_coverage,
    # _top_up_hours_gap, _extend_shifts_to_close_gap,
    # _trim_server_overlap_cap) each recompute hours_scheduled by summing
    # every row's scheduled_hours, and a bare float() in that sum aborted
    # the whole rest of the job (including the CSV rebuild) the moment it
    # hit this row -- caught only by the outer try/except, silently
    # skipping whatever backstop work hadn't finished yet. Eight
    # overlapping Server rows guarantee _trim_server_overlap_cap actually
    # runs its hours_scheduled recompute (the exact vulnerable line), not
    # just parses cleanly and exits early.
    rows = [
        f"2026-08-17,Monday,Server{i},Server,5:00pm,10:00pm,5,closer"
        for i in range(8)
    ]
    rows.append("2026-08-17,Monday,Jamie L.,Host,5:00pm,9:30pm,2:30pm,closer")  # scheduled_hours corrupted
    csv_text = HEADER + "\n" + "\n".join(rows)

    result = _run(monkeypatch, csv_text)

    assert result["ok"] is True
    # The corrupted row is skipped (contributes 0), not crashed on.
    server_rows = [r for r in result["preview_rows"] if r["role"] == "Server"]
    assert len(server_rows) <= 7  # trim pass actually ran
    # schedule_csv was rebuilt (proves the job didn't abort partway).
    assert "Host" in result["schedule_csv"]


def test_shift_start_already_past_close_is_flagged_not_fabricated(monkeypatch):
    # shift_start itself (10pm) is already past the 9pm ceiling — trimming
    # shift_end to the ceiling would produce negative/zero hours, so this
    # must flag for a human instead of inventing a number.
    csv_text = HEADER + "\n" + "2026-08-21,Friday,Sofia R.,Server,10:00pm,11:00pm,1.0,late add"

    result = _run(monkeypatch, csv_text, close_times={"Friday": "9:00pm"})

    row = result["preview_rows"][0]
    assert row.get("needs_review") is True
    # Left as the model wrote it — not silently rewritten to something wrong.
    assert row["shift_end"] == "11:00pm"
