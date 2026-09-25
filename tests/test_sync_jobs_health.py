"""Workstream J of the Data Freshness fix round (9/24/26): every ingest point
records its attempts, sync jobs stop reporting success on failure, a job that
didn't run is noticed from the request path, depletion is a freshness source
the snapshot and trusted orders respect, open business days are provisional,
partial pages fail, Toast names resolve, the clock-in feed is local, POS syncs
recover on their own, and the intraday slot is bounded and resumable.

Top-50 items #1, #4, #5, #9, #15, #16, #19, #20, #26, #28, #29, #30, #32,
#35, #40, #41, #43, #47 and the owner-approved scale fixes."""
import json
import types
from datetime import date, datetime, timedelta, timezone

import pytest

import data_freshness as df
import data_health as dh
import models
import ops
from models import Restaurant, create_restaurant, get_conn, update_restaurant


@pytest.fixture(autouse=True)
def _fresh_cache():
    dh.invalidate()
    yield
    dh.invalidate()


@pytest.fixture
def tmpdb(db_path, monkeypatch):
    """Every call-time models.get_conn() — ops, data_health, scheduler,
    strategy_jobs — reaches the test database."""
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda _ignored=None: real(db_path))
    import admin_ops
    monkeypatch.setattr(admin_ops, "get_conn", lambda _ignored=None: real(db_path))
    return db_path


def _rid(db_path, **kw):
    kw.setdefault("name", "Sync Cafe")
    kw.setdefault("owner_email", "s@x.test")
    rid = create_restaurant(Restaurant(name=kw.pop("name"), owner_email=kw.pop("owner_email")), db_path=db_path)
    if kw:
        update_restaurant(rid, kw, db_path=db_path)
    return rid


def _row(db_path, rid):
    return models.get_restaurant(rid, db_path=db_path)


def _q(db_path, sql, args=()):
    c = get_conn(db_path)
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _x(db_path, sql, args=()):
    c = get_conn(db_path)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _local_today(db_path, rid):
    return df._today(_row(db_path, rid))


# ── #4: job_runs tells the truth ─────────────────────────────────────────────

def test_a_sweep_where_restaurants_failed_is_a_partial_or_failed_run(tmpdb):
    ops.run_job("pos_sync", lambda: {"attempted": 3, "ok": 0, "failed": 3, "hit_bound": False})
    ops.run_job("depletion_x", lambda: {"attempted": 3, "ok": 2, "failed": 1})
    ops.run_job("clean_x", lambda: {"attempted": 3, "ok": 3, "failed": 0})
    ops.run_job("bound_x", lambda: {"attempted": 2, "ok": 2, "failed": 0, "hit_bound": True})
    ops.run_job("named_x", lambda: {"analysed": 2, "failed": 1})
    runs = {r["job"]: r for r in _q(tmpdb, "SELECT job, ok, result_json, error FROM job_runs")}
    assert runs["pos_sync"]["ok"] == ops.RUN_FAILED and "3 of 3 failed" in runs["pos_sync"]["error"]
    assert runs["depletion_x"]["ok"] == ops.RUN_PARTIAL and json.loads(runs["depletion_x"]["result_json"])["failed"] == 1
    assert runs["clean_x"]["ok"] == ops.RUN_OK
    assert runs["bound_x"]["ok"] == ops.RUN_PARTIAL
    assert runs["named_x"]["ok"] == ops.RUN_PARTIAL


def test_the_pos_wrapper_no_longer_swallows_a_crash(tmpdb, monkeypatch):
    import pos, scheduler
    monkeypatch.setattr(pos, "sync_all", lambda **k: (_ for _ in ()).throw(RuntimeError("database is locked")))
    assert ops.run_job("pos_sync", scheduler.run_toast_sync) is None
    run = _q(tmpdb, "SELECT ok FROM job_runs WHERE job='pos_sync'")[0]
    assert run["ok"] == 0
    assert any(f["job"] == "pos_sync" for f in _q(tmpdb, "SELECT job FROM job_failures"))


def test_a_dead_run_is_reclaimed_by_the_claim_it_ran_under(tmpdb):
    ops.init_ops(tmpdb)
    _x(tmpdb, "INSERT INTO job_period_claims (job_key, claimed_at) VALUES ('ops_digest:2026-09-24', "
              "datetime('now', '-180 minutes'))")
    _x(tmpdb, "INSERT INTO job_runs (job, started_at, claim_job) VALUES ('ops_failure_digest', "
              "datetime('now', '-179 minutes'), 'ops_digest')")
    assert ops.claim_period("ops_digest", "2026-09-24") is True


def test_run_job_stores_the_claim_it_ran_under(tmpdb):
    ops.run_job("weekly_digests", lambda: None, claim="weekly_digest")
    assert _q(tmpdb, "SELECT claim_job FROM job_runs WHERE job='weekly_digests'")[0]["claim_job"] == "weekly_digest"


def test_run_now_has_no_thirty_minute_hole(tmpdb, monkeypatch):
    import admin_ops, scheduler
    ran = []
    monkeypatch.setattr(scheduler, "run_daily_fetch", lambda: ran.append(1))

    class _UtcServer(datetime):
        """Railway's clock: local time IS UTC, so a local-vs-UTC mix-up
        can't mask the window."""
        @classmethod
        def now(cls, tz=None):
            return datetime.utcnow() if tz is None else datetime.now(tz)
    monkeypatch.setattr(admin_ops, "datetime", _UtcServer)
    _x(tmpdb, "INSERT INTO job_runs (job, started_at) VALUES ('review_fetch', datetime('now', '-45 minutes'))")
    out = admin_ops.run_job_now("review_fetch", "test")
    assert out["ok"] is False and "already running" in out["error"] and ran == []


