"""Audit #25 WS6 — jobs and queue health, paging (A07-03, A07-07, A07-09,
A07-11, A07-12; risk R14).

A07-03  a job that fails almost everywhere, or attempts nothing while
        restaurants are eligible, warns (ops.jobs_failing); jobs_overdue
        counts only clean runs or partial runs under half failed.
A07-07  a queue kind's dead letters in the last hour warn; a failed owner
        press or auto-draft is urgent. An unknown kind is deferred, never
        failed (the contract other workstreams rely on).
A07-12  a partial run of a retryable non-sending job that failed at least
        20% retries once, 30 minutes later.
A07-09  page_operator says which channels failed and always tries the
        paging service when OPS_PAGER_KIND is set — first.
A07-11  a DSR night still provisional is said as such on the page.
"""
import json
import sqlite3
import time

import pytest

# Imported at collection, so modules that bind get_conn at import bind the
# real one (CLAUDE.md, bound imports).
import admin_ops  # noqa: F401
import jobs_registry
import models
import ops
import scheduler
import status_manager


@pytest.fixture(autouse=True)
def db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH
    monkeypatch.setattr(models, "get_conn", lambda path=None, *a, **k: real(db_path if path in (None, default) else path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    status_manager.seed_default_services()
    ops._claim_fallback.clear()
    ops._page_memory.clear()
    ops._failing_cache.clear()
    ops._bound_cache.clear()
    for v in ("WILL_PHONE", "OPS_ONCALL_PHONES", "OPS_PAGER_KIND", "OPS_PAGER_ROUTING_KEY", "OPS_PAGER_WEBHOOK_URL",
              "HEALTHCHECK_PING_URL"):
        monkeypatch.delenv(v, raising=False)
    yield db_path
    ops._page_memory.clear()


def _x(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _q(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _run(db_path, job, ok, counts=None, hours_ago=1.0):
    """One finished job_runs row `hours_ago` old with these counts."""
    _x(db_path, "INSERT INTO job_runs (job, started_at, finished_at, ok, result_json) VALUES "
                "(?, datetime('now', ?), datetime('now', ?), ?, ?)",
       (job, f"-{hours_ago * 60 + 1:.0f} minutes", f"-{hours_ago * 60:.0f} minutes", ok,
        json.dumps(counts) if counts is not None else None))


def _beat(db_path):
    _x(db_path, "UPDATE scheduler_heartbeat SET beat_at=datetime('now'), loop_completed_at=datetime('now'), "
                "running_job=NULL, running_since=NULL WHERE id=1")


# ── A07-03: failure share and empty runs ────────────────────────────────────

def test_a_run_is_scored_by_the_share_it_failed():
    assert ops.run_failed_share(0, None) == 1.0
    assert ops.run_failed_share(2, json.dumps({"attempted": 10, "failed": 9})) == 0.9
    assert ops.run_failed_share(2, json.dumps({"hit_bound": True})) is None
    assert ops._healthy_run(1, None) and ops._healthy_run(2, json.dumps({"attempted": 10, "failed": 1}))
    assert not ops._healthy_run(2, json.dumps({"attempted": 10, "failed": 9}))
    assert ops._healthy_run(2, None), "a partial with no counts is not judged failing"


def test_a_job_whose_runs_fail_most_restaurants_is_overdue(db):
    # pos_sync's SLA is daily; its only runs in two days failed 9 of 10.
    _run(db, "review_fetch", 1, {"attempted": 1, "ok": 1, "failed": 0}, hours_ago=60)     # job_runs is old
    _run(db, "pos_sync", 1, {"attempted": 10, "ok": 10, "failed": 0}, hours_ago=50)
    _run(db, "pos_sync", 2, {"attempted": 10, "ok": 1, "failed": 9}, hours_ago=25)
    _run(db, "pos_sync", 2, {"attempted": 10, "ok": 1, "failed": 9}, hours_ago=1)
    overdue = {j["job"]: j for j in ops.jobs_overdue()}
    assert "pos_sync" in overdue, "a run failing 9 of 10 stores was counted a success"
    assert overdue["pos_sync"]["hours_since"] >= 49


def test_a_partial_run_that_failed_few_still_counts(db):
    _run(db, "review_fetch", 1, {"attempted": 1, "ok": 1, "failed": 0}, hours_ago=60)
    _run(db, "pos_sync", 2, {"attempted": 10, "ok": 9, "failed": 1}, hours_ago=1)
    assert "pos_sync" not in {j["job"] for j in ops.jobs_overdue()}


def test_two_runs_failing_half_or_more_warn_and_page(db, monkeypatch):
    _beat(db)
    _run(db, "pos_sync", 2, {"attempted": 10, "ok": 1, "failed": 9}, hours_ago=25)
    _run(db, "pos_sync", 2, {"attempted": 10, "ok": 2, "failed": 8}, hours_ago=1)
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    sent = []
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: sent.append(lines) or True)
    out = ops.check_platform_sla(send=True)
    failing = {f["job"]: f for f in out["jobs_failing"]}
    assert failing["pos_sync"]["why"] == "failing" and failing["pos_sync"]["share"] == 0.8
    assert any("pos_sync failed at least 50%" in p and "8 of 10" in p for p in out["problems"])
    assert out["alerted"] and sent
    key = ops.last_operator_alert()["key"]
    assert key.startswith("platform_sla_warn:") and "job_failing:pos_sync" in key, "a warning, keyed by job"


def test_one_bad_run_between_good_ones_does_not_warn(db):
    _run(db, "pos_sync", 2, {"attempted": 10, "ok": 1, "failed": 9}, hours_ago=25)
    _run(db, "pos_sync", 1, {"attempted": 10, "ok": 10, "failed": 0}, hours_ago=1)
    assert ops.jobs_failing(use_cache=False) == []


def test_two_crashed_runs_count_as_failing(db):
    _run(db, "loss_sync", 0, None, hours_ago=25)
    _run(db, "loss_sync", 0, None, hours_ago=1)
    assert [f["job"] for f in ops.jobs_failing(use_cache=False)] == ["loss_sync"]


def test_a_job_that_attempts_nothing_while_restaurants_are_eligible_warns(db):
    idle = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False, "eligible": 12}
    _run(db, "review_fetch", 1, idle, hours_ago=5)
    _run(db, "review_fetch", 1, idle, hours_ago=1)
    (f,) = ops.jobs_failing(use_cache=False)
    assert f["job"] == "review_fetch" and f["why"] == "idle" and f["eligible"] == 12
    # Every restaurant skipped on purpose (not due, quiet hours) is not idle.
    _run(db, "pos_sync", 1, dict(idle, skipped=12), hours_ago=5)
    _run(db, "pos_sync", 1, dict(idle, skipped=12), hours_ago=1)
    assert [x["job"] for x in ops.jobs_failing(use_cache=False)] == ["review_fetch"]


def test_the_read_only_health_call_neither_reads_nor_pages_the_failing_jobs(db, monkeypatch):
    _beat(db)
    _run(db, "pos_sync", 2, {"attempted": 10, "failed": 9}, hours_ago=25)
    _run(db, "pos_sync", 2, {"attempted": 10, "failed": 9}, hours_ago=1)
    monkeypatch.setattr(ops, "alert_will", lambda *a: pytest.fail("/health must not page"))
    out = ops.check_platform_sla(send=False)
    assert out["jobs_failing"] == [] and out["alerted"] is False


def test_the_review_fetch_and_pos_sync_say_what_they_were_for(db, monkeypatch):
    import pos
    monkeypatch.setattr(models, "get_all_restaurants", lambda *a, **k: [])
    stats = {}
    pos.sync_all(stats=stats)
    assert stats["eligible"] == 0 and "attempted" in stats


# ── A07-07: dead letters and the unknown kind ───────────────────────────────

def _job(db_path, kind, state, finished_ago=60.0, grp="bulk"):
    now = time.time()
    _x(db_path, "INSERT INTO jobs (kind, grp, priority_class, state, payload_json, not_before, attempts, "
                "max_attempts, enqueued_at, finished_at) VALUES (?, ?, 'bulk', ?, '{}', 0, 2, 2, ?, ?)",
       (kind, grp, state, now - finished_ago - 10, now - finished_ago))


def test_failed_queue_rows_past_the_floor_are_a_warning(db):
    import job_queue
    for _ in range(6):
        _job(db, "learner", "failed")
    for _ in range(10):
        _job(db, "learner", "done")
    _job(db, "pos_archive", "failed")              # one failure among many is below the floor
    for _ in range(20):
        _job(db, "pos_archive", "done")
    st = job_queue.stats()
    assert st["kinds"]["learner"]["failed_1h"] == 6 and st["kinds"]["learner"]["finished_1h"] == 16
    kinds = [k for k, _ in ops.queue_problems(st, worker_expected=False)]
    assert kinds == ["queue_failed:learner"]


def test_old_failures_do_not_warn(db):
    import job_queue
    for _ in range(6):
        _job(db, "learner", "failed", finished_ago=7200)
    assert ops.queue_problems(job_queue.stats(), worker_expected=False) == []


def test_a_failed_owner_press_is_urgent(db, monkeypatch):
    import job_queue
    _beat(db)
    for _ in range(2):
        _job(db, "schedule_generate", "failed", grp="generation")
    _job(db, "schedule_generate", "done", grp="generation")
    kinds = [k for k, _ in ops.queue_problems(job_queue.stats(), worker_expected=False)]
    assert kinds == ["queue_failed_urgent:schedule_generate"]
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: True)
    out = ops.check_platform_sla(send=True)
    assert out["queue_urgent"] and out["alerted"]
    assert ops.last_operator_alert()["key"] == "platform_sla_alert", "urgent pages hourly, not once a day"


def test_an_unknown_kind_is_deferred_never_failed(db, monkeypatch):
    import job_queue
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    qid, _ = job_queue.enqueue("kind_from_a_newer_build", {"n": 1})
    assert job_queue.claim("old-worker", groups=("bulk",)) is None, "an old worker claimed a kind it cannot run"
    row = _q(db, "SELECT state, attempts FROM jobs WHERE id=?", (qid,))[0]
    assert row == {"state": "queued", "attempts": 0}
    # Past any deadline it is not expired by a process that cannot run it.
    _x(db, "UPDATE jobs SET deadline=? WHERE id=?", (time.time() - 60, qid))
    assert job_queue.claim("old-worker", groups=("bulk",)) is None
    assert _q(db, "SELECT state FROM jobs WHERE id=?", (qid,))[0]["state"] == "queued"
    # A row of it that reached run_one some other way goes back untouched.
    _x(db, "UPDATE jobs SET state='running', attempts=1, lease_owner='old-worker', lease_until=? WHERE id=?",
       (time.time() + 60, qid))
    job = job_queue.Job({"id": qid, "kind": "kind_from_a_newer_build", "worker_id": "old-worker", "payload": {}})
    assert job_queue.run_one(job) is None
    row = _q(db, "SELECT state, attempts, error, not_before FROM jobs WHERE id=?", (qid,))[0]
    assert row["state"] == "queued" and row["attempts"] == 0 and "deferred" in row["error"]
    assert row["not_before"] > time.time()
    # The build that knows the kind claims and runs it.
    monkeypatch.setitem(job_queue.HANDLERS, "kind_from_a_newer_build", "newer:handler")
    _x(db, "UPDATE jobs SET not_before=0, deadline=NULL WHERE id=?", (qid,))
    got = job_queue.claim("new-worker", groups=("bulk",))
    assert got is not None and got["id"] == qid


# ── A07-12: a partial run of a retryable job retries once ───────────────────

@pytest.fixture
def pulsed(db, monkeypatch):
    monkeypatch.setattr(scheduler, "record_running_job", lambda *a, **k: None)
    clock = {"t": 1000.0}
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock["t"])
    return scheduler._PulsedOps(), clock


