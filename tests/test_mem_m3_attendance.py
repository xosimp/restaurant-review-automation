"""Attendance is watched, per person, and unwatched is unknown (memory audit
9/29/26, attendance).

RPOWER sets scheduled equal to actual, Toast falls back the same way, and a
no-show produces no row, so at a POS restaurant nothing could ever say
someone missed a shift — the engine reported everyone as reliable. The real
no-shows became coverage issues nothing fed back to the person: Maria
missed three Saturdays and next week's draft put her alone on Saturday bar.
"""
from datetime import date, datetime, timedelta

import pytest

import attendance
import models
import schedule_engine
import schedule_learning
import shift_facts
import shift_quality
import staff_settings
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    import intraday
    monkeypatch.setattr(intraday, "get_conn", fake)
    yield


def _rid():
    return create_restaurant(Restaurant(name="Attendance Co", owner_email="a@x.test", module_labor=1))


SATURDAYS = [date(2026, 9, 5), date(2026, 9, 12), date(2026, 9, 19), date(2026, 9, 26)]


def _pos_day(rid, day, workers):
    """The POS's record of one day: punches for `workers`, the day final."""
    rows = [{"date": day.isoformat(), "day": day.strftime("%A"), "employee": w, "role": "Bartender",
             "shift_start": "16:00", "shift_end": "23:00", "scheduled_hours": "7", "actual_hours": "7",
             "sales": "3000", "schedule_known": "0"} for w in workers]
    shift_facts.ingest(rid, rows, "rpower")
    conn = models.get_conn()
    conn.execute("INSERT OR REPLACE INTO labor_daily_history (restaurant_id, date, day_of_week, labor_cost, sales, "
                 "total_hours, source, provider, final) VALUES (?,?,?,?,?,?,?,?,1)",
                 (rid, day.isoformat(), day.strftime("%A"), 500, 3000, 21, "rpower", "rpower"))
    conn.commit()
    conn.close()


def _publish(rid, days, names):
    lines = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"]
    for d in days:
        for n in names:
            lines.append(f"{d.isoformat()},{d.strftime('%A')},{n},Bartender,4:00pm,11:00pm,7,")
    ws = min(days) - timedelta(days=min(days).weekday())
    conn = models.get_conn()
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                 "VALUES (?,?,?,?,datetime('now'))",
                 (rid, ws.isoformat(), (max(days) + timedelta(days=6)).isoformat(), "\n".join(lines) + "\n"))
    conn.commit()
    conn.close()


def test_an_rpower_restaurant_reads_unknown_never_reliable():
    rid = _rid()
    for d in SATURDAYS:
        _pos_day(rid, d, ["Maria G.", "Ana B."])
    assert staff_settings.reliability(rid) == {}              # copied schedules say nothing
    assert "not watched yet" in schedule_engine._reliability_block({})


def test_the_nightly_join_sees_three_missed_saturdays():
    rid = _rid()
    nights = []
    for d in SATURDAYS:
        week = [d - timedelta(days=2), d - timedelta(days=1), d]      # Thu, Fri, Sat — one published week
        _publish(rid, week, ["Maria G.", "Ana B."])
        for day in week:
            missing = day == d and d != SATURDAYS[-1]
            _pos_day(rid, day, ["Ana B."] if missing else ["Ana B.", "Maria G."])
        nights += week
    for d in nights:
        attendance.join_published(rid, d.isoformat())
    evs = attendance.events(rid)
    misses = sorted(e["business_date"] for e in evs if e["employee_name"] == "Maria G." and e["outcome"] == "no_show")
    assert misses == [d.isoformat() for d in SATURDAYS[:3]]
    rel = staff_settings.reliability(rid, today=date(2026, 9, 29))
    assert rel["Maria G."]["no_shows"] == 3 and rel["Maria G."]["unreliable"] is True
    assert rel["Ana B."]["no_shows"] == 0
    by_day = schedule_learning.attendance_by_weekday(rid, min_shifts=3)
    assert by_day["Maria G."]["Saturday"]["no_shows"] == 3


