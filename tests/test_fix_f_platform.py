"""Workstream F — what the web process records about itself and shows the
console: request rollups and 5xx (#30, #77), boots and crash loops (#76),
the supervisor thread (#3, #134), the system card (#28, #102, #125) and
status-page incidents (#61).
"""
import json
import sqlite3
import threading
import time

import pytest
from flask import Flask

import http_layer
import models
import ops
import platform_monitor as pm
import status_manager as sm


@pytest.fixture
def db(db_path, monkeypatch):
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(sm, "DB_PATH", db_path)
    sm.seed_default_services()
    pm.init_platform_tables(db_path)
    import provider_health
    provider_health.init_provider_health(db_path)
    monkeypatch.setattr(ops, "backup_status", None, raising=False)
    http_layer.reset_metrics()
    yield db_path
    http_layer.reset_metrics()


def _rows(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _rollup(minute, route="/api/x", klass="web_api", n=10, e5=0, buckets=None, max_inflight=0):
    b = buckets or ([n] + [0] * len(http_layer.LATENCY_BUCKETS_MS))
    return {"minute_at": minute, "route": route, "method": "GET", "klass": klass, "requests": n,
            "errors_4xx": 0, "errors_5xx": e5, "total_ms": 30.0 * n, "max_ms": 80.0, "buckets": b,
            "max_inflight": max_inflight}


def _minute(ago_minutes=5):
    return time.strftime("%Y-%m-%d %H:%M:00", time.gmtime(time.time() - ago_minutes * 60))


# ── request rollups (#77) ───────────────────────────────────────────────────

def test_the_rollup_table_has_a_column_per_latency_bucket(db):
    cols = {r["name"] for r in _rows(db, "PRAGMA table_info(request_rollups)")}
    for c in pm._bucket_columns():
        assert c in cols
    assert len(pm._bucket_columns()) == len(http_layer.LATENCY_BUCKETS_MS) + 1


def test_rollups_persist_add_up_and_summarise_by_route_and_class(db):
    m = _minute(5)
    slow = [0, 0, 0, 0, 0, 1, 0, 0, 0]          # one request in the 2.5 s bucket
    pm.persist_request_rollups([_rollup(m, n=10), _rollup(m, route="/admin/api/overview", klass="admin",
                                                          n=1, buckets=slow)], db)
    pm.persist_request_rollups([_rollup(m, n=5, e5=2)], db)    # the same minute again adds up
    row = _rows(db, "SELECT requests, errors_5xx FROM request_rollups WHERE route='/api/x'")[0]
    assert row == {"requests": 15, "errors_5xx": 2}
    s = pm.request_rollup_summary(24, db)
    routes = {r["route"]: r for r in s["routes"]}
    assert routes["/api/x"]["requests"] == 15 and routes["/api/x"]["errors_5xx"] == 2
    assert routes["/api/x"]["p95_le_ms"] == 50
    assert s["classes"]["admin"]["p95_le_ms"] == 2500
    hour = s["hourly"][-1]
    assert hour["customer"]["requests"] == 15 and hour["admin"]["requests"] == 1, \
        "admin traffic is its own line, never inside the customer p95"


def test_saturated_minutes_are_counted(db, monkeypatch):
    monkeypatch.setattr(http_layer, "worker_threads", lambda: 4)
    pm.persist_request_rollups([_rollup(_minute(3), route="*", klass="*", n=40, max_inflight=4),
                                _rollup(_minute(2), route="*", klass="*", n=10, max_inflight=2)], db)
    s = pm.request_rollup_summary(24, db)
    assert s["saturated_minutes"] == 1 and s["peak_inflight"] == 4


# ── 5xx (#30) ────────────────────────────────────────────────────────────────

def test_server_errors_persist_and_group_by_route(db):
    now = time.time()
    samples = [{"at": now, "method": "GET", "route": "/mobile/api/home", "status": 500, "error": "boom",
                "request_id": f"r{i}", "klass": "mobile", "restaurant_id": 5, "duration_ms": 12,
                "unhandled": False, "exc_type": None} for i in range(3)]
    samples.append({"at": now, "method": "POST", "route": "/api/x", "status": 502, "error": None,
                    "request_id": "r9", "klass": "web_api", "restaurant_id": None, "duration_ms": 3,
                    "unhandled": True, "exc_type": "KeyError"})
    assert pm.persist_server_errors(samples, db) == 4
    out = pm.recent_server_errors(24, 50, db)
    assert out["total"] == 4
    assert out["by_route"][0]["route"] == "/mobile/api/home" and out["by_route"][0]["n"] == 3
    assert out["latest"][0]["request_id"] == "r9" and out["latest"][0]["unhandled"] == 1


def test_telemetry_is_pruned_past_its_retention(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO http_5xx_log (created_at, route, status) VALUES (datetime('now','-40 days'), '/a', 500)")
    conn.execute("INSERT INTO http_5xx_log (created_at, route, status) VALUES (datetime('now'), '/b', 500)")
    conn.execute("INSERT INTO request_rollups (minute_at, route, method, klass) VALUES "
                 "(datetime('now','-20 days'), '/a', 'GET', 'web')")
    conn.execute("INSERT INTO provider_health (provider, state, checked_at) VALUES "
                 "('resend', 'ok', datetime('now','-60 days'))")
    conn.commit()
    conn.close()
    out = pm.prune_platform_telemetry(db)
    assert out == {"http_5xx_log": 1, "request_rollups": 1, "provider_health": 1}
    assert [r["route"] for r in _rows(db, "SELECT route FROM http_5xx_log")] == ["/b"]


# ── boots (#76) ──────────────────────────────────────────────────────────────

def test_each_boot_says_how_the_one_before_it_ended(db, monkeypatch):
    first = pm.record_boot_start(db)
    pm.record_boot_ready(first, 1234, db)
    pm.record_boot_stop(first, db)                 # a clean shutdown
    second = pm.record_boot_start(db)              # ...then killed without one
    third = pm.record_boot_start(db)
    pm.record_boot_failed(third, RuntimeError("init_db: disk I/O error"), db)
    fourth = pm.record_boot_start(db)
    boots = {b["id"]: b for b in pm.recent_boots(10, db)}
    assert boots[first]["previous_exit"] is None and boots[first]["ok"] == 1 and boots[first]["boot_ms"] == 1234
    assert boots[second]["previous_exit"] == "clean"
    assert boots[third]["previous_exit"] == "unclean"
    assert boots[fourth]["previous_exit"] == "boot_failed" and "disk I/O" in boots[third]["error"]


def test_a_crash_loop_pages_once_but_four_deploys_do_not(db, monkeypatch):
    import scheduler
    pages = []
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: pages.append((subject, lines)) or True)
    for i in range(4):                              # four deploys, each shut down cleanly
        monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", f"dep-{i}")
        b = pm.record_boot_start(db)
        pm.record_boot_stop(b, db)
    assert pm.crash_loop_state(b, db)["looping"] is False
    assert pm.page_if_crash_looping(b, db) is False
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", "dep-crashy")
    for _ in range(4):                              # one deployment restarting
        b = pm.record_boot_start(db)
    state = pm.crash_loop_state(b, db)
    assert state["looping"] is True and state["same_deployment"] == 4
    assert pm.page_if_crash_looping(b, db) is True
    assert pm.page_if_crash_looping(b, db) is False, "at most one page an hour"
    assert len(pages) == 1 and "restarting" in pages[0][0]


# ── the supervisor thread (#3, #134) ────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_a_dead_scheduler_thread_is_restarted_with_backoff_and_a_live_one_never(db, monkeypatch):
    captured = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: captured.append(str(e)))
    monkeypatch.setattr(ops, "check_platform_sla", lambda send=True, db_path=None: {})
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    started = []

    def restart():
        t = threading.Thread(target=lambda: None)   # dies at once, like a loop that crashes
        t.start()
        t.join()
        started.append(t)
        return t
    clock = _Clock()
    sup = pm.PlatformSupervisor(db_path=db, scheduler_thread=dead, restart_scheduler=restart, clock=clock)
    sup.tick()
    assert len(started) == 1 and sup.scheduler_restarts == 1 and captured
    sup.tick()
    assert len(started) == 1, "backoff: not again straight away"
    clock.t += 31
    sup.tick()
    assert len(started) == 2
    clock.t += 31
    sup.tick()
    assert len(started) == 2, "the backoff doubled to a minute"
    clock.t += 31
    sup.tick()
    assert len(started) == 3

    stop = threading.Event()
    alive = threading.Thread(target=stop.wait, daemon=True)
    alive.start()
    try:
        sup2 = pm.PlatformSupervisor(db_path=db, scheduler_thread=alive,
                                     restart_scheduler=lambda: pytest.fail("restarted a live loop"), clock=clock)
        sup2.tick()
        assert sup2.state()["scheduler_thread"] == "alive"
    finally:
        stop.set()


