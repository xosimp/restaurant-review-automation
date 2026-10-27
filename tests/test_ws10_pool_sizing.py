"""Audit #25 WS10 — multi-worker readiness (docs/plans/POSTGRES_AND_WORKERS_PLAN.md).

Every bounded pool's setting is the CONTAINER's bound; with `--workers N`
each gunicorn web worker takes bound // N (pool_sizing), and with one worker
— production today — every pool is built exactly as before. The schedule
call pool keeps its whole bound (claimed queue rows hold the fleet's), and a
process that is not a web worker (worker.py) keeps every bound whole.

Ask's polled turns: with one worker nothing is written; with several, every
event is also a row in ai_lane_events, so a poll another worker serves reads
the turn — by the same login of the same restaurant only.

Nothing here starts a server, reaches a provider or reads the wall clock for
a date.
"""
import importlib.util
import os
import sys
import time
import types

import pytest

import pool_sizing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def one_worker(monkeypatch):
    monkeypatch.delenv("CAVNAR_WEB_WORKERS", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.delenv("CAVNAR_PROCESS_ROLE", raising=False)


# ── the sizing function ────────────────────────────────────────────────────

def test_one_worker_keeps_every_bound_exactly(one_worker):
    assert pool_sizing.web_workers() == 1
    for bound in (0, 1, 2, 3, 4, 6, 32):
        assert pool_sizing.per_worker(bound) == bound
    assert pool_sizing.fleet_total(6) == 6


@pytest.mark.parametrize("bound, workers, share", [
    (6, 2, 3), (6, 4, 1), (8, 4, 2), (4, 2, 2), (2, 4, 1), (1, 3, 1), (3, 2, 1),
])
def test_n_workers_split_the_bound_never_below_one(bound, workers, share):
    assert pool_sizing.per_worker(bound, workers=workers) == share
    # Never more than the bound in all — unless the floor of one overshoots it.
    total = pool_sizing.fleet_total(bound, workers=workers)
    assert total <= bound or share == 1


def test_a_minimum_above_one_is_kept():
    assert pool_sizing.per_worker(4, minimum=2, workers=4) == 2


def test_the_masters_own_count_wins_over_web_concurrency(monkeypatch, one_worker):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    assert pool_sizing.web_workers() == 4                 # gunicorn's own default, when nothing else says
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "1")
    assert pool_sizing.web_workers() == 1                 # the start command's --workers 1 wins
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "nonsense")
    assert pool_sizing.web_workers() == 4
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "0")
    monkeypatch.delenv("WEB_CONCURRENCY")
    assert pool_sizing.web_workers() == 1


def test_a_process_that_is_not_a_web_worker_keeps_the_whole_bound(monkeypatch, one_worker):
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "4")
    assert pool_sizing.web_workers() == 4 and pool_sizing.per_worker(8) == 2
    for role in ("queue", "scheduler"):
        monkeypatch.setenv("CAVNAR_PROCESS_ROLE", role)
        assert pool_sizing.web_workers() == 1 and pool_sizing.per_worker(8) == 8