# ── #5: a job that didn't run is noticed outside the scheduler ────────────────

class _StopLoop(BaseException):
    pass


def test_the_heartbeat_is_stamped_at_the_end_of_a_tick_and_a_loop_error_is_captured(monkeypatch):
    import scheduler
    stamps, captured = [], []
    monkeypatch.setattr(scheduler._ops, "acquire_scheduler_lease", lambda *a, **k: True)

    def _boom(*a, **k):
        raise RuntimeError("tick died after the lease")
    monkeypatch.setattr(scheduler._ops, "claim_period", _boom)
    monkeypatch.setattr(scheduler._ops, "capture", lambda e, **k: captured.append((str(e), k.get("job"))))
    monkeypatch.setattr(scheduler, "record_scheduler_heartbeat", lambda *a, **k: stamps.append(1))
    monkeypatch.setattr(scheduler, "run_health_checks", lambda *a, **k: None)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: (_ for _ in ()).throw(_StopLoop()))
    with pytest.raises(_StopLoop):
        scheduler.scheduler_loop()
    assert stamps == [], "a tick that died must not leave a fresh heartbeat"
    assert ("tick died after the lease", "scheduler_loop") in captured


def test_an_expected_job_past_its_sla_is_overdue_and_alerts_will_once(tmpdb, monkeypatch):
    import scheduler, status_manager
    _x(tmpdb, "INSERT INTO job_runs (job, started_at, finished_at, ok) VALUES "
              "('pos_sync', datetime('now','-30 hours'), datetime('now','-30 hours'), 1)")
    _x(tmpdb, "INSERT INTO job_runs (job, started_at, finished_at, ok) VALUES "
              "('review_fetch', datetime('now','-1 hours'), datetime('now','-1 hours'), 1)")
    late = {j["job"]: j for j in ops.jobs_overdue()}
    assert "pos_sync" in late and late["pos_sync"]["max_hours"] == 26
    assert "review_fetch" not in late
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(status_manager, "scheduler_heartbeat_age_minutes", lambda: 2.0)
    sent = []
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: sent.append(lines) or True)
    assert ops.check_platform_sla()["alerted"] is True
    assert ops.check_platform_sla()["alerted"] is False, "one alert an hour at most"
    assert len(sent) == 1 and any("pos_sync" in x for x in sent[0])


def test_health_reports_overdue_jobs_as_degraded(tmpdb, monkeypatch):
    import status_manager
    monkeypatch.setattr(ops, "check_platform_sla",
                        lambda **k: {"jobs_overdue": [{"job": "pos_sync", "max_hours": 26}]})
    payload, status = status_manager.health_snapshot(tmpdb)
    assert status == 200 and payload["status"] == "degraded" and payload["jobs_overdue"] == ["pos_sync"]


def test_a_laptop_never_pages_will(tmpdb, monkeypatch):
    import scheduler, status_manager
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    monkeypatch.setattr(status_manager, "scheduler_heartbeat_age_minutes", lambda: 500.0)
    monkeypatch.setattr(ops, "alert_will", lambda *a: pytest.fail("paged from a laptop"))
    assert ops.check_platform_sla()["alerted"] is False


# ── #1 / #9: depletion is recorded, caught up and a freshness source ─────────

def _recipe(db_path, rid, guid="g1"):
    c = get_conn(db_path)
    try:
        ing = c.execute("INSERT INTO ingredients (restaurant_id, name, unit, is_active) VALUES (?,?,?,1)",
                        (rid, "Short rib", "lb")).lastrowid
        mi = c.execute("INSERT INTO menu_items (restaurant_id, toast_guid, name) VALUES (?,?,?)",
                       (rid, guid, "Braise")).lastrowid
        c.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
                  (mi, ing, 0.5))
        c.commit()
        return ing
    finally:
        c.close()


def test_depletion_catches_up_since_the_last_depleted_day_capped_at_fourteen(tmpdb, monkeypatch):
    import pos, scheduler, inventory_ledger
    rid = _rid(tmpdb, module_inventory=1)
    _recipe(tmpdb, rid)
    windows = []
    monkeypatch.setattr(pos, "supports", lambda r, cap: True)
    monkeypatch.setattr(pos, "fetch_business_days",
                        lambda r, s, e: windows.append((s, e)) or ({}, "rpower"))
    end = scheduler._chi_now().date()
    dh.record_attempt(rid, "depletion", True, data_through=(end - timedelta(days=9)).isoformat())
    out = scheduler.run_daily_depletion_sync()
    assert windows[-1] == (end - timedelta(days=9), end)
    assert out["attempted"] == 1 and out["ok"] == 1
    row = dh.health_rows(rid)["depletion"]
    assert row["consecutive_failures"] == 0 and row["provider"] == "rpower" and row["data_through"]
    dh.record_attempt(rid, "depletion", True, data_through=(end - timedelta(days=40)).isoformat())
    scheduler.run_daily_depletion_sync()
    assert windows[-1][0] == end - timedelta(days=scheduler.DEPLETION_CATCHUP_MAX_DAYS)


