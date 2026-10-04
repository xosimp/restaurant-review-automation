"""Schedule audit 10/3/26, workstream B2: what each model call is sent, how
a redo and the quality gate regenerate only their days, the one wall clock
a generation runs on, the bounded generation pool, the download, and the
owner's words when something stops.

P-9 / E-21 / PR-18  a redo writes only its dates, with the kept days handed
                    over, and the owner's reason reaches the prompt fenced
P-22                one deadline per generation; each call waits at most the
                    time left; a stream still writing then is cut
P-34 / E-28         a cut or failed call keeps its finished days; the rest is
                    written again smaller; a failure never loses paid days
P-39                a bounded pool, and a claim-based auto-draft
P-42                the download serves the draft in force
P-44                every stop has its own sentence
E-20                days nobody can work are never asked for, and named
E-30                a new restaurant with a hand-built team can draft
E-29 / PR-17        a department call gets its own rules and its own rows
P-45 / P-46         docs say what the code does; the dead code is gone

The model is never called: labor.generate_optimized_schedule or
create_with_retry is stubbed, as tests/test_edge_sched_generator.py does.
"""
import datetime as dt
import json
import re
import sys
import threading
import time
import types

import pytest

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_versions, shift_requests, staff_schedule  # noqa: E401,F401
import staff_settings, strategy_jobs, time_off, labor_replacements  # noqa: E401,F401
# Every module the generation imports lazily, imported here before any
# fixture patches models.get_conn: one first imported mid-test would bind the
# test's redirect and keep it after the test (a later file then reads a
# deleted database).
import analyser, ask_cavnar, benchmark_registry, business_intelligence, clover, cogs  # noqa: E401,F401
import command_center, credentials, data_freshness, data_health, demand, food_cost_intelligence  # noqa: E401,F401
import forecast_log, home_brief, inventory, inventory_ledger, kitchen_stations, marketing  # noqa: E401,F401
import marketing_signals, morning_brief, ordering, owner_memory, permissions, pos, pos_health  # noqa: E401,F401
import rec_trust, reservation_feeds, review_intelligence, rpower, schedule_learning  # noqa: E401,F401
import schedule_optimizer, schedule_solver, square, staffing_curve, staffing_signals, toast  # noqa: E401,F401
import waste_trend, weather  # noqa: E401,F401
from flask import Flask

import ai_utils
import auth
import client_api
import labor
import mobile_api
import models
import ops
import schedule_engine as se
import schedule_prompt
import schedule_requirements as sreq
import schedule_rules as sr
from models import Restaurant, create_restaurant

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    return db_path


def _restaurant(db_path, **cols):
    rid = create_restaurant(Restaurant(name="Edge Grill", owner_email="edge@x.com"), db_path=db_path)
    if cols:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?",
                     (*cols.values(), rid))
        conn.commit()
        conn.close()
    return rid


def _line(date, emp, start="4:00pm", end="10:00pm", hours=6, role="Server"):
    return f"{date},{DAYS[WEEK.index(date)]},{emp},{role},{start},{end},{hours},"


def _csv(lines):
    return HEADER + "\n" + "\n".join(lines)


def _pin_week(monkeypatch):
    monkeypatch.setattr(se, "_week_monday", lambda today, ws=None: dt.datetime(2026, 10, 5))


def _gen(answers, calls):
    """A stubbed generate_optimized_schedule: each call pops its answer — a
    list of CSV lines, or a dict merged into the result, or an exception."""
    def fake(analysis, shifts, week_slice=None, prior_rows=None, **kwargs):
        calls.append({"slice": list(week_slice) if week_slice else None, "prior": list(prior_rows or []),
                      "kwargs": kwargs})
        ans = answers.pop(0)
        if isinstance(ans, Exception):
            raise ans
        extra = {}
        if isinstance(ans, dict):
            extra, ans = ans, ans.get("lines", [])
        out = {"schedule_csv": _csv(ans), "narrative": ["ok"], "generation_seconds": 1.0,
               "truncated": False, "stop_reason": "end_turn", "week_dates": WEEK, "hours_budget": 0}
        out.update({k: v for k, v in extra.items() if k != "lines"})
        return out
    return fake


# ── P-9 / E-21 / PR-18: a redo writes only its dates, with the kept days in view ──

def _build_harness(monkeypatch, db, rid=None, shifts=None, analysis=None, **restaurant_cols):
    """_build_schedule_result over a real (test) restaurant, with
    _generate_in_parts' model call stubbed: returns (rid, calls)."""
    import time_utils
    import weather
    rid = rid or _restaurant(db, module_labor=1, **restaurant_cols)
    monkeypatch.setattr(labor, "load_shifts_for_restaurant",
                        lambda r: shifts if shifts is not None else
                        [{"date": "2026-09-28", "employee": "Ana", "role": "Server"},
                         {"date": "2026-09-28", "employee": "Bo", "role": "Server"}])
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant",
                        lambda r, **k: analysis if analysis is not None else {"is_live": True, "blended_rate": 20.0})
    monkeypatch.setattr(labor, "build_demand_forecast", lambda r: {"ok": False})
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: dt.datetime(2026, 10, 1, 9, 0))
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: [])
    _pin_week(monkeypatch)
    return rid


