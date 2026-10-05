"""A "hasn't clocked in" issue once the shift is over (Simple EJ's, 10/5/26).

The live clock-in check raised the issue and nothing ever closed it but a
clock-in or a manager: at 4:20pm Home still said "Mia Martin hasn't clocked
in" for a 11:00am–4:00pm shift and offered "Ask Amy to cover", and an
earlier day's issue stayed there the next morning. Now a gap whose shift has
ended is marked over — "didn't come in", no cover offered or askable — and
an issue from an earlier business date closes itself, still read by the
nightly attendance as a no-show.
"""
import datetime as dt

import attendance
import intraday
import issues
import strategy_jobs
import strategy_routes
from models import Restaurant, create_restaurant

DAY = "2026-10-05"


def _rid(db_path):
    return create_restaurant(Restaurant(name="Ended Co", owner_email="e@x.test", module_labor=1), db_path=db_path)


def _group(db_path, rid, people, covers, day=DAY, role="server"):
    meta = {"business_date": day, "role": role, "family": role, "people": people, "covers": covers,
            "scheduled_in_role": 6}
    title, detail = issues.coverage_texts(meta)
    issue, _t = issues.create_issue(rid, "coverage", title, detail=detail, severity="high",
                                    source_key=issues.coverage_key(day, role), notify=False, meta=meta,
                                    db_path=db_path)
    return issue


def _gap(name, start, end, status="missing"):
    return {"employee": name, "role": "Server AM", "shift_start": start, "shift_end": end, "status": status,
            "minutes_late": 20}


def _at(h, m=0, day=DAY):
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(h, m))


def test_a_gap_whose_shift_ended_reads_didnt_come_in_and_offers_no_cover(db_path):
    rid = _rid(db_path)
    iss = _group(db_path, rid, [_gap("Evan Price", "11:00am", "3:30pm")],
                 [{"name": "Amy Baylis", "kind": "off", "for": "Evan Price", "shift_start": "11:00am"}])
    out = issues.close_ended_coverage(rid, DAY, _at(15, 0), db_path=db_path)
    assert out["ended"] == [], "still on shift at 3:00pm"
    assert strategy_routes.askable_covers(issues.get_issue(rid, iss["id"], db_path)), "a cover while it runs"
    out = issues.close_ended_coverage(rid, DAY, _at(16, 20), db_path=db_path)
    assert out["ended"] == ["Evan Price"]
    now = issues.get_issue(rid, iss["id"], db_path)
    assert now["status"] != "resolved", "open for the day: the manager still sees who didn't come"
    assert now["title"] == "Evan Price didn't come in"
    assert "ended with no clock-in" in now["detail"] and "cover" not in now["detail"].lower()
    gap = issues.cover_gaps(now)[0]
    assert gap["cover"] is None and gap["shift_over"] is True
    assert strategy_routes.askable_covers(now) == []
    asked = intraday.ask_to_cover(rid, iss["id"], "Amy Baylis", db_path=db_path)
    assert asked == {"ok": False, "error": "That shift has already ended."}


def test_only_the_ended_gap_stops_asking_for_a_cover(db_path):
    rid = _rid(db_path)
    iss = _group(db_path, rid, [_gap("Evan Price", "11:00am", "3:30pm"), _gap("Kerri Trejo", "4:00pm", "11:00pm")],
                 [{"name": "Amy Baylis", "kind": "off", "for": "Evan Price", "shift_start": "11:00am"},
                  {"name": "Gideon Kopalchick", "kind": "off", "for": "Kerri Trejo", "shift_start": "4:00pm"}])
    issues.close_ended_coverage(rid, DAY, _at(16, 20), db_path=db_path)
    now = issues.get_issue(rid, iss["id"], db_path)
    by = {g["employee"]: g for g in issues.cover_gaps(now)}
    assert by["Evan Price"]["cover"] is None
    assert by["Kerri Trejo"]["cover"]["name"] == "Gideon Kopalchick"
    assert now["title"] == "Kerri Trejo hasn't clocked in"
    assert "Didn't come in (shift over): Evan Price." in now["detail"]
    assert [c["name"] for c in strategy_routes.askable_covers(now)] == ["Gideon Kopalchick"]


