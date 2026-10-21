"""Audit #23 (10/21/26), fix group F6 — monitoring and the operator console.

The RPOWER health rollup (A6-01, A4-10): one precomputed row per store with
the last good time of each data type, coverage, gaps, the queue, the
ledger, the anomalies and the integrity checks; the fleet rules evaluated
from it (A6 section 3.6: A6-02, A6-12, A6-17, A4-16); data findings kept
apart from job failures (A6-04); the auth latch surfaced to the console and
the owner (A6-09, A6-10); the console's paged views (A6-15); the status
page and the fleet POS reading share-gated and off the rollup (A6-14,
A6-16); the DSR SLA counting provisional nights (A6-20); the storm page
share-gated (A6-26); a POS error critical only for auth or after the
retries (A6-22).
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import models
import ops
import rpower_health as rh


def _store(db_path, name="Store", token="tok-1", cg=101, mid="M1", **kw):
    rid = models.create_restaurant(models.Restaurant(name=name, owner_email=f"{name[:4].lower()}@x.test"),
                                   db_path=db_path)
    fields = {"rpower_token": token, "rpower_cg": cg, "rpower_store_mid": mid,
              "rpower_verified_at": "2026-08-01 10:00:00",
              "rpower_last_synced": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")}
    fields.update(kw)
    models.update_restaurant(rid, fields, db_path=db_path)
    return rid


def _nights(db_path, rid, dates, **cols):
    conn = models.get_conn(db_path)
    try:
        for d in dates:
            row = {"restaurant_id": rid, "provider": "rpower", "business_date": d, "tickets": 40, "lines": 120,
                   "punches": 12, "net_sales": 3000.0}
            row.update(cols.get(d, {}) if isinstance(cols.get(d), dict) else {})
            ks = ", ".join(row)
            conn.execute(f"INSERT INTO pos_archive_days ({ks}) VALUES ({', '.join('?' for _ in row)})",
                         tuple(row.values()))
        conn.commit()
    finally:
        conn.close()


def _local_days(n, back_from=2):
    """The last `n` dates ending `back_from` days before today (Chicago)."""
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    return [(today - timedelta(days=back_from + i)).isoformat() for i in range(n)][::-1]


def _row(db_path, rid):
    return rh.store_rows(db_path=db_path)[rid]


# ── the table is made at boot ───────────────────────────────────────────────

def test_the_rollup_tables_exist_at_boot(db_path):
    conn = models.get_conn(db_path)
    try:
        conn.execute("SELECT restaurant_id, health, severity_rank FROM rpower_store_health").fetchall()
        conn.execute("SELECT date, detail_json FROM rpower_fleet_health_daily").fetchall()
    finally:
        conn.close()


# ── one row per store, each data type on its own ────────────────────────────

def test_a_healthy_store_reads_ok_with_coverage_and_no_gaps(db_path):
    rid = _store(db_path)
    _nights(db_path, rid, _local_days(40, back_from=1))
    out = rh.run_rollup(db_path=db_path)
    assert out["attempted"] == 1 and out["ok"] == 1 and out["failed"] == 0 and out["hit_bound"] is False
    r = _row(db_path, rid)
    assert r["health"] == "ok", r["reasons"]
    assert r["gaps_14d"] == 0 and r["coverage_90d"] == 1.0
    assert r["type_states"]["archive"] == "ok" and r["type_states"]["labor"] == "ok"


def test_the_labor_stamp_alone_no_longer_stands_for_the_pos(db_path):
    """A6-01: labor fresh, the archive two trading nights behind with its
    rows failing — the store is failing, not 'RPOWER connected, fresh'."""
    rid = _store(db_path)
    days = _local_days(40, back_from=1)
    _nights(db_path, rid, days[:-3])
    _nights(db_path, rid, days[-2:-1], **{days[-2]: {"tickets": 0, "lines": 0, "punches": 0,
                                                     "last_error": "ArchiveCountMismatch", "attempts": 2}})
    rh.run_rollup(db_path=db_path)
    r = _row(db_path, rid)
    assert r["type_states"]["labor"] == "ok"
    assert r["type_states"]["archive"] == "failing" and r["health"] == "failing"
    assert any("ArchiveCountMismatch" in x for x in r["reasons"])


def test_a_missed_night_is_a_gap_and_shows_on_the_store(db_path):
    """A6-03: a night owed for over a day and missing is counted, named and
    raises the store's own console issue."""
    rid = _store(db_path)
    days = _local_days(40, back_from=1)
    missing = days[-5]
    _nights(db_path, rid, [d for d in days if d != missing])
    rh.run_rollup(db_path=db_path)
    r = _row(db_path, rid)
    assert r["gaps_14d"] == 1 and r["gap_dates"] == [missing] and r["health"] == "watch"
    issues = rh.store_issues(r)
    assert [i["key"] for i in issues] == ["rpower:gap"] and missing in issues[0]["detail"]