def _gunicorn_conf():
    spec = importlib.util.spec_from_file_location("_ws10_gunicorn_conf", os.path.join(ROOT, "gunicorn.conf.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_gunicorn_master_publishes_its_real_worker_count(monkeypatch, one_worker):
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "1")       # restored by monkeypatch at the end
    conf = _gunicorn_conf()
    server = types.SimpleNamespace(cfg=types.SimpleNamespace(workers=3))
    assert conf.publish_worker_count(server) == 3
    assert os.environ["CAVNAR_WEB_WORKERS"] == "3" and pool_sizing.web_workers() == 3
    server.cfg.workers = 1
    conf.publish_worker_count(server)
    assert pool_sizing.web_workers() == 1


def test_the_production_start_command_still_runs_one_worker():
    """The split exists so --workers can rise; until the plan's steps are
    done it stays 1 (CLAUDE.md) — this pins that nothing here changed it."""
    import json
    cmd = json.load(open(os.path.join(ROOT, "railway.json")))["deploy"]["startCommand"]
    assert "--workers 1 " in cmd


# ── every listed pool, built ───────────────────────────────────────────────

def _built_pools(monkeypatch):
    """{name: max workers} for every bounded pool, each built fresh in this
    process under the environment the test set."""
    import admin_routes
    import ai_async
    import ai_lane
    import ai_learning
    import ai_orchestrator
    import ask_cavnar
    import client_api
    import ops
    import push
    import schedule_engine
    import staff_knowledge
    import webhooks
    for mod, attr in ((ops, "_admin_pool"), (admin_routes, "_admin_job_pool"), (push, "_executor"),
                      (webhooks, "_executor"), (ask_cavnar, "_READ_POOL"), (staff_knowledge, "_tx_pool"),
                      (ai_async, "_pool"), (ai_lane, "_lane"), (client_api, "_SUMMARY_POOL"),
                      (ai_orchestrator, "_SHADOW_POOL"), (ai_learning, "_SCORE_POOL"),
                      (schedule_engine, "_CALL_POOL")):
        monkeypatch.setattr(mod, attr, None)
    built = {}
    # (ops.run_admin_task builds its pool inside the call: its own test below.)
    built["admin_job"] = admin_routes._admin_job_executor()._max_workers
    built["push"] = push._push_executor()._max_workers
    built["webhook"] = webhooks._webhook_executor()._max_workers
    built["ask_read"] = ask_cavnar._read_pool()._max_workers
    built["owner_ai"] = ai_async._executor().workers
    lane = ai_lane._the_lane()
    built["ai_lane"], built["ai_lane_per_tenant"] = lane.workers, lane.per_tenant
    built["schedule_call"] = schedule_engine._call_pool()._max_workers
    for pool in (admin_routes._admin_job_pool, push._executor, webhooks._executor, ask_cavnar._READ_POOL,
                 schedule_engine._CALL_POOL):
        pool.shutdown(wait=False)
    return built


def test_with_one_worker_every_pool_is_built_at_its_configured_bound(monkeypatch, one_worker):
    import admin_routes
    import ai_async
    import ai_lane
    import ask_cavnar
    import push
    import schedule_engine
    import webhooks
    built = _built_pools(monkeypatch)
    assert built == {
        "admin_job": admin_routes.ADMIN_JOB_WORKERS,
        "push": push._MAX_PUSH_WORKERS, "webhook": webhooks._MAX_WEBHOOK_WORKERS,
        "ask_read": ask_cavnar.ASK_READ_WORKERS, "owner_ai": ai_async.POOL_WORKERS,
        "ai_lane": ai_lane.LANE_WORKERS, "ai_lane_per_tenant": ai_lane.LANE_PER_TENANT,
        "schedule_call": schedule_engine.call_pool_workers(),
    }


def test_with_two_workers_each_takes_half_and_the_call_pool_stays_whole(monkeypatch, one_worker):
    import push
    import schedule_engine
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "2")
    monkeypatch.setattr(push, "_MAX_PUSH_WORKERS", 4)
    import webhooks
    monkeypatch.setattr(webhooks, "_MAX_WEBHOOK_WORKERS", 4)
    import ai_lane
    monkeypatch.setattr(ai_lane, "LANE_WORKERS", 6)
    monkeypatch.setattr(ai_lane, "LANE_PER_TENANT", 2)
    import ai_async
    monkeypatch.setattr(ai_async, "POOL_WORKERS", 3)
    import admin_routes
    monkeypatch.setattr(admin_routes, "ADMIN_JOB_WORKERS", 2)
    built = _built_pools(monkeypatch)
    assert built["push"] == 2 and built["webhook"] == 2
    assert built["ai_lane"] == 3 and built["ai_lane_per_tenant"] == 1
    assert built["owner_ai"] == 1 and built["admin_job"] == 1 and built["ask_read"] == 2
    # Claimed generation rows bound the fleet's streams: never split.
    assert built["schedule_call"] == schedule_engine.call_pool_workers()
    snap = pool_sizing.snapshot()
    assert snap["workers"] == 2 and snap["pools"]["push"] == {"bound": 4, "per_process": 2, "workers": 2}
    assert "held_by" in snap["pools"]["schedule_call"]


def test_the_queue_worker_keeps_every_bound_whole_under_four_web_workers(monkeypatch, one_worker):
    import push
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "4")
    monkeypatch.setenv("CAVNAR_PROCESS_ROLE", "queue")
    monkeypatch.setattr(push, "_MAX_PUSH_WORKERS", 4)
    built = _built_pools(monkeypatch)
    assert built["push"] == 4


