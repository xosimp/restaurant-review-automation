"""Every documented rollback / kill switch's OFF state runs (release-safety
audit 10/26/26, A04-11).

During an incident ops sets one of these and restarts; a fallback no test ran
can have rotted, and the rollback becomes a second outage. ROLLBACK_SWITCHES
is read from the "Rollback and kill switches" table in docs/ops/ENVIRONMENT.md
(tests/test_docs_controls.py holds that table to the entries above it), and
each switch has one test here that sets the off value — monkeypatch.setattr on
the module constant when the code reads it at import, monkeypatch.setenv when
it reads it at call time — and checks what the fallback path observably does.
"""
import importlib.util
import json
import logging
import os
import re
import sqlite3
import sys

import pytest

import models

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_DOC = os.path.join(ROOT, "docs", "ops", "ENVIRONMENT.md")
SECTION = "## Rollback and kill switches"


def rollback_switches():
    """[(variable, off value)] from the ENVIRONMENT.md table, in order."""
    text = open(ENV_DOC, encoding="utf-8").read()
    start = text.index(SECTION)
    end = text.find("\n## ", start + len(SECTION))
    body = text[start:end if end != -1 else len(text)]
    out = []
    for line in body.splitlines():
        m = re.match(r"\|\s*`([A-Z][A-Z0-9_]+)`\s*\|\s*([^|]+?)\s*\|", line)
        if m:
            out.append((m.group(1), m.group(2).strip("` ")))
    return out


ROLLBACK_SWITCHES = [name for name, _off in rollback_switches()]


@pytest.fixture
def db(db_path, monkeypatch):
    """Every bare get_conn() — models' and any module's bound copy — on this
    test's database."""
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


def _restaurant(db_path, **kw):
    return models.create_restaurant(models.Restaurant(name=kw.pop("name", "Rollback Co"), owner_email="r@x.test",
                                                      timezone="America/Chicago", **kw), db_path=db_path)


# ── the switches, one off-state test each ────────────────────────────────────

def off_AI_LANE_ENABLED(monkeypatch, db_path):
    """0: a turn the client asks to poll streams on the request, as before —
    server-sent events ending in the answer, never a turn id."""
    from flask import Flask
    import ai_lane
    import ai_utils
    import ask_cavnar
    import auth
    import client_api
    import mobile_api
    monkeypatch.setenv("AI_LANE_ENABLED", "0")
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: False)
    started = []
    monkeypatch.setattr(ai_lane, "start", lambda *a, **k: started.append(a) or pytest.fail("lane used"))
    monkeypatch.setattr(ask_cavnar, "ask_with_tools", lambda *a, **k: ("Labor ran 29% last week.", False, [], {}))
    rid = _restaurant(db_path)
    owner = {"id": 7, "restaurant_id": rid, "role": "client", "is_admin": 0, "username": "o", "email": "o@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: owner)
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: owner, raising=False)
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    r = app.test_client().post("/mobile/api/ask-cavnar/stream", json={"question": "labor?", "poll": True},
                               headers={"Authorization": "Bearer t"})
    assert r.mimetype == "text/event-stream"
    events = [json.loads(line[6:]) for line in r.get_data(as_text=True).splitlines() if line.startswith("data: ")]
    assert [e["answer"] for e in events if e["type"] == "answer"] == ["Labor ran 29% last week."]
    assert started == []


def off_LIVE_SALES_INCREMENTAL(monkeypatch, db_path):
    """0: every read re-reads the whole day (getbybusinessdate) and keeps no
    line set."""
    import live_sales
    import pos_io
    import rpower
    monkeypatch.setattr(live_sales, "INCREMENTAL", False)
    monkeypatch.setattr(rpower, "_ctx", lambda rid: ("tok", {"store": "s"}))
    monkeypatch.setattr(rpower, "sales_types", lambda rid: {"1": {"impacts_sales": 1}})
    asked = []

    def paged(token, path, params, max_pages=200, verify=None):
        asked.append(path)
        return [{"rid": "t1", "atom": 1, "sales": 4.0, "time_stamp": "2026-10-21 18:00:00",
                 "date": "2026-10-21T00:00:00", "slstype_mid": "1", "voided": 0}]
    monkeypatch.setattr(rpower, "_paged", paged)
    monkeypatch.setattr(pos_io, "snapshot_get", lambda *a, **k: pytest.fail("read a kept line set"))
    monkeypatch.setattr(pos_io, "snapshot_put", lambda *a, **k: pytest.fail("kept a line set"))
    first = live_sales.fetch_today(1, "2026-10-21", now=1_700_000_000)
    second = live_sales.fetch_today(1, "2026-10-21", now=1_700_000_900)
    assert (first["mode"], second["mode"]) == ("full", "full")
    assert first["net"] == second["net"] == 4.0
    assert asked == ["ticketsales/getbybusinessdate"] * 2


