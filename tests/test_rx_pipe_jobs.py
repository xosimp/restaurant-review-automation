"""Schedule re-audit 10/4/26, workstream PIPE: the generation job store.

PIPE-9  a generation is judged by its own deadline, aged from when it took
        a slot — never called dead by the time it queued; one called dead
        while it queued is never run (nothing paid for a draft nobody waits on)
UI-8    Generate joins a running generation only when it asks the same thing
        (week, days, draft, instruction); any other press is told which week
        is being built, and nothing starts
"""
import threading

from test_edge_data_async_jobs import _init, client, _restaurant, _token, _pending_job, _jobs  # noqa: F401
import models
import ops
import schedule_engine as se


def _set(db_path, job_id, **cols):
    conn = models.get_conn(db_path)
    conn.execute("UPDATE async_jobs SET " + ", ".join(f"{k}={v}" for k, v in cols.items()) + " WHERE job_id=?",
                 (job_id,))
    conn.commit()
    conn.close()


# ── PIPE-9 ────────────────────────────────────────────────────────────────

def test_a_generation_that_queued_for_a_slot_is_judged_by_its_own_deadline(db_path):
    ops.claim_async_job("q1", "schedule", 7)
    _set(db_path, "q1", created_at="datetime('now','-10 minutes')")      # waited ten minutes for a slot
    clock = se.GenerationClock("q1")
    clock.plan(10)                                                      # a big week: the 40-minute ceiling
    _set(db_path, "q1", created_at="datetime('now','-46 minutes')")      # 36 minutes into its run
    assert ops.read_async_job("q1")["status"] == "pending"
    assert ops.job_still_pending("q1")
    assert ops.active_job("schedule", 7) == "q1"                         # a second press joins it
    # Past its own deadline it is dead, as before.
    _set(db_path, "q1", deadline_at="datetime('now','-5 minutes')")
    assert ops.read_async_job("q1")["status"] == "error"


def test_a_job_with_no_deadline_is_aged_from_when_it_started(db_path):
    ops.claim_async_job("q2", "schedule", 8)
    _set(db_path, "q2", created_at="datetime('now','-50 minutes')", started_at="datetime('now','-5 minutes')")
    assert ops.read_async_job("q2")["status"] == "pending"
    _set(db_path, "q2", started_at="NULL")
    assert ops.read_async_job("q2")["status"] == "error"                # still queued at 50 minutes: dead


def test_a_generation_called_dead_while_it_queued_is_never_run(db_path, monkeypatch):
    import labor
    ops.claim_async_job("q3", "schedule", 9)
    ops.finish_async_job("q3", "error", {"ok": False, "error": "didn't finish"})
    called = []
    monkeypatch.setattr(se, "_build_schedule_result", lambda *a, **k: called.append(1))
    monkeypatch.setattr(labor, "generate_optimized_schedule", lambda *a, **k: called.append(1))
    se._run_schedule_job("q3", 9)
    assert called == []
    assert ops.read_async_job("q3")["result"]["error"] == "didn't finish"


# ── UI-8 ──────────────────────────────────────────────────────────────────

def test_generate_for_another_week_never_joins_a_running_redo(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _token(client, db_path, rid)
    h = {"Authorization": f"Bearer {token}"}
    started, release = [], threading.Event()

    def slow(job_id, *a, **k):
        started.append((job_id, k))
        release.wait(3)
    monkeypatch.setattr(se, "_run_schedule_job", slow)
    hid = models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 6.0, 0, 30,
                                       "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                                       "2026-10-13,Tuesday,Ana,Server,4:00pm,10:00pm,6,", [], db_path=db_path)
    try:
        redo = client.post("/mobile/api/labor/generate-schedule", headers=h,
                           json={"dates": ["2026-10-13"], "history_id": hid}).get_json()
        assert redo["ok"] and not redo.get("joined")
        other = client.post("/mobile/api/labor/generate-schedule", headers=h,
                            json={"week_start": "2026-10-19", "instruction": "Patio closed"})
        body = other.get_json()
        # Nothing is started or joined for the other week. Since the parity
        # round (#15) the refusal names the RUNNING job so the phone can offer
        # "Watch it" — the redo's own id, never a new one.
        assert other.status_code == 409 and body["busy"] is True and body["ok"] is False
        assert body.get("job_id") in (None, redo["job_id"]) and not body.get("joined")
        assert "redoing Tuesday 10/13/26 of the week of 10/12/26" in body["error"]
        # The same redo pressed again joins it.
        again = client.post("/mobile/api/labor/generate-schedule", headers=h,
                            json={"dates": ["2026-10-13"], "history_id": hid}).get_json()
        assert again["joined"] is True and again["job_id"] == redo["job_id"]
    finally:
        release.set()
    assert len(_jobs(db_path)) == 1


def test_a_claim_joins_only_the_same_request(db_path):
    req = {"week_start": "2026-10-12", "dates": None, "history_id": None, "instruction": None}
    ops.claim_async_job("a1", "schedule", 5, request=req)
    assert ops.claim_async_job("a2", "schedule", 5, request=dict(req)) == ("a1", True)
    try:
        ops.claim_async_job("a3", "schedule", 5, request=dict(req, instruction="Patio closed"))
        raise AssertionError("joined a generation asked something else")
    except ops.JobBusy as e:
        assert e.job_id == "a1" and e.request == req
    assert "the week of 10/12/26 is being built" in se.busy_message(req)


# ── re-audit 10/7/26 #1: a press that joins a queued auto-draft ───────────

def test_a_press_that_joins_a_queued_auto_draft_promotes_it_at_the_gate(client, db_path, monkeypatch):
    # The weekly auto-draft claimed next week and is queued for a slot at
    # background priority; the owner presses Generate for the same week and
    # joins it. It must not then fail them after the background wait with
    # "No generation slot came free": the join promotes it to a press.
    import inspect
    import time
    import mobile_api
    rid = _restaurant(db_path)
    token = _token(client, db_path, rid)
    h = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(se, "GEN_BACKGROUND_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(se._ops, "set_async_job_deadline", lambda *a, **k: None)
    job_id, joined = ops.claim_async_job("auto-join1", "schedule", rid, request=se.generation_request(rid))
    assert not joined
    gate = se._GEN_SLOTS
    for n in range(gate.slots):
        gate.acquire(se.GEN_PRIORITY_PRESS, job_id=f"busy-{n}")        # every slot taken
    got, gave_up = [], []

    def draft():
        try:
            with se.generation_scope(job_id, priority=se.GEN_PRIORITY_BACKGROUND):
                got.append(job_id)
        except se.GenerationSlotTimeout:
            gave_up.append(job_id)
    t = threading.Thread(target=draft)
    t.start()
    deadline = time.time() + 5
    while gate.waiting() < 1 and time.time() < deadline:
        time.sleep(0.01)
    body = client.post("/mobile/api/labor/generate-schedule", headers=h, json={}).get_json()
    assert body["ok"] and body["joined"] is True and body["job_id"] == job_id
    time.sleep(1.0)                                    # well past the background wait
    assert gave_up == [] and t.is_alive()              # promoted: it waits on, as the owner's press
    for n in range(gate.slots):
        gate.release(f"busy-{n}")
    t.join(5)
    assert got == [job_id] and gate.busy == 0
    # Both joined branches of the one generate body (the web route calls it).
    assert inspect.getsource(mobile_api.mobile_generate_schedule).count("promote_generation(") == 2