def test_anomalies_are_counted_from_the_nights_columns(db_path):
    rid = _store(db_path)
    days = _local_days(30, back_from=1)
    last = days[-3:]
    _nights(db_path, rid, days, **{last[0]: {"recon_gap": 90.0, "pay_gap": 80.0, "open_punches": 2},
                                   last[1]: {"recon_gap": -60.0, "pay_gap": 75.0, "unknown_sales": 400.0},
                                   last[2]: {"punches": 0, "first_read": json.dumps({"verdict": "suspect"})}})
    rh.run_rollup(db_path=db_path)
    r = _row(db_path, rid)
    assert (r["recon_nights_7d"], r["pay_gap_nights_7d"], r["open_punches_7d"], r["zero_labor_days_7d"],
            r["first_read_suspect_7d"]) == (2, 2, 2, 1, 1)
    assert r["unknown_sales_7d"] == 400.0 and r["has_anomaly"] == 1
    data = [i for i in rh.store_issues(r) if i["key"] == "rpower:data"]
    assert data and "lines vs ticket totals on 2 nights" in data[0]["detail"]


def test_integrity_checks_duplicate_binding_store_key_and_simulated(db_path):
    a = _store(db_path, "Alpha", cg=7, mid="S1")
    b = _store(db_path, "Beta", token="tok-2", cg=7, mid="S1")
    c = _store(db_path, "Gamma", token="demo-store:halsted-rye", cg=8, mid="S9")
    _nights(db_path, a, _local_days(20, back_from=1), **{_local_days(20, back_from=1)[0]: {"store_key": "7:OTHER"}})
    rh.run_rollup(db_path=db_path)
    rows = rh.store_rows(db_path=db_path)
    assert rows[a]["duplicate_binding"] == 1 and rows[b]["duplicate_binding"] == 1
    assert rows[a]["store_key_mismatch"] == 1 and rows[a]["health"] == "failing"
    assert rows[c]["simulated_not_demo"] == 1
    keys = {i["key"] for i in rh.store_issues(rows[c])}
    assert "rpower:integrity:simulated_not_demo" in keys


def test_a_demo_store_on_the_same_store_is_not_a_duplicate_binding(db_path):
    """Demo rid 4 mirrors EJ's real store — it must keep working."""
    a = _store(db_path, "Real", cg=7, mid="S1")
    d = _store(db_path, "Demo", token="tok-2", cg=7, mid="S1", is_demo=1)
    rh.run_rollup(db_path=db_path)
    rows = rh.store_rows(db_path=db_path)
    assert rows[a]["duplicate_binding"] == 0 and rows[d]["duplicate_binding"] == 0


def test_the_rollup_is_bounded_and_resumes_from_its_cursor(db_path, monkeypatch):
    monkeypatch.setattr(rh, "BATCH", 2)
    ids = [_store(db_path, f"S{i}", token=f"t{i}", mid=f"M{i}") for i in range(5)]
    clock = iter([0.0] + [100.0] * 50)
    monkeypatch.setattr(rh, "_clock", lambda: next(clock))
    out = rh.run_rollup(db_path=db_path, max_seconds=50)
    assert out["hit_bound"] is True and out["attempted"] == 2
    assert set(rh.store_rows(db_path=db_path)) == set(ids[:2])
    monkeypatch.setattr(rh, "_clock", lambda: 0.0)
    out = rh.run_rollup(db_path=db_path)
    assert out["hit_bound"] is False and out["attempted"] == 5
    assert set(rh.store_rows(db_path=db_path)) == set(ids)


def test_a_store_no_longer_bound_drops_out_on_a_full_pass(db_path):
    rid = _store(db_path)
    rh.run_rollup(db_path=db_path)
    models.update_restaurant(rid, {"rpower_token": None, "rpower_store_mid": None}, db_path=db_path)
    rh.run_rollup(db_path=db_path)
    assert rid not in rh.store_rows(db_path=db_path)


