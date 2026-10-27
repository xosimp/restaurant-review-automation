"""Release-safety fix round 10/26/26, WS2 — release health, the absolute 5xx
floor and the slow-boot page (A07-01, A07-10, A05-09; R15).

Every time is fixed (NOW) and every row seeded relative to it: no wall
clock."""
import sqlite3
from datetime import datetime, timedelta

import pytest

import platform_monitor as pm

NOW = datetime(2026, 10, 26, 18, 0, 0)
NEW, OLD = "bbbbbbbbbbbb", "aaaaaaaaaaaa"


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _boot(db, build, started, ok=1, boot_ms=20000, previous_exit=None, deployment=None):
    conn = sqlite3.connect(db)
    cur = conn.execute("INSERT INTO boot_events (started_at, ready_at, build, ok, boot_ms, previous_exit, "
                       "deployment_id) VALUES (?,?,?,?,?,?,?)",
                       (_ts(started), _ts(started), build, ok, boot_ms, previous_exit, deployment))
    conn.commit()
    conn.close()
    return cur.lastrowid


def _authed_5xx(db, at, route, rid, n=1, authed=True, klass="web_api"):
    """`n` 5xx samples as http_layer records them: signed-in (authed) or
    not, from restaurant `rid`."""
    import calendar
    pm.persist_server_errors([{"at": calendar.timegm(at.timetuple()), "method": "GET", "route": route, "status": 500,
                               "klass": klass, "restaurant_id": rid, "authed": authed} for _ in range(n)], db_path=db)


def _rollup(db, at, requests, e5=0, route="/api/x", slow=0, klass="web_api"):
    import http_layer
    buckets = [0] * (len(http_layer.LATENCY_BUCKETS_MS) + 1)
    buckets[0] = requests - slow
    buckets[-1] = slow
    pm.persist_request_rollups([{"minute_at": at.strftime("%Y-%m-%d %H:%M:00"), "route": route, "method": "GET",
                                 "klass": klass, "requests": requests, "errors_4xx": 0, "errors_5xx": e5,
                                 "total_ms": 1.0, "max_ms": 1.0, "buckets": buckets}], db_path=db)


@pytest.fixture
def two_builds(db_path, monkeypatch):
    """OLD served the hour before NEW's first boot, 20 minutes ago."""
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", NEW + "0000")
    for v in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID", "CAVNAR_ENV",
              "ALLOW_LOCAL_SCHEDULER", "RAILWAY_DEPLOYMENT_ID"):
        monkeypatch.delenv(v, raising=False)
    start = NOW - timedelta(minutes=20)
    _boot(db_path, OLD, start - timedelta(hours=3))
    _boot(db_path, NEW, start)
    for m in range(5, 60, 5):                       # the previous build's last hour: clean, quick
        _rollup(db_path, start - timedelta(minutes=m), 40)
    return db_path, start


def test_inside_the_window_it_is_pending(two_builds):
    db, start = two_builds
    res = pm.release_health(db_path=db, now=start + timedelta(minutes=5))
    assert res["verdict"] == "pending" and res["previous_build"] == OLD


def test_a_clean_release_is_ok(two_builds):
    db, start = two_builds
    for m in range(1, 15, 3):
        _rollup(db, start + timedelta(minutes=m), 40)
    res = pm.release_health(db_path=db, now=NOW)
    assert res["verdict"] == "ok", res


def test_five_times_the_errors_is_a_regression_and_pages_once(two_builds, monkeypatch):
    db, start = two_builds
    _rollup(db, start + timedelta(minutes=2), 30, e5=4, route="/api/dsr")
    _rollup(db, start + timedelta(minutes=4), 30, e5=3, route="/mobile/api/home")
    _authed_5xx(db, start + timedelta(minutes=2), "/api/dsr", rid=5, n=4)
    _authed_5xx(db, start + timedelta(minutes=4), "/mobile/api/home", rid=6, n=3)
    res = pm.release_health(db_path=db, now=NOW)
    assert res["verdict"] == "regressed"
    assert any("7 signed-in customer 5xx on 2 routes from 2 restaurants" in u for u in res["urgent"]), res
    assert any("clean for 7 days" in w for w in res["warn"])
    pages = []
    import ops
    import scheduler
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(ops, "page_operator", lambda key, subject, lines, cooldown_minutes=0:
                        pages.append((key, subject, lines)) or {"sent": True})
    sup = pm.PlatformSupervisor(db_path=db, clock=lambda: 0.0, release_every=0)
    monkeypatch.setattr(pm, "release_health", lambda build=None, db_path=None, now=None: res)
    sup.tick(now=1.0)
    sup.tick(now=2.0)
    assert len(pages) == 1
    key, subject, lines = pages[0]
    assert key == f"release_health:{NEW}" and "roll back" in subject
    assert lines[-1] == pm.ROLLBACK_STEP and any(NEW in line for line in lines)