def test_a_failed_depletion_night_is_recorded_per_restaurant(tmpdb, monkeypatch):
    import pos, scheduler
    rid = _rid(tmpdb, module_inventory=1)
    _recipe(tmpdb, rid)
    monkeypatch.setattr(pos, "supports", lambda r, cap: True)
    monkeypatch.setattr(pos, "fetch_business_days",
                        lambda r, s, e: (_ for _ in ()).throw(RuntimeError("HTTP 403 ordersBulk")))
    out = scheduler.run_daily_depletion_sync()
    assert out["failed"] == 1 and out["ok"] == 0
    row = dh.health_rows(rid)["depletion"]
    assert row["consecutive_failures"] == 1 and "403" in row["last_error"]


def test_depletion_is_a_freshness_source_for_food(tmpdb):
    rid = _rid(tmpdb, module_inventory=1)
    for m in ("inventory", "food", "food_cost", "ordering"):
        assert "depletion" in df.MODULE_SOURCES[m], m
    assert df.source_state(_row(tmpdb, rid), "depletion", db_path=tmpdb)["state"] == "not_connected"
    _recipe(tmpdb, rid)
    today = _local_today(tmpdb, rid)
    dh.record_attempt(rid, "depletion", True, data_through=(today - timedelta(days=1)).isoformat(), db_path=tmpdb)
    st = df.source_state(_row(tmpdb, rid), "depletion", db_path=tmpdb)
    assert st["state"] == "current" and st["as_of_iso"] == (today - timedelta(days=1)).isoformat()
    assert df.depletion_behind(_row(tmpdb, rid), max_days_behind=0, db_path=tmpdb) is None
    dh.record_attempt(rid, "depletion", True, data_through=(today - timedelta(days=4)).isoformat(), db_path=tmpdb)
    behind = df.depletion_behind(_row(tmpdb, rid), max_days_behind=1, db_path=tmpdb)
    assert behind and "3 days behind" in behind
    assert df.source_state(_row(tmpdb, rid), "depletion", db_path=tmpdb)["pct"] < 100


def test_depletion_never_run_is_unknown_only_where_the_pos_reports_items(db_path):
    square = _rid(db_path, name="Sq", square_access_token="t")
    _recipe(db_path, square, guid="s1")
    assert df.source_state(_row(db_path, square), "depletion", db_path=db_path)["state"] == "not_connected"
    toast_r = _rid(db_path, name="To", toast_restaurant_guid="g")
    _recipe(db_path, toast_r, guid="t1")
    st = df.source_state(_row(db_path, toast_r), "depletion", db_path=db_path)
    assert st["state"] == "unknown" and st["pct"] is None


def test_the_snapshot_is_held_while_depletion_is_behind(tmpdb, monkeypatch):
    import scheduler, food_cost_intelligence as fci
    rid = _rid(tmpdb, module_inventory=1)
    _recipe(tmpdb, rid)
    today = _local_today(tmpdb, rid)
    dh.record_attempt(rid, "depletion", False, error="HTTP 503",
                      data_through=None)
    dh.record_attempt(rid, "depletion", True, data_through=(today - timedelta(days=3)).isoformat())
    wrote = []
    monkeypatch.setattr(fci, "weekly_snapshot", lambda r, **k: wrote.append(r) or {"ok": True})
    monkeypatch.setattr(fci, "record_profitability_forecast", lambda r, **k: None)
    monkeypatch.setattr(fci, "score_forecasts", lambda r, **k: {"scored": 0})
    out = scheduler.run_food_cost_snapshots()
    assert wrote == [] and out["held"] == 1


def test_trusted_orders_are_held_while_depletion_is_more_than_a_day_behind(tmpdb, monkeypatch):
    import strategy_jobs, scheduler, ordering, time_utils
    rid = _rid(tmpdb, module_inventory=1, auto_order_trusted=1)
    _recipe(tmpdb, rid)
    today = _local_today(tmpdb, rid)
    dh.record_attempt(rid, "depletion", True, data_through=(today - timedelta(days=5)).isoformat(), db_path=tmpdb)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: datetime(2026, 9, 21, 8, 30))
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    queued, told = [], []
    monkeypatch.setattr(ordering, "queue_trusted_orders", lambda *a, **k: queued.append(1) or [])
    monkeypatch.setattr(strategy_jobs, "_reach", lambda rid_, kind, title, body, *a, **k: told.append((kind, body)))
    out = strategy_jobs.run_trusted_orders(db_path=tmpdb)
    assert queued == [] and out["queued"] == 0
    assert told and told[0][0] == "order_send_held" and "behind" in told[0][1]


# ── #15: an open business day is never complete ──────────────────────────────

def test_complete_through_waits_for_the_day_to_end(db_path):
    import pos
    rid = _rid(db_path, timezone="America/Los_Angeles")
    r = _row(db_path, rid)
    # 1am Thursday 9/24 local, no hours set: Wednesday ends at 5am.
    assert pos.complete_through(r, now_local=datetime(2026, 9, 24, 1, 0)) == date(2026, 9, 22)
    assert pos.complete_through(r, now_local=datetime(2026, 9, 24, 6, 0)) == date(2026, 9, 23)
    # With hours: Wednesday closed at 11pm, so at 1am it is whole.
    update_restaurant(rid, {"open_times_json": '{"Wednesday": "11:00am"}',
                            "close_times_json": '{"Wednesday": "11:00pm"}'}, db_path=db_path)
    assert pos.complete_through(_row(db_path, rid), now_local=datetime(2026, 9, 24, 1, 0)) == date(2026, 9, 23)


