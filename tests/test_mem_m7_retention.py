"""Memory fix round (9/29/26), workstream M7 — "retention_registry".

One registry (ops) where each table declares its window, its stamp column,
its FLOOR, the rollup run before its rows go and its readers' windows. A
RETAIN_* typo such as RETAIN_ALERT_LOG_DAYS=18 used to erase every
restaurant's alert history overnight; now a window under its floor is
refused, the run is partial and the operator is paged. Each pass deletes
at most RETENTION_PASS_MAX_ROWS rows per table. And every declared
reader's window must fit inside its table's floor — the test that would
have caught the seasonal food-cost re-check reaching past inventory
history and "since you started" reading a pruned alert_log.
"""
import importlib
import inspect
import os
import sqlite3

import pytest

import models
import ops


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default, db_path) else path)
    monkeypatch.setattr(models, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    ops._claim_fallback.clear()
    yield


@pytest.fixture
def paged(monkeypatch):
    got = []
    monkeypatch.setattr(ops, "page_operator", lambda key, subject, lines, **k: got.append((key, subject, lines))
                        or {"sent": True})
    return got


def _x(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _n(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    try:
        return c.execute(sql, args).fetchone()[0]
    finally:
        c.close()


def _resolve(window):
    if isinstance(window, str):
        mod, const = window.rsplit(".", 1)
        return int(getattr(importlib.import_module(mod), const))
    return window


def _fn(path):
    mod, fn = path.rsplit(".", 1)
    return getattr(importlib.import_module(mod), fn)


# ── the declaration ─────────────────────────────────────────────────────────

def test_every_registered_table_declares_a_floor_it_can_meet():
    assert not [k for k in os.environ if k.startswith("RETAIN_")], "a RETAIN_* override leaked into the test"
    for table, days in ops._RETENTION_DAYS.items():
        assert table in ops._RETENTION_FLOOR_DAYS, f"{table} has no floor"
        assert 0 < ops._RETENTION_FLOOR_DAYS[table] <= days, table
        assert ops.retention_state(table)["state"] == "ok", table
    for name, (days, floor) in ops._special_retention().items():
        assert 0 < floor <= days, name
        assert ops.retention_state(name)["state"] == "ok", name
    assert set(ops._RETENTION_ROLLUP) <= set(ops._RETENTION_DAYS)


def test_every_declared_readers_window_fits_inside_its_tables_floor():
    """The test that would have caught seasonal_food (inventory history under
    the seasonal re-check) and rollups (lifetime counts over pruned rows)."""
    known = set(ops._RETENTION_KNOWN_GAPS)
    for table, readers in ops._RETENTION_READERS.items():
        floor = ops.retention_floor(table)
        for reader, window, instead in readers:
            fn = _fn(reader)                                   # renamed or removed: fails here
            if (table, reader) in known:
                continue
            w = _resolve(window)
            if w is None:
                assert instead and instead in inspect.getsource(fn), \
                    f"{reader} reads {table} for its lifetime and names no summary"
            else:
                assert w <= floor, f"{reader} reads {w} days of {table}, whose floor is {floor}"


def test_the_known_gaps_are_real_readers_and_only_shrink():
    for (table, reader), why in ops._RETENTION_KNOWN_GAPS.items():
        assert table in ops._RETENTION_DAYS and why
        _fn(reader)
    assert len(ops._RETENTION_KNOWN_GAPS) <= 10, "a new reader past its window: fix it or say why here"


def test_the_seasonal_reach_is_served_by_the_weekly_summary():
    readers = dict((r[0], r) for r in ops._RETENTION_READERS["inventory_history"])
    assert readers["food_cost_intelligence.seasonal_baseline"][1] <= ops.retention_floor("inventory_history")
    assert "inventory_summary_weeks" in inspect.getsource(_fn("waste_trend.load_waste_history"))
    import history_rollups
    assert "inventory_weekly_summary" in inspect.getsource(history_rollups.inventory_summary_weeks)


# ── a typo is refused, paged, and deletes nothing ───────────────────────────

def test_a_window_under_its_floor_is_refused_and_paged_and_nothing_is_deleted(db_path, monkeypatch, paged):
    monkeypatch.setitem(ops._RETENTION_DAYS, "alert_log", 18)          # RETAIN_ALERT_LOG_DAYS=18
    _x(db_path, "INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (1, 'one_star', datetime('now','-40 days'))")
    captured = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: captured.append((k.get("job"), str(e))))
    out = ops.prune_ledgers(db_path)
    assert _n(db_path, "SELECT COUNT(*) FROM alert_log") == 1, "a typo erased history"
    assert out["refused"] == [{"table": "alert_log", "days": 18, "floor": 90}]
    assert out["failed"] >= 1
    state, _blob = ops.run_outcome(out)
    assert state == ops.RUN_PARTIAL
    assert paged and paged[0][0] == "retention_floor" and "alert_log" in paged[0][2][0]
    assert any(job == "prune_ledgers" and "alert_log" in msg for job, msg in captured)
    assert [r["table"] for r in ops.retention_refusals()] == ["alert_log"]


def test_zero_still_means_keep_forever_and_is_not_a_refusal(db_path, monkeypatch, paged):
    monkeypatch.setitem(ops._RETENTION_DAYS, "alert_log", 0)
    _x(db_path, "INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (1, 'x', datetime('now','-900 days'))")
    out = ops.prune_ledgers(db_path)
    assert _n(db_path, "SELECT COUNT(*) FROM alert_log") == 1
    assert out["refused"] == [] and not paged


def test_a_special_prune_under_its_floor_is_refused_too(db_path, monkeypatch, paged):
    monkeypatch.setattr(ops, "INVENTORY_HISTORY_DAYS", 30)
    _x(db_path, "INSERT INTO inventory_history (restaurant_id, week_end, waste_json, inv_value) "
                "VALUES (1, date('now','-100 days'), '{}', 500)")
    out = ops.prune_ledgers(db_path)
    assert _n(db_path, "SELECT COUNT(*) FROM inventory_history") == 1
    assert {"table": "inventory_history", "days": 30, "floor": 395} in out["refused"]
    assert paged


# ── the per-pass cap ────────────────────────────────────────────────────────

def test_a_pass_deletes_at_most_its_cap_per_table_and_reports_it(db_path, monkeypatch, paged):
    monkeypatch.setattr(ops, "RETENTION_PASS_MAX_ROWS", 5)
    monkeypatch.setattr(ops, "RETENTION_CHUNK_ROWS", 2)
    for _ in range(12):
        _x(db_path, "INSERT INTO job_failures (job, created_at) VALUES ('old', datetime('now','-400 days'))")
    captured = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: captured.append(str(e)))
    out = ops.prune_ledgers(db_path)
    assert out["job_failures"] == 5 and out["capped"] == ["job_failures"] and out["hit_bound"] is True
    assert _n(db_path, "SELECT COUNT(*) FROM job_failures") == 7
    assert any("retention cap" in m for m in captured)
    assert not paged, "a backlog is reported, not paged"
    ops.prune_ledgers(db_path)
    ops.prune_ledgers(db_path)
    assert _n(db_path, "SELECT COUNT(*) FROM job_failures") == 0


# ── the rollup runs first, and a failed one keeps the rows ──────────────────

def test_a_failed_rollup_keeps_its_tables_rows(db_path, monkeypatch, paged):
    import history_rollups
    monkeypatch.setattr(history_rollups, "roll_alerts",
                        lambda db_path=None, **k: (_ for _ in ()).throw(RuntimeError("rollup broke")))
    _x(db_path, "INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (1, 'x', datetime('now','-400 days'))")
    out = ops.prune_ledgers(db_path)
    assert _n(db_path, "SELECT COUNT(*) FROM alert_log") == 1
    assert "alert_log" not in out and out["skipped"] >= 1


def test_every_declared_rollup_runs_before_any_delete(db_path, monkeypatch):
    order = []
    real_delete = ops._chunked_delete
    import ai_utils
    import history_rollups
    monkeypatch.setattr(ai_utils, "rollup_usage", lambda db_path=None, **k: order.append("ai") or {})
    for fn in ("roll_alerts", "roll_engagement", "stamp_newsletter_results"):
        monkeypatch.setattr(history_rollups, fn, lambda db_path=None, _n=fn, **k: order.append(_n) or {})
    monkeypatch.setattr(ops, "_chunked_delete", lambda conn, table, *a, **k: order.append(table) or
                        real_delete(conn, table, *a, **k))
    ops.prune_ledgers(db_path)
    assert order[:4] == ["ai", "roll_alerts", "roll_engagement", "stamp_newsletter_results"]


# ── the boot prune of sign-in history reads the one registry ────────────────

def _login(db_path, days_ago):
    _x(db_path, "INSERT INTO users (id, restaurant_id, username, email, password_hash) VALUES (901, 1, 'lh', 'lh@x.test', 'x') "
                "ON CONFLICT(id) DO NOTHING")
    _x(db_path, "INSERT INTO login_history (user_id, restaurant_id, created_at) VALUES (901, 1, datetime('now', ?))",
       (f"-{days_ago} days",))


def test_the_boot_login_prune_uses_the_registry_window_and_its_floor(db_path, monkeypatch):
    import auth
    auth.init_auth(db_path=db_path)
    models.create_restaurant(models.Restaurant(name="Login Co", owner_email="l@x.test"))
    _login(db_path, 150)
    _login(db_path, 100)
    monkeypatch.setitem(ops._RETENTION_DAYS, "login_history", 120)     # raised: the boot prune follows
    assert auth.prune_login_history(db_path=db_path) == 1
    assert _n(db_path, "SELECT COUNT(*) FROM login_history") == 1
    monkeypatch.setitem(ops._RETENTION_DAYS, "login_history", 10)      # under the floor: nothing goes
    assert auth.prune_login_history(db_path=db_path) == 0
    assert _n(db_path, "SELECT COUNT(*) FROM login_history") == 1


def test_models_unscheduled_prune_honours_the_floor(db_path, monkeypatch):
    monkeypatch.setitem(models._LOG_RETENTION_DAYS, "activity_log", (5, "created_at"))
    _x(db_path, "INSERT INTO activity_log (restaurant_id, event_type, created_at) VALUES (1, 'x', datetime('now','-30 days'))")
    out = models.prune_operational_logs(db_path=db_path)
    assert _n(db_path, "SELECT COUNT(*) FROM activity_log") == 1
    assert any("activity_log" in p and "floor" in p for p in out.get("_problems", []))
