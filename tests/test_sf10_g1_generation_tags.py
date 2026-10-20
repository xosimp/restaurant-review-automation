"""Schedule fix round 10/10/26, workstream G1 — a generation keeps what made
it: each row's origin (AI-29), the arms, the owner's rules honoured, and —
when the learned memory binds — the learning ablation, the same repair loop
run with the memory off (RM-4, LEARNING-25). The model is never called."""
import pytest

from test_schedule_b2_calls import db, _csv, _line, _restaurant, WEEK, DAYS  # noqa: F401
import models
import ops
import schedule_job
import schedule_engine as se
import schedule_memory
import schedule_scorecard as ssc


def _run(db_path, monkeypatch, learned=None):
    rid = _restaurant(db_path, module_labor=1)
    for n in ("Ana", "Bo", "Cy"):
        models.add_manual_team_member(rid, n, role="Server", db_path=db_path)
    ops.claim_async_job("tags-job", "schedule", rid)
    base = {"ok": True, "schedule_csv": _csv([_line(WEEK[0], "Ana"), _line(WEEK[0], "Bo"), _line(WEEK[1], "Cy")]),
            "week_dates": WEEK, "week_days": DAYS, "summary": [], "hours_budget": 0, "daily_target_hours": {},
            "labor_target": 30, "roster": ["Ana", "Bo", "Cy"],
            "roster_roles": {"Ana": "Server", "Bo": "Server", "Cy": "Server"}}
    monkeypatch.setattr(se, "_build_schedule_result", lambda r, week_start=None, **k: dict(base))
    if learned is not None:
        monkeypatch.setattr(schedule_memory, "enforced_signals", lambda *a, **k: [dict(x) for x in learned])
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    se._run_schedule_job("tags-job", rid)
    assert finished.get("status") == "done", finished
    hid = finished["result"]["history_id"]
    return rid, hid, ssc.generation_tags(hid)


def test_each_saved_row_carries_its_origin_and_the_week_its_arms(db, monkeypatch):
    rid, hid, tags = _run(db, monkeypatch)
    assert tags and tags["restaurant_id"] == rid
    assert tags["origins"], "no row origins kept"
    assert set(tags["origins"].values()) <= {"model", "manager_plan", "post_loop"} | \
        {v for v in tags["origins"].values() if v.startswith(("added:", "model_edited:"))}
    assert "model" in tags["origins"].values()
    assert isinstance(tags["arms"], list) and tags["rules"] is not None
    assert tags["ablation"] is None, "nothing learned binds: no ablation"


def test_with_a_learned_memory_binding_the_ablation_runs_the_loop_again_without_it(db, monkeypatch):
    learned = [{"kind": "moved_off", "key": "pattern:moved_off|ana|Monday|night", "person": "Ana", "day": "Monday",
                "daypart": "night", "role": "Server", "value": {"slot": "off"}, "confidence": 0.9,
                "enforcement": "soft", "source": "manager_edits"}]
    import json
    import job_queue
    ran = []
    real_repair = se.repair_week
    monkeypatch.setattr(se, "repair_week", lambda rows, x: ran.append(bool(getattr(x, "shadow", False)))
                        or real_repair(rows, x))
    # This process's own queue pool would drain the queued row on a thread
    # at once (it consumes while no worker heartbeat is fresh) — a race this
    # test lost on Postgres. The row is run below, by hand.
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    rid, hid, tags = _run(db, monkeypatch, learned=learned)
    # Off the owner's wait (blind re-audit 10/11/26 ARCH-14): the generation
    # ran ONE repair loop and queued the ablation as a bulk row.
    assert ran == [False], "the ablation ran inline"
    assert tags["ablation"] == {"ran": False, "queued": True, "learned": 1}
    conn = models.get_conn(db)
    row = conn.execute("SELECT * FROM jobs WHERE kind='schedule_ablation'").fetchone()
    conn.close()
    assert row["priority_class"] == "bulk" and row["idempotency_key"] == f"schedule_ablation:{hid}"
    job = job_queue.Job(dict(row), payload=json.loads(row["payload_json"]))
    assert schedule_job.run_queued_ablation(job)["ran"] is True
    assert ran[1:] and all(ran[1:])                          # the shadow loop, on the worker
    ab = ssc.generation_tags(hid)["ablation"]
    assert ab and ab["ran"] and ab["learned"] == 1 and ab["offline"] is True
    assert ab["rows_on"] and ab["rows_off"] is not None and ab["seconds"] is not None
    monkeypatch.setenv(ssc.ABLATION_ENV, "0")
    _rid2, _hid2, tags2 = _run(db, monkeypatch, learned=learned)
    assert tags2["ablation"] is None, "the switch did not turn the ablation off"


def test_the_learning_experiments_off_week_binds_no_learned_memory(db, monkeypatch):
    import schedule_experiments as sx
    learned = [{"kind": "moved_off", "key": "pattern:moved_off|ana|Monday|night", "person": "Ana", "day": "Monday",
                "daypart": "night", "role": "Server", "value": {"slot": "off"}, "confidence": 0.9,
                "enforcement": "soft", "source": "manager_edits"}]
    monkeypatch.setenv(sx.PIN_ENV, "learning_v1:memory_off")
    _rid, _hid, tags = _run(db, monkeypatch, learned=learned)
    arms = {a["experiment"]: a["arm"] for a in tags["arms"]}
    assert arms.get("learning_v1") == "memory_off"
    # Nothing learned bound the week's passes, so there was nothing to ablate.
    assert tags["ablation"] is None