# ── the fleet rules, from the rollup only (A6 section 3.6) ──────────────────

def _put(db_path, rid, **cols):
    """A rollup row written directly (the rules read only the table)."""
    now = rh._stamp(rh._utcnow())
    row = {c: None for c in rh._ROW_COLS}
    row.update(restaurant_id=rid, name=f"S{rid}", is_demo=0, in_service=1, health="ok", health_since=now,
               severity_rank=0, type_states=json.dumps({}), reasons="[]", gap_dates="[]", updated_at=now,
               duplicate_binding=0, store_key_mismatch=0, simulated_not_demo=0, has_anomaly=0, gaps_14d=0)
    row.update(cols)
    conn = models.get_conn(db_path)
    try:
        rh._write_rows(conn, [row])
    finally:
        conn.close()


def _fleet_day(db_path, detail):
    conn = models.get_conn(db_path)
    try:
        conn.execute("INSERT INTO rpower_fleet_health_daily (date, stores, detail_json, updated_at) VALUES (?,?,?,?) "
                     "ON CONFLICT(date) DO UPDATE SET detail_json=excluded.detail_json",
                     (rh._utcnow().date().isoformat(), 0, json.dumps(detail), rh._stamp(rh._utcnow())))
        conn.commit()
    finally:
        conn.close()


def _keys(db_path):
    return {r["key"]: r for r in rh.fleet_rules(db_path=db_path)}


def test_a_data_type_failing_across_the_fleet_is_one_rule_once_sustained(db_path):
    old = rh._stamp(rh._utcnow() - timedelta(hours=2))
    for i in range(1, 41):
        bad = i <= 6
        _put(db_path, i, health="failing" if bad else "ok", health_since=old,
             type_states=json.dumps({"archive": "failing" if bad else "ok"}))
    rule = _keys(db_path)["rpower_failing:archive"]
    assert rule["severity"] == "critical" and rule["page"] == "warn" and sorted(rule["fold"]) == list(range(1, 7))
    # The same six, failing for a minute: not yet a fleet problem.
    for i in range(1, 7):
        _put(db_path, i, health="failing", health_since=rh._stamp(rh._utcnow()),
             type_states=json.dumps({"archive": "failing"}))
    assert "rpower_failing:archive" not in _keys(db_path)


def test_one_store_failing_is_its_own_issue_not_a_fleet_rule(db_path):
    _put(db_path, 1, health="failing", health_since=rh._stamp(rh._utcnow() - timedelta(days=1)),
         type_states=json.dumps({"archive": "failing", "labor": "stale"}))
    assert _keys(db_path) == {}


def test_stale_share_is_a_fleet_rule(db_path):
    """A6-12: stale (the nightly runs not reaching stores) counts, not only error."""
    for i in range(1, 11):
        _put(db_path, i, health="stale" if i <= 3 else "ok",
             type_states=json.dumps({"labor": "stale" if i <= 3 else "ok"}))
    assert _keys(db_path)["rpower_stale"]["page"] == "warn"


def test_an_auth_wave_is_one_urgent_page_and_folds_the_stores(db_path):
    """A4-16: a partner token revoked is one problem, not N reconnects."""
    now = rh._stamp(rh._utcnow() - timedelta(minutes=10))
    for i in range(1, 5):
        _put(db_path, i, health="auth", auth_failed_at=now, token_bucket="rpower:shared")
    rule = _keys(db_path)["rpower_auth_wave"]
    assert rule["page"] == "urgent" and rule["severity"] == "critical" and sorted(rule["fold"]) == [1, 2, 3, 4]


def test_a_shared_token_refused_long_ago_stays_one_console_issue_without_a_page(db_path):
    old = rh._stamp(rh._utcnow() - timedelta(hours=5))
    _put(db_path, 1, health="auth", auth_failed_at=old, token_bucket="rpower:shared")
    _put(db_path, 2, health="auth", auth_failed_at=old, token_bucket="rpower:shared")
    _put(db_path, 3, health="ok")
    rule = _keys(db_path)["rpower_auth_wave"]
    assert rule["page"] is None and sorted(rule["fold"]) == [1, 2]


def test_one_store_refused_is_never_a_wave(db_path):
    _put(db_path, 1, health="auth", auth_failed_at=rh._stamp(rh._utcnow()), token_bucket="rpower:a")
    _put(db_path, 2, health="ok", token_bucket="rpower:b")
    assert "rpower_auth_wave" not in _keys(db_path)


