"""RPOWER audit #23 (10/21/26), fix group F5 — scale & scheduling.

A5-02  the nightly POS sync as one `pos_sync` queue row per store at its own
       hour (driven through a real scheduler tick).
A5-03  live labor's per-store reads as `live` queue rows; the slot jobs'
       pools and the POS groups sized from the budget.
A1-01 / A5-06  live sales read incrementally from the change feed.
A1-02  live labor's adaptive cadence.
A5-04 / A1-24  one shared closeday answer; the DSR polls from the learned
       close lag on widening steps.
A4-11 / A5-11  onboarding in its own group, admitted by the budget; expired
       POS rows recorded as missed runs.
A4-12 / A5-01  lane shares by time of day with borrowing; the nightly
       backpressure check; budget utilisation % per lane.
A5-09  the sale lines' cold tier; retention for the growing tables.
A5-13  the request ledger rolled up to days after a week.
"""
import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

import job_queue
import models
import ops
import pos
import pos_capacity
import pos_closed
import pos_io
import rpower


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(rpower.time, "sleep", lambda s: None)

    def _no_network(*a, **k):
        raise AssertionError("a test reached the network")
    import requests
    monkeypatch.setattr(requests, "get", _no_network)
    monkeypatch.setattr(requests, "post", _no_network)
    monkeypatch.setattr(requests.Session, "request", _no_network)
    for k in ("RPOWER_LANE_SHARES", "POS_BUDGET_SHED", "QUEUE_POS_IO_CONCURRENCY", "QUEUE_LIVE_CONCURRENCY",
              "POS_ONBOARD_PER_HOUR", "SLOT_JOB_WORKERS"):
        monkeypatch.delenv(k, raising=False)
    pos_capacity.reset_for_tests()
    pos_io.reset_for_tests()
    yield
    pos_capacity.reset_for_tests()
    pos_io.reset_for_tests()


def _restaurant(rid=1, **kw):
    conn = models.get_conn()
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test",
            "rpower_token": "tok", "rpower_cg": 1280, "rpower_store_mid": f"store-{rid}"}
    cols.update(kw)
    conn.execute(f"INSERT INTO restaurants ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                 tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


def _q(sql, args=()):
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _x(sql, args=()):
    conn = models.get_conn()
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


CHI = __import__("zoneinfo").ZoneInfo("America/Chicago")


# ── A4-12 / A5-01: lane shares, borrowing, backpressure ────────────────────

def test_the_lane_windows_and_their_shares():
    assert pos_capacity.window(datetime(2026, 10, 21, 19, 0)) == "service"
    assert pos_capacity.window(datetime(2026, 10, 21, 23, 30)) == "close"
    assert pos_capacity.window(datetime(2026, 10, 22, 1, 0)) == "close"
    assert pos_capacity.window(datetime(2026, 10, 22, 4, 0)) == "nightly"
    at = datetime(2026, 10, 21, 19, 0)
    assert pos_capacity.share("nightly", at) == 0.30 and pos_capacity.share("bulk", at) == 0.10
    assert pos_capacity.share("live", at) == pytest.approx(0.60)
    night = datetime(2026, 10, 22, 4, 0)
    assert pos_capacity.share("nightly", night) == 0.80 and pos_capacity.share("bulk", night) == 0.20
    assert pos_capacity.share("close", datetime(2026, 10, 21, 23, 0)) == pytest.approx(0.50)


def test_an_override_moves_a_share(monkeypatch):
    monkeypatch.setenv("RPOWER_LANE_SHARES", json.dumps({"service": {"nightly": 0.5}}))
    assert pos_capacity.share("nightly", datetime(2026, 10, 21, 12, 0)) == 0.5


def test_background_lanes_borrow_an_idle_share_and_keep_to_theirs_beside_work_above():
    at = datetime(2026, 10, 21, 19, 0)
    assert pos_capacity.background_share("live", True, True, now=at) is None, "live is never capped"
    assert pos_capacity.background_share("close", True, True, now=at) is None
    assert pos_capacity.background_share("nightly", False, False, now=at) is None, "nothing above: borrows"
    assert pos_capacity.background_share("nightly", True, False, now=at) == 0.30
    assert pos_capacity.background_share("bulk", False, False, now=at) is None
    assert pos_capacity.background_share("bulk", False, True, now=at) == 0.10, "nightly is above bulk"
    assert pos_capacity.background_share("bulk", True, False, now=at) == 0.10


def test_rpower_runs_bulk_at_its_share_while_the_nightly_archive_is_reading(monkeypatch):
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0.001)
    monkeypatch.setattr(rpower, "FLEET_RPS", 0)
    taken = []
    monkeypatch.setattr(rpower, "_take_shared_slot", lambda t, s, key=None: (taken.append((key, s)), 0.0)[1])
    key = rpower._bucket_key("tok")
    with pos_io.lane("nightly"):
        rpower._sleep_for_spacing("tok")                  # marks the bucket's nightly lane, for every process
    rpower._live_seen.clear()
    taken.clear()
    with pos_io.lane("bulk"):
        rpower._sleep_for_spacing("tok")
    assert taken[0][0] == key + ":bg:bulk" and taken[0][1] == pytest.approx(0.001 / pos_capacity.share("bulk"))


