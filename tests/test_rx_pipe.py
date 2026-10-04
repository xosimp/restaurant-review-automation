"""Schedule re-audit 10/4/26, workstream PIPE: the generation pipeline end to
end — the redo, the save, the auto-draft, the job's clock — and the pins.

PIPE-1   a redo never re-parses the kept days: their rows (the plan's and
         the owner's) come back exactly as saved, pinned, never clamped
PIPE-2   an edit saved to the draft while a redo runs is never lost
PIPE-3   the auto-draft never supersedes a draft the owner has; auto-publish
         never sends an unedited AI draft over an edited one
PIPE-4   no row on a closed day is kept; one there is a hard breach
PIPE-5   the owner's row for a dormant person on a kept day is legal
PIPE-6   a stale NEEDS REVIEW mark never survives a redo
PIPE-7   the draft and its review are one write
PIPE-8   a failure after the save never says "nothing was saved"
PIPE-9   a job is judged by its own deadline, not the time it queued
PIPE-10  the late-close pad stops at the owner's after-close allowance
UI-6     the manager plan's pin survives save, reload, fixes, Improve, redo
UI-8     Generate never joins a running generation of another request

The model is never called (labor.generate_optimized_schedule stubbed, as
tests/test_schedule_b2_calls.py does).
"""
import json

import pytest

from test_schedule_b2_calls import db, _build_harness, _gen, _csv, _line, _restaurant, WEEK  # noqa: F401
import client_api
import labor
import models
import ops
import schedule_engine as se
import schedule_rules as sr
import schedule_skeleton as skel
import schedule_versions as sv
import staff_settings

DAYS7 = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _finish(monkeypatch):
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    return finished


def _person(name, role, last="2026-09-28"):
    return {"name": name, "role": role, "shifts": 9, "last_worked": last, "is_manual": False,
            "added_at": None, "active": True, "settings": {}, "recent_roles": [role]}


def _history(db_path, rid):
    conn = models.get_conn(db_path)
    try:
        return [tuple(r) for r in conn.execute("SELECT id, superseded_by FROM schedule_history WHERE restaurant_id=? "
                                               "ORDER BY id", (rid,))]
    finally:
        conn.close()


# ── PIPE-1: the kept days are owner rows ──────────────────────────────────

def _late_close_harness(monkeypatch, db_path):
    rid = _build_harness(monkeypatch, db_path,
                         close_times_json=json.dumps({d: "11:00pm" for d in DAYS7}),
                         role_close_buffer_json=json.dumps({"Bartender": 60}))
    people = [_person("Ana", "Server"), _person("Bo", "Bartender"), _person("Mia", "Manager")]
    monkeypatch.setattr(staff_settings, "roster", lambda *a, **k: [dict(p) for p in people])
    return rid


def test_a_redo_keeps_a_planned_manager_row_on_a_kept_day_exactly_and_opens_no_manager_gap(db, monkeypatch):
    rid = _late_close_harness(monkeypatch, db)
    sat = WEEK[5]
    lines = [f"{sat},Saturday,Mia,Manager,3:00pm,12:00am,9,Cavnar AI: manager plan — covers open to close",
             f"{sat},Saturday,Bo,Bartender,6:00pm,12:00am,6,",
             f"{sat},Saturday,Ana,Server,5:00pm,11:00pm,6,",
             # the owner's own late close on a kept day, past its role's cap
             f"{WEEK[4]},Friday,Ana,Server,5:00pm,11:45pm,6.75,stay for the party"]
    base = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 27.75, 0, 30, _csv(lines), [], db_path=db)
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _gen([[f"{WEEK[0]},Monday,Mia,Manager,3:00pm,11:00pm,8,", _line(WEEK[0], "Ana")]], []))
    finished = _finish(monkeypatch)
    se._run_schedule_job("redo-cap", rid, dates=[WEEK[0]], base_history_id=base)
    assert finished["status"] == "done", finished
    res = finished["result"]
    sat_rows = {r["employee"]: r for r in res["preview_rows"] if r["date"] == sat}
    mia = sat_rows["Mia"]
    assert (mia["shift_start"], mia["shift_end"], mia["scheduled_hours"]) == ("3:00pm", "12:00am", "9")
    assert "auto-capped" not in mia["notes"]
    assert mia["_pinned"] == skel.PLAN_SOURCE and mia["_pin_reason"] == "covers open to close"
    fri = [r for r in res["preview_rows"] if r["date"] == WEEK[4]][0]
    assert (fri["shift_end"], fri["scheduled_hours"]) == ("11:45pm", "6.75")       # never clamped
    assert fri["_pinned"] == skel.KEPT_SOURCE
    assert not [v for v in res["rule_violations"] if v["kind"] == "no_manager" and v.get("date") == sat]
    blockers = [b["text"] for b in client_api.publish_review(rid, res["history_id"])["blockers"]]
    assert not [b for b in blockers if "Saturday" in b and "manager" in b.lower()], blockers