def off_LIVE_LABOR_QUEUE(monkeypatch, db_path):
    """0: each due store is read on the slot pool here, no live_labor row."""
    import job_queue
    import labor_live
    monkeypatch.setattr(labor_live, "LIVE_QUEUE", False)
    monkeypatch.setattr(job_queue, "enqueue_many", lambda *a, **k: pytest.fail("queued live_labor rows"))
    rid = _restaurant(db_path, module_labor=1)
    r = models.get_restaurant(rid, db_path=db_path)
    monkeypatch.setattr(labor_live, "_due_store", lambda r, st, lock, db_path=None: ("local", "p", 15))
    ran = []
    monkeypatch.setattr(labor_live, "run_for", lambda r, now_local=None, db_path=None: ran.append(r.id) or
                        {"offered": False, "acted": False})
    out = labor_live.run_live_labor(db_path=db_path, restaurants=[r])
    assert ran == [rid]
    assert out["attempted"] == 1 and out["failed"] == 0


def off_POS_SYNC_QUEUE(monkeypatch, db_path):
    """0: the nightly POS sync runs the old sweep (pos.sync_all); its
    followers do not wait for queue rows."""
    import pos
    import scheduler
    monkeypatch.setattr(pos, "POS_SYNC_QUEUE", False)
    swept = []
    monkeypatch.setattr(pos, "sync_all", lambda stats=None: swept.append(1) or [{"ok": True}])
    monkeypatch.setattr(pos, "queue_nightly_syncs", lambda *a, **k: pytest.fail("queued pos_sync rows"))
    monkeypatch.setattr(pos, "open_sync_rows", lambda *a, **k: pytest.fail("waited for queue rows"))
    monkeypatch.setattr(scheduler, "run_inventory_sync", lambda: None)
    scheduler.run_toast_sync()
    assert swept == [1]

    class _Ops:
        def settled(self, name, period):
            return True
    from datetime import datetime
    assert scheduler._pos_sync_settled(_Ops(), datetime(2026, 10, 21, 3, 5), datetime(2026, 10, 21).date())


def _gunicorn_conf():
    spec = importlib.util.spec_from_file_location("gunicorn_conf_rollback", os.path.join(ROOT, "gunicorn.conf.py"))
    conf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conf)
    return conf


def _when_ready_threads(monkeypatch):
    """The thread names gunicorn's when_ready would start (nothing runs)."""
    conf = _gunicorn_conf()
    started = []

    class _Thread:
        def __init__(self, target=None, args=(), daemon=None, name=None):
            self.name = name

        def start(self):
            started.append(self.name)
    monkeypatch.setattr(conf.threading, "Thread", _Thread)
    monkeypatch.delenv("SCHEDULER_PROCESS_SUPERVISED", raising=False)
    monkeypatch.delenv("QUEUE_WORKER_SUPERVISED", raising=False)

    class _Server:
        log = logging.getLogger("rollback-switches")
    conf.when_ready(_Server())
    return started


def off_QUEUE_WORKER(monkeypatch, db_path):
    """0: the web process consumes its own queue — even beside a live worker
    heartbeat — and gunicorn starts no queue worker."""
    import job_queue
    monkeypatch.setenv("QUEUE_WORKER", "0")
    monkeypatch.delenv("SCHEDULER_PROCESS", raising=False)
    monkeypatch.setattr(job_queue, "worker_alive", lambda db_path=None: True)
    assert job_queue.mode() == "inprocess"
    assert job_queue.worker_process_wanted() is False
    assert job_queue.this_process_consumes() is True
    assert _when_ready_threads(monkeypatch) == []
    assert "QUEUE_WORKER_SUPERVISED" not in os.environ


def off_SCHEDULER_PROCESS(monkeypatch, db_path):
    """Unset: gunicorn starts no scheduler process (the web process runs the
    scheduler thread, hosted_dashboard's default)."""
    monkeypatch.delenv("SCHEDULER_PROCESS", raising=False)
    monkeypatch.setenv("QUEUE_WORKER", "0")
    assert _gunicorn_conf().scheduler_process_wanted() is False
    assert "scheduler-process-supervisor" not in _when_ready_threads(monkeypatch)
    assert "SCHEDULER_PROCESS_SUPERVISED" not in os.environ