def test_every_request_is_counted_in_its_lane_and_utilisation_reads_it(monkeypatch):
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0)
    monkeypatch.setattr(rpower, "FLEET_RPS", 10)
    monkeypatch.setattr(rpower, "_take_shared_slot", lambda t, s, key=None: 0.0)
    for _ in range(3):
        with pos_io.lane("nightly"):
            rpower._sleep_for_spacing("tok")
    with pos_io.lane("live"):
        rpower._sleep_for_spacing("tok")
    pos_capacity.flush_lanes()
    rows = {r["lane"]: r["n"] for r in _q("SELECT lane, SUM(n) AS n FROM pos_lane_ledger GROUP BY lane")}
    assert rows == {"nightly": 3, "live": 1}
    assert not _q("SELECT 1 FROM pos_request_ledger"), "the per-endpoint ledger is left as it was"
    u = pos_capacity.utilisation(2)
    assert u["lanes"]["nightly"]["requests"] == 3 and u["lanes"]["nightly"]["pct"] is not None
    assert u["total"]["requests"] == 4 and u["rps"] == 10


def test_one_partner_token_is_the_binding_budget(monkeypatch):
    monkeypatch.setenv("RPOWER_FLEET_RPS", "10")
    monkeypatch.setenv("RPOWER_REQUEST_SPACING_SECONDS", "0.15")
    _x("INSERT INTO rpower_rate_bucket (bucket, tokens, updated_at) VALUES (?, 0, ?)",
       ("rpower:" + "a" * 24, time.time()))
    _x("INSERT INTO rpower_rate_bucket (bucket, tokens, updated_at) VALUES (?, 0, ?)",
       ("rpower:" + "a" * 24 + ":live", time.time()))
    pos_capacity.reset_for_tests()
    assert pos_capacity.effective_rps() == pytest.approx(1 / 0.15)
    _x("INSERT INTO rpower_rate_bucket (bucket, tokens, updated_at) VALUES (?, 0, ?)",
       ("rpower:" + "b" * 24, time.time()))
    pos_capacity.reset_for_tests()
    assert pos_capacity.effective_rps() == 10, "two tokens: the fleet ceiling binds"


def _rpower_stores(n, monkeypatch):
    for i in range(1, n + 1):
        _restaurant(i)
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("rpower", rpower))


def test_the_budget_check_sheds_bulk_then_stretches_live_and_never_close_or_nightly(monkeypatch):
    _rpower_stores(3, monkeypatch)
    caught = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: caught.append(str(e)))
    monkeypatch.setattr(pos_capacity, "effective_rps", lambda db_path=None: 10.0)
    out = pos_capacity.budget_check(now=datetime(2026, 10, 22, 2, 0))
    assert out["shed_bulk"] is False and out["stretch_live"] is False and not caught
    # 3 stores x 50,000 requests a night against 10 req/s x 4 h x 80 %.
    monkeypatch.setattr(pos_capacity, "NIGHTLY_REQUESTS_PER_STORE", 50000)
    out = pos_capacity.budget_check(now=datetime(2026, 10, 22, 2, 0))
    assert out["shed_bulk"] is True and out["stretch_live"] is True
    assert pos_capacity.bulk_shed() and pos_capacity.live_stretched()
    assert "bulk work (onboarding) waits until 7am" in caught[-1]
    st = pos_capacity.state()
    assert datetime.fromtimestamp(st["until"], CHI).hour == 7
    # Shed bulk keeps a sliver and never borrows; nightly is untouched.
    assert pos_capacity.background_share("bulk", False, False) == pytest.approx(pos_capacity.SHED_BULK_SHARE)
    assert pos_capacity.background_share("nightly", False, False) is None
    monkeypatch.setenv("POS_BUDGET_SHED", "0")
    assert not pos_capacity.bulk_shed()


def test_a_shed_verdict_lapses_when_the_window_ends(monkeypatch):
    _x("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now'))",
       (pos_capacity.STATE_KEY, json.dumps({"shed_bulk": True, "stretch_live": True, "until": time.time() - 5})))
    assert pos_capacity.bulk_shed() is False and pos_capacity.live_stretched() is False