def test_the_one_thread_pools_stay_one_thread(monkeypatch, one_worker):
    """Ask's summary, the shadow review and the shadow scoring: a bound of
    one cannot be split — each worker keeps its one thread."""
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "4")
    for name in ("ask_summary", "shadow_review", "shadow_score"):
        assert pool_sizing.sized(name, 1) == 1


def test_the_admin_task_pool_runs_at_its_share(monkeypatch, one_worker, db_path):
    """ops.run_admin_task builds its pool at the per-worker share."""
    import ops
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "2")
    monkeypatch.setattr(ops, "ADMIN_TASK_WORKERS", 4)
    monkeypatch.setattr(ops, "_admin_pool", None)
    monkeypatch.setattr(ops, "claim_async_job", lambda job_id, kind, rid: (job_id, False))
    monkeypatch.setattr(ops, "finish_async_job", lambda *a, **k: None)
    monkeypatch.setattr(ops, "run_job", lambda name, fn, *a, **k: fn(*a))
    ops.run_admin_task("ws10_probe", 1, "ws10_probe", lambda: {"attempted": 0, "ok": 0, "failed": 0,
                                                                "skipped": 0, "hit_bound": False})
    assert ops._admin_pool._max_workers == 2
    ops._admin_pool.shutdown(wait=True)


# ── Ask's polled turns across workers ──────────────────────────────────────

@pytest.fixture
def lane_db(db_path, monkeypatch):
    import ai_lane
    monkeypatch.setattr(ai_lane, "_EVENTS_DB", db_path)
    monkeypatch.setattr(ai_lane, "_prune_state", {"at": 0.0})
    ai_lane.reset_for_tests()
    yield db_path
    ai_lane.reset_for_tests()


def _rows(db_path, turn_id=None):
    import models
    conn = models.get_conn(db_path)
    try:
        if turn_id is None:
            return conn.execute("SELECT turn_id, seq, kind FROM ai_lane_events ORDER BY turn_id, seq").fetchall()
        return conn.execute("SELECT seq, kind FROM ai_lane_events WHERE turn_id=? ORDER BY seq",
                            (turn_id,)).fetchall()
    finally:
        conn.close()


def _wait(turn, seconds=3):
    deadline = time.monotonic() + seconds
    while not turn.done and time.monotonic() < deadline:
        time.sleep(0.01)
    assert turn.done


def _answer(turn):
    turn.put({"type": "progress", "text": "Reading your reviews"})
    turn.put({"type": "sentence", "text": "Reviews look steady."})
    turn.put({"type": "answer", "answer": "Reviews look steady."})


def test_with_one_worker_a_turn_writes_nothing(lane_db, one_worker):
    import ai_lane
    assert ai_lane.shared_events() is False
    turn = ai_lane.start(1, 7, _answer)
    _wait(turn)
    assert ai_lane.read(turn.id, 1, 7)[2] is True
    assert _rows(lane_db) == []
    # And the memory copy is the only one: gone from memory, gone.
    ai_lane.reset_for_tests()
    assert ai_lane.read(turn.id, 1, 7) is None


def test_with_two_workers_a_poll_on_the_other_worker_reads_the_turn(lane_db, monkeypatch, one_worker):
    import ai_lane
    import client_api
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "2")
    assert ai_lane.shared_events() is True
    turn = ai_lane.start(1, 7, _answer)
    _wait(turn)
    here = ai_lane.read(turn.id, 1, 7)
    # The worker that never saw the turn: no memory of it at all.
    ai_lane.reset_for_tests()
    there = ai_lane.read(turn.id, 1, 7)
    assert there == here and there[2] is True and [e["type"] for e in there[0]] == ["progress", "sentence", "answer"]
    assert ai_lane.read(turn.id, 1, 7, after=2) == ([{"type": "answer", "answer": "Reviews look steady."}], 3, True)
    # Another login, another restaurant, an unknown id: nothing.
    assert ai_lane.read(turn.id, 1, 8) is None
    assert ai_lane.read(turn.id, 2, 7) is None
    assert ai_lane.read("nope", 1, 7) is None
    payload, status = client_api._do_ask_turn_events(1, 7, turn.id, 0)
    assert status == 200 and payload["done"] and payload["events"][-1]["type"] == "answer"
    payload, status = client_api._do_ask_turn_events(2, 7, turn.id, 0)
    assert status == 404 and not payload["ok"]


