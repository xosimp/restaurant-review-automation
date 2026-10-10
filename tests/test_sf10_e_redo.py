"""Scheduler audit 10/10/26, workstream E — AI-28: an owner's redo of a day
is drafted in code first (re-shaped from SHIFT REQUIREMENTS on the usual
shifts and solved, the reason read as rules); the model is called only when
that changes nothing or the reason is a judgement code cannot read."""
import json
import sys
import types

import pytest

import labor
import models
import schedule_engine as se
import schedule_rules as sr
from models import Restaurant, create_restaurant

WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


def _row(d, who, start="4:00pm", end="10:00pm"):
    return {"date": d, "day": DAYS[WEEK.index(d)], "employee": who, "role": "Server", "shift_start": start,
            "shift_end": end, "scheduled_hours": "6", "notes": ""}


def test_a_chip_or_readable_words_are_redone_in_code_a_judgement_goes_to_the_model(monkeypatch):
    assert se._redo_reads_as_rules(1, None)
    assert se._redo_reads_as_rules(1, se.with_redo_reason(None, {"chip": "too_many", "text": None}))
    import owner_rules as orr
    monkeypatch.setattr(orr, "_roster", lambda rid, db_path=None: (["Maria Lopez"], ["Server"]))
    monkeypatch.setattr(orr, "read", lambda rid, todo, source, names, roles, week_dates=None, **k:
                        {s: ([{"kind": "close"}] if "Maria" in s else []) for s in todo})
    assert se._redo_reads_as_rules(1, se.with_redo_reason(None, {"chip": "times", "text": "Maria closes Friday"}))
    assert not se._redo_reads_as_rules(1, se.with_redo_reason(None, {"chip": "other",
                                                                    "text": "make Friday feel less chaotic"}))


def test_whether_the_redo_changed_the_days():
    base = [_row(WEEK[4], "Ana"), _row(WEEK[3], "Bo")]
    assert not se._redo_changed(base, [dict(base[0]), _row(WEEK[3], "Cy")], [WEEK[4]])     # other days ignored
    assert se._redo_changed(base, [_row(WEEK[4], "Ana", "5:00pm"), base[1]], [WEEK[4]])


def test_the_redos_days_are_drafted_in_code_with_no_model_call(monkeypatch):
    names = ["Ann", "Bob", "Cat"]
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=names,
                       active={n.lower() for n in names})
    sent = []

    def gen(analysis, shifts, prepare_only=False, **kw):
        if not prepare_only:
            sent.append(kw.get("week_slice"))
            raise AssertionError("the model was called for a redo code could draft")
        return {"week_dates": WEEK, "week_days": DAYS, "rows": [], "schedule_csv": HEAD + "\n", "narrative": [],
                "requirements_by_date": {f"{WEEK[4]}|night": {"Server": 2}}}
    monkeypatch.setattr(labor, "generate_optimized_schedule", gen)
    monkeypatch.setattr(se, "_quality_signals", lambda rid, res: ({"roster": names,
                                                                   "roster_roles": {n: "Server" for n in names}}, None))
    fb = {"source": "solver", "code_redo": True, "restaurant_id": None, "constraints": c, "roster_names": names,
          "roster_roles": {n: "Server" for n in names}, "week_dates": WEEK, "plan_rows": [], "plan_dates": [],
          "open_times": {}, "close_times": {"Friday": "10:00pm"}}
    kept = [_row(d, "Ann", "11:00am", "3:00pm") for d in WEEK if d != WEEK[4]]
    out = se._generate_in_parts({}, [], [(n, "Server") for n in names],
                                {"week_start": WEEK[0], "restaurant_id": None, "redo_dates": [WEEK[4]],
                                 "prior_rows": kept}, fallback=fb)
    assert not sent
    fri = [r for r in out["rows"] if r["date"] == WEEK[4]]
    assert len(fri) == 2 and len({r["employee"] for r in fri}) == 2
    assert out["generation_notes"][0].startswith("Redone without the AI model:")


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    return db_path


def _result(rows):
    return {"ok": True, "schedule_csv": HEAD + "\n" + "\n".join(",".join(r[c] for c in se._ANSWER_COLS)
                                                               for r in rows),
            "rows": rows, "week_dates": WEEK, "week_days": DAYS, "summary": [], "hours_budget": 0,
            "daily_target_hours": {}, "labor_target": 30, "unwritten_dates": [], "closed_dates": [],
            "generation_notes": []}


def test_a_code_redo_that_changes_nothing_goes_to_the_model(db, monkeypatch):
    rid = create_restaurant(Restaurant(name="Redo Grill", owner_email="r@x.com"), db_path=db)
    base_rows = [_row(d, "Ana") for d in WEEK]
    base = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 42.0, 0, 30,
                                        HEAD + "\n" + "\n".join(",".join(r[c] for c in se._ANSWER_COLS)
                                                                for r in base_rows), [], db_path=db)
    seen = []

    def build(restaurant_id, week_start=None, focus=None, dates=None, prior_rows=None, instruction=None,
              code_redo=False):
        seen.append(code_redo)
        if code_redo:
            return _result([_row(WEEK[4], "Ana")])                   # the same Friday: no change
        return _result([_row(WEEK[4], "Bo", "5:00pm", "11:00pm")])
    monkeypatch.setattr(se, "_build_schedule_result", build)
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    se._run_schedule_job("redo-code", rid, dates=[WEEK[4]], base_history_id=base,
                         instruction=se.with_redo_reason(None, {"chip": "wrong_people", "text": None}))
    assert seen == [True, False] and finished["status"] == "done"
    assert finished["result"]["redo"]["how"] == "model"
    assert [r["employee"] for r in finished["result"]["preview_rows"] if r["date"] == WEEK[4]] == ["Bo"]


def test_a_code_redo_that_changes_the_day_is_kept_with_no_model_call(db, monkeypatch):
    rid = create_restaurant(Restaurant(name="Redo Grill 2", owner_email="r2@x.com"), db_path=db)
    base_rows = [_row(d, "Ana") for d in WEEK]
    base = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 42.0, 0, 30,
                                        HEAD + "\n" + "\n".join(",".join(r[c] for c in se._ANSWER_COLS)
                                                                for r in base_rows), [], db_path=db)
    seen = []

    def build(restaurant_id, week_start=None, focus=None, dates=None, prior_rows=None, instruction=None,
              code_redo=False):
        seen.append(code_redo)
        return _result([_row(WEEK[4], "Cy", "4:30pm", "10:00pm")])
    monkeypatch.setattr(se, "_build_schedule_result", build)
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    se._run_schedule_job("redo-code-2", rid, dates=[WEEK[4]], base_history_id=base,
                         instruction=se.with_redo_reason(None, {"chip": "times", "text": None}))
    assert seen == [True] and finished["result"]["redo"]["how"] == "code"
    assert [r["employee"] for r in finished["result"]["preview_rows"] if r["date"] == WEEK[4]] == ["Cy"]