def test_three_nights_over_the_alert_line_are_said_once(monkeypatch):
    _rpower_stores(1, monkeypatch)
    caught = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: caught.append(str(e)))
    monkeypatch.setattr(pos_capacity, "utilisation",
                        lambda hours, db_path=None: {"lanes": {"nightly": {"pct": 70, "peak_hour_pct": 95}}})
    for _ in range(4):
        pos_capacity.budget_check()
    assert sum("3 nights running" in c for c in caught) == 1
    assert pos_capacity.state()["over_nights"] == 4


# ── A4-11 / A5-11 / A5-03: the queue's POS groups ───────────────────────────

def test_the_pos_groups_are_sized_from_the_budget(monkeypatch):
    monkeypatch.setattr(pos_capacity, "effective_rps", lambda db_path=None: 10.0)
    assert job_queue.group_concurrency("pos_io") == 5            # 10 x 0.8 x 0.4 x 1.5 = 4.8
    assert job_queue.group_concurrency("live") == 5              # 10 x 0.6 x 0.48 x 1.5 = 4.3
    assert job_queue.group_concurrency("pos_bulk") == 2
    monkeypatch.setattr(pos_capacity, "effective_rps", lambda db_path=None: 1 / 0.15)
    assert job_queue.group_concurrency("pos_io") == 4, "never under 4"
    monkeypatch.setattr(pos_capacity, "effective_rps", lambda db_path=None: 45.0)
    assert job_queue.group_concurrency("pos_io") == 22
    monkeypatch.setenv("QUEUE_POS_IO_CONCURRENCY", "3")
    assert job_queue.group_concurrency("pos_io") == 3, "the operator's number is kept"
    import strategy_jobs
    monkeypatch.setattr(pos_capacity, "effective_rps", lambda db_path=None: 30.0)
    assert strategy_jobs.slot_workers() == pos_capacity.live_concurrency() > 4


def test_onboarding_has_its_own_group_and_the_nightly_rows_theirs():
    assert job_queue.KINDS["pos_onboard"][0] == "pos_bulk"
    assert job_queue.KINDS["pos_archive"][0] == "pos_io" and job_queue.KINDS["pos_sync"][0] == "pos_io"
    assert job_queue.KINDS["live_labor"][0] == "live"
    assert {"pos_bulk", "live"} <= set(job_queue.GROUPS)
    for kind in ("pos_archive", "pos_onboard", "pos_sync"):
        assert job_queue.EXPIRE_HOOKS[kind] == "strategy_jobs:on_pos_row_expired"
    assert job_queue.HANDLERS["pos_sync"] == "strategy_jobs:run_queued_pos_sync"
    assert job_queue.HANDLERS["live_labor"] == "labor_live:run_queued_live"


def test_onboarding_is_admitted_by_the_budget(monkeypatch):
    import strategy_jobs
    monkeypatch.setenv("POS_ONBOARD_PER_HOUR", "30")              # one every 2 minutes
    for i in (1, 2, 3):
        _restaurant(i)
        assert strategy_jobs.enqueue_pos_onboard(i)
    nbs = [r["not_before"] for r in _q("SELECT not_before FROM jobs WHERE kind='pos_onboard' ORDER BY id")]
    assert nbs[1] - nbs[0] == pytest.approx(120, abs=1) and nbs[2] - nbs[1] == pytest.approx(120, abs=1)
    assert nbs[0] <= time.time() + 1


def test_shed_onboarding_waits_for_the_window_to_end(monkeypatch):
    import strategy_jobs
    until = time.time() + 3600
    _x("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now'))",
       (pos_capacity.STATE_KEY, json.dumps({"shed_bulk": True, "until": until})))
    _restaurant(1)
    assert strategy_jobs.enqueue_pos_onboard(1)
    assert _q("SELECT not_before FROM jobs WHERE kind='pos_onboard'")[0]["not_before"] >= until - 1
    import pos_archive
    monkeypatch.setattr(pos_archive, "provider_for", lambda rid: ("rpower", rpower))
    monkeypatch.setattr(pos_archive, "onboard", lambda rid: pytest.fail("onboarding ran while shed"))
    with pytest.raises(RuntimeError) as e:
        strategy_jobs.run_queued_pos_onboard(job_queue.Job({"payload": {"restaurant_id": 1}}))
    assert e.value.retry_after > 3000


def test_an_expired_pos_row_is_a_recorded_missed_run(monkeypatch):
    _restaurant(1, created_at="2026-01-01 00:00:00")
    day = "2026-10-20"
    qid, _ = job_queue.enqueue("pos_archive", {"restaurant_id": 1, "day": day}, restaurant_id=1,
                               deadline=time.time() - 1, wake=False)
    assert job_queue.claim("w1") is None
    assert _q("SELECT state FROM jobs WHERE id=?", (qid,))[0]["state"] == "expired"
    rows = _q("SELECT job, restaurant_id, local_date FROM missed_windows")
    assert rows == [{"job": "pos_archive", "restaurant_id": 1, "local_date": day}]