def test_a_redo_of_two_days_is_one_call_for_those_days_with_the_kept_days_in_view(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _gen([[_line(WEEK[5], "Ana"), _line(WEEK[6], "Bo")]], calls))
    kept = [{"date": d, "day": DAYS[i], "employee": "Ana", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6"} for i, d in enumerate(WEEK[:5])]
    reason = se.with_redo_reason(None, {"chip": "too_few", "text": "two more on Saturday night"})
    out = se._build_schedule_result(rid, week_start=WEEK[0], dates=[WEEK[5], WEEK[6]], prior_rows=kept,
                                    instruction=reason)
    assert len(calls) == 1, "one short call for the redo, not the whole week again"
    assert calls[0]["slice"] == [WEEK[5], WEEK[6]]
    assert [r["date"] for r in calls[0]["prior"]] == WEEK[:5]          # the kept days, as the rest of the week
    # The owner's reason rides the one instruction field (C2's path, PR-18).
    assert calls[0]["kwargs"]["instruction"] == ("What was wrong with the previous draft: Too few people on; "
                                                 "two more on Saturday night")
    # Said to this call alone, in THIS REQUEST (C1, PR-26: never inside the
    # week's shared, cached context), its dates weekday and ISO (PR-20).
    assert "THE REST OF THIS WEEK IS KEPT" in calls[0]["kwargs"]["call_notes"]
    assert "Fri 2026-10-09 until 10:00pm" in calls[0]["kwargs"]["call_notes"]   # the kept shift next to Saturday
    assert "THE REST OF THIS WEEK IS KEPT" not in (calls[0]["kwargs"].get("extra_blocks") or "")
    # Only the redone dates come back; the kept days are never written.
    assert se._missing_dates(out["schedule_csv"], WEEK[:5]) == WEEK[:5]
    # ...and are never closed to the passes after (they carry the owner's rows).
    assert not set(WEEK[:5]) & set(out["closed_dates"])


def test_the_focus_block_names_its_dates():
    block = sreq.focus_block(["Saturday 2026-10-10 dinner: 2 servers short"], dates=["2026-10-10"])
    assert "THE PREVIOUS DRAFT OF Sat 2026-10-10 SCORED WEAK ON" in block    # the one date format (C1, PR-20)
    assert "Saturday 2026-10-10 dinner: 2 servers short" in block
    assert sreq.focus_block(None) == "" and sreq.focus_block([]) == ""


@pytest.mark.parametrize("body,expect", [
    ({"reason_chip": "too_thin", "whats_wrong": "  Sat  is slammed "}, {"chip": "too_thin", "text": "Sat is slammed"}),
    ({"reason": "WRONG_PEOPLE", "reason_text": None}, {"chip": "wrong_people", "text": None}),
    ({"reason_text": "x" * 400}, {"chip": None, "text": "x" * se.REDO_REASON_MAX_CHARS}),
    ({"reason": 7, "whats_wrong": ["no"]}, None),
    ({}, None),
])
def test_the_redo_reason_is_read_from_every_key_the_clients_send(body, expect):
    assert se.redo_reason_from(body) == expect


def test_the_redo_reason_joins_the_instruction_once_in_the_owners_words():
    assert se.with_redo_reason("Keep Ana off doubles", {"chip": "too_thin", "text": "Sat is slammed"}) == (
        "Keep Ana off doubles What was wrong with the previous draft: Too few people on; Sat is slammed")
    # A chip off the list is kept as the owner's own label, never refused.
    assert se.with_redo_reason(None, {"chip": "make_it_cheaper", "text": None}) == (
        "What was wrong with the previous draft: Make it cheaper")
    assert se.with_redo_reason("as asked", None) == "as asked" and se.with_redo_reason(None, None) is None
    assert len(se.with_redo_reason("a" * 450, {"chip": "times", "text": "b" * 300})) == 500


def _prompt_harness(monkeypatch):
    prompts = []

    class _Msg:
        def __init__(self, text):
            self.text, self.stop_reason = text, "end_turn"

    def fake_create(client, **kw):
        # The user message is the request's three blocks (schedule_prompt, C1).
        prompts.append({"prompt": schedule_prompt.prompt_text(kw["messages"][0]["content"]),
                        "deadline": kw.get("deadline")})
        dates = schedule_prompt.request_dates(kw["messages"][0]["content"])
        return _Msg(json.dumps({"days": [{"date": d, "shifts": [{"employee": "Ana", "role": "Server",
                                                             "start": "11:00am", "end": "3:00pm", "note": ""}]}
                                     for d in dates],
                            "summary": ["a"]}))
    monkeypatch.setattr(labor, "create_with_retry", fake_create)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(labor, "extract_text", lambda m: m.text)
    monkeypatch.setattr(labor, "model_for", lambda k: "m")
    return prompts


_ANALYSIS = {"overall_labor_pct": 25, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
             "period_days": 0, "total_sales": 0}


def test_the_redo_prompt_names_the_dates_and_says_the_owners_words_once(db, monkeypatch):
    import ai_guard
    prompts = _prompt_harness(monkeypatch)
    reason = se.with_redo_reason("Keep Ana off doubles",
                                 {"chip": "too_few", "text": "Saturday is slammed " + ai_guard.OWNER_RULE_OPEN})
    labor.generate_optimized_schedule(_ANALYSIS, [], roster=[("Ana", "Server")], week_start=WEEK[0],
                                      week_slice=[WEEK[5]], prior_rows=[{"date": WEEK[4], "employee": "Ana",
                                                                         "shift_end": "11:00pm", "day": "Friday",
                                                                         "scheduled_hours": "6"}],
                                      focus=["Saturday 2026-10-10 dinner: short"], instruction=reason,
                                      deadline=time.time() + 600)
    p = prompts[-1]["prompt"]
    assert "Sat 2026-10-10 SCORED WEAK ON" in p and "ALREADY WRITTEN FOR THE OTHER DAYS" in p
    # One owner-reason path: the instruction block, once, priority 5, its
    # markers neutralised so it can never open a fence.
    # (The standing instructions name the channel to rank it — C1, PR-2; the
    # owner's words themselves are said once, in its own block.)
    assert p.count("THE OWNER'S REQUEST FOR THIS DRAFT —") == 1 and p.count("Saturday is slammed") == 1
    said = p[p.index("THE OWNER'S REQUEST FOR THIS DRAFT —"):]
    said = said[:said.index("Saturday is slammed") + 60]
    assert "priority 5" in said and "Too few people on" in said and ai_guard.OWNER_RULE_OPEN not in said
    assert "WHY THE OWNER IS REDOING" not in p
    assert prompts[-1]["deadline"] is not None                         # the job's clock reaches the call


def test_the_gate_and_an_owner_redo_regenerate_only_their_dates_through_the_job(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    base = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 30.0, 0, 30,
                                        _csv([_line(d, "Ana") for d in WEEK]), [], db_path=db)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _gen([[_line(WEEK[5], "Bo", "5:00pm", "11:00pm")]], calls))
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    reason = se.with_redo_reason(None, {"chip": "wrong_people", "text": None})
    se._run_schedule_job("redo-job", rid, dates=[WEEK[5]], base_history_id=base, instruction=reason)
    assert finished["status"] == "done", finished
    res = finished["result"]
    assert len(calls) == 1 and calls[0]["slice"] == [WEEK[5]]
    assert calls[0]["kwargs"]["instruction"] == "What was wrong with the previous draft: The wrong people on"
    assert res["regenerated_dates"] == [WEEK[5]]
    assert res["redo"] == {"dates": [WEEK[5]], "base_history_id": base, "by": "owner"}
    saturday = [r for r in res["preview_rows"] if r["date"] == WEEK[5]]
    assert [r["employee"] for r in saturday] == ["Bo"]                 # rewritten
    assert {r["date"] for r in res["preview_rows"] if r["employee"] == "Ana"} == set(WEEK) - {WEEK[5]}   # kept


def test_a_redo_day_the_model_could_not_write_keeps_the_owners_rows_and_says_so(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    base = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 30.0, 0, 30,
                                        _csv([_line(d, "Ana") for d in WEEK]), [], db_path=db)
    calls = []
    # Saturday and Sunday asked; Sunday comes back empty twice.
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _gen([[_line(WEEK[5], "Bo")], []], calls))
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    se._run_schedule_job("redo-2", rid, dates=[WEEK[5], WEEK[6]], base_history_id=base)
    res = finished["result"]
    assert res["regenerated_dates"] == [WEEK[5]] and not res["partial"]
    assert [r["employee"] for r in res["preview_rows"] if r["date"] == WEEK[6]] == ["Ana"]
    assert any("Sunday 10/11/26 couldn't be redone" in ln for ln in res["review"]["lines"])


def test_the_redo_route_hands_the_owners_reason_to_the_job_on_the_instruction(db, monkeypatch):
    import schedule_versions as sv
    rid = _restaurant(db, module_labor=1)
    auth.init_auth(db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    uid = auth.create_user(rid, "owner", "owner@x.com", "pw", db_path=db)
    headers = {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}
    got, kept = [], []
    monkeypatch.setattr(se, "submit_generation", lambda job_id, r, **k: got.append(k))
    monkeypatch.setattr(sv, "record_rejection", lambda r, hid, kind, **k: kept.append((hid, kind, k)))
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: False)
    c = app.test_client()
    # H1's keys (reason_chip / whats_wrong), with an instruction of the owner's own.
    ok = c.post("/mobile/api/labor/generate-schedule", headers=headers,
                json={"dates": [WEEK[5]], "history_id": 1, "reason_chip": "too_thin",
                      "whats_wrong": "  Sat  is slammed ", "instruction": "Keep Ana off doubles"})
    body = ok.get_json()
    assert ok.status_code == 200 and body["wait_seconds"] >= se.SCHEDULE_JOB_MIN_SECONDS
    assert got[0]["instruction"] == ("Keep Ana off doubles What was wrong with the previous draft: "
                                     "Too few people on; Sat is slammed")
    assert "redo_reason" not in got[0] and got[0]["dates"] == [WEEK[5]] and got[0]["base_history_id"] == 1
    # ...and H1 still records the owner's rejection of those days from the request.
    assert kept[0][:2] == (1, "redo_days") and kept[0][2]["reason_chip"] == "too_thin"
    assert kept[0][2]["reason_text"] == "  Sat  is slammed "
    # A chip off the list and long words are kept as the owner's (never a 400).
    ops.finish_async_job(body["job_id"], "done", {})
    long = c.post("/mobile/api/labor/generate-schedule", headers=headers,
                  json={"dates": [WEEK[6]], "history_id": 1, "reason": "make_it_cheaper", "reason_text": "x" * 400})
    assert long.status_code == 200 and len(got) == 2, long.get_json()
    assert got[-1]["instruction"] == ("What was wrong with the previous draft: Make it cheaper; "
                                      + "x" * se.REDO_REASON_MAX_CHARS)
    # No reason and no instruction: the job is handed none.
    ops.finish_async_job(long.get_json()["job_id"], "done", {})
    plain = c.post("/mobile/api/labor/generate-schedule", headers=headers, json={"dates": [WEEK[4]], "history_id": 1})
    assert plain.status_code == 200 and len(got) == 3 and "instruction" not in got[-1]


# ── P-22: one wall clock; each call waits at most the time left ─────────────

class _FakeTimeout(Exception):
    pass