def test_kept_rows_are_pinned_rows_with_their_marks_off():
    rows = skel.kept_rows([
        {"date": WEEK[0], "employee": "Mia", "notes": "Cavnar AI: manager plan — covers open — NEEDS REVIEW: x"},
        {"date": WEEK[1], "employee": "Ana", "notes": "bring keys — NEEDS REVIEW: over 40h for the week"}])
    assert rows[0]["_pinned"] == skel.PLAN_SOURCE and rows[0]["_pin_reason"] == "covers open"
    assert rows[0]["notes"] == "Cavnar AI: manager plan — covers open"
    assert rows[1]["_pinned"] == skel.KEPT_SOURCE and rows[1]["notes"] == "bring keys"


# ── PIPE-2: an edit saved while a redo runs ───────────────────────────────

def _race(monkeypatch, db_path, rid, base, base_csv, old_line, new_line, answer):
    calls = []
    inner = _gen([answer], calls)

    def fake(*a, **k):
        edited = base_csv.replace(old_line, new_line)
        assert edited != base_csv
        conn = models.get_conn(db_path)
        conn.execute("BEGIN IMMEDIATE")
        sv.write_on(conn, rid, base, "studio_save", edited, saved_by="owner")
        conn.commit()
        conn.close()
        return inner(*a, **k)
    monkeypatch.setattr(labor, "generate_optimized_schedule", fake)
    return calls


def test_an_edit_to_a_kept_day_saved_while_a_redo_runs_is_in_the_redo(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    base_csv = _csv([_line(d, "Ana") for d in WEEK])
    base = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 42.0, 0, 30, base_csv, [], db_path=db)
    calls = _race(monkeypatch, db, rid, base, base_csv, f"{WEEK[4]},Friday,Ana,Server,4:00pm,10:00pm,6,",
                  f"{WEEK[4]},Friday,Ana,Server,5:00pm,9:00pm,4,owner edit", [_line(WEEK[0], "Bo")])
    finished = _finish(monkeypatch)
    se._run_schedule_job("redo-race", rid, dates=[WEEK[0]], base_history_id=base)
    assert finished["status"] == "done", finished
    res = finished["result"]
    assert len(calls) == 1                                      # the answer is reused, never paid twice
    fri = [r for r in res["preview_rows"] if r["date"] == WEEK[4]]
    assert [(r["shift_start"], r["shift_end"], r["notes"]) for r in fri] == [("5:00pm", "9:00pm", "owner edit")]
    mon = [r["employee"] for r in res["preview_rows"] if r["date"] == WEEK[0]]
    assert mon == ["Bo"]
    conn = models.get_conn(db)
    saved = conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (res["history_id"],)).fetchone()
    conn.close()
    assert "5:00pm,9:00pm,4,owner edit" in saved["schedule_csv"]
    assert _history(db, rid) == [(base, res["history_id"]), (res["history_id"], None)]