def test_a_partial_retryable_run_that_failed_a_fifth_retries_once_after_30_minutes(pulsed):
    p, clock = pulsed
    assert jobs_registry.spec("pos_sync").get("retry") and not jobs_registry.spec("pos_sync").get("sends")
    assert p.claim_period("pos_sync", "2026-10-26") is True
    p.run_job("pos_sync", lambda: {"attempted": 10, "ok": 7, "failed": 3, "skipped": 0, "hit_bound": False})
    assert p._retries[("pos_sync", "2026-10-26")]["attempt"] == 1
    assert ("pos_sync", "2026-10-26", 1) == ops.pending_retries()[0][:3], "kept in the database too"
    clock["t"] += 29 * 60
    assert p.claim_period("pos_sync", "2026-10-26") is False, "retried before 30 minutes"
    clock["t"] += 2 * 60
    assert p.claim_period("pos_sync", "2026-10-26") is True
    # Partial again: the period stays spent — one retry only.
    p.run_job("pos_sync", lambda: {"attempted": 10, "ok": 6, "failed": 4, "skipped": 0, "hit_bound": False})
    assert ("pos_sync", "2026-10-26") not in p._retries and ops.pending_retries() == []
    clock["t"] += 31 * 60
    assert p.claim_period("pos_sync", "2026-10-26") is False