def test_a_day_pulled_while_trading_is_provisional_and_does_not_date_sales(db_path):
    rid = _rid(db_path)
    today = _local_today(db_path, rid)
    d1, d2 = (today - timedelta(days=2)).isoformat(), (today - timedelta(days=1)).isoformat()
    by_day = {d1: {"sales": 5000.0, "actual": 60, "labor_cost": 1200}, d2: {"sales": 900.0, "actual": 20, "labor_cost": 400}}
    models.save_labor_daily_history(rid, by_day, db_path=db_path, provenance={
        "source": "toast", "provider": "toast", "synced_at": "2026-09-24 08:00:00", "window": (d1, d2),
        "complete_through": d1})
    rows = {r["date"]: r for r in _q(db_path, "SELECT date, source, provider, synced_at, final FROM labor_daily_history "
                                              "WHERE restaurant_id=?", (rid,))}
    assert rows[d1]["final"] == 1 and rows[d2]["final"] == 0 and rows[d2]["source"] == "toast"
    st = df.source_state(_row(db_path, rid), "sales", db_path=db_path)
    assert st["as_of_iso"] == d1, "the partial day must not date Sales"
    # The next pull, after the day ended, makes it whole.
    models.save_labor_daily_history(rid, {d2: {"sales": 6100.0, "actual": 60, "labor_cost": 1300}}, db_path=db_path,
                                    provenance={"source": "toast", "provider": "toast", "synced_at": "x",
                                                "window": (d2, d2), "complete_through": d2})
    assert df.source_state(_row(db_path, rid), "sales", db_path=db_path)["as_of_iso"] == d2


# ── #16: partial pages fail; a missing figure is blank ───────────────────────

def test_rpower_hitting_its_page_ceiling_raises(monkeypatch):
    import rpower
    monkeypatch.setattr(rpower, "_request", lambda token, path, params=None:
                        [{"rid": f"{params['pagenumber']}-{n}"} for n in range(rpower.PAGE_SIZE)])
    with pytest.raises(rpower.RPowerError, match="truncated"):
        rpower._paged("tok", "ticketsales/getbybusinessdate", {}, max_pages=2)


def test_toast_missing_net_sales_is_unknown_never_zero(db_path, monkeypatch):
    import toast, requests
    rid = _rid(db_path, toast_restaurant_guid="g-1")
    monkeypatch.setattr(toast, "get_toast_token", lambda r: "tok")
    monkeypatch.setattr(models, "get_restaurant", lambda r, **k: types.SimpleNamespace(toast_restaurant_guid="g-1"))

    class _R:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return [{"businessDate": 20260921, "netSales": 4100.5}, {"businessDate": 20260922}]
    monkeypatch.setattr(requests, "get", lambda url, **k: _R())
    out = toast.fetch_business_days(rid, date(2026, 9, 21), date(2026, 9, 22))
    assert out == {"2026-09-21": 4100.5, "2026-09-22": None}


# ── #19: Toast names resolve through the employees and jobs endpoints ────────

def _toast_world(monkeypatch, employees, jobs):
    import toast, requests
    monkeypatch.setattr(toast, "get_toast_token", lambda r: "tok")
    monkeypatch.setattr(models, "get_restaurant", lambda r, **k: types.SimpleNamespace(toast_restaurant_guid="g-1"))
    calls = []

    class _R:
        status_code = 200
        headers = {}

        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    def _get(url, **k):
        calls.append(url)
        return _R(employees if url.endswith("/employees") else jobs)
    monkeypatch.setattr(requests, "get", _get)
    return calls


def _entry(emp_guid, job_guid, day="20260921"):
    return {"employeeReference": {"guid": emp_guid}, "jobReference": {"guid": job_guid},
            "inDate": "2026-09-21T15:00:00.000+0000", "outDate": "2026-09-21T23:00:00.000+0000",
            "businessDate": day}


def test_reference_only_toast_entries_get_their_people(monkeypatch):
    import toast
    calls = _toast_world(monkeypatch, [{"guid": "e1", "firstName": "Maria", "lastName": "Garcia"}],
                         [{"guid": "j1", "title": "Line Cook"}])
    entries = [_entry("e1", "j1") for _ in range(3)]
    toast._require_resolved(1, entries)
    rows = toast.normalise_entries(entries, {})
    assert {r["employee"] for r in rows} == {"Maria Garcia"} and {r["role"] for r in rows} == {"Line Cook"}
    assert sum(1 for c in calls if c.endswith("/employees")) == 1, "cached for the pull"


def test_a_pull_with_too_many_unmatched_people_fails(monkeypatch, db_path):
    import toast
    _toast_world(monkeypatch, [{"guid": "e1", "firstName": "Maria", "lastName": "Garcia"}], [])
    entries = [_entry("e1", "j1") for _ in range(18)] + [_entry("ghost", "j1") for _ in range(2)]
    with pytest.raises(toast.ToastUnresolved):
        toast._require_resolved(1, entries)
    monkeypatch.setattr(toast, "_is_demo", lambda r: False)
    monkeypatch.setattr(toast, "fetch_time_entries", lambda *a, **k: [_entry("ghost", "j1") for _ in range(4)])
    monkeypatch.setattr(toast, "fetch_business_days", lambda *a, **k: {})
    with pytest.raises(toast.ToastUnresolved):
        toast.build_shifts_csv(1)


# ── #20: the clock-in feed is the local business day, and empty is unknown ───

