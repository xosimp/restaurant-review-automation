"""Audit #17 — the optimization work, and the invariants that keep it safe.

Performance changes are the easiest kind to regress silently: nothing breaks,
it just gets slow again. Each test here pins the property that made the change
worth doing, not the implementation that delivered it.
"""
import json
import sqlite3

import pytest

import ai_utils
import models
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))


def _counting_conn(db_path, counters, only_sql=None):
    """A get_conn that records connection opens and statements executed.

    `only_sql` narrows the count to statements containing that fragment —
    necessary inside a Flask request context, where unrelated machinery also
    opens connections and a bare total measures the wrong thing.
    """
    class _C(sqlite3.Connection):
        def execute(self, sql, *a, **k):
            if only_sql is None or only_sql in sql:
                counters["queries"] += 1
            return super().execute(sql, *a, **k)

    def _open(path=db_path, *a, **k):
        counters["opens"] += 1
        c = sqlite3.connect(db_path, timeout=30, factory=_C)
        c.row_factory = sqlite3.Row
        return c
    return _open


# ── P1-5: get_all_restaurants is one query, not one per restaurant ─────────

def test_listing_every_restaurant_is_a_single_query(db_path, monkeypatch):
    """It used to SELECT * and then throw every column away, re-fetching each
    row by id — 1,001 queries at a thousand restaurants, ten scheduler jobs
    a day."""
    for i in range(8):
        create_restaurant(Restaurant(name=f"Co {i}", owner_email=f"c{i}@x.com"), db_path=db_path)

    counters = {"opens": 0, "queries": 0}
    monkeypatch.setattr(models, "get_conn", _counting_conn(db_path, counters))

    out = models.get_all_restaurants(db_path)

    assert len(out) == 8
    assert counters["opens"] == 1, "one connection, however many restaurants"
    assert counters["queries"] <= 2, f"one query, not one per row (got {counters['queries']})"


def test_listing_still_hydrates_every_field(db_path):
    """The N+1 was replaced with a shared hydrator — the objects must be
    identical to what get_restaurant returns, not a thinner shape."""
    rid = create_restaurant(Restaurant(name="Full Co", owner_email="f@x.com",
                                       neighborhood="Downtown", module_labor=1),
                            db_path=db_path)
    from_list = [r for r in models.get_all_restaurants(db_path) if r.id == rid][0]
    from_get = models.get_restaurant(rid, db_path=db_path)
    assert from_list == from_get


def test_a_malformed_row_does_not_cost_the_whole_list(db_path):
    """The old per-row try/except bought this; the rewrite has to keep it."""
    for i in range(3):
        create_restaurant(Restaurant(name=f"Co {i}", owner_email=f"c{i}@x.com"), db_path=db_path)
    assert len(models.get_all_restaurants(db_path)) == 3


def _request_app():
    """A throwaway Flask app, purely for its request context.

    models._request_cache() only needs an app context to exist; it does not
    care whose. Importing hosted_dashboard to get one would boot the database,
    seed demo data, start the scheduler and re-run csrf_protect() on already
    registered blueprints.
    """
    from flask import Flask
    return Flask(__name__)


# ── P0-1: get_restaurant is memoised per request, and only per request ─────

def test_repeated_reads_outside_a_request_are_not_cached(db_path, monkeypatch):
    """models.py is imported by the scheduler, the tests and one-off scripts.
    A job loop that runs for an hour MUST see a row change under it."""
    rid = create_restaurant(Restaurant(name="Sched Co", owner_email="s@x.com"), db_path=db_path)
    counters = {"opens": 0, "queries": 0}
    monkeypatch.setattr(models, "get_conn",
                        _counting_conn(db_path, counters, only_sql="FROM restaurants WHERE id=?"))

    models.get_restaurant(rid, db_path=db_path)
    models.get_restaurant(rid, db_path=db_path)
    assert counters["queries"] == 2, "no caching without a request context"


