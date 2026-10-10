"""Schedule fix round 10/10/26, workstream I1 — TRUST-17: a regenerate keeps
the owner's locked shifts exactly (contract #4: D1 stores `locked`; a Studio
save locks the rows the manager changed; "Keep this shift" locks one).

A full Generate replaced a draft with saved edits without a word, and no
shift could be pinned. The locked rows of the week's working copy now join
the manager plan's fixed rows: merged into the answer by code, restored after
the parse, never moved by a pass — and they stay locked on the new draft."""
import datetime as dt
import json

import models
import schedule_engine as se
import schedule_rows as srows
import schedule_skeleton as sk
import schedule_versions as sv
from tests.test_schedule_skeleton import (  # noqa: F401
    DAYS, HEADER, WEEK, _c, _quiet_passes, _restaurant, db)


def _draft_with_lock(db, rid):
    text = HEADER + "\n" + "\n".join([
        f"{WEEK[1]},Tuesday,Ana,Bartender,5:30pm,11:00pm,5.5,the owner moved this",
        f"{WEEK[2]},Wednesday,Ana,Bartender,4:00pm,11:00pm,7,"])
    hid = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 0, 0, 30, text, [], db_path=db)
    rows = sv.draft_rows(hid)
    rows[0]["locked"] = True
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_history SET schedule_csv=? WHERE id=?", (srows.to_csv(rows), hid))
    conn.commit()
    conn.close()
    return hid


def test_the_locked_rows_of_the_week_are_read_from_its_working_copy(db):
    rid = _restaurant(db, [("Erik", "Owner"), ("Ana", "Bartender")])
    _draft_with_lock(db, rid)
    got = se.locked_rows_of(rid, WEEK[0])
    assert [(r["date"], r["employee"], r["shift_start"]) for r in got] == [(WEEK[1], "Ana", "5:30pm")]
    assert se.locked_rows_of(rid, WEEK[0], dates=[WEEK[2]]) == []


def test_a_locked_row_wins_over_a_planned_row_and_is_told_to_the_model():
    plan = {"rows": [{"date": WEEK[1], "employee": "Ana", "role": "Manager", "shift_start": "3:00pm",
                      "shift_end": "10:00pm", "_pinned": sk.PLAN_SOURCE}]}
    locked = [{"date": WEEK[1], "day": "Tuesday", "employee": "Ana", "role": "Bartender", "shift_start": "5:30pm",
               "shift_end": "11:00pm", "scheduled_hours": "5.5", "notes": "", "locked": True}]
    out = sk.with_locked(plan, locked)
    assert [(r["employee"], r["shift_start"], r["_pinned"]) for r in out["rows"]] == [("Ana", "5:30pm", "locked")]
    block = sk.locked_block(out["rows"])
    assert "THE OWNER'S PINNED SHIFTS" in block and "Ana 5:30pm–11:00pm" in block
    # The model's own row over the locked one is dropped; the locked row joins.
    merged, dropped = sk.merge_pinned([dict(locked[0], shift_start="4:00pm", locked=False)], out["rows"])
    assert [r["shift_start"] for r in merged] == ["5:30pm"] and len(dropped) == 1
    assert sk.locked_block([]) == ""


def test_the_job_keeps_a_locked_shift_exact_and_locked(db, monkeypatch):
    _quiet_passes(monkeypatch)
    rid = _restaurant(db, [("Erik", "Owner"), ("Ana", "Bartender")],
                      close_times_json=json.dumps({d: "10:00pm" for d in DAYS}))
    c = _c(roster_names=["Erik", "Ana"], active={"erik", "ana"}, managers={"erik": "Owner"},
           close_times={d: "10:00pm" for d in DAYS})
    plan = sk.plan_manager_coverage(c, WEEK)
    lock = {"date": WEEK[1], "day": "Tuesday", "employee": "Ana", "role": "Bartender", "shift_start": "5:30pm",
            "shift_end": "11:00pm", "scheduled_hours": "5.5", "notes": "the owner moved this", "locked": True}
    plan = sk.with_locked(plan, [lock])
    # The "model" re-timed her Tuesday and wrote the rest of the week.
    staff = [f"{d},{sk._weekday(d)},Ana,Bartender,{'4:00pm' if d == WEEK[1] else '4:00pm'},10:00pm,6,"
             for d in WEEK[:6]]
    lines, _ = sk.merge_pinned_lines(staff, [r for r in plan["rows"] if r.get("_pinned") == sk.PLAN_SOURCE])
    base = {"ok": True, "schedule_csv": HEADER + "\n" + "\n".join(lines), "week_dates": list(WEEK),
            "week_days": DAYS, "summary": [], "hours_budget": 0, "daily_target_hours": {}, "labor_target": 30,
            "blended_rate": 20.0, "roster": ["Erik", "Ana"], "manager_plan": plan}
    monkeypatch.setattr(se, "_build_schedule_result", lambda r, week_start=None: dict(base))
    done = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, status, result: done.update(status=status, result=result))
    se._run_schedule_job("lock-job", rid)
    res = done["result"]
    assert done["status"] == "done", res
    tue = [r for r in res["preview_rows"] if r["date"] == WEEK[1] and r["employee"] == "Ana"]
    assert [(r["shift_start"], r["shift_end"]) for r in tue] == [("5:30pm", "11:00pm")]
    assert tue[0]["locked"] is True
    stored = sv.draft_rows(res["history_id"])
    kept = [r for r in stored if r["date"] == WEEK[1] and r["employee"] == "Ana"]
    assert len(kept) == 1 and srows.is_locked(kept[0]) and kept[0]["shift_start"] == "5:30pm"


def test_a_full_generation_hands_the_locked_rows_to_every_call(db, monkeypatch):
    import labor
    import time_utils
    import weather
    rid = _restaurant(db, [("Erik", "Owner"), ("Ana", "Bartender")],
                      open_times_json=json.dumps({d: "11:00am" for d in DAYS}),
                      close_times_json=json.dumps({d: "10:00pm" for d in DAYS}))
    _draft_with_lock(db, rid)
    monkeypatch.setattr(labor, "load_shifts_for_restaurant", lambda r: [{"date": "2026-09-28", "employee": "Ana"}])
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda r, **k: {"is_live": True, "blended_rate": 20.0})
    monkeypatch.setattr(labor, "build_demand_forecast", lambda r: {"ok": False})
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: dt.datetime(2026, 10, 1, 9, 0))
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: [])
    captured = {}

    def fake_parts(analysis, shifts, roster_pairs, kwargs):
        captured.update(kwargs)
        return {"schedule_csv": HEADER, "narrative": [], "summary": []}
    monkeypatch.setattr(se, "_generate_in_parts", fake_parts)
    se._build_schedule_result(rid)
    locked = [r for r in captured["pinned_rows"] if r.get("_pinned") == sk.LOCKED_SOURCE]
    assert [(r["date"], r["employee"], r["shift_start"]) for r in locked] == [(WEEK[1], "Ana", "5:30pm")]
    captured.clear()
    se._build_schedule_result(rid, keep_locked=False)       # "Start fresh"
    assert not [r for r in captured["pinned_rows"] or [] if r.get("_pinned") == sk.LOCKED_SOURCE]
