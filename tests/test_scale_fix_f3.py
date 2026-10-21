"""Scale fix round 10/19/26, workstream F3 — background jobs, infrastructure
and observability (scale audit A1 / A8):

  INF-4   a recycled queue worker waits for a web boot since the CONTAINER
          started, not since its own start (it waited forever and the web
          process silently took the queue); the web row is kept alive; the
          platform check warns when the worker goes silent;
  A5-F3   only generations count toward the recycle, and the replacement
  INF-17  starts when the old worker stops claiming, not when it exits;
  INF-6   the POS archive in its own pos_io group, nightly rows with a
          deadline, a backlog warning;
  INF-7   a job its bound cuts short every run warns (coverage), and the
          queue's per-kind stats;
  INF-18  no DDL on the lease's per-call path;
  INF-10  the morning briefs side by side;
  INF-13  the scheduler as its own process (SCHEDULER_PROCESS=1, off);
  OPS-5   Sentry in every process;
  OPS-7   5xx rate and latency paged from the request rollups;
  OPS-9   per-restaurant request load;
  OPS-13  print() as log records on Railway, and a ratchet on new prints.
(The DSR sweep's workers and lane — INF-1/INF-2/A4-06 — are in
tests/test_dsr_pipeline.py.)"""
import ast
import importlib.util
import inspect
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time

import pytest

import models

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    import job_queue
    job_queue.reset_for_tests()
    monkeypatch.delenv("QUEUE_WORKER", raising=False)
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    return job_queue


def _sql(sql, args=()):
    conn = models.get_conn()
    try:
        out = conn.execute(sql, args).fetchall()
        conn.commit()
        return out
    finally:
        conn.close()


def _ok_handler(job):
    return {"ok": True}


def _quiet_boot(monkeypatch):
    import logging_setup
    monkeypatch.setattr(logging_setup, "configure", lambda *a, **k: None)
    monkeypatch.setattr(models, "require_volume", lambda *a, **k: None)


# ── INF-4: the recycled queue worker ────────────────────────────────────────

def _web_booted_ago(jq, seconds):
    jq.heartbeat("web")
    _sql("UPDATE job_workers SET started_at=? WHERE role='web'", (time.time() - seconds,))


def test_a_recycled_queue_worker_starts_again_at_once(db, monkeypatch):
    """The trace that bit: the web process booted an hour ago, the worker
    recycles after its generations, and the supervisor starts a fresh one.
    The fresh one must consume at once — it waited for a web boot newer than
    its own start, which never came, exited 1 every 600 s, and the web
    process ran the whole queue."""
    jq = db
    import worker
    _quiet_boot(monkeypatch)
    monkeypatch.setenv("QUEUE_WORKER_BOOT_SINCE", str(time.time() - 7200))   # the container's start
    monkeypatch.delenv("QUEUE_WORKER_HANDOFF_FD", raising=False)
    _web_booted_ago(jq, 3600)
    monkeypatch.setitem(jq.HANDLERS, "schedule_generate", f"{__name__}:_ok_handler")
    monkeypatch.setattr(worker, "QUEUE_WORKER_MAX_JOBS", 1)
    monkeypatch.setattr(worker, "BOOT_WAIT_SECONDS", 3)
    jq.enqueue("schedule_generate", {"i": 1}, restaurant_id=None, wake=False)

    began = time.time()
    assert worker.main_queue() == 0                       # ran its generation and recycled
    assert [r[0] for r in _sql("SELECT state FROM jobs")] == ["done"]

    calls = []
    monkeypatch.setattr(jq, "run_worker", lambda **k: calls.append(k) or "stopped")
    assert worker.main_queue() == 0                       # the supervisor's fresh process
    assert calls, "the fresh worker never got past the boot wait"
    assert time.time() - began < 3, "it waited for a web boot that had already happened"
    assert calls[0]["count_groups"] == ("generation",) and calls[0]["on_recycle"] is worker._announce_draining


def test_a_queue_worker_still_waits_for_its_own_containers_web_boot(db, monkeypatch):
    """The boot wait keeps its job: a web row from before this container
    started (the previous deployment's) is not this container's migrated
    schema."""
    jq = db
    import worker
    _quiet_boot(monkeypatch)
    _web_booted_ago(jq, 3600)
    monkeypatch.setenv("QUEUE_WORKER_BOOT_SINCE", str(time.time() - 60))
    monkeypatch.setattr(worker, "BOOT_WAIT_SECONDS", 0.5)
    calls = []
    monkeypatch.setattr(jq, "run_worker", lambda **k: calls.append(k) or "stopped")
    assert worker.main_queue() == 1 and calls == []
    monkeypatch.setenv("QUEUE_WORKER_BOOT_SINCE", "50")
    assert worker._boot_since(100.0) == 50.0
    monkeypatch.delenv("QUEUE_WORKER_BOOT_SINCE")
    assert worker._boot_since(100.0) == 100.0              # run some other way: its own start, as before