def test_clock_ins_are_read_over_the_local_business_day(db_path, monkeypatch):
    import toast
    rid = _rid(db_path, timezone="America/Los_Angeles")
    seen = {}

    def _entries(r, s, e, start_at=None, end_at=None):
        seen.update(start_at=start_at, end_at=end_at)
        return [{"employee": {"firstName": "Ana", "lastName": "Ruiz"}, "businessDate": "20260921",
                 "inDate": "2026-09-22T00:10:00.000+0000"},
                {"employee": {"firstName": "Old", "lastName": "Shift"}, "businessDate": "20260920",
                 "inDate": "2026-09-21T02:00:00.000+0000"}]
    monkeypatch.setattr(toast, "fetch_time_entries", _entries)
    monkeypatch.setattr(toast, "_tz_for", lambda r: __import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
    rows = toast.fetch_clock_ins_today(rid, date(2026, 9, 21))
    assert seen["start_at"].astimezone(timezone.utc) == datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    assert seen["end_at"].astimezone(timezone.utc) == datetime(2026, 9, 22, 11, 59, 59, tzinfo=timezone.utc)
    assert [r["employee"] for r in rows] == ["Ana Ruiz"], "a 5:10pm PDT clock-in is today's; yesterday's is not"


def test_an_empty_feed_with_people_well_past_due_is_unavailable(db_path, monkeypatch):
    import intraday, pos
    rid = _rid(db_path)
    monkeypatch.setattr(intraday, "_todays_scheduled", lambda *a, **k: [
        {"employee": "Dana K", "role": "Server", "shift_start": "11:00am"}])
    monkeypatch.setattr(pos, "fetch_clock_ins_today", lambda *a: ([], "toast"))
    out = intraday.coverage_gaps(rid, now_local=datetime(2026, 9, 21, 11, 45), db_path=db_path)
    assert out["available"] is False and "incomplete" in out["reason"]
    out = intraday.coverage_gaps(rid, now_local=datetime(2026, 9, 21, 11, 20), db_path=db_path)
    assert out["available"] is True and [m["employee"] for m in out["missing"]] == ["Dana K"]


# ── #26: automatic recovery ──────────────────────────────────────────────────

def test_the_http_helper_retries_server_errors_but_never_auth(monkeypatch):
    import pos
    waits = []
    monkeypatch.setattr(pos, "_sleep", waits.append)

    def _resp(code, ra=None):
        return types.SimpleNamespace(status_code=code, headers={"Retry-After": ra} if ra else {})
    seq = iter([_resp(503), _resp(429, "7"), _resp(200)])
    assert pos.http_call(lambda url, **k: next(seq), "u", timeout=5).status_code == 200
    assert waits[0] == pos.HTTP_BACKOFF_SECONDS and waits[1] == 7.0
    seq = iter([_resp(401), _resp(200)])
    assert pos.http_call(lambda url, **k: next(seq), "u", timeout=5).status_code == 401
    got = []
    pos.http_call(lambda url, **k: got.append(k.get("timeout")) or _resp(200), "u", timeout=9)
    assert got == [9]


def test_toast_rides_out_a_503(db_path, monkeypatch):
    import toast, requests, pos
    monkeypatch.setattr(pos, "_sleep", lambda s: None)
    monkeypatch.setattr(toast, "get_toast_token", lambda r: "tok")
    monkeypatch.setattr(models, "get_restaurant", lambda r, **k: types.SimpleNamespace(toast_restaurant_guid="g"))
    codes = iter([503, 200])

    class _R:
        headers = {}

        def __init__(self):
            self.status_code = next(codes)

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"{self.status_code}")

        def json(self):
            return [{"businessDate": 20260921, "netSales": 10.0}]
    monkeypatch.setattr(requests, "get", lambda url, **k: _R())
    assert toast.fetch_business_days(1, date(2026, 9, 21), date(2026, 9, 21)) == {"2026-09-21": 10.0}


def test_a_failed_sync_schedules_its_retry_and_auth_never_does(tmpdb, monkeypatch):
    import pos
    rid = _rid(tmpdb)
    fake = types.SimpleNamespace(sync_to_db=lambda r: {"ok": False, "error": "HTTP 503 from Toast"})
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("toast", fake))
    pos.sync_restaurant(rid)
    row = dh.health_rows(rid)["pos"]
    at = datetime.strptime(row["next_retry_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    assert timedelta(minutes=50) < at - datetime.now(timezone.utc) <= timedelta(hours=1)
    pos.sync_restaurant(rid, trigger="retry", attempt=len(pos.RETRY_DELAYS_HOURS))
    assert dh.health_rows(rid)["pos"]["next_retry_at"] is None, "three retries a day at most"
    fake.sync_to_db = lambda r: {"ok": False, "error": "RPOWER rejected the token (401)", "auth": True}
    pos.sync_restaurant(rid)
    row = dh.health_rows(rid)["pos"]
    assert row["error_class"] == "auth" and row["next_retry_at"] is None


def test_the_hourly_retry_sweep_retries_what_is_due_before_eleven_local(tmpdb, monkeypatch):
    import pos, scheduler, time_utils
    rid = _rid(tmpdb)
    dh.record_attempt(rid, "pos", False, error="HTTP 503", next_retry_at="2000-01-01 00:00:00")
    calls = []
    monkeypatch.setattr(pos, "sync_restaurant", lambda r, trigger="nightly", attempt=0:
                        calls.append((r, trigger, attempt)) or {"ok": True})
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: datetime(2026, 9, 24, 6, 0))
    out = scheduler.run_pos_retry()
    assert calls == [(rid, "retry", 1)] and out["ok"] == 1 and out["hit_bound"] is False
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: datetime(2026, 9, 24, 11, 5))
    assert scheduler.run_pos_retry()["skipped"] == 1 and len(calls) == 1