def test_a_slower_p95_and_a_crashing_job_are_urgent(two_builds):
    db, start = two_builds
    _rollup(db, start + timedelta(minutes=3), 40, slow=30)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO job_runs (job, started_at, ok) VALUES ('dsr_sweep', ?, 1)",
                 (_ts(start - timedelta(hours=1)),))
    conn.execute("INSERT INTO job_runs (job, started_at, ok, error) VALUES ('dsr_sweep', ?, 0, 'boom')",
                 (_ts(start + timedelta(minutes=6)),))
    conn.commit()
    conn.close()
    res = pm.release_health(db_path=db, now=NOW)
    assert res["verdict"] == "regressed"
    assert any(u.startswith("p95 within") for u in res["urgent"])
    assert any("job dsr_sweep crashed" in u for u in res["urgent"])


def _queue_rows(db, kind, state, n, at):
    import calendar
    t = float(calendar.timegm(at.timetuple()))
    conn = sqlite3.connect(db)
    for _ in range(n):
        conn.execute("INSERT INTO jobs (kind, grp, priority_class, state, enqueued_at, finished_at) "
                     "VALUES (?, 'g', 'bulk', ?, ?, ?)", (kind, state, t, t))
    conn.commit()
    conn.close()


def _provider(db, provider, state, at):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO provider_health (provider, state, checked_at) VALUES (?,?,?)",
                 (provider, state, _ts(at)))
    conn.commit()
    conn.close()


def test_failed_queue_rows_are_judged_as_a_rate_against_the_deployment_before(two_builds):
    """Re-audit B7 P2: 3 failed rows of any kind said ROLL BACK whatever the
    kind's volume — 3 pos_archive rows of 1,500 on RPOWER's bad night."""
    db, start = two_builds
    during, before = start + timedelta(minutes=3), start - timedelta(minutes=20)
    _queue_rows(db, "strategy_learner", "failed", 3, during)                  # 3 of 3: new with this deployment
    _queue_rows(db, "pos_archive", "failed", 3, during)                       # 3 of 1,503: routine
    _queue_rows(db, "pos_archive", "done", 1500, during)
    _queue_rows(db, "home_snapshot", "failed", 4, during)                     # 4 of 5, but as before it
    _queue_rows(db, "home_snapshot", "done", 1, during)
    _queue_rows(db, "home_snapshot", "failed", 4, before)
    _queue_rows(db, "home_snapshot", "done", 1, before)
    _queue_rows(db, "schedule_generate", "failed", 1, during)
    res = pm.release_health(db_path=db, now=NOW)
    assert any(u.startswith("3 of 3 strategy_learner queue rows failed") for u in res["urgent"]), res
    assert not any("pos_archive" in x for x in res["urgent"] + res["warn"]), res
    assert not any("home_snapshot" in u for u in res["urgent"]) and any("home_snapshot" in w and "as before" in w
                                                                        for w in res["warn"])
    assert any("failed schedule generation" in w for w in res["warn"])


def test_a_providers_outage_is_never_a_rollback(two_builds):
    db, start = two_builds
    during = start + timedelta(minutes=3)
    _queue_rows(db, "pos_archive", "failed", 4, during)
    _queue_rows(db, "pos_archive", "done", 2, during)
    conn = sqlite3.connect(db)
    for i in range(10):
        conn.execute("INSERT INTO ai_usage (created_at, action, model, outcome) VALUES (?, 'x', 'm', ?)",
                     (_ts(start + timedelta(minutes=2)), "error" if i < 3 else "ok"))
    conn.commit()
    conn.close()
    res = pm.release_health(db_path=db, now=NOW)
    assert any("pos_archive" in u for u in res["urgent"]) and any("AI provider errors on 3 of 10" in u
                                                                  for u in res["urgent"]), res
    _provider(db, "rpower", "failing", start - timedelta(minutes=30))
    _provider(db, "anthropic", "error", start + timedelta(minutes=5))
    res = pm.release_health(db_path=db, now=NOW)
    assert res["urgent"] == [], res["urgent"]
    assert any("pos_archive" in w and "the provider, not the release" in w for w in res["warn"])
    assert any("AI provider errors on 3 of 10" in w and "the provider, not the release" in w for w in res["warn"])