def test_a_shift_past_midnight_is_over_only_after_it_ends(db_path):
    rid = _rid(db_path)
    iss = _group(db_path, rid, [_gap("Andrew Marola", "5:00pm", "2:00am")], [], role="manager foh")
    assert issues.close_ended_coverage(rid, DAY, _at(23, 30), db_path=db_path)["ended"] == []
    late = dt.datetime.combine(dt.date(2026, 10, 6), dt.time(2, 15))           # still the 10/5 business date
    assert issues.close_ended_coverage(rid, DAY, late, db_path=db_path)["ended"] == ["Andrew Marola"]
    assert issues.get_issue(rid, iss["id"], db_path)["title"] == "Andrew Marola didn't come in"


def test_an_earlier_days_issue_closes_and_still_counts_as_a_no_show(db_path):
    rid = _rid(db_path)
    old = _group(db_path, rid, [_gap("Marissa Kalamaris", "11:00am", "3:30pm")], [], day="2026-10-04")
    legacy, _t = issues.create_issue(rid, "coverage", "Mia Martin hasn't clocked in", severity="high",
                                     source_key="coverage:2026-10-04:mia martin", notify=False,
                                     meta={"missing": "Mia Martin", "role": "Host AM", "shift_start": "11:00am"},
                                     db_path=db_path)
    today = _group(db_path, rid, [_gap("Evan Price", "4:00pm", "11:00pm")], [])
    out = issues.close_ended_coverage(rid, DAY, _at(10, 0), db_path=db_path)
    assert sorted(out["closed"]) == sorted([old["id"], legacy["id"]])
    for iid in (old["id"], legacy["id"]):
        row = issues.get_issue(rid, iid, db_path)
        assert row["status"] == "resolved" and row["resolution_note"] == issues.AUTO_ENDED_NOTE
    assert issues.get_issue(rid, today["id"], db_path)["status"] != "resolved", "today's is still today's"
    open_ids = [i["id"] for i in issues.list_issues(rid, status="unresolved", db_path=db_path)]
    assert old["id"] not in open_ids and legacy["id"] not in open_ids
    n = attendance.from_coverage_issues(rid, today=dt.date.fromisoformat(DAY), db_path=db_path)
    assert n == 2, "both closed-at-day's-end gaps are recorded as no-shows"


def test_a_manager_closing_an_issue_by_hand_is_still_not_a_no_show(db_path):
    rid = _rid(db_path)
    iss = _group(db_path, rid, [_gap("Marissa Kalamaris", "11:00am", "3:30pm")], [], day="2026-10-04")
    issues._resolve(rid, iss["id"], "She swapped with Anna", db_path)
    legacy, _t = issues.create_issue(rid, "coverage", "Mia Martin hasn't clocked in", source_key="coverage:2026-10-04:mia",
                                     notify=False, meta={"missing": "Mia"}, db_path=db_path)
    issues._resolve(rid, legacy["id"], "Excused", db_path)
    assert attendance.from_coverage_issues(rid, today=dt.date.fromisoformat(DAY), db_path=db_path) == 0


def test_the_check_closes_ended_shifts_even_when_the_restaurant_is_closed(db_path, monkeypatch):
    """Driven through run_coverage_check itself: the close-out runs before
    the open-hours gate, so a night after close still settles the day."""
    import models
    rid = _rid(db_path)
    r = models.get_restaurant(rid, db_path=db_path)
    old = _group(db_path, rid, [_gap("Evan Price", "11:00am", "3:30pm")], [], day="2026-10-04")
    monkeypatch.setattr(strategy_jobs, "_slot_iter", lambda *a, **k: iter([r]))
    monkeypatch.setattr(strategy_jobs, "_open_now", lambda *a, **k: False)
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: _at(9, 0))
    monkeypatch.setattr(time_utils, "business_date", lambda *a, **k: dt.date.fromisoformat(DAY))
    strategy_jobs.run_coverage_check(db_path=db_path)
    assert issues.get_issue(rid, old["id"], db_path)["status"] == "resolved"