def test_an_edit_to_a_redone_day_saved_while_the_redo_runs_wins_and_nothing_is_saved(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    base_csv = _csv([_line(d, "Ana") for d in WEEK])
    base = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 42.0, 0, 30, base_csv, [], db_path=db)
    _race(monkeypatch, db, rid, base, base_csv, f"{WEEK[0]},Monday,Ana,Server,4:00pm,10:00pm,6,",
          f"{WEEK[0]},Monday,Ana,Server,5:00pm,9:00pm,4,", [_line(WEEK[0], "Bo")])
    finished = _finish(monkeypatch)
    se._run_schedule_job("redo-race2", rid, dates=[WEEK[0]], base_history_id=base)
    assert finished["status"] == "error"
    assert "Monday 10/5/26 was edited and saved while Cavnar AI redid it" in finished["result"]["error"]
    assert "your edit is kept" in finished["result"]["error"]
    assert _history(db, rid) == [(base, None)]                 # the owner's draft is still the one in force


def test_a_save_over_a_draft_that_changed_refuses_inside_its_one_write(db):
    rid = _restaurant(db, module_labor=1)
    base_csv = _csv([_line(WEEK[0], "Ana")])
    base = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6.0, 0, 30, base_csv, [], db_path=db)
    with pytest.raises(models.DraftChanged) as e:
        models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6.0, 0, 30, base_csv, [], db_path=db,
                                     base=(base, base_csv + "\n"))
    assert e.value.current_csv == base_csv
    assert _history(db, rid) == [(base, None)]


# ── PIPE-5 / PIPE-6: a redo's kept days are judged as they are ────────────

def test_an_owner_row_for_a_dormant_person_on_a_kept_day_is_legal_after_a_redo(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    people = [_person("Ana", "Server"), _person("Bo", "Server"), _person("Dana", "Server", last="2026-06-01")]
    monkeypatch.setattr(staff_settings, "roster", lambda *a, **k: [dict(p) for p in people])
    monkeypatch.setattr(staff_settings, "dormant_people", lambda *a, **k: {"dana": "2026-06-01"})
    lines = [_line(d, "Ana") for d in WEEK] + [_line(WEEK[4], "Dana", "5:00pm", "10:00pm", 5)]
    base = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 47.0, 0, 30, _csv(lines), [], db_path=db)
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([[_line(WEEK[0], "Bo")]], []))
    finished = _finish(monkeypatch)
    se._run_schedule_job("redo-dormant", rid, dates=[WEEK[0]], base_history_id=base)
    res = finished["result"]
    dana = [r for r in res["preview_rows"] if r["employee"] == "Dana"]
    assert dana and not dana[0].get("needs_review") and "NEEDS REVIEW" not in (dana[0].get("notes") or "")
    assert not [v for v in res["rule_violations"] if v["kind"] == "off_roster"]


def test_a_stale_needs_review_mark_comes_off_a_kept_row_and_a_live_one_reads_todays_reason(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    lines = [_line(d, "Ana") for d in WEEK]
    lines[4] = lines[4] + "NEEDS REVIEW: over 40h for the week"
    base = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 42.0, 0, 30, _csv(lines), [], db_path=db)
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([[_line(WEEK[0], "Bo")]], []))
    finished = _finish(monkeypatch)
    se._run_schedule_job("redo-mark", rid, dates=[WEEK[0]], base_history_id=base)
    res = finished["result"]
    fri = [r for r in res["preview_rows"] if r["date"] == WEEK[4]][0]
    assert not fri.get("needs_review") and "NEEDS REVIEW" not in (fri.get("notes") or "")
    blockers = [b["text"] for b in client_api.publish_review(rid, res["history_id"])["blockers"]]
    assert not [b for b in blockers if "NEEDS REVIEW" in b], blockers


# ── PIPE-4: a closed day ──────────────────────────────────────────────────

def test_a_whole_week_answers_row_on_a_closed_day_is_never_kept(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    sr.save_closures(rid, closed_dates=[WEEK[2]], db_path=db)
    lines = [_line(d, "Ana" if i % 2 else "Bo") for i, d in enumerate(WEEK)]   # the model wrote the closed day
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([lines], []))
    finished = _finish(monkeypatch)
    se._run_schedule_job("closed", rid)
    assert finished["status"] == "done", finished
    assert not [r for r in finished["result"]["preview_rows"] if r["date"] == WEEK[2]]