# ── A5-02: the nightly sync as queue rows ───────────────────────────────────

def test_each_store_is_due_at_its_own_hour_inside_the_spread():
    r_chi = models.get_restaurant(_restaurant(1, timezone="America/Chicago"))
    r_la = models.get_restaurant(_restaurant(2, timezone="America/Los_Angeles"))
    night, nb = pos._sync_slot(r_chi, datetime(2026, 10, 21, 3, 0))
    local = datetime.fromtimestamp(nb, CHI)
    assert night == "2026-10-20" and local.date() == date(2026, 10, 21)
    assert 3 <= local.hour < 5 and (local.hour - 3) * 60 + local.minute == (1 * 37) % pos.POS_SYNC_SPREAD_MINUTES
    # 3am Chicago is 1am in Los Angeles: its row waits for ITS 3am.
    night, nb = pos._sync_slot(r_la, datetime(2026, 10, 21, 1, 0))
    la = datetime.fromtimestamp(nb, __import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
    assert night == "2026-10-20" and la.date() == date(2026, 10, 21) and la.hour in (3, 4)
    # An evening run is for the next morning.
    assert pos._sync_slot(r_chi, datetime(2026, 10, 21, 20, 0))[0] == "2026-10-21"


def test_the_nightly_job_queues_one_row_a_store_once(monkeypatch):
    _rpower_stores(3, monkeypatch)
    stats = {}
    assert pos.queue_nightly_syncs(stats=stats) == 3
    assert stats["attempted"] == 3 and stats["hit_bound"] is False
    assert pos.queue_nightly_syncs() == 0, "keyed on the store and the night"
    rows = _q("SELECT restaurant_id, grp, deadline, not_before FROM jobs WHERE kind='pos_sync' ORDER BY restaurant_id")
    assert [r["restaurant_id"] for r in rows] == [1, 2, 3] and {r["grp"] for r in rows} == {"pos_io"}
    assert all(r["deadline"] > r["not_before"] for r in rows)


def test_a_sync_row_runs_the_stores_sync_and_never_raises_on_its_failure(monkeypatch):
    import strategy_jobs
    _rpower_stores(1, monkeypatch)
    calls = []
    monkeypatch.setattr(pos, "sync_restaurant", lambda rid, **k: calls.append(rid) or {"ok": False, "error": "boom"})
    monkeypatch.setattr(pos, "note_sync_failure", lambda rid: None)
    caught = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: caught.append(k.get("job")))
    out = strategy_jobs.run_queued_pos_sync(job_queue.Job({"payload": {"restaurant_id": 1, "night": "2026-10-20"}}))
    assert calls == [1] and out["ok"] is False and out["error"] == "boom" and caught == ["pos_sync"]


class _StopLoop(Exception):
    pass


def _tick(monkeypatch, at, allowed):
    """One real scheduler_loop tick at `at` (Chicago), claiming only the
    jobs in `allowed` (the rest are left alone)."""
    import scheduler
    import marketing_publish, notify, issues, morning_brief, delayed
    real_claim = ops.claim_period
    monkeypatch.setattr(scheduler, "_chi_now", lambda: at)
    monkeypatch.setattr(ops, "claim_period", lambda job, period: job in allowed and real_claim(job, period))
    monkeypatch.setattr(notify, "release_due_alerts", lambda *a, **k: {})
    monkeypatch.setattr(issues, "tick", lambda *a, **k: None)
    monkeypatch.setattr(morning_brief, "run_due", lambda *a, **k: None)
    monkeypatch.setattr(delayed, "run_due", lambda *a, **k: {})
    monkeypatch.setattr(marketing_publish, "run_due_posts", lambda **k: {})
    monkeypatch.setattr(scheduler, "record_scheduler_heartbeat", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(scheduler, "run_health_checks", lambda *a, **k: None, raising=False)

    def _stop(_s):
        raise _StopLoop()
    real_sleep = time.sleep
    monkeypatch.setattr(scheduler.time, "sleep", _stop)
    with pytest.raises(_StopLoop):
        scheduler.scheduler_loop()
    monkeypatch.setattr(scheduler.time, "sleep", real_sleep)
    for _ in range(200):                                  # the lanes the tick started
        if not any(lane.busy() for lane in scheduler._LANES.values()):
            break
        real_sleep(0.02)
    monkeypatch.setattr(ops, "claim_period", real_claim)


def test_a_real_tick_queues_the_stores_and_the_followers_wait_for_their_rows(monkeypatch):
    import scheduler
    import strategy_jobs
    _restaurant(1, timezone="America/Chicago")
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("rpower", rpower))
    monkeypatch.setattr(scheduler, "run_inventory_sync", lambda: 0)
    # The rows stay queued for the test to finish by hand (no consumer here).
    monkeypatch.setattr(job_queue, "claim", lambda *a, **k: None)
    synced = []
    monkeypatch.setattr(pos, "sync_restaurant", lambda rid, **k: synced.append(rid) or {"ok": True})
    monkeypatch.setattr(pos, "note_sync_failure", lambda rid: None)
    ran = []
    monkeypatch.setattr(strategy_jobs, "run_loss_sync", lambda: ran.append("loss_sync") or {"attempted": 0})
    at = datetime.now(CHI).replace(tzinfo=None, hour=3, minute=5, second=0, microsecond=0)
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now",
                        lambda r=None, naive=False: at if naive else at.replace(tzinfo=CHI))
    allowed = {"pos_sync", "loss_sync"}
    _tick(monkeypatch, at, allowed)
    rows = _q("SELECT id, state FROM jobs WHERE kind='pos_sync'")
    assert len(rows) == 1 and rows[0]["state"] == "queued", "the 3am tick queued the store's row"
    assert _q("SELECT 1 FROM job_runs WHERE job='pos_sync'"), "the fan-out ran through ops.run_job"
    _tick(monkeypatch, at.replace(minute=10), allowed)
    assert ran == [], "loss sync waits while the store's sync row is open"
    _x("UPDATE jobs SET state='done' WHERE kind='pos_sync'")
    _tick(monkeypatch, at.replace(minute=15), allowed)
    assert ran == ["loss_sync"]