def test_create_with_retry_gives_each_attempt_the_time_left_and_never_retries_a_timeout(db, monkeypatch):
    import anthropic
    import httpx
    seen = []

    class Messages:
        def create(self, **kw):
            seen.append(kw.get("timeout"))
            raise anthropic.APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))

    client = types.SimpleNamespace(messages=Messages(), timeout=anthropic.Timeout(360.0, connect=5.0))
    with pytest.raises(anthropic.APITimeoutError):
        ai_utils.create_with_retry(client, model="m", messages=[], max_tokens=5, restaurant_id=None,
                                   action="labor_schedule", deadline=time.time() + 30)
    assert len(seen) == 1, "a timed-out call under a deadline is re-planned by its caller, not resent"
    assert seen[0].read <= 30.5


def test_a_stream_still_writing_at_the_deadline_is_cut_and_its_tokens_filed(db, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ai_utils.time, "time", lambda: clock[0])
    usage = types.SimpleNamespace(input_tokens=900, output_tokens=300, cache_creation_input_tokens=0,
                                  cache_read_input_tokens=0)
    snapshot = types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text='{"shifts": [')],
                                     usage=usage, stop_reason=None, id="msg_1")
    filed = []
    monkeypatch.setattr(ai_utils, "log_ai_usage", lambda *a, **k: filed.append((a, k)))

    class Stream:
        current_message_snapshot = snapshot

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            for _ in range(10):
                clock[0] += 30.0                  # thirty seconds an event
                yield object()

        def close(self):
            pass

        def get_final_message(self):
            raise AssertionError("never finished: the deadline cut it")

    client = types.SimpleNamespace(messages=types.SimpleNamespace(stream=lambda **kw: Stream(), create=None),
                                   timeout=360.0)
    with pytest.raises(ai_utils.CallDeadlineExceeded) as e:
        ai_utils.create_with_retry(client, model="m", messages=[], max_tokens=5, restaurant_id=3,
                                   action="labor_schedule", stream=True, deadline=1100.0)
    assert e.value.partial is snapshot
    assert clock[0] <= 1100.0 + 30.0
    a, k = filed[-1]
    assert k["outcome"] == "truncated" and k["stop_reason"] == "deadline" and a[3] == 900 and a[4] == 300


def test_a_call_cut_at_the_deadline_comes_back_as_a_truncated_answer(db, monkeypatch):
    partial = types.SimpleNamespace(text=HEADER + "\n" + _line(WEEK[0], "Ana") + "\n" + _line(WEEK[1], "Ana"),
                                    stop_reason=None)

    def fake_create(client, **kw):
        raise ai_utils.CallDeadlineExceeded(partial=partial)
    monkeypatch.setattr(labor, "create_with_retry", fake_create)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(labor, "extract_text", lambda m: m.text)
    monkeypatch.setattr(labor, "model_for", lambda k: "m")
    out = labor.generate_optimized_schedule(_ANALYSIS, [], roster=[("Ana", "Server")], week_start=WEEK[0],
                                            structured=False, deadline=time.time() + 1)
    assert out["truncated"] is True and out["stop_reason"] == "deadline"
    assert se._rows_by_date(out["schedule_csv"]) == {WEEK[0]: 1, WEEK[1]: 1}


def test_every_call_carries_the_jobs_model_deadline(db, monkeypatch):
    _pin_week(monkeypatch)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([[_line(d, "Ana") for d in WEEK]], calls))
    with se.generation_scope("clock-job") as clock:
        se._generate_in_parts({}, [], [("Ana", "Server"), ("Bo", "Server")], {"tz_name": None})
    assert calls[0]["kwargs"]["deadline"] == clock.model_deadline
    assert clock.model_deadline == clock.deadline - se.SCHEDULE_POST_MODEL_SECONDS
    assert se.SCHEDULE_JOB_MAX_SECONDS + ops.ASYNC_DEADLINE_GRACE_SECONDS < ops.JOB_MAX_MINUTES * 60


def test_a_timed_out_week_is_written_again_in_smaller_parts(monkeypatch):
    import anthropic
    import httpx
    _pin_week(monkeypatch)
    calls = []
    timeout = anthropic.APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([
        timeout,
        [_line(d, "Ana") for d in WEEK[:4]],
        [_line(d, "Ana") for d in WEEK[4:]],
    ], calls))
    out = se._generate_in_parts({}, [], [("Ana", "Server"), ("Bo", "Server")], {"tz_name": None})
    assert [c["slice"] for c in calls] == [None, WEEK[:4], WEEK[4:]]
    assert se._missing_dates(out["schedule_csv"], WEEK) == [] and not out["unwritten_dates"]
    assert out["slices"][0]["error"] == "timeout"


def test_the_job_store_reads_a_job_dead_past_its_own_deadline_and_a_late_finish_cannot_overwrite_it(db):
    rid = _restaurant(db)
    ops.claim_async_job("dl-1", "schedule", rid)
    ops.set_async_job_deadline("dl-1", time.time() + 600)
    job = ops.read_async_job("dl-1", restaurant_id=rid)
    assert job["status"] == "pending" and 590 <= job["seconds_left"] <= 600
    ops.set_async_job_deadline("dl-1", time.time() - ops.ASYNC_DEADLINE_GRACE_SECONDS - 5)
    assert ops.active_job("schedule", rid) is None                     # a new press never joins a dead job
    dead = ops.read_async_job("dl-1", restaurant_id=rid)
    assert dead["status"] == "error" and "nothing was saved" in dead["result"]["error"]
    assert ops.job_still_pending("dl-1") is False
    ops.finish_async_job("dl-1", "done", {"ok": True})                 # the thread that ran on
    assert ops.read_async_job("dl-1", restaurant_id=rid)["status"] == "error"


def test_a_job_its_poll_called_dead_saves_nothing(db, monkeypatch):
    rid = _restaurant(db)
    ops.claim_async_job("late-job", "schedule", rid)
    base = {"ok": True, "schedule_csv": _csv([_line(WEEK[0], "Ana")]), "week_dates": WEEK, "week_days": DAYS,
            "summary": [], "hours_budget": 0, "daily_target_hours": {}, "labor_target": 30}
    monkeypatch.setattr(se, "_build_schedule_result", lambda r, week_start=None: dict(base))
    monkeypatch.setattr(ops, "job_still_pending", lambda job_id: False)
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    se._run_schedule_job("late-job", rid)
    conn = models.get_conn(db)
    saved = conn.execute("SELECT COUNT(*) FROM schedule_history WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.close()
    assert saved == 0 and finished["status"] == "error" and "nothing was saved" in finished["result"]["error"]


def test_a_generation_plans_its_deadline_and_the_store_learns_it(db):
    rid = _restaurant(db)
    ops.claim_async_job("plan-job", "schedule", rid)
    clock = se.GenerationClock("plan-job")
    assert round(clock.deadline - clock.started) == se.SCHEDULE_JOB_MIN_SECONDS
    clock.plan(6)                                                       # a big roster: six calls
    assert round(clock.deadline - clock.started) == min(se.SCHEDULE_JOB_MAX_SECONDS,
                                                        6 * se.SCHEDULE_CALL_SECONDS + se.SCHEDULE_POST_MODEL_SECONDS)
    before = clock.deadline
    clock.plan(1)                                                       # the gate's re-plan never shortens it
    assert clock.deadline == before
    assert ops.read_async_job("plan-job", restaurant_id=rid)["seconds_left"] > se.SCHEDULE_JOB_MIN_SECONDS


# ── P-34 / E-28: keep what a call finished, write the rest again smaller ────

def test_a_cut_single_call_keeps_its_finished_days_and_writes_only_the_rest(monkeypatch):
    _pin_week(monkeypatch)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([
        {"lines": [_line(WEEK[0], "Ana"), _line(WEEK[1], "Ana"), _line(WEEK[2], "Ana")],
         "truncated": True, "stop_reason": "max_tokens"},
        [_line(d, "Bo") for d in WEEK[2:5]],
        [_line(d, "Bo") for d in WEEK[5:]],
    ], calls))
    out = se._generate_in_parts({}, [], [("Ana", "Server"), ("Bo", "Server")], {"tz_name": None})
    # Monday and Tuesday kept; Wednesday (being written when it was cut) and
    # the days it never reached are written again, in halves, with what was
    # kept in view.
    assert [c["slice"] for c in calls] == [None, WEEK[2:5], WEEK[5:]]
    assert [r["date"] for r in calls[1]["prior"]] == WEEK[:2]
    rows = se._rows_by_date(out["schedule_csv"])
    assert rows == {d: 1 for d in WEEK}
    assert "Ana" not in "".join(ln for ln in out["schedule_csv"].split("\n") if ln.startswith(WEEK[2]))


