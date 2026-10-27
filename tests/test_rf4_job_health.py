"""Re-fix round RF4 (blind re-audit B7/B8, 10/27/26) — job and queue health,
paging, the /health read-only call.

  * a failed schedule generation (an owner's press or the auto-draft) ends
    its queue row FAILED — a dead letter the urgent rule reads — driven
    through the real handlers, the generation itself failing inside;
  * an owner-actionable refusal (closed every day) is not a dead letter;
  * the nightly pos_sync fan-out's run reads its rows' real outcomes;
  * a queue kind no build runs is named, then expired with a capture;
  * every platform condition is its own pager incident, resolved when it
    clears;
  * /health's read-only call skips the reads only paging needs.

No test reaches a provider: requests.post is stubbed where a pager is set.
"""
import json
import sqlite3
import time

import pytest

import admin_ops  # noqa: F401  (binds get_conn at import: imported before the patch)
import job_queue
import models
import ops
import scheduler
import status_manager


@pytest.fixture(autouse=True)
def db(db_path, monkeypatch):
    import strategy_jobs
    real, default = models.get_conn, models.DB_PATH
    monkeypatch.setattr(models, "get_conn", lambda path=None, *a, **k: real(db_path if path in (None, default) else path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    monkeypatch.setattr(strategy_jobs, "DB_PATH", db_path)
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    status_manager.seed_default_services()
    ops._claim_fallback.clear()
    ops._page_memory.clear()
    ops._failing_cache.clear()
    for v in ("WILL_PHONE", "OPS_ONCALL_PHONES", "OPS_PAGER_KIND", "OPS_PAGER_ROUTING_KEY", "OPS_PAGER_WEBHOOK_URL",
              "HEALTHCHECK_PING_URL"):
        monkeypatch.delenv(v, raising=False)
    yield db_path
    ops._page_memory.clear()


def _q(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _x(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _beat(db_path):
    _x(db_path, "UPDATE scheduler_heartbeat SET beat_at=datetime('now'), loop_completed_at=datetime('now'), "
                "running_job=NULL, running_since=NULL WHERE id=1")


def _restaurant(**kw):
    return models.create_restaurant(models.Restaurant(name="Harbor Grill", owner_email="harbor@x.test", **kw))


def _press(rid, job_id):
    """An owner's press of Generate as the route queues it."""
    ops.start_async_job(job_id, "schedule", rid)
    qid, _ = job_queue.enqueue("schedule_generate", {"job_id": job_id, "restaurant_id": rid, "kwargs": {}},
                               restaurant_id=rid, external_id=job_id, wake=False)
    job = job_queue.claim("test-worker", groups=("generation",))
    assert job is not None and job["id"] == qid
    job_queue.run_one(job)
    return qid


# ── a failed generation is a dead letter (re-audit B7 P1) ───────────────────

def test_a_generation_that_fails_inside_ends_its_queue_row_failed_and_pages_urgently(db, monkeypatch):
    import schedule_engine

    def boom(*a, **k):
        raise RuntimeError("no such column: restaurants.secret_col")
    monkeypatch.setattr(schedule_engine, "_build_schedule_result", boom)
    rid = _restaurant()
    rows = [_press(rid, f"press-{i}") for i in range(2)]
    for qid in rows:
        row = _q(db, "SELECT state, error, result_json FROM jobs WHERE id=?", (qid,))[0]
        assert row["state"] == "failed", "the generation failed and its row read done (no dead letter)"
        assert "RuntimeError" in row["error"] and json.loads(row["result_json"])["status"] == "error"
    assert ops.read_async_job("press-0", restaurant_id=rid)["status"] == "error", "the owner is still told"
    st = job_queue.stats()
    assert st["kinds"]["schedule_generate"]["failed_1h"] == 2
    assert [k for k, _ in ops.queue_problems(st, worker_expected=False)] == ["queue_failed_urgent:schedule_generate"]
    _beat(db)
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: True)
    out = ops.check_platform_sla(send=True)
    assert out["queue_urgent"] and out["alerted"]
    assert ops.last_operator_alert()["key"].startswith("platform_sla_alert:")


def test_a_refusal_the_owner_can_act_on_is_not_a_dead_letter(db, monkeypatch):
    import schedule_engine

    def closed(*a, **k):
        raise schedule_engine.ScheduleGenerationError("The restaurant is marked closed every day of this week, so "
                                                      "there is nothing to schedule.")
    monkeypatch.setattr(schedule_engine, "_build_schedule_result", closed)
    rid = _restaurant()
    qid = _press(rid, "press-closed")
    assert _q(db, "SELECT state FROM jobs WHERE id=?", (qid,))[0]["state"] == "done"
    assert ops.read_async_job("press-closed", restaurant_id=rid)["status"] == "error"


def test_a_failed_auto_draft_ends_its_queue_row_failed(db, monkeypatch):
    import schedule_engine
    import strategy_jobs

    def boom(*a, **k):
        raise RuntimeError("solver crashed")
    monkeypatch.setattr(schedule_engine, "_build_schedule_result", boom)
    rid = _restaurant()
    models.update_restaurant(rid, {"auto_draft_schedule": 1, "module_labor": 1}, db_path=db)
    qid, _ = job_queue.enqueue("auto_draft", {"restaurant_id": rid, "period": "2026-10-27", "attempt": 0,
                                              "batch": False}, restaurant_id=rid, wake=False)
    job = job_queue.claim("test-worker", groups=("generation",))
    assert job is not None and job["id"] == qid
    out = job_queue.run_one(job)
    assert out.get("failed") == 1 and out.get(job_queue.DEAD_LETTER_KEY)
    assert _q(db, "SELECT state FROM jobs WHERE id=?", (qid,))[0]["state"] == "failed"
    assert strategy_jobs.run_queued_auto_draft is not None


# ── pos_sync's fan-out reads its rows' outcomes (re-audit B7 P2) ────────────

def _fanout_run(db_path, queued, hours_ago=2.0):
    """A finished pos_sync run as queue_nightly_syncs leaves it (enqueue
    counts), `hours_ago` old; returns (started, finished) stamps."""
    started = _q(db_path, "SELECT datetime('now', ?) AS t", (f"-{hours_ago * 60 + 1:.0f} minutes",))[0]["t"]
    finished = _q(db_path, "SELECT datetime('now', ?) AS t", (f"-{hours_ago * 60:.0f} minutes",))[0]["t"]
    blob = json.dumps({"attempted": queued, "ok": queued, "failed": 0, "skipped": 2, "hit_bound": False,
                       "queued": queued})
    _x(db_path, "INSERT INTO job_runs (job, started_at, finished_at, ok, result_json) VALUES "
                "('pos_sync', ?, ?, 1, ?)", (started, finished, blob))
    return started, finished


def _sync_row(db_path, created, state, result=None):
    now = time.time()
    _x(db_path, "INSERT INTO jobs (kind, grp, priority_class, state, payload_json, enqueued_at, finished_at, "
                "result_json, created_at) VALUES ('pos_sync', 'pos_io', 'bulk', ?, '{}', ?, ?, ?, ?)",
       (state, now - 3600, now - 600, json.dumps(result) if result is not None else None, created))


def test_the_nightly_pos_sync_run_reads_its_rows_real_outcomes(db):
    started, _ = _fanout_run(db, queued=10)
    for i in range(10):
        _sync_row(db, started, "done", {"ok": i < 2, "provider": "toast"})
    assert ops.settle_queue_fanout("pos_sync", "pos_sync") == 1
    run = _q(db, "SELECT ok, result_json FROM job_runs WHERE job='pos_sync'")[0]
    counts = json.loads(run["result_json"])
    assert counts["attempted"] == 10 and counts["failed"] == 8 and counts["eligible"] == 10 and counts["rows_settled"]
    assert run["ok"] == ops.RUN_PARTIAL
    # Settled once; a second night like it makes the job "failing".
    assert ops.settle_queue_fanout("pos_sync", "pos_sync") == 0
    started2, _ = _fanout_run(db, queued=10, hours_ago=1.0)
    for i in range(10):
        _sync_row(db, started2, "done", {"ok": i < 1})
    ops.settle_queue_fanout("pos_sync", "pos_sync")
    failing = {f["job"]: f for f in ops.jobs_failing(use_cache=False)}
    assert failing["pos_sync"]["why"] == "failing" and failing["pos_sync"]["failed"] == 9


def test_a_fanout_whose_rows_never_ran_reads_idle_and_waits_while_rows_are_open(db):
    started, _ = _fanout_run(db, queued=4)
    _sync_row(db, started, "queued")
    assert ops.settle_queue_fanout("pos_sync", "pos_sync") == 0, "settled while a row was still queued"
    _x(db, "UPDATE jobs SET state='expired'")
    for _ in range(3):
        _sync_row(db, started, "expired")
    assert ops.settle_queue_fanout("pos_sync", "pos_sync") == 1
    blob = _q(db, "SELECT result_json FROM job_runs")[0]["result_json"]
    assert ops._run_idle(blob), json.loads(blob)


# ── a kind no build runs (re-audit B7 P2) ───────────────────────────────────

def test_a_queued_kind_no_build_handles_is_named_then_expired_with_a_capture(db, monkeypatch):
    qid, _ = job_queue.enqueue("auto_draft_v0", {"restaurant_id": 1}, wake=False)
    old = time.time() - 4 * 86400
    _x(db, "UPDATE jobs SET enqueued_at=? WHERE id=?", (old, qid))
    _x(db, "INSERT INTO job_workers (worker_id, role, started_at, heartbeat_at) VALUES ('web-1', 'web', ?, ?)",
       (time.time() - 3600, time.time()))
    st = job_queue.stats()
    assert st["unknown_kinds"]["auto_draft_v0"]["queued"] == 1
    assert not (st["groups"].get("bulk") or {}).get("oldest_ready_seconds"), "not a backlog more slots would clear"
    problems = ops.queue_problems(st, worker_expected=False)
    assert [k for k, _ in problems] == ["queue_unknown_kind:auto_draft_v0"]
    HOOKED.clear()
    monkeypatch.setitem(job_queue.RETIRED_KINDS, "auto_draft_v0", f"{__name__}:_retired_hook")
    assert job_queue.expire_unknown_kinds() == {"auto_draft_v0": 1}
    assert _q(db, "SELECT state FROM jobs WHERE id=?", (qid,))[0]["state"] == "expired"
    assert HOOKED == [qid], "a retired kind's expire hook runs (the owner told, the day handed back)"
    assert any("auto_draft_v0" in (f["error"] or "") for f in _q(db, "SELECT error FROM job_failures"))
    # A fresh row of an unknown kind (a deploy in progress) is left alone.
    job_queue.enqueue("kind_from_a_newer_build", {}, wake=False)
    assert job_queue.expire_unknown_kinds() == {}


HOOKED = []


def _retired_hook(job):
    HOOKED.append(job["id"])


# ── the pager: a key per condition, resolved when it clears (re-audit B7 P2) ─

def test_each_condition_is_its_own_incident_and_a_cleared_one_is_resolved(db, monkeypatch):
    import requests
    monkeypatch.setenv("OPS_PAGER_KIND", "pagerduty")
    monkeypatch.setenv("OPS_PAGER_ROUTING_KEY", "rk-test")
    posted = []

    class _R:
        status_code = 202
    monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None: posted.append(json) or _R())
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    import emails
    monkeypatch.setattr(emails, "_send_branded", lambda *a, **k: emails.SendResult(ok=False, error="off"))
    _beat(db)
    state = {"disk": {"state": "critical", "free_mb": 10, "pct_free": 1}}
    monkeypatch.setattr(status_manager, "disk_state", lambda *a, **k: state["disk"])
    monkeypatch.setattr(ops, "backup_status", lambda db_path=None: {"state": "stale", "summary": "backup stale"})
    out = ops.check_platform_sla(send=True)
    triggers = {p["dedup_key"]: p for p in posted if p["event_action"] == "trigger"}
    assert {"cavnar:sla:disk_critical", "cavnar:sla:backup"} <= set(triggers), sorted(triggers)
    assert triggers["cavnar:sla:disk_critical"]["payload"]["severity"] == "critical"
    assert triggers["cavnar:sla:backup"]["payload"]["severity"] == "warning"
    assert out["alerted"]
    # The disk recovers: its incident is resolved; the backup's stays open.
    posted.clear()
    state["disk"] = {"state": "ok"}
    ops.check_platform_sla(send=True)
    resolves = [p["dedup_key"] for p in posted if p["event_action"] == "resolve"]
    assert resolves == ["cavnar:sla:disk_critical"]
    # Everything clears: the last one is resolved too, though nothing is wrong to page about.
    posted.clear()
    monkeypatch.setattr(ops, "backup_status", lambda db_path=None: {"state": "ok"})
    out = ops.check_platform_sla(send=True)
    assert out["problems"] == [] and [p["dedup_key"] for p in posted] == ["cavnar:sla:backup"]
    assert all(p["event_action"] == "resolve" for p in posted)


def test_a_laptop_never_resolves_productions_incidents(db, monkeypatch):
    import requests
    monkeypatch.setenv("OPS_PAGER_KIND", "pagerduty")
    monkeypatch.setenv("OPS_PAGER_ROUTING_KEY", "rk-test")
    ops._pager_open_note("sla:disk_critical")
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("a laptop sent a pager event"))
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    _beat(db)
    ops.check_platform_sla(send=True)


# ── /health's read-only call (re-audit B7 P3) ───────────────────────────────

def test_the_read_only_call_skips_the_finished_queue_rows_and_the_fresh_route_scan(db, monkeypatch):
    import platform_monitor
    seen = {}
    real_stats, real_slo = job_queue.stats, platform_monitor.request_slo

    def stats(*a, **k):
        seen["stats"] = k
        return real_stats(*a, **k)

    def slo(*a, **k):
        seen["slo"] = k
        return real_slo(*a, **k)
    monkeypatch.setattr(job_queue, "stats", stats)
    monkeypatch.setattr(platform_monitor, "request_slo", slo)
    out = ops.check_platform_sla(send=False)
    assert seen["stats"].get("dead_letters") is False and seen["slo"].get("fresh") is False
    assert out["queue"]["failed"] is None and out["requests"]["fresh_5xx_routes"] == []


def test_queue_dead_letters_are_a_rate_with_minimums():
    def q(failed, finished):
        return {"kinds": {"pos_archive": {"failed_1h": failed, "finished_1h": finished}}}
    assert ops.queue_failed_kinds(q(5, 1500)) == [], "5 of 1,500 is routine at fleet scale"
    assert ops.queue_failed_kinds(q(80, 1500))[0]["kind"] == "pos_archive", "5% of a busy kind"
    assert ops.queue_failed_kinds(q(1, 2)) == [], "one failure is below the minimum"
    assert ops.queue_failed_kinds(q(3, 10))[0]["share"] == 0.3