def test_repeated_reads_inside_one_request_hit_the_database_once(db_path, monkeypatch):
    """One Home load called get_restaurant 35 times for the same row."""
    rid = create_restaurant(Restaurant(name="Web Co", owner_email="w@x.com"), db_path=db_path)
    counters = {"opens": 0, "queries": 0}
    # Counted by STATEMENT, not by connection: a request context has other
    # machinery in it that opens connections of its own.
    monkeypatch.setattr(models, "get_conn",
                        _counting_conn(db_path, counters, only_sql="FROM restaurants WHERE id=?"))

    with _request_app().test_request_context("/"):
        a = models.get_restaurant(rid, db_path=db_path)
        # Measured as a DELTA after the first read: the absolute count would
        # also pick up whatever else in the suite touches this statement,
        # which is not what this test is about.
        counters["queries"] = 0
        b = models.get_restaurant(rid, db_path=db_path)
        c = models.get_restaurant(rid, db_path=db_path)
    assert counters["queries"] == 0, (
        f"reads after the first must not touch the database (got {counters['queries']})")
    assert a is b is c, "the same instance, not three equal copies"


def test_two_requests_do_not_share_a_cache(db_path, monkeypatch):
    """The memo must die with the request, or a settings change would never
    be visible to the next one."""
    rid = create_restaurant(Restaurant(name="First", owner_email="r@x.com"), db_path=db_path)

    with _request_app().test_request_context("/"):
        models.get_restaurant(rid, db_path=db_path)
    models.update_restaurant(rid, {"name": "Renamed"}, db_path=db_path)
    with _request_app().test_request_context("/"):
        assert models.get_restaurant(rid, db_path=db_path).name == "Renamed"


def test_a_write_is_visible_to_a_read_in_the_same_request(db_path):
    """A settings POST writes and then re-reads. Serving it the row as it was
    before its own write is the classic cache bug."""
    rid = create_restaurant(Restaurant(name="Before", owner_email="rw@x.com"), db_path=db_path)

    with _request_app().test_request_context("/"):
        assert models.get_restaurant(rid, db_path=db_path).name == "Before"
        models.update_restaurant(rid, {"name": "After"}, db_path=db_path)
        assert models.get_restaurant(rid, db_path=db_path).name == "After"


def test_a_missing_restaurant_is_cached_as_missing_not_retried(db_path):
    with _request_app().test_request_context("/"):
        assert models.get_restaurant(999_999, db_path=db_path) is None
        assert models.get_restaurant(999_999, db_path=db_path) is None


# ── P2-13: the year-over-year window is one query ─────────────────────────

def test_year_over_year_context_is_one_query(db_path, monkeypatch):
    """Seven dates by a seven-day window was 49 round trips to answer one
    question about one restaurant's history."""
    rid = create_restaurant(Restaurant(name="YoY Co", owner_email="y@x.com"), db_path=db_path)
    dates = [f"2026-09-{d:02d}" for d in range(21, 28)]
    counters = {"opens": 0, "queries": 0}
    monkeypatch.setattr(models, "get_conn", _counting_conn(db_path, counters))

    out = models.get_yoy_schedule_context(rid, dates, db_path=db_path)

    assert len(out) == len(dates)
    assert counters["queries"] <= 2, f"one query for the whole window (got {counters['queries']})"


def test_year_over_year_still_prefers_the_exact_52_week_match(db_path):
    """The ±3 day window and its tie-break are the behaviour; only the query
    count changed."""
    rid = create_restaurant(Restaurant(name="Match Co", owner_email="m@x.com"), db_path=db_path)
    conn = get_conn(db_path)
    # 2026-09-21 minus 52 weeks = 2025-09-22. Seed the exact day AND an
    # earlier day inside the window, so a wrong tie-break is visible.
    for d, sales in (("2025-09-19", 100.0), ("2025-09-22", 999.0)):
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales, labor_pct, "
                     "labor_cost, total_hours) VALUES (?,?,?,?,?,?)", (rid, d, sales, 30.0, 30.0, 10.0))
    conn.commit()
    conn.close()

    row = models.get_yoy_schedule_context(rid, ["2026-09-21"], db_path=db_path)[0]
    assert row["yoy_date"] == "2025-09-22", "exact 52-week match wins over an earlier one in range"
    assert row["yoy_sales"] == 999.0