def test_a_cut_structured_answer_keeps_every_day_its_parse_read_whole(monkeypatch):
    """The salvage reads schedule_output.parse_answer's own account of the
    answer (labor's complete_dates / partial_dates), never a second parse: a
    structured answer cut inside Thursday kept Wednesday whole, so Wednesday
    stands — the CSV fallback above can only guess the last date written was
    unfinished."""
    _pin_week(monkeypatch)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([
        {"lines": [_line(d, "Ana") for d in WEEK[:3]], "truncated": True, "stop_reason": "max_tokens",
         "complete_dates": WEEK[:3], "partial_dates": [WEEK[3]]},
        {"lines": [_line(d, "Bo") for d in WEEK[3:5]], "complete_dates": WEEK[3:5], "partial_dates": []},
        {"lines": [_line(d, "Bo") for d in WEEK[5:]], "complete_dates": WEEK[5:], "partial_dates": []},
    ], calls))
    out = se._generate_in_parts({}, [], [("Ana", "Server"), ("Bo", "Server")], {"tz_name": None})
    assert [c["slice"] for c in calls] == [None, WEEK[3:5], WEEK[5:]]
    assert [r["date"] for r in calls[1]["prior"]] == WEEK[:3]
    assert se._rows_by_date(out["schedule_csv"]) == {d: 1 for d in WEEK}
    assert [ln.split(",")[2] for ln in out["schedule_csv"].split("\n") if ln.startswith(WEEK[2])] == ["Ana"]
    assert out["slices"][0]["cut"] and out["slices"][0]["partial_dates"] == [WEEK[3]]


def test_a_structured_answer_is_never_kept_past_what_its_parse_finished(monkeypatch):
    # A row for a date the parse did not read whole is not the day written
    # (and a day it read whole but empty is still missing): both asked again.
    _pin_week(monkeypatch)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([
        {"lines": [_line(d, "Ana") for d in WEEK[:6]], "complete_dates": WEEK[:5] + [WEEK[6]],
         "partial_dates": []},
        {"lines": [_line(d, "Bo") for d in WEEK[5:]], "complete_dates": WEEK[5:], "partial_dates": []},
    ], calls))
    out = se._generate_in_parts({}, [], [("Ana", "Server"), ("Bo", "Server")], {"tz_name": None})
    assert calls[1]["slice"] == WEEK[5:]
    assert "WROTE NO SHIFTS FOR Sat 2026-10-10, Sun 2026-10-11" in calls[1]["kwargs"]["call_notes"]
    assert [ln.split(",")[2] for ln in out["schedule_csv"].split("\n") if ln.startswith(WEEK[5])] == ["Bo"]


def test_a_refused_part_is_named_as_declined_and_the_days_before_it_kept(monkeypatch):
    _pin_week(monkeypatch)
    monkeypatch.setattr(se, "_expected_rows", lambda shifts, roster: 2 * se.CHUNK_ROWS_PER_CALL - 10)   # two slices
    refusal = se.ScheduleGenerationError("Cavnar AI couldn't write this schedule: the AI model declined the request.")
    refusal.stop_reason = "refusal"
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([[_line(d, "Ana") for d in WEEK[:4]], refusal], []))
    out = se._generate_in_parts({}, [], [("Ana", "Server")], {"tz_name": None})
    assert {u["why"] for u in out["unwritten_dates"]} == {"refused"}
    assert "the model declined to write them" in out["generation_notes"][0]


def test_a_cut_slice_splits_again_down_to_departments_and_people_then_stops(monkeypatch):
    _pin_week(monkeypatch)
    calls = []

    def always_cut(analysis, shifts, week_slice=None, prior_rows=None, **kwargs):
        calls.append({"slice": week_slice, "roster": kwargs.get("roster")})
        return {"schedule_csv": HEADER, "narrative": [], "generation_seconds": 0.1, "truncated": True,
                "stop_reason": "max_tokens", "week_dates": WEEK}
    monkeypatch.setattr(labor, "generate_optimized_schedule", always_cut)
    roster = [("Ana", "Server"), ("Bo", "Server"), ("Cy", "Line Cook"), ("Di", "Line Cook")]
    with pytest.raises(se.ScheduleGenerationError) as e:
        se._generate_in_parts({}, [], roster, {"tz_name": None})
    assert "nothing was saved" in str(e.value) and "ran out of room" in str(e.value)
    assert len(calls) <= 1 + se.MAX_EXTRA_CALLS                       # bounded, never a runaway
    assert any(c["roster"] for c in calls), "a single cut date is split by department"


def test_a_failure_partway_keeps_the_days_already_written(monkeypatch):
    _pin_week(monkeypatch)
    calls = []
    monkeypatch.setattr(se, "_expected_rows", lambda shifts, roster: 2 * se.CHUNK_ROWS_PER_CALL - 10)   # two slices
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([
        [_line(d, "Ana") for d in WEEK[:4]],
        ai_utils.AIProviderDown("down"),
    ], calls))
    out = se._generate_in_parts({}, [], [("Ana", "Server"), ("Bo", "Server")], {"tz_name": None})
    assert se._missing_dates(out["schedule_csv"], WEEK[:4]) == []
    assert [u["date"] for u in out["unwritten_dates"]] == WEEK[4:]
    assert {u["why"] for u in out["unwritten_dates"]} == {"provider_down"}
    assert out["generation_notes"][0].startswith("⚠ Friday 10/9/26, Saturday 10/10/26 and Sunday 10/11/26 were not written")
    assert set(WEEK[4:]) <= set(out["closed_dates"])


def test_a_failure_before_anything_is_written_fails_the_generation_with_its_own_cause(monkeypatch):
    _pin_week(monkeypatch)
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([ai_utils.AIProviderDown("down")], []))
    with pytest.raises(ai_utils.AIProviderDown):
        se._generate_in_parts({}, [], [("Ana", "Server")], {"tz_name": None})


def test_a_partial_week_is_saved_flagged_and_not_counted_as_built(db, monkeypatch):
    rid = _restaurant(db)
    base = {"ok": True, "schedule_csv": _csv([_line(d, "Ana") for d in WEEK[:5]]), "week_dates": WEEK,
            "week_days": DAYS, "summary": [], "hours_budget": 0, "daily_target_hours": {}, "labor_target": 30,
            "unwritten_dates": [{"date": WEEK[5], "day": "Saturday", "why": "provider"},
                                {"date": WEEK[6], "day": "Sunday", "why": "provider"}],
            "closed_dates": WEEK[5:]}
    base["generation_notes"] = [se._unwritten_line(base["unwritten_dates"])]
    monkeypatch.setattr(se, "_build_schedule_result", lambda r, week_start=None: dict(base))
    built = []
    monkeypatch.setattr(se, "mark_next_week_built", lambda r, h: built.append(h))
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    se._run_schedule_job("partial-job", rid)
    res = finished["result"]
    assert finished["status"] == "done" and res["partial"] is True and not built
    assert res["review"]["lines"][0].startswith("⚠ Saturday 10/10/26 and Sunday 10/11/26 were not written")
    blockers = client_api.publish_review(rid, res["history_id"], unattended=True)["blockers"]
    assert any("were not written" in b["text"] for b in blockers), "the publish gate holds a partial week"