def test_one_failed_boot_that_recovers_is_counted_once_and_never_a_rollback(two_builds):
    """Re-audit B8 P2: the boot after a failed one was counted again through
    its previous_exit, so one transient failure and a clean restart read
    "2 failed boots — roll back" — and this test asserted it."""
    db, start = two_builds
    _boot(db, NEW, start + timedelta(minutes=1), ok=0)
    _boot(db, NEW, start + timedelta(minutes=2), ok=1, previous_exit="boot_failed")
    res = pm.release_health(db_path=db, now=NOW)
    assert not any("failed boots" in u for u in res["urgent"]), res["urgent"]
    assert any("1 failed boot" in w for w in res["warn"])
    _boot(db, NEW, start + timedelta(minutes=3), ok=0)
    _boot(db, NEW, start + timedelta(minutes=4), ok=1, previous_exit="boot_failed")
    res = pm.release_health(db_path=db, now=NOW)
    assert any("2 failed boots of this deployment" in u for u in res["urgent"]), res["urgent"]


# ── keyed on the deployment, the SHA alongside (re-audit B7 P2) ──────────────

def test_a_config_redeploy_of_the_same_build_gets_its_own_window(db_path, monkeypatch):
    """An env-var change redeploys the SAME SHA: keyed on the SHA, it was
    judged on a window from days before and never checked."""
    import ops
    import scheduler
    for v in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID", "CAVNAR_ENV",
              "ALLOW_LOCAL_SCHEDULER"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", NEW + "0000")
    first = NOW - timedelta(days=3)
    _boot(db_path, NEW, first, deployment="dep-1")
    redeploy = NOW - timedelta(minutes=20)
    _boot(db_path, NEW, redeploy, deployment="dep-2")
    for m in range(5, 60, 5):
        _rollup(db_path, redeploy - timedelta(minutes=m), 40)
    _rollup(db_path, redeploy + timedelta(minutes=3), 30, e5=6, route="/api/dsr")
    _authed_5xx(db_path, redeploy + timedelta(minutes=3), "/api/dsr", rid=5, n=3)
    _authed_5xx(db_path, redeploy + timedelta(minutes=3), "/api/home/brief", rid=6, n=3)
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", "dep-2")
    assert pm.release_health(db_path=db_path, now=redeploy + timedelta(minutes=5))["verdict"] == "pending"
    res = pm.release_health(db_path=db_path, now=NOW)
    assert res["deployment_id"] == "dep-2" and res["previous_deployment"] == "dep-1"
    assert res["window_start"] == _ts(redeploy) and res["verdict"] == "regressed", res
    pages = []
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(ops, "page_operator", lambda key, subject, lines, cooldown_minutes=0:
                        pages.append((key, lines)) or {"sent": True})
    assert pm.page_release_health(res) is True
    key, lines = pages[0]
    assert key == "release_health:dep-2" and "dep-2" in lines[0] and NEW in lines[0]
    # The first deployment of that build is judged on its own window.
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", "dep-1")
    assert pm.release_health(db_path=db_path, now=NOW)["window_start"] == _ts(first)


def test_no_scheduler_tick_after_the_boot_is_urgent_where_it_runs(two_builds, monkeypatch):
    db, start = two_builds
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    res = pm.release_health(db_path=db, now=NOW)
    assert any("no completed scheduler tick" in u for u in res["urgent"])
    conn = sqlite3.connect(db)
    conn.execute("INSERT OR REPLACE INTO scheduler_heartbeat (id, beat_at, loop_completed_at) VALUES (1, ?, ?)",
                 (_ts(start + timedelta(minutes=3)), _ts(start + timedelta(minutes=3))))
    conn.commit()
    conn.close()
    res = pm.release_health(db_path=db, now=NOW)
    assert not any("scheduler" in u for u in res["urgent"])


def test_no_build_named_is_unknown(db_path, monkeypatch):
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    assert pm.release_health(db_path=db_path, now=NOW)["verdict"] == "unknown"


# ── the absolute 5xx floor (A07-10) ──────────────────────────────────────────

def test_six_5xx_of_twelve_requests_on_two_routes_page_below_the_rate_floor(db_path):
    _rollup(db_path, NOW - timedelta(minutes=3), 6, e5=3, route="/api/dsr")
    _rollup(db_path, NOW - timedelta(minutes=4), 6, e5=3, route="/api/home/brief")
    _authed_5xx(db_path, NOW - timedelta(minutes=3), "/api/dsr", rid=5, n=3)
    _authed_5xx(db_path, NOW - timedelta(minutes=4), "/api/home/brief", rid=6, n=3)
    slo = pm.request_slo(db_path=db_path, now=NOW)
    assert slo["requests"] < pm.HTTP_5XX_ALERT_MIN_REQUESTS and slo["error_rate_over"] is False
    assert slo["error_floor_over"] is True and slo["floor_errors"] == 6 and slo["floor_tenants"] == 2
    import ops
    assert ops._has_urgent({"requests": slo}, {}, write_ok=True) is True