def off_CAVNAR_CONN_POOL(monkeypatch, db_path):
    """0: every connection is opened and closed for real — no pooled handle
    is reused, and a closed one is closed."""
    if os.getenv("CAVNAR_TEST_PG_URL"):
        pytest.skip("reads the SQLite file itself: CAVNAR_CONN_POOL is the SQLite connection pool")
    monkeypatch.setenv("CAVNAR_CONN_POOL", "0")
    path = os.path.join(os.path.dirname(db_path), "pool_off.db")
    a = models.get_conn(path)
    assert isinstance(a, sqlite3.Connection)
    a.execute("CREATE TABLE t (v)")
    a.execute("INSERT INTO t VALUES (1)")
    a.commit()
    a.close()
    with pytest.raises(sqlite3.ProgrammingError):
        a.execute("SELECT 1")
    b = models.get_conn(path)
    try:
        assert b is not a and b.execute("SELECT v FROM t").fetchone()[0] == 1
    finally:
        b.close()


def off_DB_BACKEND(monkeypatch, db_path):
    """sqlite: the platform's own database stays SQLite although DATABASE_URL
    names a Postgres (an address nothing listens on — never connected)."""
    import db_dialect
    monkeypatch.setenv("DATABASE_URL", "postgresql://rollback@127.0.0.1:1/never")
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setattr(models, "_PG_ROUTES", {})
    path = os.path.join(os.path.dirname(db_path), "backend_off.db")
    monkeypatch.setattr(models, "DB_PATH", path)
    assert models._db_backend() == "sqlite" and db_dialect.backend() == "sqlite"
    assert models._pg_url_for(path) is None
    c = models.get_conn()
    try:
        assert c.execute("SELECT 1").fetchone()[0] == 1
    finally:
        c.close()


def off_RESTORE_GUARD(monkeypatch, db_path):
    """0: the guard is off even on Railway with a freeze on record — the
    scheduler tick is never held."""
    import config
    import restore_guard
    import scheduler
    monkeypatch.setattr(config, "on_railway", lambda: True)
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.dirname(db_path))
    restore_guard._write_json(restore_guard.freeze_path(), {"detected_at": "2026-10-21 03:00:00", "db_seq": 1,
                                                            "volume_seq": 9, "db_id": "x"})
    try:
        monkeypatch.setenv("RESTORE_GUARD", "1")
        assert scheduler._restore_guard_holds() is True        # the guard, on, holds
        monkeypatch.setenv("RESTORE_GUARD", "0")
        assert restore_guard.active() is False
        assert restore_guard.check_and_stamp() == {"state": "off"}
        assert scheduler._restore_guard_holds() is False
    finally:
        os.remove(restore_guard.freeze_path())


def off_AI_BATCHES_ENABLED(monkeypatch, db_path):
    """0: no workflow may batch — every batchable call takes the synchronous
    path — although the host may submit and every workflow is listed."""
    import ai_batches
    monkeypatch.setattr(ai_batches, "_scheduling_allowed", lambda: True)
    monkeypatch.delenv("AI_BATCHES_WORKFLOWS", raising=False)
    monkeypatch.delenv("AI_BATCHES_ENABLED", raising=False)
    listed = sorted(ai_batches.workflows())
    assert any(ai_batches.enabled(w) for w in listed)
    monkeypatch.setenv("AI_BATCHES_ENABLED", "0")
    assert [w for w in listed if ai_batches.enabled(w)] == []


def off_AI_BATCH_CANCEL_GRACE_SECONDS(monkeypatch, db_path):
    """0: a batch whose items are all past their cutoff is not cancelled —
    its late items fall back at once."""
    import ai_batches
    monkeypatch.setattr(ai_batches, "_scheduling_allowed", lambda: True)
    cancelled = []

    class _Client:
        class messages:
            class batches:
                @staticmethod
                def cancel(batch_id):
                    cancelled.append(batch_id)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO ai_batch_jobs (batch_id, workflow) VALUES ('b-off', 'dsr_narrative')")
        conn.execute("INSERT INTO ai_batch_jobs (batch_id, workflow) VALUES ('b-on', 'dsr_narrative')")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(ai_batches, "CANCEL_GRACE_SECONDS", 600)
    assert ai_batches._cancel_for_cutoffs("b-on", _Client, {}) is True     # on: cancel first
    monkeypatch.setattr(ai_batches, "CANCEL_GRACE_SECONDS", 0)
    out = {}
    assert ai_batches._cancel_for_cutoffs("b-off", _Client, out) is False
    assert cancelled == ["b-on"] and out == {}