def test_the_nightly_sweep_reports_a_hit_bound(tmpdb, monkeypatch):
    import pos, scheduler
    for n in ("A", "B"):
        _rid(tmpdb, name=n)
    monkeypatch.setattr(scheduler, "resumable_sweep", lambda *a, **k: (0, True))
    caught = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: caught.append(k.get("context")))
    stats = {}
    pos.sync_all(stats=stats)
    assert stats["hit_bound"] is True and "time_bound" in caught


# ── #28, #29: POS state reaches sales and labor; disconnect never raises ─────

def _pos_world(db_path, **kw):
    rid = _rid(db_path, toast_restaurant_guid="g-1", **kw)
    today = _local_today(db_path, rid)
    for n in (1, 2, 3):
        _x(db_path, "INSERT INTO labor_daily_history (restaurant_id, date, labor_cost, sales, total_hours) "
                    "VALUES (?,?,?,?,?)", (rid, (today - timedelta(days=n)).isoformat(), 900, 4000, 60))
    return rid


def test_sales_and_labor_carry_the_failing_pos(db_path):
    rid = _pos_world(db_path, toast_last_synced=datetime.now(timezone.utc).isoformat(),
                     toast_sync_error="HTTP 401 unauthorized")
    for key in ("sales", "labor"):
        st = df.source_state(_row(db_path, rid), key, db_path=db_path)
        assert st["error"] and "Toast" in st["error"], key
        assert st["pct"] <= int(df.ERROR_CEILING * 100), key


def test_a_pos_that_never_synced_but_failed_reads_unknown_zero(db_path):
    rid = _rid(db_path, toast_restaurant_guid="g-1", toast_sync_error="HTTP 401 unauthorized")
    st = df.source_state(_row(db_path, rid), "pos", db_path=db_path)
    assert st["state"] == "unknown" and st["pct"] == 0
    rid2 = _rid(db_path, name="Fresh", toast_restaurant_guid="g-2")
    assert df.source_state(_row(db_path, rid2), "pos", db_path=db_path)["pct"] is None


def test_a_pos_disconnected_after_use_keeps_its_datas_age(db_path):
    rid = _pos_world(db_path, toast_last_synced=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    update_restaurant(rid, {"toast_restaurant_guid": None}, db_path=db_path)
    st = df.source_state(_row(db_path, rid), "pos", db_path=db_path)
    assert st["state"] == "disconnected" and st["pct"] is not None and st["pct"] > 0


# ── #30: weather never fetched is unknown, and each fetch is recorded ────────

def test_weather_never_fetched_is_unknown_where_a_forecast_is_possible(db_path):
    rid = _rid(db_path, google_place_id="ChIJx")
    st = df.source_state(_row(db_path, rid), "weather", db_path=db_path)
    assert st["state"] == "unknown" and st["pct"] is None
    rid2 = _rid(db_path, name="Nowhere")
    assert df.source_state(_row(db_path, rid2), "weather", db_path=db_path)["state"] == "not_connected"


def test_every_forecast_fetch_is_recorded(tmpdb, monkeypatch):
    import weather
    rid = _rid(tmpdb, latitude=41.8)
    update_restaurant(rid, {"longitude": -87.6}, db_path=tmpdb)
    monkeypatch.setattr(weather, "_geocode", lambda r, db_path=None: (41.8, -87.6))
    monkeypatch.setattr(weather, "_backing_off", lambda key: False)
    monkeypatch.setattr(weather, "_fetch_periods_ex", lambda lat, lon: ([], "transient"))
    weather._periods_by_date(_row(tmpdb, rid), db_path=tmpdb)
    assert dh.health_rows(rid)["weather"]["consecutive_failures"] == 1
    monkeypatch.setattr(weather, "_fetch_periods_ex", lambda lat, lon: (
        [{"startTime": "2026-09-24T06:00:00-05:00", "isDaytime": True, "temperature": 70}], None))
    weather._periods_by_date(_row(tmpdb, rid), db_path=tmpdb)
    assert dh.health_rows(rid)["weather"]["consecutive_failures"] == 0


# ── #32: a Places fallback is a sample, then a failure, then the owner's ────

def test_a_business_profile_on_the_places_fallback_reads_sampled_and_failing(tmpdb, monkeypatch):
    import scheduler, notify
    rid = _rid(tmpdb, gmb_refresh_token="rt", google_place_id="ChIJx")
    _x(tmpdb, "UPDATE restaurants SET last_fetched_at=? WHERE id=?",
       (scheduler._chi_now().strftime("%Y-%m-%dT%H:%M:%S"), rid))
    r = _row(tmpdb, rid)
    alerts = []
    monkeypatch.setattr(notify, "raise_alert", lambda *a, **k: alerts.append(a[1]))
    for _ in range(2):
        scheduler._record_review_fetch(r, True, False, "location not matched", False, None)
    st = df.source_state(_row(tmpdb, rid), "reviews", db_path=tmpdb)
    assert st["sampled"] is True and "Business Profile failing" in st["error"]
    assert dh.health_rows(rid)["reviews"]["provider"] == "places_fallback" and alerts == []
    for _ in range(scheduler.GBP_FALLBACK_ALERT_SLOTS - 2):
        scheduler._record_review_fetch(r, True, False, "location not matched", False, None)
    assert alerts == ["connection_lost"]
    scheduler._record_review_fetch(r, True, False, "location not matched", False, None)
    assert alerts == ["connection_lost"], "at most weekly"


# ── #35: weekly Intel jobs catch up, retry, and record soft failures ─────────

def test_the_weekly_jobs_claim_the_iso_week_with_catch_up():
    import inspect, scheduler
    src = inspect.getsource(scheduler.scheduler_loop)
    assert 'now.weekday() <= 2 and _ops.claim_period("competitor_analysis", _iso_week)' in src
    assert 'now.weekday() <= 2 and _ops.claim_period("ai_visibility", _iso_week)' in src
    assert 'claim_period("competitor_retry", str(today))' in src


def test_the_daily_retry_skips_a_restaurant_that_succeeded_this_week(tmpdb, monkeypatch):
    import scheduler, competitor
    ok_one = _rid(tmpdb, name="Done", google_place_id="p1", service_tier="full")
    todo = _rid(tmpdb, name="Todo", google_place_id="p2", service_tier="full")
    monkeypatch.setattr(models, "is_full_tier", lambda r: True)
    dh.record_attempt(ok_one, "competitor", True)
    seen = []
    monkeypatch.setattr(competitor, "run_competitor_analysis", lambda rid: seen.append(rid) or {"ok": True})
    scheduler.run_weekly_competitor_analysis(retry_only=True)
    assert seen == [todo]
    assert dh.health_rows(todo)["competitor"]["consecutive_failures"] == 0


def test_an_ai_visibility_soft_failure_is_captured_and_recorded(tmpdb, monkeypatch):
    import scheduler, client_api
    rid = _rid(tmpdb, service_tier="full")
    monkeypatch.setattr(models, "is_full_tier", lambda r: True)
    monkeypatch.setattr(client_api, "_do_ai_visibility_inner",
                        lambda r, force=False: ({"ok": False, "error": "AI budget paused"}, 200))
    caught = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: caught.append((str(e), k.get("job"))))
    out = scheduler.run_weekly_ai_visibility()
    assert out["failed"] == 1 and ("AI budget paused", "ai_visibility") in caught
    assert dh.health_rows(rid)["visibility"]["last_error"] == "AI budget paused"