def test_one_route_or_the_console_never_trips_the_floor(db_path):
    _rollup(db_path, NOW - timedelta(minutes=3), 10, e5=8, route="/api/dsr")
    _rollup(db_path, NOW - timedelta(minutes=3), 10, e5=8, route="/admin/x", klass="admin")
    _authed_5xx(db_path, NOW - timedelta(minutes=3), "/api/dsr", rid=5, n=8)
    _authed_5xx(db_path, NOW - timedelta(minutes=3), "/admin/x", rid=6, n=8, klass="admin")
    assert pm.request_slo(db_path=db_path, now=NOW)["error_floor_over"] is False


def test_unauthenticated_traffic_or_one_tenant_never_trips_the_floor(db_path):
    """Re-audit B8 P2: a crawler posting malformed bodies to two routes paged
    urgently every hour."""
    _authed_5xx(db_path, NOW - timedelta(minutes=3), "/api/dsr", rid=5, n=4, authed=False)
    _authed_5xx(db_path, NOW - timedelta(minutes=3), "/mobile/api/client-errors", rid=None, n=4, authed=False)
    assert pm.request_slo(db_path=db_path, now=NOW)["error_floor_over"] is False
    _authed_5xx(db_path, NOW - timedelta(minutes=3), "/api/dsr", rid=5, n=3)
    _authed_5xx(db_path, NOW - timedelta(minutes=3), "/api/home/brief", rid=5, n=3)
    slo = pm.request_slo(db_path=db_path, now=NOW)
    assert slo["floor_errors"] == 6 and slo["floor_tenants"] == 1 and slo["error_floor_over"] is False


def test_the_floor_is_disarmed_where_the_rate_rule_rules(db_path):
    """Re-audit B7 P2: 5 5xx in 20,000 requests is the rate rule's call —
    0.03% — never an urgent page."""
    _rollup(db_path, NOW - timedelta(minutes=3), 20000, e5=6, route="/api/dsr")
    _authed_5xx(db_path, NOW - timedelta(minutes=3), "/api/dsr", rid=5, n=3)
    _authed_5xx(db_path, NOW - timedelta(minutes=3), "/api/home/brief", rid=6, n=3)
    slo = pm.request_slo(db_path=db_path, now=NOW)
    assert slo["error_rate_over"] is False and slo["error_floor_over"] is False


def test_a_5xx_soon_after_a_boot_on_a_route_clean_for_a_week_warns(db_path):
    _boot(db_path, NEW, NOW - timedelta(minutes=10))
    _rollup(db_path, NOW - timedelta(days=3), 10, e5=1, route="/api/old-failure")
    _rollup(db_path, NOW - timedelta(minutes=2), 10, e5=1, route="/api/old-failure")
    _rollup(db_path, NOW - timedelta(minutes=2), 10, e5=1, route="/api/new-failure")
    slo = pm.request_slo(db_path=db_path, now=NOW)
    assert slo["fresh_5xx_routes"] == ["/api/new-failure"]
    later = pm.request_slo(db_path=db_path, now=NOW + timedelta(minutes=45))
    assert later["fresh_5xx_routes"] == []


# ── the slow-boot page (A05-09) ──────────────────────────────────────────────

def test_a_boot_past_a_minute_or_half_again_the_median_is_slow(db_path):
    for i in range(10):
        _boot(db_path, OLD, NOW - timedelta(hours=10 - i), boot_ms=10000)
    ok = _boot(db_path, NEW, NOW - timedelta(minutes=30), boot_ms=12000)
    assert pm.slow_boot_state(ok, db_path)["slow"] is False
    grown = _boot(db_path, NEW, NOW - timedelta(minutes=20), boot_ms=16000)
    st = pm.slow_boot_state(grown, db_path)
    assert st["slow"] and "median" in st["reason"]
    slow = _boot(db_path, NEW, NOW - timedelta(minutes=10), boot_ms=70000)
    assert "alert past 60 s" in pm.slow_boot_state(slow, db_path)["reason"]


def test_the_slow_boot_pages_once_per_build_where_the_scheduler_runs(db_path, monkeypatch):
    import ops
    import scheduler
    bid = _boot(db_path, NEW, NOW, boot_ms=90000)
    pages = []
    monkeypatch.setattr(ops, "page_operator", lambda key, subject, lines, cooldown_minutes=0:
                        pages.append((key, cooldown_minutes)) or {"sent": True})
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    assert pm.page_if_slow_boot(bid, db_path) is False and pages == []
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    assert pm.page_if_slow_boot(bid, db_path) is True
    assert pages == [(f"slow_boot:{NEW}", 7 * 24 * 60)]
