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


def _run(monkeypatch, csv_text):
    monkeypatch.setattr(client_api, "_build_schedule_result", _fake_build_schedule_result(csv_text))
    monkeypatch.setattr(models, "get_staff_notes", lambda restaurant_id: [])
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