def test_a_row_on_a_closed_day_is_a_hard_breach_and_no_pass_may_add_one(db):
    rid = _restaurant(db, module_labor=1)
    sr.save_closures(rid, closed_dates=[WEEK[2]], db_path=db)
    c = sr.build_constraints(rid, WEEK, DAYS7)
    row = {"date": WEEK[2], "day": "Wednesday", "employee": "Bo", "role": "Server", "shift_start": "4:00pm",
           "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}
    kinds = [(v["kind"], v["hard"], v["no_show"]) for v in sr.violations([row], c)]
    assert ("closed_day", True, True) in kinds
    assert c.can_add(row, [])[0] is False


def test_labor_asks_for_and_keeps_only_open_dates(db, monkeypatch):
    import inspect
    src = inspect.getsource(labor.generate_optimized_schedule)
    assert "and d not in _closed_set]" in src                 # _gen_dates, every contract
    assert "not in _closed_set]" in src.split("elif _closed_set:")[1][:200]     # the CSV fallback


# ── PIPE-7 / PIPE-8: the save ─────────────────────────────────────────────

def test_the_partial_week_blocker_is_saved_with_the_draft_in_one_write(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    week = [_line(d, "Ana" if i % 2 else "Bo") for i, d in enumerate(WEEK) if d != WEEK[2]]
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([week, []], []))
    finished = _finish(monkeypatch)
    se._run_schedule_job("partial", rid)
    res = finished["result"]
    assert res["unwritten_dates"] and res["history_id"]
    conn = models.get_conn(db)
    row = conn.execute("SELECT review_json, stage_seconds_json, economics_json FROM schedule_history WHERE id=?",
                       (res["history_id"],)).fetchone()
    conn.close()
    assert row["review_json"] and row["stage_seconds_json"] and row["economics_json"]
    blockers = [b["text"] for b in client_api.publish_review(rid, res["history_id"])["blockers"]]
    assert [b for b in blockers if "was not written" in b], blockers


def test_a_failed_review_write_is_a_failed_save_never_a_draft_without_its_blockers(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _gen([[_line(d, "Ana" if i % 2 else "Bo") for i, d in enumerate(WEEK)]], []))
    seen = {}

    def locked(*a, **k):
        seen.update(k)
        raise models.sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(models, "save_schedule_history", locked)
    finished = _finish(monkeypatch)
    se._run_schedule_job("locked", rid)
    assert isinstance(seen.get("review"), dict) and "lines" in seen["review"]     # the review rides the one write
    assert finished["result"].get("history_id") is None
    assert _history(db, rid) == []


def test_a_failure_after_the_save_names_the_saved_draft(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    prev = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 42.0, 0, 30, _csv([_line(d, "Ana") for d in WEEK]),
                                        [], db_path=db)
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _gen([[_line(d, "Ana" if i % 2 else "Bo") for i, d in enumerate(WEEK)]], []))

    def boom(result):
        raise KeyError("date")
    monkeypatch.setattr(se, "_quality_gate", boom)
    finished = _finish(monkeypatch)
    se._run_schedule_job("after-save", rid)
    res = finished["result"]
    assert finished["status"] == "done" and res["history_id"] and res["history_id"] != prev
    assert "nothing was saved" not in json.dumps(res)
    assert res["review"]["lines"][0].startswith("Cavnar AI saved this draft but couldn't finish its last step")


def test_a_saved_draft_whose_payload_failed_is_named_not_called_unsaved(monkeypatch):
    finished = _finish(monkeypatch)
    se._finish_after_save("j", 1, 42, None, WEEK)
    assert finished["status"] == "error"
    assert finished["result"]["saved"] is True and finished["result"]["history_id"] == 42
    assert "Cavnar AI saved the draft for the week of 10/5/26" in finished["result"]["error"]
    assert "nothing was saved" not in finished["result"]["error"]


# ── PIPE-10: the late-close pad and the owner's after-close allowance ─────