# ── #40: one primary provider and one eligibility rule ───────────────────────

def test_sync_and_freshness_read_the_same_primary_provider(db_path, monkeypatch):
    import pos, pos_health
    rid = _rid(db_path, toast_restaurant_guid="g", rpower_token="t", rpower_store_mid="m", pos_system="RPOWER",
               toast_last_synced=datetime.now(timezone.utc).isoformat(),
               rpower_last_synced=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat())
    row = _row(db_path, rid)
    monkeypatch.setattr(models, "get_restaurant", lambda r, **k: row)
    fakes = {n: types.SimpleNamespace(is_connected=lambda r: True) for n in ("toast", "rpower")}
    monkeypatch.setattr(pos, "PROVIDERS", fakes)
    assert pos.connected_provider(rid)[0] == "rpower"
    assert pos_health.pos_sync_state(row)["provider"] == "rpower"


def test_the_nightly_sync_uses_in_service(tmpdb, monkeypatch):
    import pos
    internal = _rid(tmpdb, name="Internal", billing_status="internal")
    synced = []
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("toast", object()))
    monkeypatch.setattr(pos, "sync_restaurant", lambda r, **k: synced.append(r) or {"ok": True})
    monkeypatch.setattr(pos, "note_sync_failure", lambda r: None)
    pos.sync_all()
    assert internal in synced


# ── #41: provenance, and depletion labelled by its provider ──────────────────

def test_rpower_depletion_is_labelled_rpower(tmpdb, monkeypatch):
    import pos, inventory_ledger
    rid = _rid(tmpdb)
    ing = _recipe(tmpdb, rid, guid="dish-1")
    monkeypatch.setattr(pos, "fetch_order_selections",
                        lambda r, d: ([{"item": {"guid": "dish-1"}, "quantity": 4}], "rpower"))
    inventory_ledger.compute_daily_depletion(rid, date(2026, 9, 22))
    rows = _q(tmpdb, "SELECT source, qty FROM ingredient_stock_events WHERE ingredient_id=? "
                     "AND event_type='depletion'", (ing,))
    assert rows == [{"source": "rpower", "qty": 2.0}]


def test_the_archive_has_provenance_columns(db_path):
    cols = {r["name"] for r in _q(db_path, "PRAGMA table_info(labor_daily_history)")}
    assert {"source", "provider", "synced_at", "final"} <= cols


# ── #43: the daily report and the POS agree, or sales says so ────────────────

def test_final_reports_that_disagree_with_the_pos_put_an_error_on_sales(db_path):
    rid = _rid(db_path)
    today = _local_today(db_path, rid)
    for n, (dsr_net, pos_net) in enumerate([(8420.0, 7960.0), (5000.0, 5010.0), (6000.0, 5400.0)], start=1):
        d = (today - timedelta(days=n)).isoformat()
        _x(db_path, "INSERT INTO labor_daily_history (restaurant_id, date, labor_cost, sales) VALUES (?,?,?,?)",
           (rid, d, 1000, pos_net))
        _x(db_path, "INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) "
                    "VALUES (?,?,?,?,?)", (rid, d, "sales.net", dsr_net, "final"))
    chk = df.sales_consistency(rid, db_path=db_path)
    assert chk["checked"] == 3 and len(chk["mismatches"]) == 2
    st = df.source_state(_row(db_path, rid), "sales", db_path=db_path)
    assert st["error"] and "disagree on 2 nights" in st["error"]