def test_the_row_sizing_is_derived_from_the_ceiling_the_thinking_and_the_schema():
    import schedule_output as so
    assert se.SCHEDULE_TOKEN_CEILING == labor.SCHEDULE_MAX_TOKENS_THINKING
    # One source for a row's cost: the output contract's own figure (C2).
    assert se.OUTPUT_TOKENS_PER_ROW == so.ANSWER_TOKENS_PER_ROW_ESTIMATE
    by_tokens = (se.SCHEDULE_TOKEN_CEILING - se.THINKING_TOKENS_RESERVED - se.SUMMARY_TOKENS) // se.OUTPUT_TOKENS_PER_ROW
    assert se.ROWS_PER_CALL_BY_TOKENS == by_tokens
    assert se.ROWS_PER_CALL_BY_TIME == se.ROW_TOKENS_PER_CALL // se.OUTPUT_TOKENS_PER_ROW
    assert se.CHUNK_ROWS_PER_CALL == min(by_tokens, se.ROWS_PER_CALL_BY_TIME)
    # The figure is what one row costs under the schema the call is sent
    # (labor.SCHEDULE_SCHEMA is schedule_output.schedule_schema()): never
    # under the row's serialized size (the calls would be cut), and never far
    # over it (the calls would be needlessly small) — so a schema change
    # re-sizes the calls. ~3.2 characters a token for dense JSON.
    assert json.dumps(labor.SCHEDULE_SCHEMA, sort_keys=True) == json.dumps(so.schedule_schema(), sort_keys=True)
    est = _row_tokens_estimate(labor.SCHEDULE_SCHEMA)
    assert est * 0.9 <= se.OUTPUT_TOKENS_PER_ROW <= est * 1.6, (est, se.OUTPUT_TOKENS_PER_ROW)
    # The old eight-key row cost twice as much: a figure sized for it would
    # fail the band above.
    old_row = {"type": "object", "properties": {"shifts": {"type": "array", "items": {
        "type": "object", "properties": {k: {"type": "string"} for k in (
            "date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes")}}}}}
    assert _row_tokens_estimate(old_row) > se.OUTPUT_TOKENS_PER_ROW * 1.6


def test_a_restaurants_measured_row_cost_shrinks_its_calls_and_never_grows_them(db, monkeypatch):
    import schedule_output as so
    rid = _restaurant(db, module_labor=1)
    seen = []

    def measured(cost, calls=12):
        def fake(restaurant_id=None, model=None, **k):
            seen.append((restaurant_id, model))
            if cost is None:
                return {"output_tokens_per_row": float(so.ANSWER_TOKENS_PER_ROW_ESTIMATE),
                        "answer_chars_per_row": None, "calls": 0, "source": "estimate"}
            return {"output_tokens_per_row": float(cost), "answer_chars_per_row": 84.0, "calls": calls,
                    "source": "measured"}
        return fake
    monkeypatch.setattr(so, "measured_tokens_per_row", measured(None))
    assert se.rows_per_call(rid) == se.CHUNK_ROWS_PER_CALL              # no calls yet: the estimate
    assert seen[-1] == (rid, ai_utils.model_for("schedule"))           # the restaurant's own, on the model in force
    monkeypatch.setattr(so, "measured_tokens_per_row", measured(400))   # thinking included, per row written
    assert se.rows_per_call(rid) == int(se.SCHEDULE_TOKEN_CEILING * se.MEASURED_HEADROOM // 400)
    monkeypatch.setattr(so, "measured_tokens_per_row", measured(60))    # cheaper than planned: no bigger call
    assert se.rows_per_call(rid) == se.CHUNK_ROWS_PER_CALL
    monkeypatch.setattr(so, "measured_tokens_per_row", measured(5000))  # never below the floor
    assert se.rows_per_call(rid) == se.MIN_ROWS_PER_CALL
    assert se.rows_per_call(None) == se.CHUNK_ROWS_PER_CALL

    def broken(**k):
        raise RuntimeError("no such table")
    monkeypatch.setattr(so, "measured_tokens_per_row", broken)
    assert se.rows_per_call(rid) == se.CHUNK_ROWS_PER_CALL              # a store failure never fails the week
    # The plan follows it: 300 rows fit one call at the estimate, three at 400 tokens a row.
    monkeypatch.setattr(so, "measured_tokens_per_row", measured(400))
    _pin_week(monkeypatch)
    monkeypatch.setattr(se, "_expected_rows", lambda shifts, roster: 300)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([[_line(d, "Ana") for d in WEEK[i:j]]
                                                                     for i, j in ((0, 3), (3, 6), (6, 7))], calls))
    out = se._generate_in_parts({}, [], [("Ana", "Server")], {"tz_name": None, "restaurant_id": rid})
    assert [c["slice"] for c in calls] == [WEEK[0:3], WEEK[3:6], WEEK[6:]] and out["chunked"] == 3


def _row_tokens_estimate(schema) -> float:
    """Characters one more shift adds to an answer under `schema`, at ~3.2
    characters a token: a sample answer with two shifts less one with one.
    Only the array of shifts grows — a schema that groups shifts under each
    date keeps one date — whether a shift is an object or a tuple."""
    samples = {"date": "2026-10-05", "day": "Wednesday", "employee": "Jamie Lopez", "name": "Jamie Lopez",
               "role": "Line Cook", "shift_start": "10:30am", "start": "10:30am", "shift_end": "10:30pm",
               "end": "10:30pm", "scheduled_hours": 7.5, "hours": 7.5, "notes": "staggered opener"}
    row_keys = ("shift_start", "start", "employee", "name", "shift_end", "end")

    def is_rows(node):
        items = node.get("items") or {}
        return (any(k in (items.get("properties") or {}) for k in row_keys)
                or bool(items.get("prefixItems")) or (items.get("type") == "array"))

    def build(node, n, key=""):
        if "enum" in node:
            return node["enum"][0]
        if "const" in node:
            return node["const"]
        t = node.get("type")
        if isinstance(t, list):
            t = next((x for x in t if x != "null"), t[0])
        if t == "object":
            return {k: build(v, n, k) for k, v in (node.get("properties") or {}).items()}
        if t == "array":
            if node.get("prefixItems"):
                return [build(x, n, key) for x in node["prefixItems"]]
            return [build(node.get("items") or {}, n, key) for _ in range(n if is_rows(node) else 1)]
        return samples.get(key, 6 if t in ("number", "integer") else "Server")
    one, two = json.dumps(build(schema, 1)), json.dumps(build(schema, 2))
    return (len(two) - len(one)) / 3.2


# ── P-39: a bounded pool, and an auto-draft that claims ─────────────────────

def test_the_generate_route_queues_on_the_pool_and_starts_no_thread_of_its_own(db, monkeypatch):
    import inspect
    rid = _restaurant(db, module_labor=1)
    auth.init_auth(db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    uid = auth.create_user(rid, "owner", "owner@x.com", "pw", db_path=db)
    headers = {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}
    ran = threading.Event()
    seen = []

    def job(job_id, r, **k):
        seen.append((threading.current_thread().name, se.current_clock() is not None))
        ran.set()
    monkeypatch.setattr(se, "_run_schedule_job", job)
    body = app.test_client().post("/mobile/api/labor/generate-schedule", headers=headers, json={}).get_json()
    assert body["ok"] and ran.wait(10), "the job ran"
    name, clocked = seen[0]
    assert name.startswith("schedule-gen") and clocked               # on the bounded pool, inside its clock
    assert se._gen_pool._max_workers == se.SCHEDULE_GEN_WORKERS
    assert "threading.Thread(" not in inspect.getsource(mobile_api.mobile_generate_schedule)


def test_no_more_generations_run_at_once_than_the_pool_has_slots(monkeypatch):
    monkeypatch.setattr(se, "_GEN_SLOTS", threading.BoundedSemaphore(2))
    monkeypatch.setattr(se._ops, "set_async_job_deadline", lambda *a, **k: None)
    inside, peak, gate = [0], [0], threading.Event()
    lock = threading.Lock()

    def job(n):
        with se.generation_scope(f"slot-{n}"):
            with lock:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
            gate.wait(0.3)
            with lock:
                inside[0] -= 1
    threads = [threading.Thread(target=job, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert peak[0] == 2


def test_the_auto_draft_joins_an_owners_running_generation_instead_of_starting_a_second(db, monkeypatch):
    rid = _restaurant(db, module_labor=1)
    r = models.get_restaurant(rid)
    owner_job, _ = ops.claim_async_job("owner-press", "schedule", rid)
    ran, bumps = [], []

    class _SE:
        @staticmethod
        def _run_schedule_job(job_id, rid_):
            ran.append(job_id)
    assert ops.claim_period(f"auto_draft:{rid}", "2026-10-01")
    strategy_jobs._draft_one(r, db, _SE, bumps.append, period="2026-10-01")
    assert ran == [] and bumps == ["skipped"]
    assert ops.claim_period(f"auto_draft:{rid}", "2026-10-01"), "today's claim handed back for a later pass"


def test_an_auto_draft_with_nothing_running_claims_its_own_job_and_takes_a_slot(db, monkeypatch):
    rid = _restaurant(db, module_labor=1)
    r = models.get_restaurant(rid)
    seen = []

    class _SE:
        @staticmethod
        def _run_schedule_job(job_id, rid_):
            seen.append((job_id, se.current_clock() is not None, ops.active_job("schedule", rid_)))
    strategy_jobs._draft_one(r, db, _SE, lambda k: None, period="2026-10-01")
    job_id, clocked, active = seen[0]
    assert clocked and active == job_id                                  # claimed: an owner's press now joins it


# ── P-42: the download serves the draft in force ────────────────────────────

def test_the_download_serves_the_restored_original_not_the_discarded_rewrite(db, monkeypatch):
    rid = _restaurant(db, module_labor=1)
    orig = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6.0, 0, 30, _csv([_line(WEEK[0], "Original")]), [],
                                        db_path=db)
    rewrite = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6.0, 0, 30, _csv([_line(WEEK[0], "Rewrite")]),
                                           [], db_path=db)
    se._restore_draft(rid, orig, rewrite)                               # the gate's rewrite was no better
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "test"
    app.register_blueprint(client_api.client_bp)
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 7, "restaurant_id": rid, "is_admin": 0, "role": "client",
                                 "username": "owner", "email": "o@x.test"})
    c = app.test_client()
    text = c.get("/api/download-schedule").get_data(as_text=True)
    assert "Original" in text and "Rewrite" not in text
    named = c.get(f"/api/download-schedule?history_id={rewrite}").get_data(as_text=True)
    assert "Rewrite" in named                                            # a week the screen names, as named