def test_queue_failed_and_expired_share_per_kind_is_a_rule(db_path):
    """A6-02: an expired archive row alerted nobody."""
    _put(db_path, 1)
    _fleet_day(db_path, {"queue_kinds": {"pos_archive": {"rows_24h": 100, "failed_24h": 2, "expired_24h": 3},
                                         "learner": {"rows_24h": 500, "failed_24h": 1, "expired_24h": 0}}})
    keys = _keys(db_path)
    assert keys["queue_failed:pos_archive"]["page"] == "warn" and "queue_failed:learner" not in keys


def test_budget_and_volume_rules_read_the_ledger_rollup(db_path):
    """A6-17: 429s, budget refusals and volume against the 7-day median."""
    _put(db_path, 1)
    today = rh._utcnow().date()
    by_day = {(today - timedelta(days=i)).isoformat(): 1000 for i in range(2, 9)}
    by_day[(today - timedelta(days=1)).isoformat()] = 2500
    _fleet_day(db_path, {"ledger_1h": {"n": 400, "s429": 10, "skipped": 40}, "requests_by_day": by_day})
    keys = _keys(db_path)
    assert keys["rpower_budget_skipped"]["page"] == "warn" and keys["rpower_budget_429"]["page"] == "warn"
    assert keys["rpower_volume"]["page"] == "warn" and "2.5x" in keys["rpower_volume"]["title"]


def test_a_demo_or_cancelled_store_never_counts_toward_a_rule(db_path):
    now = rh._stamp(rh._utcnow())
    for i in range(1, 5):
        _put(db_path, i, health="auth", auth_failed_at=now, token_bucket="rpower:x", is_demo=1)
    assert _keys(db_path) == {}


def test_the_platform_check_pages_an_auth_wave_as_urgent(db_path, monkeypatch):
    import scheduler
    now = rh._stamp(rh._utcnow() - timedelta(minutes=5))
    for i in range(1, 4):
        _put(db_path, i, health="auth", auth_failed_at=now, token_bucket=f"rpower:{i}")
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    pages = []
    monkeypatch.setattr(ops, "page_operator", lambda key, subject, lines, **k: (pages.append((key, lines)),
                                                                                {"sent": True})[1])
    out = ops.check_platform_sla(send=True, db_path=db_path, write_ok=True)
    assert any(r["key"] == "rpower_auth_wave" for r in out["rpower_rules"])
    assert any("refused 3 stores' tokens" in p for p in out["problems"])
    assert pages and pages[-1][0] == "platform_sla_alert"


# ── the owner told once (A6-10) ─────────────────────────────────────────────

@pytest.fixture
def notices(monkeypatch):
    import notify
    import scheduler
    sent, claimed = [], set()
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(ops, "claim_period", lambda job, period: (job, period) not in claimed and not claimed.add((job, period)))
    monkeypatch.setattr(notify, "raise_alert", lambda rid, kind, sms, subject, **k: sent.append((rid, kind, subject)))
    return sent


def test_the_owner_is_told_once_when_rpower_refuses_the_token(db_path, notices):
    rid = _store(db_path, "Owner Told", rpower_auth_failed_at="2026-10-21 09:00:00")
    rh.run_rollup(db_path=db_path)
    assert notices == [(rid, "connection_lost", "RPOWER needs reconnecting — Owner Told")]
    rh.run_rollup(db_path=db_path)
    assert len(notices) == 1, "a second pass must not tell the owner again"
    r = _row(db_path, rid)
    assert r["health"] == "auth" and r["token_state"] == "auth_failed"


def test_a_shared_token_is_the_operators_not_the_owners(db_path, notices):
    _store(db_path, "One", token="partner", mid="A", rpower_auth_failed_at="2026-10-21 09:00:00")
    _store(db_path, "Two", token="partner", mid="B", rpower_auth_failed_at="2026-10-21 09:00:00")
    rh.run_rollup(db_path=db_path)
    assert notices == []


def test_no_owner_notice_from_a_process_that_may_not_send(db_path, notices, monkeypatch):
    import scheduler
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    _store(db_path, "Local", rpower_auth_failed_at="2026-10-21 09:00:00")
    rh.run_rollup(db_path=db_path)
    assert notices == []