def test_the_web_row_is_beaten_every_minute_while_the_worker_consumes(db, monkeypatch):
    """The web row was beaten only when the web process consumed; a day
    later a worker heartbeat pruned it and no recycled worker could start."""
    jq = db
    jq.heartbeat("web")
    _sql("UPDATE job_workers SET heartbeat_at=? WHERE role='web'", (time.time() - 80000,))
    monkeypatch.setattr(jq, "this_process_consumes", lambda *a, **k: False)
    assert jq.tick()["skipped"] == 1
    beat = _sql("SELECT heartbeat_at FROM job_workers WHERE role='web'")[0][0]
    assert time.time() - float(beat) < 5


def test_the_supervisor_tells_each_worker_when_the_container_started():
    src = open(os.path.join(ROOT, "gunicorn.conf.py")).read()
    assert "QUEUE_WORKER_BOOT_SINCE" in src and "_MASTER_STARTED = time.time()" in src
    assert 'os.environ["QUEUE_WORKER_SUPERVISED"] = "1"' in src


def test_the_platform_check_warns_when_the_queue_worker_goes_silent():
    import ops
    silent = {"worker_heartbeat_age_seconds": 900.0, "web_started_age_seconds": 7200.0, "groups": {}}
    kinds = [k for k, _ in ops.queue_problems(silent, worker_expected=True)]
    assert kinds == ["queue_worker"]
    assert ops.queue_problems(silent, worker_expected=False) == []      # QUEUE_WORKER=0, or a laptop
    never = {"worker_heartbeat_age_seconds": None, "web_started_age_seconds": 7200.0, "groups": {}}
    assert [k for k, _ in ops.queue_problems(never, worker_expected=True)] == ["queue_worker"]
    booting = {"worker_heartbeat_age_seconds": None, "web_started_age_seconds": 30.0, "groups": {}}
    assert ops.queue_problems(booting, worker_expected=True) == []
    alive = {"worker_heartbeat_age_seconds": 10.0, "web_started_age_seconds": 7200.0, "groups": {}}
    assert ops.queue_problems(alive, worker_expected=True) == []


def test_queue_stats_say_how_long_the_worker_has_been_silent(db):
    jq = db
    now = time.time()
    _sql("INSERT INTO job_workers (worker_id, role, pid, host, started_at, heartbeat_at, draining) "
         "VALUES ('other-worker', 'worker', 1, 'h', ?, ?, 0)", (now - 3600, now - 600))
    jq.heartbeat("web")
    st = jq.stats()
    assert 590 < st["worker_heartbeat_age_seconds"] < 700
    assert st["web_started_age_seconds"] is not None


# ── A5-F3 / INF-17: recycle granularity and the hand-off ────────────────────

def test_bulk_rows_do_not_count_toward_the_recycle(db, monkeypatch):
    jq = db
    monkeypatch.setitem(jq.HANDLERS, "learner", f"{__name__}:_ok_handler")
    for i in range(3):
        jq.enqueue("learner", {"i": i}, wake=False)
    stop = threading.Event()
    told = []
    t = threading.Thread(target=lambda: told.append(jq.run_worker(
        groups=("bulk",), stop=stop, threads=1, max_jobs=2, count_groups=("generation",),
        on_recycle=lambda: told.append("draining"))), daemon=True)
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline and _sql("SELECT COUNT(*) FROM jobs WHERE state='done'")[0][0] < 3:
        time.sleep(0.05)
    stop.set()
    t.join(10)
    assert _sql("SELECT COUNT(*) FROM jobs WHERE state='done'")[0][0] == 3
    assert told == ["stopped"], "three bulk rows must not recycle a worker counting generations"


def test_a_recycle_tells_the_supervisor_once(db, monkeypatch):
    jq = db
    monkeypatch.setitem(jq.HANDLERS, "schedule_generate", f"{__name__}:_ok_handler")
    for i in range(2):
        jq.enqueue("schedule_generate", {"i": i}, restaurant_id=None, wake=False)
    told = []
    out = jq.run_worker(groups=("generation",), threads=1, max_jobs=1, count_groups=("generation",),
                        on_recycle=lambda: told.append(1))
    assert out == "recycle" and told == [1]