# ── P-44: every stop has its own sentence ───────────────────────────────────

@pytest.mark.parametrize("exc,expect", [
    (ai_utils.AIBudgetExceeded("AI is paused — this account has reached its daily AI budget."), "AI is paused"),
    (ai_utils.AIProviderDown("x"), "isn't responding"),
    (ai_utils.DataNotReady({"decision": "refuse", "reason": "the POS sync is 3 days behind"}), "POS sync is 3 days behind"),
    (ai_utils.AIRefused("x"), "declined"),
    (ai_utils.CallDeadlineExceeded(), "took longer than"),
    (RuntimeError("no such column: secret_col"), "problem on our side"),
])
def test_each_failure_reads_as_what_stopped_it(exc, expect):
    msg = se.generation_error_message(exc)
    assert expect in msg, msg
    assert "try again in a minute" not in msg and "secret_col" not in msg


def test_a_budget_stop_reaches_the_owner_as_the_budget_sentence(db, monkeypatch):
    def stop(*a, **k):
        raise ai_utils.AIBudgetExceeded("AI is paused — this account has reached its daily AI budget. "
                                        "Contact will@cavnar.ai if this looks wrong.")
    monkeypatch.setattr(se, "_build_schedule_result", stop)
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    se._run_schedule_job("budget-job", 1)
    assert finished["status"] == "error" and "AI is paused" in finished["result"]["error"]


# ── E-20: days nobody can work ───────────────────────────────────────────────

def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_a_day_nobody_can_work_is_found_before_any_call():
    c = _c(roster_names=["Ana", "Bo"], active={"ana", "bo"},
           unavailable_days={"ana": {"Saturday"}}, daypart_avail={"bo": {"Saturday": "off"}},
           blocked_dates={"ana": {WEEK[6]: "approved time off"}})
    out = se._nobody_can_work(c, ["Ana", "Bo"], WEEK)
    assert list(out) == [WEEK[5]]                                       # Sunday: Bo can work it


def test_a_day_nobody_can_work_is_never_asked_for_and_is_named(monkeypatch):
    _pin_week(monkeypatch)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _gen([[_line(d, "Ana") for d in WEEK if d != WEEK[5]]], calls))
    out = se._generate_in_parts({}, [], [("Ana", "Server"), ("Bo", "Server")],
                                {"tz_name": None, "unstaffable_dates": {WEEK[5]: {"unavailable": 2}}})
    assert len(calls) == 1 and WEEK[5] not in (calls[0]["slice"] or [])
    assert "NOBODY ON THE ROSTER CAN WORK Sat 2026-10-10" in calls[0]["kwargs"]["call_notes"]
    assert not out["unwritten_dates"] and WEEK[5] in out["closed_dates"]


def test_the_week_names_its_unstaffable_day_for_the_owner(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    real = sr.build_constraints

    def constraints(*a, **k):
        c = real(*a, **k)
        c.unavailable_days = {"ana": {"Saturday"}, "bo": {"Saturday"}}
        return c
    monkeypatch.setattr(se._rules, "build_constraints", constraints)
    monkeypatch.setattr(se._staff, "roster", lambda r: [{"name": "Ana", "role": "Server"}, {"name": "Bo", "role": "Server"}])
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _gen([[_line(d, "Ana") for d in WEEK if d != WEEK[5]]], calls))
    out = se._build_schedule_result(rid, week_start=WEEK[0])
    assert [u["date"] for u in out["unstaffable_dates"]] == [WEEK[5]]
    assert out["generation_notes"][0] == ("⚠ Nobody on the team can work Saturday 10/10/26 (time off or "
                                          "availability), so it was left empty — mark it closed or fix "
                                          "availability, then redo that day.")


def test_a_week_nobody_can_work_at_all_is_refused_before_any_call(monkeypatch):
    _pin_week(monkeypatch)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([], calls))
    with pytest.raises(se.ScheduleGenerationError) as e:
        se._generate_in_parts({}, [], [("Ana", "Server")],
                              {"tz_name": None, "unstaffable_dates": {d: {"unavailable": 1} for d in WEEK}})
    assert calls == [] and "Nobody on the team can work" in str(e.value)


# ── E-30: a new restaurant with a hand-built team ───────────────────────────

def test_a_new_restaurant_with_a_team_and_floors_drafts_a_starting_point(db, monkeypatch):
    rid = _build_harness(monkeypatch, db, shifts=[], analysis={"is_live": False})
    models.add_manual_team_member(rid, "Ana", "Server", db_path=db)
    models.add_manual_team_member(rid, "Bo", "Line Cook", db_path=db)
    sr.save_role_floors(rid, {"Server": {"morning": 1, "night": 1}}, db_path=db)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _gen([[_line(d, "Ana") for d in WEEK]], calls))
    out = se._build_schedule_result(rid, week_start=WEEK[0])
    assert len(calls) == 1 and out["starting_point"]["no_history"] is True
    assert any(ln.startswith("A starting point: Edge Grill has no shift history") for ln in out["generation_notes"])


def test_the_no_history_prompt_says_so_instead_of_quoting_empty_figures(db, monkeypatch):
    prompts = _prompt_harness(monkeypatch)
    labor.generate_optimized_schedule(se._no_history_analysis(), [], roster=[("Ana", "Server")], week_start=WEEK[0])
    p = prompts[-1]["prompt"]
    assert "No shift history of its own yet" in p and "None%" not in p


@pytest.mark.parametrize("setup,missing,expect", [
    ("nobody", "team", "no team and no shift history"),
    ("team", "basis", "Set Role floors"),
])
def test_a_new_restaurant_without_what_a_draft_needs_is_told_in_owner_words(db, monkeypatch, setup, missing, expect):
    rid = _build_harness(monkeypatch, db, shifts=[], analysis={"is_live": False})
    if setup == "team":
        models.add_manual_team_member(rid, "Ana", "Server", db_path=db)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([], calls))
    with pytest.raises(se.ScheduleGenerationError) as e:
        se._build_schedule_result(rid, week_start=WEEK[0])
    msg = str(e.value)
    assert expect in msg and calls == []
    assert f"id {rid}" not in msg and "client data row" not in msg and "Edge Grill" in msg


def test_an_unreadable_shifts_file_is_named_as_that(db, monkeypatch):
    rid = _build_harness(monkeypatch, db, shifts=[], analysis={"is_live": True})
    models.save_client_data(rid, "shifts", "date,day,employee\nnot,a,row\nstill,not,one", source="seed", db_path=db)
    with pytest.raises(se.ScheduleGenerationError) as e:
        se._build_schedule_result(rid, week_start=WEEK[0])
    assert "couldn't be read" in str(e.value) or "could be read" in str(e.value)


