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


def _boot(db, build, started, ok=1, boot_ms=20000, previous_exit=None):
    conn = sqlite3.connect(db)
    cur = conn.execute("INSERT INTO boot_events (started_at, ready_at, build, ok, boot_ms, previous_exit) "
                       "VALUES (?,?,?,?,?,?)", (_ts(started), _ts(started), build, ok, boot_ms, previous_exit))
    conn.commit()
    conn.close()
    return cur.lastrowid


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
              "ALLOW_LOCAL_SCHEDULER"):
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
    res = pm.release_health(db_path=db, now=NOW)
    assert res["verdict"] == "regressed" and any("7 customer 5xx on 2 routes" in u for u in res["urgent"])
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


def test_failed_queue_rows_ai_errors_and_failed_boots(two_builds):
    import calendar
    db, start = two_builds
    t = float(calendar.timegm((start + timedelta(minutes=3)).timetuple()))
    conn = sqlite3.connect(db)
    for i in range(3):
        conn.execute("INSERT INTO jobs (kind, grp, priority_class, state, enqueued_at, finished_at) "
                     "VALUES ('strategy_learner', 'g', 'background', 'failed', ?, ?)", (t, t))
    conn.execute("INSERT INTO jobs (kind, grp, priority_class, state, enqueued_at, finished_at) "
                 "VALUES ('schedule_generate', 'g', 'interactive', 'failed', ?, ?)", (t, t))
    for i in range(10):
        conn.execute("INSERT INTO ai_usage (created_at, action, model, outcome) VALUES (?, 'x', 'm', ?)",
                     (_ts(start + timedelta(minutes=2)), "error" if i < 3 else "ok"))
    conn.commit()
    conn.close()
    _boot(db, NEW, start + timedelta(minutes=1), ok=0)
    _boot(db, NEW, start + timedelta(minutes=2), ok=1, previous_exit="boot_failed")
    res = pm.release_health(db_path=db, now=NOW)
    assert any("3 failed queue rows of strategy_learner" in u for u in res["urgent"])
    assert any("failed schedule generation" in w for w in res["warn"])
    assert any("AI provider errors on 3 of 10" in u for u in res["urgent"])
    assert any("failed boots" in u for u in res["urgent"])


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
    slo = pm.request_slo(db_path=db_path, now=NOW)
    assert slo["requests"] < pm.HTTP_5XX_ALERT_MIN_REQUESTS and slo["error_rate_over"] is False
    assert slo["error_floor_over"] is True and slo["floor_errors"] == 6
    import ops
    assert ops._has_urgent({"requests": slo}, {}, write_ok=True) is True


def test_one_route_or_the_console_never_trips_the_floor(db_path):
    _rollup(db_path, NOW - timedelta(minutes=3), 10, e5=8, route="/api/dsr")
    _rollup(db_path, NOW - timedelta(minutes=3), 10, e5=8, route="/admin/x", klass="admin")
    assert pm.request_slo(db_path=db_path, now=NOW)["error_floor_over"] is False


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