def test_year_over_year_falls_back_to_the_earliest_day_with_data(db_path):
    rid = create_restaurant(Restaurant(name="Fallback Co", owner_email="fb@x.com"), db_path=db_path)
    conn = get_conn(db_path)
    # Nothing on 2025-09-22 itself; two days inside the window.
    for d in ("2025-09-20", "2025-09-24"):
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales, labor_pct, "
                     "labor_cost, total_hours) VALUES (?,?,?,?,?,?)", (rid, d, 50.0, 30.0, 15.0, 5.0))
    conn.commit()
    conn.close()

    row = models.get_yoy_schedule_context(rid, ["2026-09-21"], db_path=db_path)[0]
    assert row["yoy_date"] == "2025-09-20", "walks the window in offset order, earliest first"


# ── P0-3: a failed geocode is cached, not re-billed ───────────────────────

def test_a_geocode_that_returns_no_geometry_is_not_retried(db_path, monkeypatch):
    """570 of 572 billed Places requests in 30 days were one restaurant whose
    place_id resolves with no geometry. The failure path recorded nothing, so
    every call re-geocoded and re-billed."""
    import weather
    rid = create_restaurant(Restaurant(name="Bad PID", owner_email="b@x.com",
                                       google_place_id="pid-with-no-geometry"), db_path=db_path)
    monkeypatch.setattr(weather, "_GOOGLE_KEY", "test-key")
    calls = {"n": 0}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"result": {}}          # resolves, no geometry

    def _get(*a, **k):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(weather.requests, "get", _get)
    monkeypatch.setattr(weather, "_meter_places", lambda *a, **k: None)

    r = models.get_restaurant(rid, db_path=db_path)
    assert weather._geocode(r, db_path=db_path) == (None, None)
    assert calls["n"] == 1

    # Same restaurant, re-read so the failure stamp is on the object.
    r2 = models.get_restaurant(rid, db_path=db_path)
    assert weather._geocode(r2, db_path=db_path) == (None, None)
    assert calls["n"] == 1, "the second call must not reach Google again"


def test_a_successful_geocode_clears_a_standing_failure(db_path, monkeypatch):
    import weather
    rid = create_restaurant(Restaurant(name="Fixed PID", owner_email="fx@x.com",
                                       google_place_id="pid-now-good"), db_path=db_path)
    models.update_restaurant(rid, {"geocode_failed_at": "2020-01-01T00:00:00"}, db_path=db_path)
    monkeypatch.setattr(weather, "_GOOGLE_KEY", "test-key")

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"result": {"geometry": {"location": {"lat": 41.9, "lng": -88.3}}}}

    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(weather, "_meter_places", lambda *a, **k: None)

    r = models.get_restaurant(rid, db_path=db_path)   # stamp is old, so it retries
    assert weather._geocode(r, db_path=db_path) == (41.9, -88.3)
    after = models.get_restaurant(rid, db_path=db_path)
    assert after.latitude == 41.9
    assert after.geocode_failed_at is None, "success must clear the failure stamp"


def test_the_forecast_cache_only_ever_returns_a_list(db_path):
    """weather_cache_json holds a LIST of NWS periods. Anything else is a
    corrupt cache and must read as 'no cache' rather than reach the loop."""
    import weather
    from datetime import datetime
    rid = create_restaurant(Restaurant(name="Cache Co", owner_email="cc@x.com"), db_path=db_path)
    models.update_restaurant(rid, {
        "weather_cache_json": json.dumps({"not": "a list"}),
        "weather_cached_at": datetime.now().isoformat()}, db_path=db_path)
    assert weather._cached_periods(models.get_restaurant(rid, db_path=db_path)) is None