# ── E-29 / PR-17: a department call gets its own rules and rows ─────────────

def test_a_department_call_gets_only_its_own_managers_floors_and_people(monkeypatch):
    _pin_week(monkeypatch)
    monkeypatch.setattr(se, "_expected_rows", lambda shifts, roster: 4 * se.CHUNK_ROWS_PER_CALL)   # by department
    c = _c(roster_names=["Ana", "Mo", "Cy"], active={"ana", "mo", "cy"},
           managers={"mo": "Manager"}, role_floors={"Line Cook": {"morning": 1, "night": 1}, "Server": {"night": 2}},
           hours_limits={"cy": (0, 30), "ana": (0, 25)})
    roster = [("Ana", "Server"), ("Mo", "Manager"), ("Cy", "Line Cook")]
    calls = []

    def fake(analysis, shifts, week_slice=None, prior_rows=None, **kwargs):
        names = [n for n, _r in kwargs["roster"]]
        # What the part is told: its rules, the week's context and what is
        # said to it alone (C1 keeps the three apart: PR-26).
        told = (kwargs.get("rules_block") or "") + (kwargs.get("extra_blocks") or "") + (kwargs.get("call_notes") or "")
        calls.append({"names": names, "extra": told, "slice": week_slice, "rules": kwargs.get("rules_block") or "",
                      "pins": kwargs.get("pinned_rows"), "plan": kwargs.get("manager_plan")})
        # The kitchen call also writes a server it was told not to.
        lines = [_line(d, names[0], role=kwargs["roster"][0][1]) for d in week_slice]
        if names == ["Cy"]:
            lines.append(_line(week_slice[0], "Ana"))
        return {"schedule_csv": _csv(lines), "narrative": [], "generation_seconds": 1.0, "truncated": False,
                "stop_reason": "end_turn", "week_dates": WEEK}
    monkeypatch.setattr(labor, "generate_optimized_schedule", fake)
    plan_rows = _plan_rows()
    plan = {"rows": plan_rows, "dates": list(WEEK)}
    out = se._generate_in_parts({}, [], roster, {"tz_name": None, "extra_blocks": "FULL " + sr.prompt_block(c),
                                                 "rules_constraints": c, "extra_after_rules": "\n\nREST",
                                                 "pinned_rows": plan_rows, "manager_plan": plan})
    assert all(x["pins"] is plan_rows and x["plan"] is plan for x in calls)   # every part gets the plan as it is
    kitchen = [x for x in calls if x["names"] == ["Cy"]]
    front = [x for x in calls if "Mo" in x["names"]]
    assert kitchen and front
    k = kitchen[0]["extra"]
    assert "NON-NEGOTIABLE" not in k and "schedule no manager here" in k
    assert "Line Cook: at least" in k and "Server: at least" not in k
    # Each person's own limits are their ROSTER line now (C1, PR-33), built
    # from the part's own roster — never a per-person list in the rules.
    assert "PER-PERSON LIMITS" not in kitchen[0]["rules"] and "FULL" not in k
    f = front[0]["extra"]
    assert "NON-NEGOTIABLE" in f and "Mo (Manager)" in f and "Line Cook: at least" not in f
    assert "Their shifts are already planned: MANAGER COVERAGE, at the top, is fixed." in f
    assert "THE REST OF THE STAFF ON THESE DATES" in f                  # the kitchen's hours, for the manager rule
    assert out["slices"][0]["off_list_dropped"] == 1
    assert all("Ana" not in ln for ln in out["schedule_csv"].split("\n") if ",Line Cook," in ln)
    assert out["departments"] == ["KITCHEN", "FRONT OF HOUSE"]


def test_managers_go_to_the_front_of_house_part_last():
    out = se._departments([("Mo", "Kitchen Manager"), ("Ana", "Server"), ("Cy", "Line Cook")], managers={"mo"})
    assert out == {"KITCHEN": [("Cy", "Line Cook")], "FRONT OF HOUSE": [("Ana", "Server"), ("Mo", "Kitchen Manager")]}


# ── P-45 / P-46: the docs and the dead code ─────────────────────────────────

def _read(path):
    import os
    return open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path), encoding="utf-8").read()


def test_the_dead_extend_pass_and_the_unused_threshold_are_gone():
    assert not hasattr(se, "_extend_shifts_to_close_gap") and not hasattr(client_api, "_extend_shifts_to_close_gap")
    assert not hasattr(se, "CHUNK_ROSTER_THRESHOLD")
    for doc in ("MODULE_OVERVIEW.md", "PROMPT_LIBRARY.md"):
        assert "CHUNK_ROSTER_THRESHOLD" not in _read(doc), doc


def test_the_docs_no_longer_describe_retired_or_missing_things():
    assert "labor.generate_schedule`" not in _read("PROMPT_LIBRARY.md")
    assert "(`generate_schedule`)" not in _read("PROMPT_LIBRARY.md")
    overview = _read("MODULE_OVERVIEW.md")
    step4 = overview[overview.index("4. `schedule_rules.violations` sweeps"):][:900]
    assert "is repaired too" not in step4
    src = _read("schedule_engine.py") + _read("client_api.py")
    assert "never trimmed into a thinner week" not in src
    assert "A week the model wrote past it is not\n    # trimmed" not in src


# ── the quality gate rewrites only its dates, inside the job's time ─────────

def _gate_harness(monkeypatch, db, answers, calls):
    rid = _build_harness(monkeypatch, db)
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen(answers, calls))
    gates = [{"dates": [WEEK[5]], "focus": ["Saturday 2026-10-10 dinner: 2 servers short"],
              "reason": "1 busy day still had a staffing hole after repair"}]
    monkeypatch.setattr(se, "_quality_gate", lambda result: gates.pop(0) if gates else None)
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    return rid, finished


def test_the_quality_gate_rewrites_only_its_dates_with_the_rest_of_the_week_in_view(db, monkeypatch):
    calls = []
    rid, finished = _gate_harness(monkeypatch, db, [[_line(d, "Ana") for d in WEEK], [_line(WEEK[5], "Bo")]], calls)
    monkeypatch.setattr(se, "_gate_local", lambda quality, dates: 50.0 if "Bo" in json.dumps(quality) else 10.0)
    se._run_schedule_job("gate-job", rid)
    assert [c["slice"] for c in calls] == [None, [WEEK[5]]]
    assert sorted({r["date"] for r in calls[1]["prior"]}) == sorted(set(WEEK) - {WEEK[5]})
    assert "THE REST OF THIS WEEK IS KEPT" in calls[1]["kwargs"]["call_notes"]
    assert calls[1]["kwargs"]["focus"] == ["Saturday 2026-10-10 dinner: 2 servers short"]
    assert finished["status"] == "done" and finished["result"]["redo"]["by"] == "gate"


def test_the_quality_gate_is_skipped_when_the_job_has_no_time_left_for_it(db, monkeypatch):
    calls = []
    rid, finished = _gate_harness(monkeypatch, db, [[_line(d, "Ana") for d in WEEK]], calls)
    with se.generation_scope("gate-late") as clock:
        clock.deadline = time.time() + se.SCHEDULE_POST_MODEL_SECONDS + se.GATE_MIN_MODEL_SECONDS - 30
        clock.started = clock.deadline - se.SCHEDULE_JOB_MIN_SECONDS
        se._run_schedule_job("gate-late", rid)
    assert len(calls) == 1 and finished["status"] == "done"
    assert finished["result"]["gate"] == {"ran": False}


# ── pinned rows (the manager plan's) travel as fixed context, joined once ───

def _merging_gen(answers, calls):
    """A stubbed generate_optimized_schedule that merges the manager plan as
    labor does (schedule_skeleton.merge_pinned_lines over the call's own
    dates and people) — so the test sees what _generate_in_parts receives."""
    import schedule_skeleton as sk

    def fake(analysis, shifts, week_slice=None, prior_rows=None, **kwargs):
        calls.append({"slice": list(week_slice) if week_slice else None, "prior": list(prior_rows or []),
                      "kwargs": kwargs})
        dates = list(week_slice) if week_slice else WEEK
        people = {n.lower() for n, _r in kwargs["roster"]} if kwargs.get("roster") else None
        here = [p for p in (kwargs.get("pinned_rows") or []) if p["date"] in dates
                and (people is None or p["employee"].lower() in people)]
        lines, dropped = sk.merge_pinned_lines(answers.pop(0), here)
        return {"schedule_csv": _csv(lines), "narrative": ["ok"], "generation_seconds": 1.0, "truncated": False,
                "stop_reason": "end_turn", "week_dates": WEEK, "pinned_rows": here, "pinned_dropped": dropped}
    return fake