def test_a_partial_run_that_failed_few_keeps_its_period(pulsed):
    p, _clock = pulsed
    assert p.claim_period("pos_sync", "2026-10-26") is True
    p.run_job("pos_sync", lambda: {"attempted": 10, "ok": 9, "failed": 1, "skipped": 0, "hit_bound": False})
    assert p._retries == {}


def test_a_partial_sending_job_is_never_retried(pulsed):
    p, _clock = pulsed
    assert jobs_registry.spec("stale_inventory").get("sends")
    assert p.claim_period("stale_inventory", "2026-10-26") is True
    p.run_job("stale_inventory", lambda: {"attempted": 10, "ok": 2, "failed": 8, "skipped": 0, "hit_bound": False})
    assert p._retries == {} and ops.period_claimed("stale_inventory", "2026-10-26")


# ── A07-09: paging does not depend on the app's own keys ────────────────────

def _break_sms_and_email(monkeypatch, order):
    import emails
    import notify

    def sms(*a, **k):
        order.append("sms")
        raise RuntimeError("Twilio 20003: authenticate")

    def mail(*a, **k):
        order.append("email")
        return emails.SendResult(ok=False, error="resend 401")
    monkeypatch.setattr(notify, "send_sms", sms)
    monkeypatch.setattr(emails, "_send_branded", mail)