def test_the_platform_sla_runs_on_the_supervisor_not_the_request_path(db, monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "check_platform_sla",
                        lambda send=True, db_path=None: calls.append(send) or {"alerted": False, "jobs_overdue": []})
    clock = _Clock()
    sup = pm.PlatformSupervisor(db_path=db, clock=clock, sla_every=300)
    sup.tick()
    assert calls == [], "not in the first minutes after boot"
    clock.t += 301
    sup.tick()
    assert calls == [True], "it pages (send=True) — /health only ever reads"
    assert sup.state()["last_sla"]["alerted"] is False


def test_the_supervisor_flushes_buffered_telemetry_and_keeps_it_when_the_write_fails(db, monkeypatch):
    app = Flask(__name__)
    http_layer.register(app)

    @app.route("/api/fail")
    def fail():
        return {"ok": False, "error": "nope"}, 500
    app.test_client().get("/api/fail")
    monkeypatch.setattr(ops, "check_platform_sla", lambda send=True, db_path=None: {})
    sup = pm.PlatformSupervisor(db_path=db, clock=_Clock())
    real = pm.persist_server_errors
    monkeypatch.setattr(pm, "persist_server_errors",
                        lambda s, db_path=None: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    first = sup.flush(include_current=True)
    assert first["rollups"] >= 1 and first["errors"] == 0
    assert sup.state()["pending_errors"] == 1, "kept for the next tick"
    monkeypatch.setattr(pm, "persist_server_errors", real)
    written = sup.flush(include_current=True)
    assert written["errors"] == 1 and sup.state()["pending_errors"] == 0
    assert pm.recent_server_errors(24, 10, db)["latest"][0]["route"] == "/api/fail"
    assert pm.request_rollup_summary(24, db)["routes"][0]["route"] == "/api/fail"


# ── the system card (#28, #102, #125) ───────────────────────────────────────

def test_the_key_report_checks_the_security_variables_and_validates_keys():
    from cryptography.fernet import Fernet
    env = {"SECRET_KEY": "s", "CAVNAR_SECRET_KEY_EPHEMERAL": "1", "CREDENTIAL_KEY": "nope",
           "BACKUP_ENCRYPTION_KEY": Fernet.generate_key().decode(), "ADMIN_REQUIRE_2FA": "0",
           "TWILIO_ACCOUNT_SID": "AC1", "GOOGLE_API_KEY": "AIza"}
    keys = {k["label"]: k for k in pm.key_report(env)}
    assert keys["Session secret"]["state"] == "missing", "a per-boot generated key is not a configured one"
    assert keys["Credential key"]["state"] == "invalid"
    assert keys["Backup encryption key"]["state"] == "ok"
    assert keys["Admin 2FA enforced"]["state"] == "missing"
    assert keys["Staff PIN pepper"]["state"] == "missing"
    assert keys["DocuSign webhook"]["required"] is True
    assert keys["Twilio (SMS)"]["state"] == "missing", "a SID without its token cannot send"
    assert keys["Google Places"]["state"] == "ok", "either variable name counts"
    assert keys["Operator SMS"]["required"] is False
    assert all("value" not in k for k in keys.values())


def test_the_system_report_carries_the_physical_state_and_says_what_to_do(db, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_KEY", "")
    import credentials
    credentials._fernet_cache.clear()
    rid = models.create_restaurant(models.Restaurant(name="Plain", owner_email="p@x.test"), db_path=db)
    models.update_restaurant(rid, {"rpower_token": "plain-token"}, db_path=db)
    pm.record_boot_start(db)
    rep = pm.system_report(db)
    for section in ("keys", "disk", "database", "volume", "backup", "drill", "lease", "heartbeat_minutes",
                    "ai", "http", "rollups_24h", "server_errors_24h", "boots", "size_trend", "providers",
                    "credentials", "warnings"):
        assert section in rep, section
    assert rep["database"]["write"]["state"] == "ok" and rep["database"]["journal"] == "wal"
    assert rep["credentials"]["key"] == "missing" and rep["credentials"]["plaintext"] == 1
    assert any("CREDENTIAL_KEY is not set: 1 client credential is stored as plain text" in w for w in rep["warnings"])
    assert rep["size_trend"]["source"] in ("boots", "daily")
    json.dumps(rep, default=str)                    # serialisable for jsonify


# ── status-page incidents (#61) ─────────────────────────────────────────────

def test_an_open_incident_holds_its_services_and_sets_the_banner():
    statuses = [{"service_key": "email", "status": "operational", "message": None},
                {"service_key": "dashboard", "status": "operational", "message": None}]
    open_inc = [{"id": 7, "title": "Email delayed", "severity": "outage", "status": "investigating",
                 "affected_keys": json.dumps(["email"])}]
    eff = {s["service_key"]: s for s in sm.effective_statuses(statuses, open_inc)}
    assert eff["email"]["status"] == "outage" and eff["email"]["incident_id"] == 7
    assert eff["dashboard"]["status"] == "operational"
    assert statuses[0]["status"] == "operational", "the rows themselves are not changed"
    assert sm.overall_status(statuses, open_inc) == "outage"
    no_keys = [{"id": 8, "title": "Maintenance tonight", "severity": "maintenance", "status": "monitoring",
                "affected_keys": "[]"}]
    assert sm.overall_status(statuses, no_keys) == "maintenance"
    resolved = [dict(open_inc[0], status="resolved")]
    assert sm.overall_status(sm.effective_statuses(statuses, resolved), resolved) == "operational"


# ── the status page's email row reads the same answer ───────────────────────

def test_the_email_row_uses_a_fresh_recorded_probe_instead_of_calling_resend(monkeypatch, db):
    import provider_health as ph
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    conn = models.get_conn(db)
    conn.execute("INSERT INTO provider_health (provider, state, detail) VALUES "
                 "('resend', 'failing', 'quota reached (daily_quota_exceeded)')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(ph, "_get", lambda *a, **k: pytest.fail("the fresh probe should have answered"))
    sm._check_email()
    row = [s for s in sm.get_all_statuses() if s["service_key"] == "email"][0]
    assert row["status"] == "outage" and row["message"] == "Email sending limit reached"