def off_AI_WORKFLOW_OFF(monkeypatch, db_path):
    """A workflow named: no model call, a run recorded `off`, and an
    AIRefused its caller already serves a fallback for."""
    import ai_orchestrator as orch
    import ai_utils
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 0.0)
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: None)
    monkeypatch.setenv("AI_SHADOW_REVIEW", "0")
    monkeypatch.setenv("AI_WORKFLOW_OFF", "labor_insight")
    with pytest.raises(ai_utils.AIRefused) as e:
        orch.generate("labor_insight", 7, attempt=lambda r, n: pytest.fail("called a switched-off workflow"))
    assert isinstance(e.value, orch.WorkflowOff)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT status FROM ai_runs WHERE workflow='labor_insight'").fetchone()[0] == "off"
    finally:
        conn.close()


def off_SCHEDULE_CPSAT_ISOLATE(monkeypatch, db_path):
    """0: a solve big enough to isolate runs here; no solver process starts."""
    import schedule_cpsat as cps
    from ortools.sat.python import cp_model as cp
    monkeypatch.setenv("SCHEDULE_CPSAT_ISOLATE", "0")
    monkeypatch.setattr(cps._rt, "ISOLATE_MIN_VARS", 1)
    monkeypatch.setattr(cps._rt, "_start_process", lambda: pytest.fail("started a solver process"))
    m = cp.CpModel()
    xs = [m.new_bool_var(f"x{i}") for i in range(30)]
    m.add(sum(xs) == 10)
    m.minimize(sum((i % 7 + 1) * x for i, x in enumerate(xs)))
    s = cps._solver_for(5.0, workers=2, deterministic=True)
    assert cps.run(s, m) == cps._cp.OPTIMAL
    assert s.sp_info["isolated"] is False and not s.sp_info["fallback"]


def off_SCHEDULE_MANAGERS_IN_SOLVE(monkeypatch, db_path):
    """0: the solver-first draft is handed the manager plan's rows pinned and
    no manager slots to choose (manager_slots is never asked)."""
    import schedule_engine as se
    import schedule_fallback as sfb
    plan_row = {"date": "2026-11-02", "employee": "Erik", "role": "Manager", "shift_start": "09:00",
                "shift_end": "17:00"}
    fallback = {"restaurant_id": 1, "constraints": None, "manager_plan": {"rows": [plan_row]},
                "plan_rows": [plan_row], "roster_roles": {"Ana": "Server"}, "week_dates": ["2026-11-02"]}
    monkeypatch.setattr(sfb, "code_order", lambda chosen, has_requirements=False: ["solver"])
    monkeypatch.setattr(se, "_quality_signals", lambda *a, **k: (None, None))
    asked, handed = [], []
    monkeypatch.setattr(se, "manager_slots", lambda *a, **k: asked.append(1) or ([], [], []))

    def solver_first(rid, dates, facts, c, base, hist, plan, signals, weights, roles, secs, shifts, fb,
                     manager_slots=None):
        handed.append((list(plan), list(manager_slots or [])))
        return {"rows": [{"date": "2026-11-02", "employee": "Ana", "role": "Server", "shift_start": "10:00",
                          "shift_end": "16:00"}], "unassigned": [], "stats": {}}
    monkeypatch.setattr(se, "_solver_first_rows", solver_first)
    monkeypatch.setattr(se, "MANAGERS_IN_SOLVE", False)
    out = se._code_draft(["2026-11-02"], {}, fallback, {}, [], "solver")
    assert asked == []
    assert handed == [([plan_row], [])]
    assert out is not None and [r["employee"] for r in out["rows"]] == ["Ana"]