def test_the_followers_go_on_at_their_latest_hour(monkeypatch):
    import scheduler

    class _Ops:
        def settled(self, job, period):
            return True
    monkeypatch.setattr(pos, "open_sync_rows", lambda due_by, db_path=None: 5)
    today = date(2026, 10, 21)
    assert scheduler._pos_sync_settled(_Ops(), datetime(2026, 10, 21, 4, 0), today) is False
    assert scheduler._pos_sync_settled(_Ops(), datetime(2026, 10, 21, 6, 0), today) is True


def test_the_budget_check_is_a_registered_scheduled_job():
    import inspect
    import jobs_registry
    import scheduler
    assert jobs_registry.spec("pos_budget_check")["target"] == ("pos_capacity", "budget_check")
    assert 'run_job("pos_budget_check", _budget_check)' in inspect.getsource(scheduler.scheduler_loop)


# ── A1-01 / A5-06: live sales, incrementally ───────────────────────────────

def _line(atom, sales, stamp, day="2026-10-21", st="1", voided=0):
    return {"rid": "t1", "atom": atom, "sales": sales, "time_stamp": stamp, "date": day + "T00:00:00",
            "slstype_mid": st, "voided": voided}


def test_live_sales_merge_the_change_feed_into_the_days_line_set(monkeypatch):
    import live_sales
    _restaurant(1)
    monkeypatch.setattr(rpower, "sales_types", lambda rid: {"1": {"impacts_sales": 1}, "9": {"type_comp": 1,
                                                                                           "impacts_sales": 1}})
    asked = []
    answers = {
        "ticketsales/getbybusinessdate": [_line(1, 10.0, "2026-10-21 18:00:00"), _line(2, 5.0, "2026-10-21 18:05:00")],
    }

    def paged(token, path, params, max_pages=200):
        asked.append((path, params.get("startdate")))
        return answers.get(path, [])
    monkeypatch.setattr(rpower, "_paged", paged)
    t0 = time.time()
    out = live_sales.fetch_today(1, "2026-10-21", now=t0)
    assert (out["net"], out["mode"]) == (15.0, "full")
    # A new line, a line voided, and a line from another night in the feed.
    answers["ticketsales/getbytimestamp"] = [_line(3, 7.5, "2026-10-21 18:20:00"),
                                             _line(1, 10.0, "2026-10-21 18:21:00", voided=1),
                                             _line(9, 99.0, "2026-10-21 18:22:00", day="2026-10-20")]
    out = live_sales.fetch_today(1, "2026-10-21", now=t0 + 900)
    assert (out["net"], out["mode"], out["rows"]) == (12.5, "delta", 3)
    assert asked[-1] == ("ticketsales/getbytimestamp", "2026-10-21T17:50:00"), "since the newest stamp less 15 min"
    # A line re-rung as a comp drops out; the next hour is a full re-read.
    answers["ticketsales/getbytimestamp"] = [_line(2, 5.0, "2026-10-21 18:30:00", st="9")]
    assert live_sales.fetch_today(1, "2026-10-21", now=t0 + 1800)["net"] == 7.5
    answers["ticketsales/getbybusinessdate"] = [_line(3, 7.5, "2026-10-21 18:20:00")]
    out = live_sales.fetch_today(1, "2026-10-21", now=t0 + 3700)
    assert (out["net"], out["mode"]) == (7.5, "full")