def test_the_supervisor_starts_the_replacement_while_the_old_worker_drains(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("gunicorn_conf_f3", os.path.join(ROOT, "gunicorn.conf.py"))
    conf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conf)
    trace = tmp_path / "trace.txt"
    script = (
        "import os, sys, time\n"
        "p = os.environ['F3_TRACE']\n"
        "first = not os.path.exists(p)\n"
        "open(p, 'a').write('start %s\\n' % time.time())\n"
        "if first:\n"
        "    fd = int(os.environ['QUEUE_WORKER_HANDOFF_FD'])\n"
        "    os.write(fd, b'draining\\n'); os.close(fd)\n"
        "    time.sleep(1.5)\n"
        "    open(p, 'a').write('end %s\\n' % time.time())\n"
        "    sys.exit(0)\n"
        "time.sleep(30)\n")
    monkeypatch.setenv("F3_TRACE", str(trace))
    monkeypatch.setattr(conf, "_queue_command", lambda: [sys.executable, "-c", script])
    log = logging.getLogger("f3-supervisor")
    t = threading.Thread(target=conf._supervise, args=(log,), daemon=True)
    t.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            lines = trace.read_text().splitlines() if trace.exists() else []
            if sum(1 for x in lines if x.startswith("start")) >= 2:
                break
            time.sleep(0.05)
        lines = trace.read_text().splitlines()
        starts = [float(x.split()[1]) for x in lines if x.startswith("start")]
        ends = [float(x.split()[1]) for x in lines if x.startswith("end")]
        assert len(starts) >= 2, lines
        assert not ends or starts[1] < ends[0], "the replacement waited for the draining worker to exit"
    finally:
        conf.on_exit(None)
        t.join(10)


# ── INF-6: the pos_io group, deadlines, the backlog ─────────────────────────

def test_the_pos_archive_has_its_own_group_and_slots(db, monkeypatch):
    jq = db
    assert jq.KINDS["pos_archive"][0] == "pos_io" and "pos_io" in jq.GROUPS
    monkeypatch.delenv("QUEUE_POS_IO_CONCURRENCY", raising=False)
    # Sized from the RPOWER budget since audit #23 (A4-11): never under 4.
    import pos_capacity
    assert jq.group_concurrency("pos_io") == pos_capacity.pos_io_concurrency() >= 4
    assert jq.group_concurrency("bulk") == 2
    import inspect as _i
    assert _i.signature(jq.run_worker).parameters["groups"].default == jq.GROUPS


def test_nightly_queue_rows_carry_a_deadline(db, monkeypatch):
    import strategy_jobs
    rid = models.create_restaurant(models.Restaurant(name="Learner Co", owner_email="l@x.example"))
    models.update_restaurant(rid, {"module_labor": 1})
    out = strategy_jobs.fan_out_learner("schedule_outcomes", today="2026-10-19")
    assert out["attempted"] >= 1
    rows = _sql("SELECT deadline, enqueued_at FROM jobs WHERE kind='learner'")
    assert rows and all(r[0] and 20 * 3600 < float(r[0]) - float(r[1]) < 23 * 3600 for r in rows)
    src = inspect.getsource(strategy_jobs.run_pos_archive) + inspect.getsource(strategy_jobs.fan_out)
    assert src.count('"deadline": _bulk_deadline(') == 2


def test_a_bulk_backlog_warns(db):
    jq = db
    import ops
    jq.enqueue("learner", {"i": 1}, wake=False)
    _sql("UPDATE jobs SET enqueued_at=?", (time.time() - 8 * 3600,))
    st = jq.stats()
    assert st["groups"]["bulk"]["oldest_ready_seconds"] > 7 * 3600
    assert st["kinds"]["learner"]["queued"] == 1
    assert [k for k, _ in ops.queue_problems(st, worker_expected=False)] == ["queue_backlog"]
    # A row spread to later is not a backlog.
    _sql("UPDATE jobs SET not_before=?", (time.time() + 3600,))
    assert ops.queue_problems(jq.stats(), worker_expected=False) == []


# ── INF-7: coverage ─────────────────────────────────────────────────────────

def test_a_job_its_bound_cuts_short_every_run_warns(db):
    import ops
    for _ in range(3):
        ops.run_job("pos_sync", lambda: {"attempted": 700, "ok": 700, "failed": 0, "hit_bound": True,
                                         "eligible": 1500})
    capped = ops.jobs_bound_capped(use_cache=False)
    assert [c["job"] for c in capped] == ["pos_sync"] and capped[0]["eligible"] == 1500
    ops.run_job("pos_sync", lambda: {"attempted": 1500, "ok": 1500, "failed": 0, "hit_bound": False})
    assert ops.jobs_bound_capped(use_cache=False) == []