def off_STUDIO_OFFLOAD(monkeypatch, db_path):
    """0: a Studio read that would go to the worker — slow at this
    restaurant, the client polling, a live worker — is answered inline."""
    from flask import Flask
    import job_queue
    import studio_offload
    monkeypatch.setitem(studio_offload._REGISTRY, "publish_check", lambda u, **k: ({"ok": True}, 200))
    monkeypatch.setattr(job_queue, "worker_alive", lambda db_path=None: True)
    monkeypatch.delenv("QUEUE_WORKER", raising=False)
    monkeypatch.setitem(studio_offload._COST, ("publish_check", 1), 30.0)
    app = Flask(__name__)
    with app.test_request_context("/mobile/api/labor/publish-check", headers={studio_offload.OFFLOAD_HEADER: "1"}):
        monkeypatch.delenv("STUDIO_OFFLOAD", raising=False)
        assert studio_offload.wanted("publish_check", 1) is True          # on: offloaded
        monkeypatch.setenv("STUDIO_OFFLOAD", "0")
        assert studio_offload.wanted("publish_check", 1) is False


def off_HOME_SNAPSHOTS(monkeypatch, db_path):
    """0: every Home read is built on the request and nothing is kept."""
    import home_snapshots
    monkeypatch.setenv("HOME_SNAPSHOTS", "0")
    rid = _restaurant(db_path)
    builds = []
    for _ in range(2):
        got = home_snapshots.serve("good_news", rid, "d:-:s0", "2026-10-21",
                                   lambda: builds.append(1) or {"items": [1]}, db_path=db_path)
        assert got == {"items": [1]}
    assert builds == [1, 1]
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM home_snapshots").fetchone()[0] == 0
    finally:
        conn.close()


def off_HOME_SNAPSHOT_COLD(monkeypatch, db_path):
    """request: a block not kept for today is built on the request (and
    kept), never queued with a `preparing` answer."""
    from flask import Flask
    import home_snapshots
    import job_queue
    monkeypatch.setenv("HOME_SNAPSHOTS", "1")
    monkeypatch.setenv("HOME_SNAPSHOT_COLD", "request")
    monkeypatch.setattr(job_queue, "enqueue", lambda *a, **k: pytest.fail("queued a cold build"))
    rid = _restaurant(db_path)
    with Flask(__name__).test_request_context("/api/good-news"):
        got = home_snapshots.serve("good_news", rid, "d:-:s0", "2026-10-21", lambda: {"items": ["built"]},
                                   db_path=db_path, viewer={"id": 5})
    assert got == {"items": ["built"]}
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM home_snapshots WHERE restaurant_id=?", (rid,)).fetchone()[0] == 1
    finally:
        conn.close()


def off_SILENT_PUSH_JITTER_SECONDS(monkeypatch, db_path):
    """0: the fleet-wide `dsr` wake is sent at once, not queued."""
    import job_queue
    import push
    from tests.test_live_activities import _device, _login
    from tests.test_live_activities import _restaurant as _live_restaurant
    monkeypatch.setenv("SILENT_PUSH_JITTER_SECONDS", "0")
    queued, sent = [], []
    monkeypatch.setattr(job_queue, "enqueue", lambda *a, **k: queued.append(a) or (1, True))
    monkeypatch.setattr(push, "native_push_allowed", lambda: True)
    monkeypatch.setattr(push, "_submit_native", lambda fn, *args: (fn(*args), True)[1])
    monkeypatch.setattr(push, "_deliver_silent", lambda token, rid, reason, db_path=None: sent.append(reason))
    assert push.silent_offset(7, "dsr") == 0
    rid = _live_restaurant(db_path)
    uid, _t, _h = _login(db_path, rid)
    _device(db_path, uid, rid)
    assert push.fire_silent(rid, "dsr", db_path=db_path) == 1
    assert sent == ["dsr"] and queued == []


OFF_TESTS = {name[len("off_"):]: fn for name, fn in dict(globals()).items() if name.startswith("off_")}


def test_the_switch_table_is_read():
    assert len(ROLLBACK_SWITCHES) >= 18, ROLLBACK_SWITCHES
    assert {"AI_LANE_ENABLED", "LIVE_SALES_INCREMENTAL"} <= set(ROLLBACK_SWITCHES)


def test_every_switch_has_an_off_test_and_every_off_test_a_switch():
    assert sorted(set(ROLLBACK_SWITCHES) - set(OFF_TESTS)) == [], "a switch in the table with no off test"
    assert sorted(set(OFF_TESTS) - set(ROLLBACK_SWITCHES)) == [], "an off test for a switch not in the table"


@pytest.mark.parametrize("switch", ROLLBACK_SWITCHES)
def test_the_off_state_runs(switch, monkeypatch, db):
    OFF_TESTS[switch](monkeypatch, db)