def test_a_night_the_pos_has_not_finished_reporting_is_not_watched():
    rid = _rid()
    d = date(2026, 9, 26)
    _publish(rid, [d], ["Maria G."])
    assert attendance.join_published(rid, d.isoformat()) == {"watched": False, "recorded": 0}
    assert attendance.events(rid) == []


def test_a_covered_swap_is_not_a_miss():
    rid = _rid()
    d = date(2026, 9, 26)
    _publish(rid, [d], ["Maria G.", "Ana B."])
    _pos_day(rid, d, ["Ana B.", "Cy D."])
    conn = models.get_conn()
    hid = conn.execute("SELECT id FROM schedule_history WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.execute("INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, "
                 "status, replacement_name) VALUES (?,?,?,?,?,?,?)",
                 (rid, hid, "Maria G.", d.isoformat(), "4:00pm", "covered", "Cy D."))
    conn.commit()
    conn.close()
    attendance.join_published(rid, d.isoformat())
    ev = next(e for e in attendance.events(rid) if e["employee_name"] == "Maria G.")
    assert ev["outcome"] == "covered" and ev["covered_by"] == "Cy D."


def test_the_live_check_late_and_missing_become_the_persons_record():
    rid = _rid()
    conn = models.get_conn()
    for key, status, note in (("coverage:2026-09-26:maria g.", "resolved", "Closed automatically: they clocked in."),
                              ("coverage:2026-09-26:tom b.", "open", None)):
        who = key.split(":")[2]
        conn.execute("INSERT INTO ops_issues (restaurant_id, kind, source_key, title, status, resolution_note, meta_json) "
                     "VALUES (?,?,?,?,?,?,?)", (rid, "coverage", key, "x", status, note,
                                                 '{"missing": "%s", "shift_start": "4:00pm"}' % who.title()))
    conn.commit()
    conn.close()
    assert attendance.from_coverage_issues(rid, today=date(2026, 9, 29)) == 2
    got = {e["employee_name"]: e["outcome"] for e in attendance.events(rid)}
    assert got == {"Maria G.": "late", "Tom B.": "no_show"}


def test_a_closers_callout_is_recorded_only_for_someone_on_the_schedule():
    rid = _rid()
    d = date(2026, 9, 26)
    _publish(rid, [d], ["Maria Garcia", "Maria Lopez", "Tom B."])
    names = attendance.from_closeout(rid, d.isoformat(), "Tom B. (sick), Maria, someone new")
    assert names == ["Tom B."]                       # "Maria" is two people: never guessed
    # A stronger source stands: the join the next morning cannot overwrite it.
    _pos_day(rid, d, ["Maria Garcia", "Maria Lopez"])
    attendance.join_published(rid, d.isoformat())
    tom = next(e for e in attendance.events(rid) if e["employee_name"] == "Tom B.")
    assert tom["outcome"] == "called_out" and tom["source"] == "closeout_confirmed"


def test_the_reliability_dimension_withdraws_when_nobody_was_watched():
    ctx = shift_quality.ShiftContext(date="2026-10-03", daypart="night", reliability={},
                                     rows=[{"employee": "Maria G.", "role": "Bartender", "shift_start": "4:00pm",
                                            "shift_end": "11:00pm", "date": "2026-10-03"}])
    assert shift_quality.dim_reliability(ctx) is None


def test_the_nightly_people_job_records_last_weeks_nights_with_the_standard_counts(monkeypatch):
    import strategy_jobs
    rid = _rid()
    d = date(2026, 9, 26)
    _publish(rid, [d], ["Maria G.", "Ana B."])
    _pos_day(rid, d, ["Ana B."])
    out = strategy_jobs.run_people_nightly(today=date(2026, 9, 28))
    assert {"attempted", "ok", "failed", "skipped", "hit_bound"} <= set(out)
    assert out["failed"] == 0 and out["attendance"] >= 2
    assert {e["employee_name"]: e["outcome"] for e in attendance.events(rid)} == {"Maria G.": "no_show",
                                                                               "Ana B.": "on_time"}