def test_rpower_fetch_sales_today_reads_through_live_sales(monkeypatch):
    import live_sales
    monkeypatch.setattr(live_sales, "fetch_today", lambda rid, day: {"net": None})
    with pytest.raises(rpower.RPowerNoSalesYet):
        rpower.fetch_sales_today(1, "2026-10-21")
    monkeypatch.setattr(live_sales, "fetch_today", lambda rid, day: {"net": 42.0})
    assert rpower.fetch_sales_today(1, "2026-10-21") == 42.0


def test_with_the_rollback_every_read_is_the_whole_day(monkeypatch):
    import live_sales
    _restaurant(1)
    monkeypatch.setattr(live_sales, "INCREMENTAL", False)
    monkeypatch.setattr(rpower, "sales_types", lambda rid: {"1": {"impacts_sales": 1}})
    monkeypatch.setattr(rpower, "_paged", lambda t, p, params, max_pages=200: [_line(1, 4.0, "2026-10-21 18:00:00")])
    assert live_sales.fetch_today(1, "2026-10-21")["mode"] == "full"
    assert live_sales.fetch_today(1, "2026-10-21")["mode"] == "full"


# ── A1-02 / A5-03: live labor's cadence and its queue rows ─────────────────

def _labor_store(monkeypatch, rows):
    import labor_live
    rid = _restaurant(1, module_labor=1, timezone="America/Chicago")
    monkeypatch.setattr(labor_live, "_today_rows", lambda r, d, db_path=None: rows)
    monkeypatch.setattr(labor_live, "cut_minute", lambda *a, **k: (22 * 60, "test"))
    return models.get_restaurant(rid)


def test_live_labor_reads_every_15_minutes_only_inside_a_decision_window(monkeypatch):
    import labor_live
    shift = {"_a": datetime(2026, 10, 21, 17, 0), "_z": datetime(2026, 10, 21, 21, 0)}
    r = _labor_store(monkeypatch, [shift])
    assert labor_live.cadence_minutes(r, datetime(2026, 10, 21, 16, 30)) == 15     # 30 min before a start
    assert labor_live.cadence_minutes(r, datetime(2026, 10, 21, 18, 30)) == 30     # mid-shift
    assert labor_live.cadence_minutes(r, datetime(2026, 10, 21, 21, 30)) == 15     # inside the cut window too
    _x("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now'))",
       (pos_capacity.STATE_KEY, json.dumps({"stretch_live": True, "until": time.time() + 600})))
    pos_capacity.reset_for_tests()
    assert labor_live.cadence_minutes(r, datetime(2026, 10, 21, 18, 30)) == 60     # stretched
    assert labor_live.cadence_minutes(r, datetime(2026, 10, 21, 16, 30)) == 15     # never inside a window


def test_no_schedule_or_labor_off_reads_rarely_or_never(monkeypatch):
    import labor_live
    r = _labor_store(monkeypatch, [])
    assert labor_live.cadence_minutes(r, datetime(2026, 10, 21, 18, 30)) == 60
    _x("UPDATE restaurants SET module_labor=0 WHERE id=1")
    assert labor_live.cadence_minutes(models.get_restaurant(1), datetime(2026, 10, 21, 18, 30)) is None


def test_the_live_labor_job_queues_one_row_per_due_store_and_the_row_reads_it(monkeypatch):
    import labor_live
    import strategy_jobs
    import time_utils
    r = _labor_store(monkeypatch, [{"_a": datetime(2026, 10, 21, 17, 0), "_z": datetime(2026, 10, 21, 21, 0)}])
    monkeypatch.setattr(time_utils, "restaurant_now", lambda rr, naive=False: datetime(2026, 10, 21, 16, 50))
    monkeypatch.setattr(strategy_jobs, "_open_now", lambda rr, local: True)
    out = labor_live.run_live_labor(restaurants=[r])
    assert out["queued"] == 1 and out["by_cadence"] == {"15": 1}
    assert labor_live.run_live_labor(restaurants=[r])["queued"] == 0, "the slot is claimed once"
    row = _q("SELECT grp, payload_json, deadline FROM jobs WHERE kind='live_labor'")[0]
    assert row["grp"] == "live" and json.loads(row["payload_json"])["restaurant_id"] == 1
    ran = []
    monkeypatch.setattr(labor_live, "run_for", lambda rr, now_local=None, db_path=None: ran.append(rr.id) or {})
    out = labor_live.run_queued_live(job_queue.Job({"payload": {"restaurant_id": 1}}))
    assert ran == [1] and out["attempted"] == 1


# ── A5-04 / A1-24: one closeday answer, polled from the learned lag ────────