# ── data findings are not job failures (A6-04) ──────────────────────────────

def test_capture_keeps_the_data_quality_kind(db_path):
    ops.capture(RuntimeError("recon gap $40"), job="pos_reconcile", restaurant_id=7, kind="data_quality",
                db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        row = conn.execute("SELECT kind, restaurant_id FROM job_failures WHERE job='pos_reconcile'").fetchone()
    finally:
        conn.close()
    assert (row["kind"], row["restaurant_id"]) == ("data_quality", 7)


def _platform(db_path, failures, recs=()):
    import admin_ops
    d = {"job_failures": failures, "job_failures_has_kind": True, "resolved": {}}
    with admin_ops._using_db(db_path):
        issues, facts = admin_ops._platform_issues(list(recs), d)
    return {i["key"]: i for i in issues}, facts


def _f(job, rid=None, kind="job", n=1):
    stamp = (datetime.utcnow() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    return [{"job": job, "error": "boom", "created_at": stamp, "restaurant_id": rid, "kind": kind} for _ in range(n)]


def test_a_job_failing_at_one_store_is_a_warning_not_a_permanent_critical(db_path):
    recs = [{"id": i, "segment": "customer", "name": f"S{i}", "billing": {"status": "active"}, "modules": [],
             "reviews": {}} for i in range(100)]
    keys, facts = _platform(db_path, _f("queue:pos_archive", rid=5, n=8), recs)
    assert keys["job:queue:pos_archive"]["severity"] == "warning" and "at 1 store" in keys["job:queue:pos_archive"]["title"]
    keys, _ = _platform(db_path, sum((_f("queue:pos_archive", rid=i) for i in range(10)), []), recs)
    assert keys["job:queue:pos_archive"]["severity"] == "critical", "10% of the fleet is critical"
    keys, _ = _platform(db_path, _f("backup_db", n=5), recs)
    assert keys["job:backup_db"]["severity"] == "critical", "the job itself failing 5x stays critical"


def test_data_findings_are_never_a_job_failure(db_path):
    keys, facts = _platform(db_path, _f("pos_reconcile", rid=5, kind="data_quality", n=9))
    assert "job:pos_reconcile" not in keys and facts["failures_24h"]["data_quality"] == 9


def test_the_failure_digest_lists_data_checks_apart(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    ops.capture(RuntimeError("gap"), job="pos_reconcile", restaurant_id=3, kind="data_quality")
    ops.capture(RuntimeError("crash"), job="pos_sync", restaurant_id=3)
    since = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert [f["job"] for f in ops.failures_since(since, data_checks=False)] == ["pos_sync"]
    assert [f["job"] for f in ops.failures_since(since, data_checks=True)] == ["pos_reconcile"]
    assert {f["job"] for f in ops.failures_since(since)} == {"pos_sync", "pos_reconcile"}


# ── the console (A6-09, A6-22, A6-15, the RPOWER view) ──────────────────────

@pytest.fixture
def console(monkeypatch, db_path):
    from flask import Flask
    import admin_ops
    import admin_routes
    import auth
    import client_api
    import mobile_api
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, admin_routes, client_api, mobile_api, admin_ops):
        monkeypatch.setattr(mod, "get_conn", redirect)
    auth.init_auth(db_path=db_path)
    models.init_two_fa_backup_codes(db_path=db_path)
    models.init_email_log(db_path=db_path)
    import ai_utils
    conn = real(db_path)
    conn.executescript(ai_utils._USAGE_TABLE_SQL)
    conn.commit()
    conn.close()
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": None, "is_admin": 1,
                                                           "username": "will", "email": "x@x.com", "role": "client"})
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_routes.admin_bp)
    admin_ops.invalidate_fleet_cache()
    return app.test_client()


def _issues(client, rid):
    import admin_ops
    admin_ops.invalidate_fleet_cache()
    return {i["key"].split(":", 1)[1]: i for i in client.get(f"/admin/api/client/{rid}").get_json()["client"]["issues"]}


def test_the_auth_latch_is_a_critical_console_issue_at_once(console, db_path):
    rid = _store(db_path, "Latched", rpower_auth_failed_at="2026-10-21 10:00:00")
    i = _issues(console, rid)["int:rpower"]
    assert i["severity"] == "critical" and "refused this store's token on 10/21/26" in i["detail"]