# ── INF-18: the lease ───────────────────────────────────────────────────────

def test_the_lease_runs_no_ddl_per_call_and_survives_a_missing_row(db):
    import ops
    src = inspect.getsource(ops.acquire_scheduler_lease)
    assert "CREATE TABLE" not in src and "_LEASE_SQL" not in src
    assert ops.acquire_scheduler_lease(owner="f3-a") is True
    _sql("DELETE FROM scheduler_lease")
    ops.acquire_scheduler_lease(owner="f3-a")              # makes the row back
    assert ops.acquire_scheduler_lease(owner="f3-a") is True


# ── INF-10: the morning briefs side by side ─────────────────────────────────

def test_the_morning_briefs_run_side_by_side_and_keep_their_cursor(db, monkeypatch):
    import morning_brief
    import ops
    rids = [models.create_restaurant(models.Restaurant(name=f"Brief {i}", owner_email=f"b{i}@x.example"))
            for i in range(4)]
    due = [(rid, "2026-10-19") for rid in rids]
    monkeypatch.setattr(morning_brief, "_brief_queue", lambda db_path=None: (list(due), []))
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def deliver(rid, restaurant=None, today=None, db_path=None):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.2)
        with lock:
            active["now"] -= 1
        return {"sent": 1}
    monkeypatch.setattr(morning_brief, "deliver", deliver)
    monkeypatch.setattr(morning_brief, "BRIEF_WORKERS", 4)
    began = time.time()
    c = morning_brief.run_due()
    assert c["attempted"] == 4 and c["ok"] == 4 and c["sent"] == 4 and c["eligible"] == 4
    assert active["max"] > 1 and time.time() - began < 0.7
    cur = _sql("SELECT value FROM job_cursors WHERE key=?", (morning_brief.BRIEF_CURSOR_KEY,))
    assert int(cur[0][0]) == max(rids)
    # A second pass finds every one claimed: nothing is sent twice.
    assert morning_brief.run_due()["skipped"] == 4
    assert all(ops.period_claimed(f"morning_brief:{rid}", "2026-10-19") for rid in rids)


# ── INF-13: the scheduler as its own process (off by default) ───────────────

def test_the_scheduler_process_is_off_by_default_and_waits_for_the_web_boot(monkeypatch):
    spec = importlib.util.spec_from_file_location("gunicorn_conf_f3b", os.path.join(ROOT, "gunicorn.conf.py"))
    conf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conf)
    monkeypatch.delenv("SCHEDULER_PROCESS", raising=False)
    assert conf.scheduler_process_wanted() is False
    monkeypatch.setenv("SCHEDULER_PROCESS", "1")
    assert conf.scheduler_process_wanted() is True
    import worker
    src = inspect.getsource(worker.main_scheduler)
    assert "_web_booted(since)" in src and "init_db()" not in src and "_run_scheduler()" in src
    hd = open(os.path.join(ROOT, "hosted_dashboard.py")).read()
    assert '_SCHED_DEFAULT = "0" if' in hd


# ── OPS-5: Sentry in every process ──────────────────────────────────────────

def test_sentry_is_initialised_in_the_queue_worker_and_the_scheduler_process(monkeypatch):
    import sentry_setup
    import worker
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert sentry_setup.init("queue-worker") is False
    for fn in (worker.main_queue, worker.main_scheduler, worker.main):
        assert "_init_sentry(" in inspect.getsource(fn), fn.__name__
    hd = open(os.path.join(ROOT, "hosted_dashboard.py")).read()
    assert '_sentry_setup.init("web", integrations=[FlaskIntegration()])' in hd


# ── OPS-7: 5xx rate and latency from the rollups ────────────────────────────

def _rollup(minute_ago, requests, e5=0, slow=0, klass="web_api"):
    import http_layer
    import platform_monitor as pm
    from datetime import datetime, timedelta
    at = (datetime.utcnow() - timedelta(minutes=minute_ago)).strftime("%Y-%m-%d %H:%M:00")
    buckets = [0] * (len(http_layer.LATENCY_BUCKETS_MS) + 1)
    buckets[0] = requests - slow
    buckets[-1] = slow
    pm.persist_request_rollups([{"minute_at": at, "route": "/api/x", "method": "GET", "klass": klass,
                                 "requests": requests, "errors_4xx": 0, "errors_5xx": e5, "total_ms": 1.0,
                                 "max_ms": 1.0, "buckets": buckets}])