def test_a_closed_day_is_never_asked_again_and_an_open_answer_is_shared():
    asks = []

    def ask_open():
        asks.append("open")
        return False
    assert pos_closed.day_closed(1, "2026-10-20", ask_open, fresh_seconds=300, now=1000) is False
    assert pos_closed.day_closed(1, "2026-10-20", ask_open, fresh_seconds=300, now=1200) is False
    assert asks == ["open"], "an open answer is shared for the asker's freshness"
    assert pos_closed.day_closed(1, "2026-10-20", lambda: True, fresh_seconds=300, now=1400) is True
    assert pos_closed.day_closed(1, "2026-10-20", lambda: pytest.fail("asked again"), now=99999) is True
    assert pos_closed.known(1, "2026-10-20")["polls"] == 2


def test_the_archive_reads_the_same_shared_row(monkeypatch):
    _restaurant(1)
    pos_closed.day_closed(1, "2026-10-20", lambda: True)
    monkeypatch.setattr(rpower, "fetch_day_closed", lambda rid, day: pytest.fail("the archive asked RPOWER"))
    assert rpower.day_closed_cached(1, "2026-10-20") is True


def _night(day, close_epoch, seen_after_minutes):
    _x("INSERT INTO pos_closed_days (restaurant_id, business_date, closed, closed_seen_at, checked_at, polls, "
       "close_epoch) VALUES (1, ?, 1, ?, ?, 4, ?)",
       (day, close_epoch + seen_after_minutes * 60, close_epoch + seen_after_minutes * 60, close_epoch))


def test_the_close_lag_is_learned_and_the_first_poll_waits_for_it():
    close = datetime(2026, 10, 20, 23, 0, tzinfo=timezone.utc).timestamp()
    assert pos_closed.close_lag_minutes(1) is None
    for i, lag in enumerate((140, 150, 160)):
        _night(f"2026-10-1{i}", close - (i + 1) * 86400, lag)
    assert pos_closed.close_lag_minutes(1) == 150
    first = pos_closed.first_poll_at(1, close)
    assert first == close + (150 - pos_closed.FIRST_EARLY_MINUTES) * 60
    assert pos_closed.next_poll_at(1, "2026-10-20", close, close + 60) == first
    # From there 10, 20, 30, 30 minutes apart.
    steps = []
    now = first
    for _ in range(4):
        pos_closed.day_closed(1, "2026-10-20", lambda: False, fresh_seconds=0, now=now)
        nxt = pos_closed.next_poll_at(1, "2026-10-20", close, now)
        steps.append(round((nxt - now) / 60))
        now = nxt
    assert steps == [10, 20, 30, 30]


def test_the_dsr_holds_its_first_poll_until_the_learned_lag(monkeypatch):
    from dsr import pipeline
    rid = _restaurant(1, timezone="America/Chicago")
    r = models.get_restaurant(rid)
    day = date(2026, 10, 20)
    close_epoch = pipeline._close_epoch(r, day)
    for i, lag in enumerate((120, 125, 130)):
        _night(f"2026-10-1{i}", close_epoch - (i + 1) * 86400, lag)
    now_utc = datetime.fromtimestamp(close_epoch + 30 * 60, timezone.utc).replace(tzinfo=None)
    hold, nxt = pipeline._close_poll(r, day, now_utc, pipeline.TRIGGER_SWEEP)
    assert hold is True
    assert abs((nxt.replace(tzinfo=timezone.utc).timestamp() - close_epoch) / 60 - 120) < 1
    assert pipeline._close_poll(r, day, now_utc, pipeline.TRIGGER_MANUAL)[0] is False, "a manual run never holds"


# ── A5-09: the cold tier; retention for the growing tables ─────────────────