def _plan_rows(name="Mo"):
    import schedule_skeleton as sk
    return [{"date": d, "day": DAYS[i], "employee": name, "role": "Manager", "shift_start": "10:00am",
             "shift_end": "6:00pm", "scheduled_hours": "8", "notes": sk.PLAN_NOTE, "_pinned": "manager_plan"}
            for i, d in enumerate(WEEK)]


def test_the_manager_plan_reaches_every_call_as_it_is_and_lands_once(monkeypatch):
    """Decision 1 of the merge (M's design): the plan passes through to every
    call — labor shows and merges each call's own planned rows — with no
    second FIXED SHIFTS block, and a day carrying only planned rows is not a
    day the model wrote."""
    import schedule_skeleton as sk
    _pin_week(monkeypatch)
    calls = []
    plan_rows = _plan_rows()
    plan = {"rows": plan_rows, "dates": list(WEEK)}
    monkeypatch.setattr(labor, "generate_optimized_schedule", _merging_gen([
        # Ana Monday to Friday, and a Mo row over his planned Monday (the merge drops it).
        [_line(d, "Ana") for d in WEEK[:5]] + [_line(WEEK[0], "Mo", "11:00am", "7:00pm", 8, "Manager")],
        [_line(d, "Ana") for d in WEEK[5:]],
    ], calls))
    out = se._generate_in_parts({}, [], [("Ana", "Server"), ("Mo", "Manager")],
                                {"tz_name": None, "pinned_rows": plan_rows, "manager_plan": plan})
    assert len(calls) == 2
    for call in calls:
        assert call["kwargs"]["pinned_rows"] is plan_rows and call["kwargs"]["manager_plan"] is plan
        assert "FIXED SHIFTS" not in (call["kwargs"].get("extra_blocks") or "")
    # Saturday and Sunday carried only planned rows: asked again, alone.
    assert calls[1]["slice"] == WEEK[5:]
    assert "YOUR PREVIOUS ANSWER WROTE NO SHIFTS FOR Sat 2026-10-10, Sun 2026-10-11" in calls[1]["kwargs"]["call_notes"]
    lines = out["schedule_csv"].split("\n")[1:]
    planned = [ln for ln in lines if sk.is_plan_line(ln)]
    assert sorted(planned) == sorted(sk._row_line(r) for r in plan_rows)          # each planned row once
    assert not [ln for ln in lines if ",Mo," in ln and not sk.is_plan_line(ln)]   # the model's clash dropped
    assert se._rows_by_date(out["schedule_csv"]) == {d: 1 for d in WEEK}           # planned rows never count
    assert len(out["pinned_rows"]) == 7 and len(out["pinned_dropped"]) == 1
    assert out["slices"][0]["pinned_dropped"] == 1
    assert out["generated_dates"] == WEEK and out["closed_dates"] == []


def test_a_planned_row_on_a_day_left_unwritten_is_not_kept(monkeypatch):
    import schedule_skeleton as sk
    _pin_week(monkeypatch)
    plan_rows = _plan_rows()
    monkeypatch.setattr(labor, "generate_optimized_schedule", _merging_gen([
        [_line(d, "Ana") for d in WEEK[:6]], [], ], []))
    out = se._generate_in_parts({}, [], [("Ana", "Server"), ("Bo", "Server"), ("Mo", "Manager")],
                                {"tz_name": None, "pinned_rows": plan_rows, "manager_plan": {"rows": plan_rows}})
    assert [u["date"] for u in out["unwritten_dates"]] == [WEEK[6]]
    assert not [ln for ln in out["schedule_csv"].split("\n") if ln.startswith(WEEK[6])]
    assert len([ln for ln in out["schedule_csv"].split("\n") if sk.is_plan_line(ln)]) == 6
    # The job's restore (schedule_skeleton.restore_pinned) skips a closed date:
    # the generation closes the day it could not write.
    assert WEEK[6] in out["closed_dates"]
    assert [r["date"] for r in out["pinned_rows"]] == WEEK[:6]


def test_expected_rows_never_plans_a_partial_first_week_below_the_roster():
    shifts = [{"date": "2026-09-30"}] * 12 + [{"date": "2026-10-01"}] * 12        # a feed's first two days
    roster = [(f"P{i}", "Server") for i in range(20)]
    assert se._expected_rows(shifts, roster) == 70
    full = [{"date": d} for d in ("2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25")
            for _ in range(10)]
    assert se._expected_rows(full, roster) == 50                        # a real week is taken as it is


def test_an_auto_draft_with_unwritten_days_is_announced_as_partly_drafted(db, monkeypatch):
    import morning_brief
    import notify
    rid = _restaurant(db, module_labor=1)
    r = models.get_restaurant(rid)
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: [{"id": 9, "role": "client", "grants": ()}])
    monkeypatch.setattr(notify, "briefing_allowed", lambda *a, **k: True)
    fired = []
    monkeypatch.setattr(push, "fire_push", lambda rid_, kind, title, body, **k: fired.append((title, body)))

    class _SE:
        @staticmethod
        def _run_schedule_job(job_id, rid_):
            ops.finish_async_job(job_id, "done", {"ok": True, "partial": True, "unwritten_dates": [
                {"date": WEEK[6], "day": "Sunday", "why": "provider"}]})
    strategy_jobs._draft_one(r, db, _SE, lambda k: None, period="2026-10-01")
    assert fired and fired[0][0] == "Next week's schedule is partly drafted"
    assert "1 day couldn't be written" in fired[0][1]


def test_a_read_once_admin_value_is_still_removed_after_its_first_read(db, monkeypatch):
    """finish_async_job writes only a pending job now (P-22); the console's
    read-once scrub of a finished job's result has its own door."""
    import admin_routes
    auth.init_auth(db_path=db)
    hq = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="will@cavnar.test"), db_path=db)
    uid = auth.create_user(hq, "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "b2"
    app.register_blueprint(admin_routes.admin_bp)
    c = app.test_client()
    c.set_cookie("session_token", auth.create_session(uid, password_verified_at=True, db_path=db))
    ops.start_async_job("seed-9", "admin_review_account", 0)
    ops.finish_async_job("seed-9", "done", {"ok": True, "password_once": "abcde-FGHJK", "_scrub": ["password_once"]})
    first = c.get("/admin/api/admin-jobs/seed-9")
    assert first.status_code == 200 and first.get_json().get("password_once") == "abcde-FGHJK"
    again = c.get("/admin/api/admin-jobs/seed-9")
    assert "abcde" not in again.get_data(as_text=True)
    assert ops.read_async_job("seed-9")["status"] == "done"


def test_a_failure_around_the_job_on_the_pool_still_closes_it(db, monkeypatch):
    rid = _restaurant(db)
    ops.claim_async_job("pool-fail", "schedule", rid)

    @__import__("contextlib").contextmanager
    def broken(job_id):
        raise RuntimeError("slot table unreadable")
        yield
    monkeypatch.setattr(se, "generation_scope", broken)
    se.submit_generation("pool-fail", rid).result(10)
    job = ops.read_async_job("pool-fail", restaurant_id=rid)
    assert job["status"] == "error" and "problem on our side" in job["result"]["error"]


def test_the_finished_payload_says_chunked_as_the_bool_both_clients_read(db, monkeypatch):
    """The phone decodes `chunked` as Bool and failed on the call count it
    used to be (Swift's JSONDecoder refuses 1 for a Bool); the web showed
    "generated in parts" for a one-call week (!!1)."""
    rid = _restaurant(db)
    base = {"ok": True, "schedule_csv": _csv([_line(WEEK[0], "Ana")]), "week_dates": WEEK, "week_days": DAYS,
            "summary": [], "hours_budget": 0, "daily_target_hours": {}, "labor_target": 30, "chunked": 1}
    monkeypatch.setattr(se, "_build_schedule_result", lambda r, week_start=None: dict(base))
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    se._run_schedule_job("one-call", rid)
    assert finished["result"]["chunked"] is False and finished["result"]["calls"] == 1