def test_a_pos_error_with_a_retry_coming_is_a_warning(console, db_path):
    """A6-22: critical only for auth or once the retries are spent."""
    import data_health
    rid = _store(db_path, "Retrying", rpower_sync_error="RPOWER returned 503")
    data_health.record_attempt(rid, "pos", False, provider="rpower", error="RPOWER returned 503",
                               error_class="provider", next_retry_at="2099-01-01 10:00:00", db_path=db_path)
    assert _issues(console, rid)["int:rpower"]["severity"] == "warning"
    data_health.record_attempt(rid, "pos", False, provider="rpower", error="RPOWER returned 503",
                               error_class="provider", next_retry_at=None, db_path=db_path)
    assert _issues(console, rid)["int:rpower"]["severity"] == "critical"


def test_a_data_finding_is_a_store_data_issue(console, db_path):
    rid = models.create_restaurant(models.Restaurant(name="Toast Place", owner_email="t@x.test"), db_path=db_path)
    ops.capture(RuntimeError("short first read"), job="sales_check", restaurant_id=rid, kind="data_quality",
                db_path=db_path)
    i = _issues(console, rid)["data:sales_check"]
    assert i["severity"] == "warning" and "1 issue" in i["title"]


def test_integrations_are_filtered_and_paged_by_the_server(console, db_path):
    for n in range(3):
        _store(db_path, f"Int{n}", token=f"t{n}", mid=f"M{n}", rpower_sync_error="boom" if n == 0 else None)
    d = console.get("/admin/api/integrations?f=error&page=1&per_page=1").get_json()
    assert d["filter"] == "error" and d["total"] == 1 and d["pages"] == 1 and len(d["rows"]) == 1
    assert d["counts"]["all"] >= 3 and d["counts"]["error"] == 1
    d = console.get("/admin/api/data-sources?page=1&per_page=5").get_json()
    assert "pages" in d and "total" in d


def test_the_rpower_view_and_drawer_read_the_rollup(console, db_path):
    rid = _store(db_path, "Viewed")
    days = _local_days(30, back_from=1)
    _nights(db_path, rid, [d for d in days if d != days[-4]])
    rh.run_rollup(db_path=db_path)
    d = console.get("/admin/api/rpower-health?limit=10").get_json()
    assert d["ok"] and d["total"] == 1 and d["rows"][0]["restaurant_id"] == rid and d["fleet"]["connected"] == 1
    assert d["rows"][0]["cursor"] and d["next"] is None
    assert console.get("/admin/api/rpower-health?health=failing").get_json()["total"] == 0
    x = console.get(f"/admin/api/rpower-health/{rid}").get_json()
    assert len(x["calendar"]) == rh.WINDOW_DAYS and {"archived", "missing"} <= {c["state"] for c in x["calendar"]}
    assert [t["type"] for t in x["timeline"]] == list(rh.DATA_TYPES)
    assert console.get("/admin/api/rpower-health/99999").status_code == 404


def test_keyset_paging_walks_every_store_once(db_path):
    for i in range(1, 8):
        _put(db_path, i, severity_rank=(i % 3) * 100)
    seen, after = [], None
    while True:
        pg = rh.page(limit=3, after=after, db_path=db_path)
        seen += [r["restaurant_id"] for r in pg["rows"]]
        after = pg["next"]
        if not after:
            break
    assert sorted(seen) == list(range(1, 8)) and len(seen) == 7


# ── the fleet POS reading and the status page off the rollup (A6-14/16) ─────

def test_the_fleet_reading_never_hydrates_every_restaurant(db_path, monkeypatch):
    import pos_health
    monkeypatch.setattr(models, "get_all_restaurants", lambda *a, **k: pytest.fail("hydrated every restaurant"))
    rid = _store(db_path, "Narrow", rpower_sync_error="boom")
    rh.run_rollup(db_path=db_path)
    out = pos_health.fleet_state(db_path=db_path)
    assert out["rpower"]["connected"] == 1


def test_the_rpower_fleet_reading_counts_stale_as_failing(db_path):
    import pos_health
    _put(db_path, 1, health="stale")
    _put(db_path, 2, health="ok")
    rows = pos_health.fleet_rows(db_path=db_path)
    assert sorted(f for p, f, _r in rows if p == "rpower") == [False, True]


