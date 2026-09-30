"""The RPOWER schedule push, previewed: the exact body it would send, checked
against the store's own records, and never sent (POST enabled 9/29/26)."""
import pytest

import labor
import rpower
from tests.test_rpower import _connected, _redirect, _stub_api  # noqa: F401 — _redirect is autouse

_JOBS = [{"mid": "7777", "name": "Kitchen", "ext_id": "Q39U9N"},
         {"mid": "7778", "name": "Server AM", "ext_id": "Q39U9O"}]
_EMPS = [{"mid": "4444", "fname": "Dana", "lname": "Reyes", "name": "REYES, DANA", "payroll_id": "20CY2G",
          "term_date": "2000-01-01T00:00:00"},
         {"mid": "4445", "fname": "Bo", "lname": "Park", "name": "PARK, BO", "payroll_id": "1C4PM4",
          "term_date": "2000-01-01T00:00:00"},
         {"mid": "4446", "fname": "Old", "lname": "Hand", "name": "HAND, OLD", "payroll_id": "N8UKIB",
          "term_date": "2025-03-01T00:00:00"}]
_HELD = [{"mid": "1", "emp_mid": "4444", "job_mid": "7778", "is_inactive": 0, "reg_rate": 2.13},
         {"mid": "2", "emp_mid": "4445", "job_mid": "7777", "is_inactive": 0, "reg_rate": 16}]

CSV = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
       "2026-10-05,Monday,Dana Reyes,Server AM,10:00 AM,3:00 PM,5,\n"
       "2026-10-05,Monday,Bo Park,Kitchen,4:00 PM,12:30 AM,8.5,close\n"
       "2026-10-06,Tuesday,New Hire,Kitchen,9:00 AM,5:00 PM,8,\n")


@pytest.fixture(autouse=True)
def _no_post(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the preview must never POST")
    monkeypatch.setattr("requests.post", refuse)
    rpower.clear_caches()


def _stub(monkeypatch):
    return _stub_api(monkeypatch, {"job/getbycg": _JOBS, "employee/getbycg": _EMPS,
                                   "employeejob/getbystore": _HELD})


def test_the_schedule_csv_becomes_local_datetimes_and_a_close_ends_the_next_day():
    shifts = labor.timed_shifts_from_csv(CSV)
    assert shifts[0] == {"employee": "Dana Reyes", "role": "Server AM",
                         "start": "2026-10-05T10:00:00", "end": "2026-10-05T15:00:00"}
    assert shifts[1]["end"] == "2026-10-06T00:30:00"


def test_the_preview_builds_rpowers_documented_body_by_name_and_sends_nothing(db_path, monkeypatch):
    rid = _connected(db_path)
    calls = _stub(monkeypatch)
    out = rpower.preview_labor_schedule(rid, labor.timed_shifts_from_csv(CSV))
    assert out["sent"] is False and out["ok"] is True and out["problems"] == []
    body = out["would_send"]
    assert body["storeMid"] == "1123000884211712110"
    dana = next(s for s in body["schedules"] if s["payrollid"] == "20CY2G")
    assert dana["schedules"] == [{"inTime": "2026-10-05T10:00:00", "jobCode": "Server AM",
                                  "outTime": "2026-10-05T15:00:00"}]
    # no POS id linked in Cavnar: the payroll id came from RPOWER's list by exact name
    assert out["skipped"] == [{"employee": "New Hire", "why": "no payroll id at the POS"}]
    assert {c["path"] for c in calls} <= {"job/getbycg", "employee/getbycg", "employeejob/getbystore"}


def test_the_preview_names_what_rpower_would_reject_or_misfile(db_path, monkeypatch):
    rid = _connected(db_path)
    _stub(monkeypatch)
    out = rpower.preview_labor_schedule(rid, [
        {"employee_payroll_id": "20CY2G", "job": "Kitchen", "start": "2026-10-05T10:00:00", "end": "2026-10-05T15:00:00"},
        {"employee_payroll_id": "20CY2G", "job": "Server AM", "start": "2026-10-05T14:00:00", "end": "2026-10-05T18:00:00"},
        {"employee_payroll_id": "1C4PM4", "job": "Dishwasher", "start": "2026-10-05T10:00:00", "end": "2026-10-05T09:00:00"},
        {"employee_payroll_id": "N8UKIB", "job": "Kitchen", "start": "2026-10-05T10:00", "end": "2026-10-05T15:00"},
    ])
    problems = [p["problem"] for p in out["problems"]]
    assert out["ok"] is False
    assert 'Dana Reyes does not hold "Kitchen" at the POS' in problems
    assert any(p.startswith("overlaps 2026-10-05T10:00:00") for p in problems)
    assert 'the store has no job "Dishwasher"' in problems and "ends before it starts" in problems
    assert "no current employee has this payroll id" in problems        # left the store
    assert "time not in YYYY-MM-DDTHH:mm:ss" in problems


def test_a_name_two_current_people_share_matches_neither(db_path, monkeypatch):
    rid = _connected(db_path)
    twin = dict(_EMPS[0], mid="4447", payroll_id="ZZZ111")
    _stub_api(monkeypatch, {"job/getbycg": _JOBS, "employee/getbycg": _EMPS + [twin],
                            "employeejob/getbystore": _HELD})
    out = rpower.preview_labor_schedule(rid, labor.timed_shifts_from_csv(CSV))
    assert "Dana Reyes" in [s["employee"] for s in out["skipped"]]