@pytest.fixture
def cold(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet
    import pos_cold
    monkeypatch.setenv("BLOB_STORE", "local")
    monkeypatch.setenv("BLOB_STORE_DIR", str(tmp_path / "blobs"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(pos_cold, "HOT_DAYS", 400)
    return pos_cold


def _lines(rid, day, n, rolled=True):
    import pos_archive
    _x("INSERT INTO pos_archive_days (restaurant_id, provider, business_date, item_rollup) VALUES (?,?,?,?)",
       (rid, "rpower", day, pos_archive.ITEM_ROLLUP_VERSION if rolled else 0))
    for i in range(n):
        _x("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, business_date, kind, qty, sales) "
           "VALUES (?,?,?,?,?,?,?)", (rid, "rpower", f"{day}-{i}", day, "sale", 1, 2.5))


def test_an_old_month_is_exported_verified_and_only_then_deleted(cold):
    _restaurant(1)
    _lines(1, "2025-01-10", 3)
    _lines(1, "2025-01-11", 2)
    _lines(1, "2026-10-01", 1)                                   # hot
    out = cold.tier(today=date(2026, 10, 21))
    assert out["exported"] == 1 and out["deleted"] == 5 and out["failed"] == 0
    assert [r["business_date"] for r in _q("SELECT business_date FROM pos_ticket_lines")] == ["2026-10-01"]
    ex = _q("SELECT rows_n, deleted_at, ref FROM pos_cold_exports")[0]
    assert ex["rows_n"] == 5 and ex["deleted_at"] and ex["ref"].startswith("cavnar-blob:local:pos-cold/pos-lines/1/")
    assert cold.cold_months(1) == ["2025-01"]
    back = cold.restore(1, "2025-01")
    assert back["restored"] == 5 and back["held_until"]
    assert len(_q("SELECT 1 FROM pos_ticket_lines WHERE business_date < '2026-01-01'")) == 5
    again = cold.tier(today=date(2026, 10, 21))
    assert again["held"] == 1 and again["deleted"] == 0, "a restored month is held hot"


def test_nothing_is_deleted_without_a_store_and_a_key(cold, monkeypatch):
    _restaurant(1)
    _lines(1, "2025-01-10", 3)
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY")
    out = cold.tier(today=date(2026, 10, 21))
    assert "BACKUP_ENCRYPTION_KEY" in out["skipped"] and len(_q("SELECT 1 FROM pos_ticket_lines")) == 3
    monkeypatch.setenv("BLOB_STORE", "db")
    assert "no object store" in cold.tier(today=date(2026, 10, 21))["skipped"]


def test_a_month_not_rolled_into_its_item_history_stays(cold):
    _restaurant(1)
    _lines(1, "2025-01-10", 3, rolled=False)
    out = cold.tier(today=date(2026, 10, 21))
    assert out["unrolled"] == 1 and out["deleted"] == 0 and len(_q("SELECT 1 FROM pos_ticket_lines")) == 3


def test_a_store_that_refused_the_object_keeps_its_lines(cold, monkeypatch):
    import blob_store
    _restaurant(1)
    _lines(1, "2025-01-10", 3)
    monkeypatch.setattr(blob_store, "store", lambda kind, key, data, in_transaction=False: data)
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    out = cold.tier(today=date(2026, 10, 21))
    assert out["failed"] == 1 and out["deleted"] == 0 and len(_q("SELECT 1 FROM pos_ticket_lines")) == 3


def test_the_retention_pass_runs_the_cold_tier(cold, monkeypatch):
    _restaurant(1)
    _lines(1, "2025-01-10", 2)
    monkeypatch.setattr(cold, "cutoff", lambda today=None: "2026-01-01")
    out = ops.prune_ledgers()
    assert out.get("pos_lines_cold") == 2 and not _q("SELECT 1 FROM pos_ticket_lines")
    monkeypatch.setattr(cold, "HOT_DAYS", 30)
    assert ops.retention_state("pos_lines_cold")["state"] == "below_floor"


def test_the_growing_tables_have_their_stated_windows():
    for table, days, col in (("dsr_reports", 1095, "business_date"), ("dsr_metrics", 1095, "business_date"),
                             ("pos_intraday", 400, "business_date"), ("pos_closed_days", 400, "business_date"),
                             ("pos_lane_ledger", 90, "hour")):
        assert ops._RETENTION_DAYS[table] == days and ops._RETENTION_COLUMN[table] == col, table
        assert ops.retention_state(table)["state"] == "ok"
    assert ops._RETENTION_ROLLUP["pos_request_ledger"] == "pos_capacity:roll_ledger_daily"


# ── A5-13: the request ledger by day after a week ──────────────────────────

def test_the_ledger_rolls_old_hours_into_days(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    new = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:00:00")
    for h, n in ((f"{old} 01:00:00", 3), (f"{old} 02:00:00", 4), (new, 5)):
        _x("INSERT INTO pos_request_ledger (hour, provider, restaurant_id, endpoint, n, ok, ms_total, ms_max) "
           "VALUES (?,?,?,?,?,?,?,?)", (h, "rpower", 5, "ticketsales/getbybusinessdate", n, n, n * 100.0, 300.0 + n))
    assert pos_capacity.roll_ledger_daily() == 1
    rows = {r["hour"]: r for r in _q("SELECT hour, n, ok, ms_total, ms_max FROM pos_request_ledger")}
    assert set(rows) == {old, new}
    assert (rows[old]["n"], rows[old]["ok"], rows[old]["ms_total"], rows[old]["ms_max"]) == (7, 7, 700.0, 304.0)
    assert pos_capacity.roll_ledger_daily() == 0, "a rolled day is not rolled again"
    assert pos_io.summary(24)["total"]["n"] == 5, "the console's day is unchanged"