def test_with_twilio_and_resend_failing_the_pager_still_takes_the_page_first(db, monkeypatch):
    import requests
    order, posted = [], []
    _break_sms_and_email(monkeypatch, order)
    monkeypatch.setenv("WILL_PHONE", "+15555550100")
    monkeypatch.setenv("OPS_PAGER_KIND", "pagerduty")
    monkeypatch.setenv("OPS_PAGER_ROUTING_KEY", "rk-test")

    class _R:
        status_code = 202

    def post(url, json=None, timeout=None):
        order.append("pager")
        posted.append(json)
        return _R()
    monkeypatch.setattr(requests, "post", post)
    res = ops.page_operator("backup_failed", "Cavnar AI: backup failed", ["no off-site copy"])
    assert res["sent"] is True and res["channels"]["pager"] is True
    assert set(res["failed"]) >= {"sms", "email"}, res
    assert order[0] == "pager", order
    assert posted[0]["dedup_key"] == "cavnar:backup_failed"
    assert json.loads(ops.last_operator_alert()["channels"])["pager"] is True


def test_a_pager_kind_set_without_its_key_is_a_failed_channel_not_a_silent_skip(db, monkeypatch):
    order = []
    _break_sms_and_email(monkeypatch, order)
    monkeypatch.setenv("OPS_PAGER_KIND", "pagerduty")
    res = ops.page_operator("k1", "Cavnar AI: test", ["x"])
    assert res["sent"] is False and res["channels"]["pager"] is False and "pager" in res["failed"]
    assert "OPS_PAGER_ROUTING_KEY" in (res["error"] or "")


def test_the_pager_is_tried_even_when_the_sms_module_cannot_load(db, monkeypatch):
    import builtins
    import requests
    real_import = builtins.__import__
    monkeypatch.setenv("WILL_PHONE", "+15555550100")
    monkeypatch.setenv("OPS_PAGER_WEBHOOK_URL", "https://pager.test/hook")

    def fake_import(name, *a, **k):
        if name == "notify":
            raise ImportError("notify is broken in this build")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    class _R:
        status_code = 200
    monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None: _R())
    import emails
    monkeypatch.setattr(emails, "_send_branded", lambda *a, **k: emails.SendResult(ok=False, error="down"))
    got = ops.alert_will("Cavnar AI: test", ["x"])
    assert bool(got) and got.channels["pager"] is True and got.channels["sms"] is False


# ── A07-11: a provisional night is named as such ────────────────────────────

def test_a_night_still_provisional_is_said_apart_from_a_missing_one(db, monkeypatch):
    from dsr import pipeline
    _beat(db)
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(pipeline, "nights_missing", lambda db_path=None: [
        {"restaurant_id": 5, "restaurant": "Simple EJ's", "business_date": "2026-10-25", "status": "provisional"},
        {"restaurant_id": 6, "restaurant": "Halsted", "business_date": "2026-10-25", "status": "missing"}])
    sent = []
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: sent.append(lines) or True)
    out = ops.check_platform_sla(send=True)
    assert out["dsr_missing_paged"]
    line = next(x for x in sent[0] if "Daily Sales Report" in x)
    assert "1 missing, 1 still provisional" in line
