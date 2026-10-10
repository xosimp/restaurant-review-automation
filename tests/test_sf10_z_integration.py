"""Where the scheduler fix round's workstreams met (10/10/26, workstream Z).

A stopped run starts no further model call: the finished week's note
(schedule_narrative.write, TRUST-15/AI-21) is a model call made after the
draft's own calls, and it went out after the owner pressed Stop. Now the
job checks the stop first — stopped by the owner is said as stopped,
nothing saved, no note written.
"""
import sys

import pytest

import models
import schedule_engine as se
from models import Restaurant, create_restaurant

WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    return db_path


class _Clock(se.GenerationClock):
    """A generation clock the test stops (the owner's Stop) at one moment."""

    def __init__(self):
        super().__init__(job_id=None)
        self.stop = False

    def stopped(self):
        return self.stop

    def stop_requested(self):
        return self.stop


def _week():
    rows = [{"date": d, "day": se._date_of(d).strftime("%A"), "employee": "Ana", "role": "Server",
             "shift_start": "11:00am", "shift_end": "3:00pm", "scheduled_hours": "4", "notes": ""} for d in WEEK]
    return {"ok": True, "schedule_csv": HEAD + "\n".join(",".join(r[c] for c in se._ANSWER_COLS) for r in rows),
            "rows": rows, "week_dates": WEEK, "week_days": [se._date_of(d).strftime("%A") for d in WEEK],
            "summary": [], "hours_budget": 0, "daily_target_hours": {}, "labor_target": 30,
            "unwritten_dates": [], "closed_dates": [], "narrative": ["Ana works every lunch."]}


def _run(monkeypatch, rid, stop_before_note):
    import schedule_narrative
    import schedule_output
    clock = _Clock()
    notes = []
    real_unmet = schedule_output.unmet_items

    def unmet(*a, **k):
        # The last step before the note: the owner presses Stop here.
        clock.stop = stop_before_note
        return real_unmet(*a, **k)

    def write(*a, **k):
        notes.append(1)
        return {"bullets": [], "source": "none", "dropped": []}
    monkeypatch.setattr(schedule_output, "unmet_items", unmet)
    monkeypatch.setattr(schedule_narrative, "write", write)
    monkeypatch.setattr(se, "_build_schedule_result", lambda r, week_start=None: _week())
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    monkeypatch.setattr(se._ops, "job_still_pending", lambda j: True)
    token = se._CLOCK.set(clock)
    try:
        se._run_schedule_job("z-job", rid, gate=False)
    finally:
        se._CLOCK.reset(token)
    return notes, finished


def test_a_run_stopped_before_its_note_writes_no_note_and_saves_nothing(db, monkeypatch):
    rid = create_restaurant(Restaurant(name="Stop Grill", owner_email="stop@x.com"), db_path=db)
    notes, finished = _run(monkeypatch, rid, stop_before_note=True)
    assert notes == [], "a stopped run started the note's model call"
    assert finished.get("status") != "done"
    conn = models.get_conn(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM schedule_history WHERE restaurant_id=?", (rid,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_a_run_not_stopped_writes_its_note(db, monkeypatch):
    rid = create_restaurant(Restaurant(name="Go Grill", owner_email="go@x.com"), db_path=db)
    notes, finished = _run(monkeypatch, rid, stop_before_note=False)
    assert notes == [1]
    assert finished["status"] == "done" and finished["result"]["history_id"]