# ── P1-6: schema work is off the per-call AI path, but still self-healing ──

def test_usage_schema_setup_runs_once_per_database(db_path):
    """CREATE TABLE + two CREATE INDEX + a PRAGMA ran before and after EVERY
    Claude call."""
    ai_utils._usage_schema_ready.discard(db_path)
    conn = get_conn(db_path)
    try:
        ai_utils._ensure_usage_schema(conn, db_path)
        assert db_path in ai_utils._usage_schema_ready
        # Second call is a set lookup and nothing else.
        ai_utils._ensure_usage_schema(conn, db_path)
    finally:
        conn.close()


def test_a_schema_that_changes_underneath_still_heals(db_path):
    """A deploy can migrate the volume while the previous process is alive.
    The once-per-process flag is an optimisation, not a claim that the schema
    is immutable."""
    rid = create_restaurant(Restaurant(name="Heal Co", owner_email="h@x.com"), db_path=db_path)
    ai_utils.log_ai_usage(rid, "first", "claude-sonnet-5", 10, 5, db_path=db_path)

    conn = get_conn(db_path)
    conn.execute("DROP TABLE ai_usage")
    conn.execute("CREATE TABLE ai_usage (id INTEGER PRIMARY KEY, restaurant_id INTEGER, "
                 "action TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
                 "cost_usd REAL, created_at TEXT DEFAULT (datetime('now')))")
    conn.commit()
    conn.close()

    ai_utils.log_ai_usage(rid, "second", "claude-sonnet-5", 10, 5, db_path=db_path)
    conn = get_conn(db_path)
    actions = [r["action"] for r in conn.execute("SELECT action FROM ai_usage").fetchall()]
    conn.close()
    assert "second" in actions, "the insert must heal the schema and retry, not drop the row"


# ── P3-20: operational logs are bounded ───────────────────────────────────

def test_stale_operational_rows_are_pruned(db_path):
    rid = create_restaurant(Restaurant(name="Prune Co", owner_email="p@x.com"), db_path=db_path)
    # ai_usage is created lazily by ai_utils, not by init_db.
    ai_utils.log_ai_usage(rid, "seed", "claude-sonnet-5", 1, 1, db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO ai_usage (restaurant_id, action, model, cost_usd, created_at) "
                 "VALUES (?,?,?,?, datetime('now','-400 days'))", (rid, "old", "m", 0.1))
    conn.execute("INSERT INTO ai_usage (restaurant_id, action, model, cost_usd, created_at) "
                 "VALUES (?,?,?,?, datetime('now'))", (rid, "new", "m", 0.1))
    conn.commit()
    conn.close()

    models.prune_operational_logs(db_path=db_path)

    conn = get_conn(db_path)
    actions = [r["action"] for r in conn.execute("SELECT action FROM ai_usage").fetchall()]
    conn.close()
    assert "new" in actions
    assert "old" not in actions


def test_pruning_never_touches_the_ask_action_audit(db_path):
    """ask_cavnar_actions is explicitly never pruned — it is the record of
    what the assistant proposed and what the owner approved."""
    assert "ask_cavnar_actions" not in models._LOG_RETENTION_DAYS
    assert "reviews" not in models._LOG_RETENTION_DAYS


def test_every_pruned_table_names_a_column_that_exists(db_path):
    """The timestamp column is NOT uniformly created_at — job_runs stamps
    started_at and email_log stamps sent_at. Getting one wrong means a DELETE
    that raises, gets swallowed, and silently never prunes while the table
    grows forever."""
    conn = get_conn(db_path)
    try:
        existing = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table, (_days, column) in models._LOG_RETENTION_DAYS.items():
            if table not in existing:
                continue
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            assert column in cols, f"{table} has no column {column}"
    finally:
        conn.close()