def test_a_5xx_rate_pages_and_a_slow_quarter_hour_warns(db):
    import ops
    import platform_monitor as pm
    pm.init_platform_tables(models.DB_PATH)
    _rollup(2, 60, e5=1)
    assert pm.request_slo()["error_rate_over"] is False                   # under 2%
    _rollup(3, 60, e5=5)
    slo = pm.request_slo()
    assert slo["error_rate_over"] is True and slo["requests"] == 120 and slo["errors_5xx"] == 6
    assert ops._has_urgent({"requests": slo}, {}, write_ok=True) is True
    assert slo["latency_over"] is False
    _rollup(8, 150, slow=20)
    _rollup(30, 400, e5=400, klass="admin")                                # the console and old minutes: never
    slo = pm.request_slo()
    assert slo["latency_over"] is True and slo["p95_le_ms"] > pm.HTTP_P95_SLO_MS


# ── OPS-9: per-restaurant load ──────────────────────────────────────────────

def test_the_busiest_restaurants_are_kept_per_hour(db, monkeypatch):
    import http_layer
    import platform_monitor as pm
    pm.init_platform_tables(models.DB_PATH)
    http_layer.reset_metrics()
    rid = models.create_restaurant(models.Restaurant(name="Noisy Co", owner_email="n@x.example"))
    hour_ago = time.time() - 3600
    with http_layer._metrics_lock:
        for _ in range(30):
            http_layer._tenant_add(hour_ago, rid, 120.0, 200)
        http_layer._tenant_add(hour_ago, rid, 900.0, 500)
        http_layer._tenant_add(time.time(), rid, 10.0, 200)              # this hour: still filling
    rows = http_layer.drain_tenant_rollups()
    assert len(rows) == 1 and rows[0]["requests"] == 31 and rows[0]["errors_5xx"] == 1
    assert pm.persist_tenant_rollups(rows) == 1
    load = pm.tenant_load(24)
    top = load["by_requests"][0]
    assert top["restaurant_id"] == rid and top["name"] == "Noisy Co" and top["requests"] == 31
    assert load["by_time"][0]["max_ms"] == 900.0
    assert len(http_layer.drain_tenant_rollups(include_current=True)) == 1
    assert "tenant_request_rollups" in pm.TELEMETRY_RETENTION_DAYS


def test_the_tenant_sample_keeps_the_busiest_by_requests_and_by_time():
    import http_layer
    http_layer.reset_metrics()
    hour_ago = time.time() - 3600
    with http_layer._metrics_lock:
        for rid in range(1, 11):
            for _ in range(rid):
                http_layer._tenant_add(hour_ago, rid, 10.0, 200)
        http_layer._tenant_add(hour_ago, 99, 60000.0, 200)                # one very slow request
    kept = {r["restaurant_id"] for r in http_layer.drain_tenant_rollups(top=2)}
    assert kept == {10, 9, 99}


# ── OPS-13: print() on Railway, and the ratchet ─────────────────────────────

def test_a_printed_line_becomes_a_log_record_with_a_level():
    import logging_setup
    raw = io.StringIO()
    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append((record.name, record.levelno, record.getMessage()))
    h = _H()
    lg = logging.getLogger("print")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        out = logging_setup.PrintToLog(raw)
        print("Review fetch done for 4 restaurants", file=out)
        print("[pos] sync failed for rid=5", file=out)
        out.write("partial ")
        out.write("line\n")
    finally:
        lg.removeHandler(h)
    assert records == [("print", logging.INFO, "Review fetch done for 4 restaurants"),
                       ("print", logging.WARNING, "[pos] sync failed for rid=5"),
                       ("print", logging.INFO, "partial line")]
    assert raw.getvalue() == ""


# The print() calls in the app's modules when the ratchet was set (scale
# audit 10/19/26 OPS-13: 1,079 by the AST, against ~865 logging calls). On Railway they
# reach the log as records (PrintToLog), but with no logger name of their
# own: new code logs (logging.getLogger(__name__)), and this number only
# goes down — lower it when you convert some.
PRINT_CEILING = 1079


def test_no_new_print_calls_in_the_app_modules():
    files = subprocess.check_output(["git", "ls-files", "*.py"], cwd=ROOT, text=True).split()
    files = [f for f in files if not f.startswith(("tests/", "scripts/", "brand/"))]
    n = 0
    for f in files:
        tree = ast.parse(open(os.path.join(ROOT, f), encoding="utf-8").read())
        n += sum(1 for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "print")
    assert n <= PRINT_CEILING, f"{n} print() calls (ceiling {PRINT_CEILING}): log instead"