def _friday_close(db_path, **cols):
    rid = _restaurant(db_path, module_labor=1, close_times_json=json.dumps({"Friday": "10:00pm"}),
                      role_close_buffer_json=json.dumps({"Bartender": 30}), **cols)
    return rid, sr.build_constraints(rid, WEEK, DAYS7)


def test_the_late_close_pad_stops_at_the_after_close_allowance_and_says_the_rest(db):
    import schedule_memory as sm
    rid, c = _friday_close(db)
    row = {"date": WEEK[4], "day": "Friday", "employee": "Bo", "role": "Bartender",
           "shift_start": "5:00pm", "shift_end": "10:00pm", "scheduled_hours": "5", "notes": ""}
    learned = [{"kind": "end_overrun", "day": "Friday", "daypart": None, "role": sm._fam("Bartender", None),
                "value": {"minutes": 45, "role": "Bartender", "typical_over": 45, "over": 5, "closes": 6}}]
    out = sm.pad_overruns([row], learned, c=c)
    assert out["rows"][0]["shift_end"] == "10:30pm"              # close + the 30 minutes allowed, no further
    assert out["padded"][0]["minutes"] == 30
    assert out["left"] and "after-close allowance" in out["left"][0]["reason"]
    # A close already at the allowance is not padded at all, and says why.
    at_cap = dict(row, shift_end="10:30pm", scheduled_hours="5.5")
    out = sm.pad_overruns([at_cap], learned, c=c)
    assert out["rows"][0]["shift_end"] == "10:30pm" and not out["padded"]
    assert "after-close allowance" in out["left"][0]["reason"]


def test_a_shift_past_the_after_close_allowance_is_flagged_by_its_role_never_by_who_works_it(db):
    rid, c = _friday_close(db)
    late = {"date": WEEK[4], "day": "Friday", "employee": "Bo", "role": "Bartender",
            "shift_start": "5:00pm", "shift_end": "11:15pm", "scheduled_hours": "6.25", "notes": ""}
    v = [x for x in sr.violations([late], c) if x["kind"] == "past_close"]
    assert v and not v[0]["hard"] and v[0]["severity"] == 0.75
    assert sr.breach_id(v[0]) == ("past_close", WEEK[4], "bartender")
    assert sr.BREACH_TIER["past_close"] == sr.TIER_COVERAGE         # no pass ranked below may cause one
    ok = dict(late, shift_end="10:30pm", scheduled_hours="5.5")
    assert not [x for x in sr.violations([ok], c) if x["kind"] == "past_close"]
    # A manager may stay as long as the longest stay of any role (the plan).
    c.managers = {"mia": "Manager"}
    mgr = dict(ok, employee="Mia", role="Manager")
    assert not [x for x in sr.violations([mgr], c) if x["kind"] == "past_close"]


# ── PIPE-3: the auto-draft and a draft the owner has ──────────────────────

def test_the_auto_draft_leaves_a_week_the_owner_drafted_a_week_ago_and_edited(db, monkeypatch):
    import datetime as dt
    import strategy_jobs
    rid = _restaurant(db, module_labor=1, auto_draft_schedule=1)
    owner = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 42.0, 0, 30, _csv([_line(d, "Ana") for d in WEEK]),
                                         [], db_path=db)
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_history SET generated_at=datetime('now','-6 days'), edited_at=datetime('now'), "
                 "edited_by='owner' WHERE id=?", (owner,))
    conn.commit()
    conn.close()
    ran = []
    monkeypatch.setattr(se, "_run_schedule_job", lambda job_id, r: ran.append(r))
    monkeypatch.setattr(ops, "read_async_job", lambda *a, **k: {"status": "done"})
    monkeypatch.setattr("push.fire_push", lambda *a, **k: None)
    thursday = dt.datetime(2026, 10, 1, 9, 0)                        # drafts the week of 10/5
    out = strategy_jobs.run_auto_draft_schedules(db_path=db, now=thursday)
    assert ran == [] and out["drafted"] == 0
    assert _history(db, rid) == [(owner, None)]