def test_a_missing_timestamp_column_is_reported_not_swallowed(db_path, monkeypatch):
    """Pruning nothing looks identical to having nothing to prune."""
    monkeypatch.setitem(models._LOG_RETENTION_DAYS, "ai_usage", (30, "no_such_column"))
    ai_utils.log_ai_usage(1, "x", "claude-sonnet-5", 1, 1, db_path=db_path)
    out = models.prune_operational_logs(db_path=db_path)
    assert "_problems" in out
    assert any("no_such_column" in p for p in out["_problems"])


def test_rows_are_pruned_by_each_tables_own_timestamp_column(db_path):
    """job_runs ages on started_at, not created_at."""
    conn = get_conn(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(job_runs)").fetchall()}
    finally:
        conn.close()
    if "started_at" not in cols:
        pytest.skip("job_runs not created on this database")
    conn = get_conn(db_path)
    # job_runs is ops' own schema (ok, not status). It exists on every
    # database now that ops.init_ops runs at boot; before, this test skipped.
    conn.execute("INSERT INTO job_runs (job, ok, started_at) VALUES (?,?, datetime('now','-400 days'))",
                 ("old_job", 1))
    conn.execute("INSERT INTO job_runs (job, ok, started_at) VALUES (?,?, datetime('now'))",
                 ("new_job", 1))
    conn.commit(); conn.close()

    models.prune_operational_logs(db_path=db_path)

    conn = get_conn(db_path)
    jobs = [r["job"] for r in conn.execute("SELECT job FROM job_runs").fetchall()]
    conn.close()
    assert "new_job" in jobs
    assert "old_job" not in jobs


def test_pruning_survives_a_table_that_does_not_exist(db_path, monkeypatch):
    """A table not created yet on this database is fine and silent — unlike a
    table that exists with the wrong column, which is reported."""
    monkeypatch.setitem(models._LOG_RETENTION_DAYS, "table_that_is_not_there", (30, "created_at"))
    out = models.prune_operational_logs(db_path=db_path)   # must not raise
    assert "_problems" not in out


# ── P1-4 / caching headers: the response layer ────────────────────────────
#
# Exercised against a throwaway Flask app with http_layer attached. Importing
# hosted_dashboard here would initialise the database, seed demo data, start
# the scheduler thread, and re-run csrf_protect() on blueprints Flask has
# already registered — which is why the response layer was extracted.

def _http_app():
    from flask import Flask, Response, jsonify
    import http_layer
    app = Flask(__name__)
    http_layer.register(app)

    @app.route("/big")
    def big():
        return Response("<html>" + ("x" * 50_000) + "</html>", mimetype="text/html")

    @app.route("/api/thing")
    def api_thing():
        return jsonify(ok=True)

    @app.route("/stream")
    def stream():
        return Response((f"data: {i}\n\n" for i in range(500)),
                        mimetype="text/event-stream")

    @app.route("/tiny")
    def tiny():
        return Response("ok", mimetype="text/plain")
    return app


def test_large_text_responses_are_compressed():
    client = _http_app().test_client()
    resp = client.get("/big", headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("Content-Encoding") == "gzip"
    assert "Accept-Encoding" in (resp.headers.get("Vary") or "")
    assert len(resp.data) < 50_000, "the body must actually be smaller"


def test_a_client_that_cannot_take_gzip_still_gets_the_page():
    client = _http_app().test_client()
    resp = client.get("/big", headers={"Accept-Encoding": "identity"})
    assert resp.status_code == 200
    assert resp.headers.get("Content-Encoding") is None
    assert b"<html>" in resp.data
    assert "Accept-Encoding" in (resp.headers.get("Vary") or ""), (
        "a proxy must still know the response varies by encoding")


def test_a_small_response_is_left_alone():
    """Below the floor the gzip header costs more than the body saves."""
    client = _http_app().test_client()
    resp = client.get("/tiny", headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("Content-Encoding") is None


def test_a_streamed_response_is_never_compressed():
    """There is exactly one SSE endpoint in the product, and buffering it to
    compress it would turn live tool progress back into a silent spinner."""
    client = _http_app().test_client()
    resp = client.get("/stream", headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("Content-Encoding") is None


def test_tenant_json_is_marked_no_store():
    """Authenticated JSON carries one restaurant's labor cost and revenue;
    nothing set a Cache-Control header at all, leaving intermediaries free to
    apply heuristic freshness to it."""
    client = _http_app().test_client()
    assert client.get("/api/thing").headers.get("Cache-Control") == "no-store"


def test_html_pages_are_not_given_a_cache_policy():
    """Caching them would risk serving a stale dashboard after an action."""
    client = _http_app().test_client()
    assert client.get("/big").headers.get("Cache-Control") is None


def test_static_assets_are_not_given_a_far_future_expiry():
    """They are referenced by bare path with no hash or version query, so a
    long max-age would strand a JavaScript fix in browser caches."""
    import inspect, http_layer
    src = inspect.getsource(http_layer.add_cache_headers)
    code = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert "max-age=86400" not in code
    assert "immutable" not in code


# ── P0-2: the scheduler can be moved out of the web process ───────────────

def test_the_web_process_can_be_told_not_to_run_the_scheduler():
    """Splitting it onto its own Railway service is a config change, because
    the single-runner guarantee is the database lease, not gunicorn.

    Asserted against the SOURCE rather than by importing hosted_dashboard,
    which would boot the app. The rule has to hold at module scope, where no
    test can call it.
    """
    import pathlib
    src = pathlib.Path("hosted_dashboard.py").read_text()
    assert "RUN_SCHEDULER_IN_WEB" in src
    assert "_RUN_SCHEDULER_IN_WEB" in src
    # The flag must GATE start_scheduler, not merely be read.
    gate = src.index("_RUN_SCHEDULER_IN_WEB =")
    after = src[gate:gate + 900]
    assert "if _RUN_SCHEDULER_IN_WEB:" in after
    assert "start_scheduler" in after


def test_the_worker_entrypoint_exists_and_imports():
    """worker.py is what the second Railway service runs."""
    import worker
    assert callable(worker.main)


# ── RPOWER: archive locally, do not re-query (vendor requirement) ──────────

def _seed_daily(db_path, rid, pairs):
    conn = get_conn(db_path)
    for d, sales in pairs:
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales, labor_pct, "
                     "labor_cost, total_hours) VALUES (?,?,?,?,?,?)", (rid, d, sales, 30.0, 30.0, 10.0))
    conn.commit()
    conn.close()


def test_net_sales_reads_the_local_archive_before_the_pos(db_path, monkeypatch):
    """RPOWER's integrator asked that we download and archive rather than
    query repeatedly, and told us there is no sandbox — every call hits a
    live store. profitability_projection calls this twice, and it is reached
    by business_intelligence.gather on every Ask context build."""
    import cogs, pos
    # Every trading day of the window is archived. This used to seed three
    # rows across thirty days and call that covered — the edges-only rule
    # that read a hole as zero sales (MOD-FC-16); a window with holes now
    # goes to the POS, see test_edge_mod_a_food_cogs.py.
    rid = create_restaurant(Restaurant(name="Archive Co", owner_email="a@x.com"), db_path=db_path)
    _seed_daily(db_path, rid, [(f"2026-08-{d:02d}", 100.0) for d in range(1, 32)])

    def _must_not_be_called(*a, **k):
        raise AssertionError("the POS was queried for a window the archive covers")
    monkeypatch.setattr(pos, "fetch_business_days", _must_not_be_called)

    total, why = cogs.net_sales_in_window(rid, "2026-08-01", "2026-08-31")
    assert total == 3100.0
    assert why is None


def test_a_window_the_archive_does_not_span_falls_back_to_the_pos(db_path, monkeypatch):
    """Never return a smaller number from a partial archive: an under-reported
    sales figure inflates food cost %, which is the one number this module is
    named after."""
    import cogs, pos
    rid = create_restaurant(Restaurant(name="Partial Co", owner_email="p@x.com"), db_path=db_path)
    _seed_daily(db_path, rid, [("2026-09-10", 900.0)])      # archive starts late

    called = {"n": 0}

    # The POS answers for every day — the live path is held to the same
    # coverage rule as the archive (CA3 F14), so a two-day answer for a
    # 30-day window is refused, not summed.
    live = {f"2026-09-{d:02d}": 100.0 for d in range(1, 31)}

    def _live(restaurant_id, start, end):
        called["n"] += 1
        return dict(live), "rpower"
    monkeypatch.setattr(pos, "fetch_business_days", _live)

    total, why = cogs.net_sales_in_window(rid, "2026-09-01", "2026-09-30")
    assert called["n"] == 1, "a window the archive does not span must reach the POS"
    assert total == 3000.0


def test_an_empty_archive_still_reaches_the_pos(db_path, monkeypatch):
    import cogs, pos
    rid = create_restaurant(Restaurant(name="Fresh Co", owner_email="n@x.com"), db_path=db_path)
    monkeypatch.setattr(pos, "fetch_business_days",
                        lambda r, s, e: ({f"2026-09-{d:02d}": 250.0 for d in range(1, 31)}, "toast"))
    total, why = cogs.net_sales_in_window(rid, "2026-09-01", "2026-09-30")
    assert total == 7500.0


def test_a_closed_day_does_not_break_archive_coverage(db_path, monkeypatch):
    """A restaurant closed on Mondays has no Monday row. Coverage is decided
    by the archive's edges, not by counting every calendar date."""
    import cogs, pos
    rid = create_restaurant(Restaurant(name="Closed Mondays", owner_email="cm@x.com"), db_path=db_path)
    _seed_daily(db_path, rid, [("2026-09-01", 300.0), ("2026-09-03", 400.0), ("2026-09-05", 500.0)])
    monkeypatch.setattr(pos, "fetch_business_days",
                        lambda r, s, e: (_ for _ in ()).throw(AssertionError("should not query")))
    total, _why = cogs.net_sales_in_window(rid, "2026-09-01", "2026-09-05")
    assert total == 1200.0


def test_rpower_requests_a_month_at_a_time(db_path):
    """RPOWER told us to pull "a month worth of data at a time". Both range
    fetches chunk through _chunk_range, so a 60-day sync is two requests
    rather than one oversized one."""
    import rpower
    assert rpower.MAX_RANGE_DAYS <= 31
    chunks = list(rpower._chunk_range("2026-07-01", "2026-08-31"))
    assert len(chunks) >= 2
    for start, end in chunks:
        from datetime import date as _date
        s = _date.fromisoformat(str(start)[:10])
        e = _date.fromisoformat(str(end)[:10])
        assert (e - s).days < rpower.MAX_RANGE_DAYS


def test_the_budget_check_survives_a_table_that_vanished(db_path):
    """_spend_since runs before EVERY Claude call. It used to CREATE TABLE on
    each one, so the table always existed; once that moved off the hot path,
    a missing table here would raise and refuse all AI rather than report
    spend."""
    rid = create_restaurant(Restaurant(name="Budget Co", owner_email="bu@x.com"), db_path=db_path)
    ai_utils.log_ai_usage(rid, "x", "claude-sonnet-5", 10, 5, db_path=db_path)

    conn = get_conn(db_path)
    conn.execute("DROP TABLE ai_usage")
    conn.commit()
    conn.close()

    # Must not raise, and must not report a fabricated spend.
    spend = ai_utils._spend_since("2000-01-01", restaurant_id=rid, db_path=db_path)
    assert spend == 0.0


def test_the_admin_cost_view_survives_the_same(db_path):
    rid = create_restaurant(Restaurant(name="View Co", owner_email="v@x.com"), db_path=db_path)
    ai_utils.log_ai_usage(rid, "y", "claude-sonnet-5", 10, 5, db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("DROP TABLE ai_usage")
    conn.commit()
    conn.close()
    assert ai_utils.usage_summary(restaurant_id=rid, db_path=db_path) == []