def test_one_late_void_is_not_an_error(db_path):
    rid = _rid(db_path)
    d = (_local_today(db_path, rid) - timedelta(days=1)).isoformat()
    _x(db_path, "INSERT INTO labor_daily_history (restaurant_id, date, labor_cost, sales) VALUES (?,?,?,?)",
       (rid, d, 1000, 7960.0))
    _x(db_path, "INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) VALUES (?,?,?,?,?)",
       (rid, d, "sales.net", 8420.0, "final"))
    assert df.source_state(_row(db_path, rid), "sales", db_path=db_path)["error"] is None


# ── #47: the competitor stamp is UTC and dated locally ───────────────────────

def test_a_utc_competitor_stamp_is_dated_in_the_restaurants_own_day(db_path):
    rid = _rid(db_path, timezone="America/Chicago", competitor_updated_at="2026-09-24T03:00:00+00:00")
    st = df.source_state(_row(db_path, rid), "competitor", db_path=db_path)
    assert st["as_of_iso"] == "2026-09-23"
    import inspect, competitor
    src = inspect.getsource(competitor.run_competitor_analysis)
    assert "_dt_utc.now(_tz.utc).isoformat" in src


# ── #1: the other ingest points write the ledger ────────────────────────────

def test_the_metrics_sync_writes_both_its_stamp_and_the_ledger(tmpdb):
    import scheduler
    rid = _rid(tmpdb)
    scheduler.record_metrics_sync(rid, False, "Meta token expired")
    assert scheduler.metrics_sync_state(rid)["error"] == "Meta token expired"
    assert dh.health_rows(rid)["marketing"]["consecutive_failures"] == 1


def test_the_loss_sync_records_its_attempts(tmpdb, monkeypatch):
    import strategy_jobs, loss_detection
    rid = _rid(tmpdb)
    monkeypatch.setattr(loss_detection, "sync", lambda r, db_path=None: {"ok": True, "provider": "rpower"})
    monkeypatch.setattr(strategy_jobs, "_loss_flags_to_issues", lambda r, db: None)
    strategy_jobs.run_loss_sync(db_path=tmpdb)
    assert dh.health_rows(rid)["loss"]["provider"] == "rpower"


def test_the_dsr_sales_collection_is_recorded(tmpdb, monkeypatch):
    import dsr
    from dsr import pipeline
    rid = _rid(tmpdb)
    r = _row(tmpdb, rid)
    pipeline._record_sales_collect(r, date(2026, 9, 23), dsr.block(dsr.READY, block_name="sales"), tmpdb)
    row = dh.health_rows(rid)["dsr"]
    assert row["consecutive_failures"] == 0 and row["data_through"] == "2026-09-23"
    pipeline._record_sales_collect(r, date(2026, 9, 23),
                                   dsr.block(dsr.AWAITING, block_name="sales", detail={"error": "collector_failed"}),
                                   tmpdb)
    assert dh.health_rows(rid)["dsr"]["consecutive_failures"] == 1


# ── scale: the intraday slot, the nightly writes, the daily snapshot ─────────

def test_the_intraday_slot_reads_restaurants_once_and_resumes_from_its_cursor(tmpdb, monkeypatch):
    import strategy_jobs, intraday
    rows = [_row(tmpdb, _rid(tmpdb, name=f"R{n}")) for n in range(3)]
    monkeypatch.setattr(strategy_jobs, "_restaurants", lambda db=None: pytest.fail("re-read the list"))
    monkeypatch.setattr(strategy_jobs, "_open_now", lambda r, local: True)
    monkeypatch.setattr(ops, "claim_period", lambda *a: True)
    got = []
    monkeypatch.setattr(intraday, "capture", lambda rid, **k: got.append(rid) or {"ok": True, "provider": "toast"})
    out = strategy_jobs.run_intraday_capture(db_path=tmpdb, restaurants=rows)
    assert sorted(got) == sorted(r.id for r in rows) and out["captured"] == 3 and out["hit_bound"] is False
    assert strategy_jobs._read_cursor("intraday_capture_cursor", tmpdb) == max(r.id for r in rows)
    assert dh.health_rows(rows[0].id)["pos_intraday"]["provider"] == "toast"
    # A sibling walks the same list and stops at its bound, resuming after
    # the last restaurant it finished.
    import time as _time
    clock = iter([0, 999, 999, 999, 999])
    monkeypatch.setattr(_time, "monotonic", lambda: next(clock))
    walked = [r.id for r in strategy_jobs._slot_iter("coverage_check", rows, tmpdb)]
    assert len(walked) == 1
    assert strategy_jobs._read_cursor("coverage_check_cursor", tmpdb) == walked[0]


def test_the_nightly_pos_write_section_is_serialized():
    import inspect, pos
    assert pos.POS_SYNC_WORKERS >= 4
    assert "@_serialized_write" in inspect.getsource(pos)
    assert "workers=POS_SYNC_WORKERS" in inspect.getsource(pos.sync_all)


def test_the_daily_data_health_sweep_feeds_the_admin_rollup(tmpdb, monkeypatch):
    import scheduler, admin_ops
    rid = _rid(tmpdb)
    out = scheduler.run_data_health_daily()
    assert out["attempted"] >= 1 and out["hit_bound"] is False
    assert _q(tmpdb, "SELECT COUNT(*) AS n FROM data_health_daily WHERE restaurant_id=?", (rid,))[0]["n"] == 1
    dh.record_attempt(rid, "pos", False, error="HTTP 503")
    d = admin_ops._load_everything()
    rec = admin_ops.location_record(next(r for r in d["rests"] if r["id"] == rid), d)
    assert rec["data_health"]["as_of"] and rec["data_health"]["failing"][0]["source"] == "pos"
