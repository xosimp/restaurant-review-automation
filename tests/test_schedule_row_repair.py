"""client_api.py's _run_schedule_job — day/employee column-repair.

Live generations occasionally come back with one CSV row scrambled: the
model swaps the day and employee columns (e.g. "...,Jamie L.,Friday,
Server,..." instead of "...,Friday,Jamie L.,Server,..."), which used to
render in the app as its own fake "Friday" day-pill holding one name, or
get dumped into NEEDS REVIEW labeled by the wrong field. `date` was correct
in every malformed row observed, so day is now always re-derived from date
server-side: a clean swap gets fully repaired, anything messier gets its
day corrected and an explicit `needs_review` flag set instead of silently
passing a still-wrong row through unmarked.
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


def test_deeper_scramble_gets_flagged_not_silently_passed_through(monkeypatch):
    # Day column omitted entirely — every field after date shifts left.
    csv_text = HEADER + "\n" + "2026-08-21,Jamie L.,Server,11:00am,6:00pm,7,second server staggered"

    result = _run(monkeypatch, csv_text)

    assert len(result["preview_rows"]) == 1
    row = result["preview_rows"][0]
    # Day is still corrected from the (reliable) date...
    assert row["day"] == "Friday"
    # ...but the rest of the row couldn't be confidently recovered, so it
    # must be flagged rather than rendered as a normal, silently-wrong row.
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