def test_a_queued_turn_reads_as_pending_on_the_other_worker(lane_db, monkeypatch, one_worker):
    """Before its first event the turn exists for any worker (its open row):
    a poll waits, it is never told the answer is gone."""
    import threading
    import ai_lane
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "2")
    gate = threading.Event()
    turn = ai_lane.start(1, 7, lambda t: (gate.wait(3), _answer(t)))
    ai_lane.reset_for_tests()
    assert ai_lane.read(turn.id, 1, 7) == ([], 0, False)
    gate.set()
    _wait(turn)
    assert ai_lane.read(turn.id, 1, 7)[2] is True


def test_a_turn_whose_worker_died_is_answered_not_left_waiting(lane_db, monkeypatch, one_worker):
    """The worker running a shared turn was recycled mid-turn: its rows stop and never end. A poll on
    another worker is answered the lane's error once they are stale — never a wait without end."""
    import ai_lane
    import models
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "2")
    old = time.time() - ai_lane.SHARED_STALE_SECONDS - 5
    conn = models.get_conn(lane_db)
    try:
        conn.execute("INSERT INTO ai_lane_events (turn_id, seq, restaurant_id, user_key, kind, event_json, "
                     "created_at) VALUES ('dead', -1, 1, '7', 'open', NULL, ?)", (old,))
        conn.execute("INSERT INTO ai_lane_events (turn_id, seq, restaurant_id, user_key, kind, event_json, "
                     "created_at) VALUES ('dead', 0, 1, '7', 'event', '{\"type\": \"progress\"}', ?)", (old,))
        conn.commit()
    finally:
        conn.close()
    events, nxt, done = ai_lane.read("dead", 1, 7)
    assert done and nxt == 2 and events[-1] == {"type": "error", "error": ai_lane.STALE_MESSAGE}


def test_a_staff_login_reads_its_own_turn_on_the_other_worker(lane_db, monkeypatch, one_worker):
    import ai_lane
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "2")
    turn = ai_lane.start(3, ("staff", 41), lambda t: t.put({"type": "answer", "answer": {"text": "Yes"}}))
    _wait(turn)
    ai_lane.reset_for_tests()
    events, nxt, done = ai_lane.read(turn.id, 3, ("staff", 41))
    assert done and events == [{"type": "answer", "answer": {"text": "Yes"}}] and nxt == 1
    assert ai_lane.read(turn.id, 3, ("staff", 42)) is None


def test_a_refused_turn_leaves_no_rows(lane_db, monkeypatch, one_worker):
    import ai_lane
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "2")

    class Full:
        def submit(self, *a):
            raise ai_lane.LaneBusy("full")
    monkeypatch.setattr(ai_lane, "_the_lane", lambda: Full())
    with pytest.raises(ai_lane.LaneBusy):
        ai_lane.start(1, 7, _answer)
    assert _rows(lane_db) == []


def test_old_shared_rows_are_pruned_by_the_next_start(lane_db, monkeypatch, one_worker):
    import ai_lane
    import models
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "2")
    conn = models.get_conn(lane_db)
    try:
        conn.execute("INSERT INTO ai_lane_events (turn_id, seq, restaurant_id, user_key, kind, event_json, "
                     "created_at) VALUES ('old', -1, 1, '7', 'open', NULL, ?)",
                     (time.time() - ai_lane.TURN_TTL_SECONDS * 4,))
        conn.commit()
    finally:
        conn.close()
    turn = ai_lane.start(1, 7, _answer)
    _wait(turn)
    assert {r[0] for r in _rows(lane_db)} == {turn.id}


def test_the_switch_can_force_the_shared_copy_either_way(monkeypatch, one_worker):
    import ai_lane
    monkeypatch.setenv("AI_LANE_SHARED_EVENTS", "1")
    assert ai_lane.shared_events() is True
    monkeypatch.setenv("CAVNAR_WEB_WORKERS", "3")
    monkeypatch.setenv("AI_LANE_SHARED_EVENTS", "0")
    assert ai_lane.shared_events() is False


def test_the_shared_events_table_is_built_at_boot(db_path):
    import models
    conn = models.get_conn(db_path)
    try:
        conn.execute("SELECT turn_id, seq, restaurant_id, user_key, kind, event_json, created_at "
                     "FROM ai_lane_events LIMIT 1").fetchall()
    finally:
        conn.close()
    assert "ai_lane" in sys.modules