def test_the_status_page_degrades_only_past_a_share(db_path, monkeypatch):
    import status_manager as sm
    from auth import create_user, init_auth
    init_auth(db_path=db_path)
    monkeypatch.setattr(sm, "_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    monkeypatch.setattr(sm, "DB_PATH", db_path, raising=False)
    for n in range(30):
        rid = models.create_restaurant(models.Restaurant(name=f"T{n}", owner_email=f"t{n}@x.test"), db_path=db_path)
        models.update_restaurant(rid, {"toast_restaurant_guid": f"g{n}",
                                       "toast_last_synced": datetime.now(timezone.utc).isoformat(),
                                       "toast_sync_error": "401" if n == 0 else None}, db_path=db_path)
        create_user(rid, f"u{n}", f"u{n}@x.test", "Passw0rd!xyz", db_path=db_path)
    sm.seed_default_services()
    sm._check_labor_analytics()
    conn = models.get_conn(db_path)
    try:
        row = conn.execute("SELECT status FROM service_status WHERE service_key='labor_analytics'").fetchone()
    finally:
        conn.close()
    assert row["status"] == "operational", "one store in 30 is that store's issue, not the platform's"


# ── the DSR SLA (A6-20) and the storm page (A6-26) ──────────────────────────

def test_a_provisional_night_is_missing_for_the_sla(db_path, monkeypatch):
    from dsr import pipeline, store
    import pos
    rid = models.create_restaurant(models.Restaurant(name="Prov", owner_email="p@x.test",
                                                     timezone="America/Chicago"), db_path=db_path)
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", object()))
    now = datetime(2026, 9, 29, 14, 0)
    rep = store.create_report(rid, "2026-09-28", db_path=db_path)
    store.set_stage(rep["id"] if isinstance(rep, dict) else rep, "provisional", db_path=db_path)
    missing = pipeline.nights_missing(db_path=db_path, now_utc=now, use_cache=False)
    assert [(m["restaurant_id"], m["status"]) for m in missing] == [(rid, "provisional")]


def test_the_storm_page_is_share_gated(db_path):
    import notify
    for n in range(100):
        models.create_restaurant(models.Restaurant(name=f"R{n}", owner_email=f"r{n}@x.test"), db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        conn.execute("INSERT INTO alert_storm_caps (restaurant_id, local_day, until_at) VALUES (1, '2026-10-21', "
                     "'2026-10-22 05:00:00')")
        conn.commit()
    finally:
        conn.close()
    assert notify._storm_pages(db_path) is False, "1 of 100 is not a fleet incident"
    conn = models.get_conn(db_path)
    try:
        for rid in range(2, 5):
            conn.execute("INSERT INTO alert_storm_caps (restaurant_id, local_day, until_at) VALUES (?, '2026-10-21', "
                         "'2026-10-22 05:00:00')", (rid,))
        conn.commit()
    finally:
        conn.close()
    assert notify._storm_pages(db_path) is True


def test_one_storm_still_pages_on_todays_small_fleet(db_path):
    import notify
    models.create_restaurant(models.Restaurant(name="Solo", owner_email="s@x.test"), db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        conn.execute("INSERT INTO alert_storm_caps (restaurant_id, local_day, until_at) VALUES (1, '2026-10-21', "
                     "'2026-10-22 05:00:00')")
        conn.commit()
    finally:
        conn.close()
    assert notify._storm_pages(db_path) is True


# ── wiring ──────────────────────────────────────────────────────────────────

def test_the_rollup_is_a_registered_job_the_loop_runs():
    import inspect
    import jobs_registry
    import scheduler
    spec = jobs_registry.JOBS["rpower_health_rollup"]
    assert spec["target"] == ("rpower_health", "run_rollup") and spec["sla_minutes"] == 60
    assert '_ops.run_job("rpower_health_rollup", _rpower_health.run_rollup)' in inspect.getsource(scheduler.scheduler_loop)


def test_the_fleet_row_has_a_retention_window_and_a_reader():
    assert ops._RETENTION_DAYS["rpower_fleet_health_daily"] == 400
    assert ops._RETENTION_COLUMN["rpower_fleet_health_daily"] == "date"
    assert "data_quality" in ops.CAPTURE_KINDS


def test_every_source_the_rollup_reads_is_readable(db_path, caplog):
    """No source table silently unreadable — on SQLite and on Postgres."""
    import logging
    rid = _store(db_path, "Readable")
    _nights(db_path, rid, _local_days(10, back_from=1))
    with caplog.at_level(logging.WARNING, logger="rpower_health"):
        rh.run_rollup(db_path=db_path)
        rh.fleet(db_path=db_path)
        rh.fleet_rules(db_path=db_path)
        rh.store_detail(rid, db_path=db_path)
        rh.page(q="Read", data_type="archive", anomaly=True, coverage_lt=0.5, partner=3, db_path=db_path)
    bad = [r.getMessage() for r in caplog.records if "unreadable" in r.getMessage()]
    assert not bad, bad


def test_recipe_coverage_is_refreshed_once_a_day_within_a_budget(db_path, monkeypatch):
    """A6-21: the owner's own coverage figure, rolled up — never per request."""
    import inventory_ledger
    calls = []
    monkeypatch.setattr(inventory_ledger, "recipe_coverage",
                        lambda rid, *a, **k: calls.append(rid) or {"coverage_pct": 62.5})
    monkeypatch.setattr(rh, "RECIPE_COVERAGE_PER_PASS", 2)
    ids = [_store(db_path, f"Rc{i}", token=f"rc{i}", mid=f"R{i}") for i in range(3)]
    rh.run_rollup(db_path=db_path)
    assert len(calls) == 2
    rh.run_rollup(db_path=db_path)
    assert len(calls) == 3, "the third store is reached next pass; the first two are not re-read within a day"
    rows = rh.store_rows(db_path=db_path)
    assert all(rows[i]["recipe_coverage_pct"] == 62.5 for i in ids)
    assert rh.fleet(db_path=db_path)["recipe_coverage_p50"] == 62.5


# ── F5's surfaces, read defensively (missed runs, lanes, budget check) ─────

def test_a_missed_pos_run_is_on_the_store(db_path):
    """An expired pos_archive row is recorded as a missed window (F5's
    EXPIRE_HOOKS); the store's row counts it and the console raises it."""
    rid = _store(db_path, "Missed")
    _nights(db_path, rid, _local_days(20, back_from=1))
    conn = models.get_conn(db_path)
    try:
        conn.execute("INSERT INTO missed_windows (job, restaurant_id, local_date, detail) VALUES "
                     "('pos_archive', ?, '2026-10-20', 'expired')", (rid,))
        conn.commit()
    finally:
        conn.close()
    rh.run_rollup(db_path=db_path)
    r = _row(db_path, rid)
    assert r["missed_runs_7d"] == 1 and r["health"] == "watch"
    assert "rpower:missed" in {i["key"] for i in rh.store_issues(r)}


def test_the_capacity_reading_is_absent_without_pos_capacity(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_capacity(name, *a, **k):
        if name == "pos_capacity":
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_capacity)
    assert rh._capacity() == {}


def test_a_shed_budget_and_the_budget_checks_findings_are_one_rule(db_path, monkeypatch):
    _put(db_path, 1)
    _fleet_day(db_path, {"capacity": {"state": {"shed_bulk": True, "ratio": 1.4}},
                         "budget_findings": [{"error": "RPOWER budget: tonight needs ~9000 requests", "at": "x"}]})
    rule = _keys(db_path)["rpower_capacity"]
    assert rule["page"] == "warn" and "held tonight" in rule["detail"] and "9000" in rule["detail"]
    keys, _facts = _platform(db_path, _f("pos_budget_check", n=6))
    assert "job:pos_budget_check" not in keys, "the rule says it — no second 'Job failed' row"


def test_the_queue_is_read_by_kind_and_group(db_path):
    import time as _t
    conn = models.get_conn(db_path)
    try:
        for kind, grp, state in (("pos_onboard", "pos_bulk", "expired"), ("live_labor", "live", "queued"),
                                 ("pos_archive", "pos_io", "failed")):
            conn.execute("INSERT INTO jobs (kind, grp, restaurant_id, priority_class, state, enqueued_at, finished_at) "
                         "VALUES (?,?,?,?,?,?,?)", (kind, grp, 1, "bulk", state, _t.time(), _t.time()))
        conn.commit()
    finally:
        conn.close()
    _store(db_path, "Q")
    rh.run_rollup(db_path=db_path)
    f = rh.fleet(db_path=db_path)
    assert f["queue_kinds"]["pos_onboard"]["grp"] == "pos_bulk" and f["queue_kinds"]["pos_onboard"]["expired_24h"] == 1
    assert f["queue_groups"]["live"]["queued"] == 1 and f["queue_groups"]["pos_io"]["failed_24h"] == 1
