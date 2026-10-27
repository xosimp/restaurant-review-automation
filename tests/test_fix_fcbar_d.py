"""Food Cost + Bar audit 10/16/26, fix workstream D — RPOWER / POS data, the
archive, time and scheduling (register: docs/audits, findings POS-06..21,
SCALE-06/07/11, FC-CALC-24).

Each test names its finding and asserts the corrected behaviour."""
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import models
import pos
import pos_archive
import rpower

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHI = ZoneInfo("America/Chicago")
DAY = date.today() - timedelta(days=10)          # closed, and past the freshness window
ISO = DAY.isoformat()


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos_archive, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(pos, "PROVIDERS", None)
    pos_archive._change_memo.clear()
    rpower.clear_caches()
    yield


def _rid(db_path, rid=1, **cols):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO restaurants (id, name, owner_email, rpower_token, rpower_cg, rpower_store_mid, timezone) "
                 "VALUES (?,?,?,?,?,?,?)", (rid, f"D Co {rid}", f"d{rid}@x.test", "tok", 1280,
                                            # One store per live restaurant (the database
                                            # holds it since Audit #23 A7-06).
                                            "1123" if rid == 1 else f"1123-{rid}", "America/Chicago"))
    for k, v in cols.items():
        conn.execute(f"UPDATE restaurants SET {k}=? WHERE id=?", (v, rid))
    conn.commit()
    conn.close()
    return rid


def _q(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


TYPES = [{"mid": "S", "name": "Sale", "impacts_sales": 1, "type_sale": 1},
         {"mid": "D", "name": "Discount", "impacts_sales": 1, "type_discnt": 1},
         {"mid": "P", "name": "Promo", "impacts_sales": 1, "type_promo": 1},
         {"mid": "E", "name": "Error Correct", "impacts_sales": 1, "type_error": 1}]


def _routes(day_iso, lines=None, tickets=None, **over):
    d = f"{day_iso}T00:00:00"
    routes = {
        "salestype/getbycg": TYPES,
        "salescategory/getbycg": [{"mid": "K1", "name": "Entrees", "slsdep_mid": "DP1"}],
        "salesdepartment/getbycg": [{"mid": "DP1", "name": "Food"}, {"mid": "DP2", "name": "Catering"}],
        "menuitem/getbycg": [{"mid": "M1", "name": "Burger", "slscat_mid": "K1"}],
        "voidreason/getbycg": [{"mid": "R1", "name": "Error Made"}, {"mid": "R2", "name": "Rang In Wrong"}],
        "job/getbycg": [], "employee/getbycg": [], "table/getbystore": [], "room/getbystore": [],
        "mealtime/getbycg": [], "profitcenter/getbycg": [], "paymentmethod/getbycg": [], "payoutcategory/getbycg": [],
        "ticketpayment/getbybusinessdate": [], "payout/getbydaterange": [], "timeclock/getbydaterange": [],
        "ticketitem/getbybusinessdate": [], "closeday/getbybusinessdate": [],
        "ticketsales/getbytimestamp": [], "ticket/getbytimestamp": [], "ticketpayment/getbytimestamp": [],
        "ticketsales/getbybusinessdate": lines if lines is not None else [
            {"rid": "L1", "atom": 11, "ticket_rid": "T1", "date": d, "menuitem_mid": "M1", "slstype_mid": "S",
             "qty": 3, "sales": 45.0, "time_stamp": f"{day_iso}T23:00:00"}],
        "ticket/getbybusinessdate": tickets if tickets is not None else [
            {"rid": "T1", "date": d, "type_sale": 45.0, "time_stamp": f"{day_iso}T23:00:00"}],
    }
    routes.update(over)
    return routes


def _stub(monkeypatch, routes):
    calls = []

    def fake(token, path, params=None):
        calls.append(path)
        hit = routes[path]
        if isinstance(hit, Exception):
            raise hit
        return hit
    monkeypatch.setattr(rpower, "_request", fake)
    return calls


# ── POS-06: one classifier ──────────────────────────────────────────────────

FLAGS = ("type_sale", "type_nonsale", "type_return", "type_refund", "type_comp", "type_waste", "type_error",
         "type_promo", "type_discnt", "type_hash", "type_hashdisc", "type_grat", "type_hidgrat", "type_housegrat",
         "type_fee", "type_tax")
_SAME = {"excluded": "other", "unknown": "other"}


@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("impacts", (0, 1))
def test_pos_06_the_archive_and_the_report_classify_every_sales_type_alike(flag, impacts):
    types = {"X": {"mid": "X", flag: 1, "impacts_sales": impacts}}
    row = {"slstype_mid": "X", "sales": -5.0, "qty": 1}
    report, archive = rpower._line_kind(row, types), rpower.line_kind(row, types)
    assert _SAME.get(report, report) == ("excluded" if archive == "nonsale" else archive) or \
        (report == "excluded" and archive in ("nonsale", "other")), (flag, impacts, report, archive)
    if flag in rpower.DISCOUNT_FLAGS and impacts:
        assert report == archive == "discount"


# ── POS-07: a void is not a dish sold ───────────────────────────────────────

def test_pos_07_an_error_correct_line_is_not_depleted_unless_the_food_was_made(db_path, monkeypatch):
    rid = _rid(db_path)
    d = f"{ISO}T00:00:00"
    lines = [{"rid": "L1", "ticket_rid": "T1", "date": d, "menuitem_mid": "M1", "slstype_mid": "S", "qty": 3,
              "sales": 45.0},
             {"rid": "L2", "ticket_rid": "T1", "date": d, "menuitem_mid": "M1", "slstype_mid": "E", "qty": 2,
              "sales": 30.0, "voidrsn_mid": "R2"},                        # rang in wrong: never made
             {"rid": "L3", "ticket_rid": "T1", "date": d, "menuitem_mid": "M1", "slstype_mid": "E", "qty": 1,
              "sales": 15.0, "voidrsn_mid": "R1"}]                        # error made: it left the kitchen
    _stub(monkeypatch, _routes(ISO, lines=lines))
    monkeypatch.setenv("POS_ARCHIVE_READS", "0")
    # 3 sold + 1 made in error = 4 made; `sold` beside it (Audit #23 A2-12).
    assert rpower.fetch_order_selections(rid, DAY) == [{"item": {"guid": "M1"}, "quantity": 4.0, "sold": 3.0}]


# ── POS-08: the nightly archive archives last night ─────────────────────────

def _freeze(monkeypatch, local):
    import time_utils
    aware = local.replace(tzinfo=CHI)
    monkeypatch.setattr(time_utils, "restaurant_now",
                        lambda r=None, naive=False: aware.replace(tzinfo=None) if naive else aware)


class _Fake:
    def __init__(self, closed=True):
        self.calls, self.closed = [], closed

    def archive_rows(self, rid, day):
        iso = day.isoformat()
        self.calls.append(iso)
        return {"tickets": [], "lines": [{"line_id": f"l-{iso}", "business_date": iso, "kind": "sale", "qty": 1,
                                          "sales": 10.0}], "punches": [], "max_stamp": None, "links_ok": True}

    def day_closed_cached(self, rid, day):
        return self.closed


def test_pos_08_a_4_30am_fan_out_waits_for_the_boundary_and_archives_last_night(db_path, monkeypatch):
    import strategy_jobs
    rid = _rid(db_path)
    r = models.get_restaurant(rid)
    # The boundary is the restaurant's business-day turnover, 4am unless it
    # trades later (audit #23 A3-14: the punches' own boundary, not 5am).
    _freeze(monkeypatch, datetime(2026, 10, 16, 3, 30))
    night, not_before = strategy_jobs._archive_night(r)
    assert night == "2026-10-15"
    assert not_before == datetime(2026, 10, 16, 4, 0, 30, tzinfo=CHI).timestamp()
    _freeze(monkeypatch, datetime(2026, 10, 16, 4, 30))
    assert strategy_jobs._archive_night(r) == ("2026-10-15", 0)
    # ...and the row, when it runs at the boundary, stores that night.
    fake = _Fake()
    monkeypatch.setattr(pos_archive, "provider_for", lambda _r: ("rpower", fake))
    _freeze(monkeypatch, datetime(2026, 10, 16, 5, 0, 30))
    out = pos_archive.run_for(rid, now_utc=datetime(2026, 10, 16, 10, 0, 30, tzinfo=timezone.utc))
    assert fake.calls[0] == "2026-10-15" and "2026-10-15" in out["archived"]


def test_pos_08_16_a_night_the_pos_has_not_closed_is_not_stored_yet(db_path, monkeypatch):
    rid = _rid(db_path)
    fake = _Fake(closed=False)
    monkeypatch.setattr(pos_archive, "provider_for", lambda _r: ("rpower", fake))
    _freeze(monkeypatch, datetime(2026, 10, 16, 5, 30))
    out = pos_archive.run_for(rid, now_utc=datetime(2026, 10, 16, 10, 30, tzinfo=timezone.utc))
    assert out["ok"] and out["not_closed"] == ["2026-10-15"] and "2026-10-15" not in fake.calls


# ── POS-09: a layout bump leaves stored days readable ───────────────────────

def test_pos_09_a_layout_bump_does_not_send_readers_back_to_rpower(db_path, monkeypatch):
    rid = _rid(db_path)
    calls = _stub(monkeypatch, _routes(ISO))
    pos_archive.archive_day(rid, DAY)
    monkeypatch.setattr(pos_archive, "ARCHIVE_VERSION", pos_archive.ARCHIVE_VERSION + 1)
    assert ISO not in pos_archive.archived_dates(rid, "rpower"), "the backfill stores it again..."
    calls.clear()
    assert rpower.fetch_business_days(rid, DAY, DAY) == {ISO: 45.0}
    assert "ticketsales/getbybusinessdate" not in calls, "...and readers keep reading it meanwhile"


# ── POS-10: every line is a row ─────────────────────────────────────────────

def test_pos_10_two_lines_sharing_a_rid_are_both_stored(db_path, monkeypatch):
    rid = _rid(db_path)
    d = f"{ISO}T00:00:00"
    lines = [{"rid": "SAME", "atom": a, "ticket_rid": "T1", "date": d, "menuitem_mid": "M1", "slstype_mid": "S",
              "qty": 1, "sales": s} for a, s in ((1, 10.0), (2, 20.0))]
    _stub(monkeypatch, _routes(ISO, lines=lines, tickets=[{"rid": "T1", "date": d, "type_sale": 30.0}]))
    out = pos_archive.archive_day(rid, DAY)
    assert out["lines"] == 2
    rows = _q(db_path, "SELECT line_id, sales FROM pos_ticket_lines WHERE restaurant_id=?", (rid,))
    assert sorted(r["line_id"] for r in rows) == ["SAME:1", "SAME:2"] and sum(r["sales"] for r in rows) == 30.0


def test_pos_10_a_key_collision_fails_the_day_rather_than_storing_it_short(db_path, monkeypatch):
    rid = _rid(db_path)

    class Dup(_Fake):
        def archive_rows(self, rid, day):
            iso = day.isoformat()
            return {"tickets": [], "punches": [], "max_stamp": None,
                    "lines": [{"line_id": "x", "business_date": iso, "kind": "sale", "qty": 1, "sales": s}
                              for s in (10.0, 20.0)]}
    with pytest.raises(pos_archive.ArchiveCountMismatch):
        pos_archive.archive_day(rid, DAY, provider=("rpower", Dup()))
    assert _q(db_path, "SELECT COUNT(*) AS n FROM pos_archive_days WHERE restaurant_id=?", (rid,))[0]["n"] == 0


# ── POS-11: the nightly reconcile ───────────────────────────────────────────

def test_pos_11_unknown_type_dollars_and_a_gap_raise_a_reconcile_event(db_path, monkeypatch):
    import ops
    rid = _rid(db_path)
    seen = []
    monkeypatch.setattr(ops, "capture", lambda e, job="", **k: seen.append((job, str(e))))
    d = f"{ISO}T00:00:00"
    lines = [{"rid": "L1", "atom": 1, "ticket_rid": "T1", "date": d, "menuitem_mid": "M1", "slstype_mid": "S",
              "qty": 1, "sales": 40.0},
             {"rid": "L2", "atom": 2, "ticket_rid": "T1", "date": d, "menuitem_mid": "M1", "slstype_mid": "NEW",
              "qty": 1, "sales": 12.0}]                                   # a sales type the list doesn't name
    _stub(monkeypatch, _routes(ISO, lines=lines, tickets=[{"rid": "T1", "date": d, "type_sale": 52.0}]))
    out = pos_archive.archive_day(rid, DAY)
    assert out["unknown_sales"] == 12.0 and out["recon_gap"] == -12.0
    assert any(job == "pos_reconcile" for job, _m in seen)
    row = _q(db_path, "SELECT recon_gap, unknown_sales FROM pos_archive_days WHERE restaurant_id=?", (rid,))[0]
    assert (row["recon_gap"], row["unknown_sales"]) == (-12.0, 12.0)


def test_pos_11_a_night_that_reconciles_raises_nothing(db_path, monkeypatch):
    import ops
    rid = _rid(db_path)
    seen = []
    monkeypatch.setattr(ops, "capture", lambda e, job="", **k: seen.append(job))
    d = f"{ISO}T00:00:00"
    lines = [{"rid": "L1", "atom": 1, "ticket_rid": "T1", "date": d, "menuitem_mid": "M1", "slstype_mid": "S",
              "qty": 1, "sales": 40.0},
             {"rid": "L2", "atom": 2, "ticket_rid": "T1", "date": d, "slstype_mid": "P", "qty": 1, "sales": -5.0}]
    _stub(monkeypatch, _routes(ISO, lines=lines, tickets=[{"rid": "T1", "date": d, "type_sale": 40.0,
                                                           "type_promo": -5.0}]))
    out = pos_archive.archive_day(rid, DAY)
    assert out["recon_gap"] == 0.0 and out["unknown_sales"] == 0.0 and "pos_reconcile" not in seen


# ── POS-12: a night without its modifier links is archived again ────────────

def test_pos_12_a_failed_ticketitem_read_leaves_the_day_for_the_backfill(db_path, monkeypatch):
    rid = _rid(db_path)
    _stub(monkeypatch, _routes(ISO, **{"ticketitem/getbybusinessdate": rpower.RPowerError("HTTP 500")}))
    out = pos_archive.archive_day(rid, DAY)
    assert out["links_ok"] == 0
    assert ISO not in pos_archive.archived_dates(rid, "rpower")
    _stub(monkeypatch, _routes(ISO))
    assert pos_archive.archive_day(rid, DAY)["links_ok"] == 1 and ISO in pos_archive.archived_dates(rid, "rpower")


# ── POS-13: no live RPOWER read on a web request ────────────────────────────

def test_pos_13_a_page_load_reads_the_archive_only(db_path, monkeypatch):
    from flask import Flask
    rid = _rid(db_path)
    prev = DAY - timedelta(days=1)
    calls = _stub(monkeypatch, _routes(prev.isoformat()))
    pos_archive.archive_day(rid, prev)                          # the store's archive has started
    calls.clear()
    with Flask("t").test_request_context("/"):
        with pytest.raises(rpower.RPowerArchiveMiss):
            rpower.fetch_business_days(rid, DAY, DAY)
        with pytest.raises(pos.POSCapabilityError):
            pos.fetch_business_days(rid, DAY, DAY)
        assert calls == [] or set(calls) <= {"salestype/getbycg"}, calls
        # the day it holds is served from the archive
        assert rpower.fetch_business_days(rid, prev, prev) == {prev.isoformat(): 45.0}


def test_pos_13_retry_after_is_capped_and_never_waited_on_a_request_thread(monkeypatch):
    import requests
    from flask import Flask
    slept = []

    class R:
        status_code, ok, text = 429, False, ""
        headers = {"Retry-After": "3600"}
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    monkeypatch.setattr(rpower.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(rpower, "_sleep_for_spacing", lambda token="": None)
    # A Retry-After past the cap is no longer cut short and retried early
    # (scale audit 10/19/26 A3-10): the call raises at once with the wait,
    # and a queue row is rescheduled by it.
    with pytest.raises(rpower.RPowerRateLimited) as got:
        rpower._request("tok", "ticket/getbybusinessdate", {})
    assert got.value.retry_after == 3600 and all(s <= rpower.RETRY_AFTER_MAX_SECONDS for s in slept)
    slept.clear()
    with Flask("t").test_request_context("/"):
        with pytest.raises(rpower.RPowerError):
            rpower._request("tok", "ticket/getbybusinessdate", {})
    assert slept == []


# ── POS-16: yesterday's close, and an unchecked change read ─────────────────

def test_pos_16_yesterday_is_not_archived_on_demand_before_the_close(db_path, monkeypatch):
    rid = _rid(db_path)
    today = pos_archive._business_today(rid)
    yday = today - timedelta(days=1)
    calls = _stub(monkeypatch, _routes(yday.isoformat()))
    pos_archive.archive_day(rid, DAY)                            # the archive has started
    calls.clear()
    assert pos_archive.ready_dates(rid, "rpower", [yday]) == set()
    assert "closeday/getbybusinessdate" in calls and "ticketsales/getbybusinessdate" not in calls


def test_pos_16_a_failed_change_read_does_not_serve_recent_days_as_fresh(db_path, monkeypatch):
    rid = _rid(db_path)
    recent = pos_archive._business_today(rid) - timedelta(days=2)
    _stub(monkeypatch, _routes(recent.isoformat()))
    pos_archive.archive_day(rid, recent)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE pos_archive_days SET archived_at=datetime('now','-2 hours')")
    conn.commit()
    conn.close()
    # The archive asks for the dates with their newest write stamps (R3-02).
    monkeypatch.setattr(rpower, "changed_business_stamps", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(pos_archive, "archive_day", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert recent.isoformat() not in pos_archive.ready_dates(rid, "rpower", [recent])


# ── POS-17 / POS-18 / FC-CALC-24: the restaurant's own date ─────────────────

def test_pos_18_remakes_end_on_the_restaurants_last_complete_night(db_path, monkeypatch):
    import pos_food_waste as pfw
    rid = _rid(db_path)
    monkeypatch.setattr(pos, "local_complete_through", lambda r: date(2026, 10, 15))
    monkeypatch.setattr(pfw, "_plate_costs", lambda r, db_path=None: {})
    out = pfw.remakes(rid, days=7, db_path=db_path)
    assert out["window"] == {"from": "2026-10-09", "to": "2026-10-15", "days": 7}
    src = open(os.path.join(ROOT, "hosted_dashboard.py"), encoding="utf-8").read()
    assert "_remakes(restaurant.id, end=_lct(restaurant.id))" in src


def test_pos_18_no_server_date_defaults_in_the_pos_lens():
    import inspect
    import pos_food_waste
    for fn in (rpower.build_shifts_csv, rpower.department_sold, pos_food_waste.remakes, pos._reopen_moved_nights,
               pos_archive._levels_in_use):
        assert "date.today()" not in inspect.getsource(fn), fn.__name__


def test_pos_17_local_complete_through_is_complete_through(db_path, monkeypatch):
    rid = _rid(db_path)
    _freeze(monkeypatch, datetime(2026, 10, 16, 3, 0))           # 3am: last night is still trading
    assert pos.local_complete_through(rid) == date(2026, 10, 14)
    _freeze(monkeypatch, datetime(2026, 10, 16, 6, 0))
    assert pos.local_complete_through(rid) == date(2026, 10, 15)


# ── POS-19: a ticket rewritten with no sale line touched ────────────────────

def test_pos_19_a_ticket_only_change_is_a_restatement(db_path, monkeypatch):
    rid = _rid(db_path)
    stamp = "2026-10-12T15:00:00"
    _stub(monkeypatch, _routes(ISO, **{"ticket/getbytimestamp": [{"rid": "T9", "date": "2026-10-10T00:00:00",
                                                                  "time_stamp": stamp}]}))
    out = rpower.changed_business_dates(rid, datetime(2026, 10, 12, 3, tzinfo=timezone.utc),
                                        datetime(2026, 10, 12, 20, tzinfo=timezone.utc))
    assert out == {"2026-10-10"}


# ── POS-20: a re-read night keeps its archived filing ───────────────────────

def test_pos_20_a_reread_night_keeps_the_department_it_was_archived_under(db_path, monkeypatch):
    rid = _rid(db_path)
    routes = _routes(ISO)
    _stub(monkeypatch, routes)
    pos_archive.archive_day(rid, DAY)
    # The store moves the burger to Catering after the night was archived.
    routes["salescategory/getbycg"] = [{"mid": "K1", "name": "Entrees", "slsdep_mid": "DP2"}]
    rpower.clear_caches()
    out = rpower.fetch_day_sales(rid, DAY)
    assert set(out["by_department"]) == {"Food"}, out["by_department"]


# ── POS-21: Sync now cooldown ───────────────────────────────────────────────

def test_pos_21_sync_now_runs_once_per_cooldown(db_path, monkeypatch):
    import ops
    import rpower_routes
    import scheduler
    rid = _rid(db_path)
    started = []
    monkeypatch.setattr(scheduler, "start_manual_pos_sync", lambda r, who: (started.append(r), ("job", False))[1])
    stamps = {}

    def claim(key, minutes):
        if key in stamps:
            return False
        stamps[key] = minutes
        return True
    monkeypatch.setattr(ops, "claim_cooldown", claim)
    user = {"restaurant_id": rid, "username": "owner"}
    assert rpower_routes.do_sync(user)[1] == 200
    body, status = rpower_routes.do_sync(user)
    assert status == 429 and body["cooldown"] and started == [rid]
    # One cooldown for every Sync now (scale audit 10/19/26 A3-09).
    assert stamps == {f"pos_sync_now:{rid}": rpower_routes.SYNC_NOW_COOLDOWN_MINUTES}


# ── SCALE-07: the heavy sweeps run as queue rows ────────────────────────────

def test_scale_07_the_diagnoses_fan_out_and_run_nothing_inline(db_path, monkeypatch):
    import food_cost_intelligence as fci
    import job_queue
    import scheduler
    import strategy_jobs
    ids = [_rid(db_path, rid=i, module_inventory=1, billing_status="active") for i in range(1, 6)]
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)     # the test runs the row itself
    ran = []
    monkeypatch.setattr(fci, "diagnose", lambda rid: ran.append(rid) or {"ok": True})
    monkeypatch.setattr(scheduler, "_collect_before_cancelling", lambda job: None)
    out = scheduler.food_cost_diagnoses_pass(day=date(2026, 10, 16))
    assert out["queued"] == 5 and ran == []
    assert scheduler.food_cost_diagnoses_pass(day=date(2026, 10, 16))["queued"] == 0, "once a day"
    rows = _q(db_path, "SELECT payload_json FROM jobs WHERE kind='learner'")
    assert len(rows) == 5 and job_queue.KINDS["learner"][0] == "bulk"
    # The briefs wait for the rows.
    assert strategy_jobs.open_rows("food_cost_diagnosis", "2026-10-16") == 5

    class Ops:
        @staticmethod
        def settled(job, day):
            return True
    six = datetime(2026, 10, 16, 6, 30)
    assert scheduler._morning_input_pending(Ops, six, scheduler.DIGEST_INPUTS) == "food_cost_diagnoses"
    # A row runs the one restaurant's diagnosis.
    monkeypatch.setattr(scheduler, "_ai_budget_spent", lambda r: False)
    monkeypatch.setattr(scheduler, "_cancel_open_batch_items", lambda wf, r: None)
    job = type("J", (), {"payload": {"learner": "food_cost_diagnosis", "restaurant_id": ids[0]}})
    assert strategy_jobs.run_queued_learner(job) == {"diagnosed": 1} and ran == [ids[0]]
    conn = models.get_conn(db_path)
    conn.execute("UPDATE jobs SET state='done'")
    conn.commit()
    conn.close()
    assert scheduler._morning_input_pending(Ops, six, scheduler.DIGEST_INPUTS) is None


def test_scale_07_the_loop_queues_the_diagnoses_and_the_price_push():
    import inspect
    import scheduler
    loop = inspect.getsource(scheduler.scheduler_loop)
    assert '_ops.run_in_lane("sweep", "food_cost_diagnoses", food_cost_diagnoses_pass)' in loop
    assert '_ops.run_job("bar_price_push", bar_price_push_pass)' in loop
    assert "run_food_cost_diagnoses)" not in loop and "import run_price_push" not in loop


def test_scale_07_the_price_push_pass_is_the_one_queued_fan_out(db_path, monkeypatch):
    """D and C1 each built a fan-out; the loop's pass is bar_signals.run_price_push
    (C1's), so one Monday queues one `bar_price_push` row per restaurant."""
    import bar_signals
    import strategy_jobs
    seen = []
    monkeypatch.setattr(bar_signals, "run_price_push", lambda db_path=None, now=None: seen.append(now) or {"queued": 1})
    monday = datetime(2026, 10, 19, 9, 15)
    assert strategy_jobs.bar_price_push_pass(now=monday) == {"queued": 1} and seen == [monday]


# ── SCALE-06: the dashboard computes the Back Office food cards only for a
#    login that can see Food Cost; the plate costs are kept on their inputs ──

def test_scale_06_plate_costs_are_kept_until_their_inputs_change(db_path, monkeypatch):
    import backoffice_recipes
    import pos_food_waste as pfw
    rid = _rid(db_path)
    pfw._PLATE_CACHE.clear()
    built = []
    monkeypatch.setattr(backoffice_recipes, "plates", lambda r, today=None, db_path=None: built.append(today) or {
        "plates": [{"complete": True, "cost": 4.25, "pos_names": ["Burger"]}]})
    assert pfw._plate_costs(rid) == {"burger": 4.25}
    assert pfw._plate_costs(rid) == {"burger": 4.25} and len(built) == 1
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO bo_recipe_books (restaurant_id, payload, source, loaded_at) VALUES (?,?,?,?)",
                 (rid, "{}", "t", "2026-10-16T10:00:00"))
    conn.commit()
    conn.close()
    pfw._plate_costs(rid)
    assert len(built) == 2, "a new recipe book prices the plates again"


def test_scale_11_a_failed_plates_read_reaches_the_console(db_path, monkeypatch):
    import backoffice_recipes
    import ops
    import pos_food_waste as pfw
    rid = _rid(db_path)
    pfw._PLATE_CACHE.clear()
    seen = []
    monkeypatch.setattr(ops, "capture", lambda e, job="", **k: seen.append(job))
    monkeypatch.setattr(backoffice_recipes, "plates", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert pfw._plate_costs(rid) == {} and seen == ["pos_food_waste"]


SCRIPT = r'''
import os, sqlite3, sys
vol = sys.argv[1]
sqlite3.connect(os.path.join(vol, "reviews.db")).close()
os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", ANTHROPIC_API_KEY="",
                  RESEND_API_KEY="", TWILIO_AUTH_TOKEN="", SECRET_KEY="test-secret",
                  CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0")
import dotenv; dotenv.load_dotenv = lambda *a, **k: False  # never production's .env (audit #25)
import hosted_dashboard as h
import auth, models, re
import backoffice_ap, backoffice_recipes, pos_food_waste
from models import Restaurant
called = []
backoffice_ap.feed_state = lambda *a, **k: called.append("feed_state") or {"on_file": True, "files": 1}
backoffice_recipes.plates = lambda *a, **k: called.append("plates") or {}
pos_food_waste.remakes = lambda *a, **k: called.append("remakes") or {"available": False}
rid = models.create_restaurant(Restaurant(name="BO Co", owner_email="bo@x.test", module_reviews=1,
                                          module_inventory=1))
auth.create_user(rid, "mgr", "mgr@x.test", "correct-horse-battery", role="manager")
c = h.app.test_client()
page = c.get("/login").get_data(as_text=True)
token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
r = c.post("/login", data={"username": "mgr", "password": "correct-horse-battery", "csrf_token": token})
assert r.status_code in (302, 303), r.status_code
r = c.get("/")
print("HOME", r.status_code, "CALLED", sorted(set(called)))
'''


def test_scale_06_a_login_without_food_cost_never_computes_its_cards():
    vol = tempfile.mkdtemp(prefix="cavnar-fcbar-d-")
    out = subprocess.run([sys.executable, "-c", SCRIPT, vol], cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout[-1500:] + "\n" + out.stderr[-2500:]
    assert "HOME 200 CALLED []" in out.stdout, out.stdout[-500:]


def test_rpowers_cash_discount_item_left_out_of_its_ticket_totals_is_explained_not_a_gap(db_path, monkeypatch):
    # Owner, 10/19/26: EJ's "Cash Discount" item (-$7.98, of which RPOWER's ticket total took in -$3.99; a
    # -$14.50 one on a ticket of its own) paged as a reconcile gap on every re-read. Our net keeps the line;
    # the shortfall it explains is counted on its own, and an unexplained gap still raises once a night.
    import ops
    rid = _rid(db_path)
    seen = []
    monkeypatch.setattr(ops, "capture", lambda e, job="", **k: seen.append(job))
    routes = _routes(ISO)
    routes["menuitem/getbycg"] = routes["menuitem/getbycg"] + [{"mid": "CD", "name": "Cash Discount", "slscat_mid": "K1"}]
    d = f"{ISO}T00:00:00"
    lines = [{"rid": "L1", "atom": 1, "ticket_rid": "T1", "date": d, "menuitem_mid": "M1", "slstype_mid": "S",
              "qty": 1, "sales": 457.63},
             {"rid": "L2", "atom": 2, "ticket_rid": "T1", "date": d, "menuitem_mid": "CD", "slstype_mid": "S",
              "qty": 2, "sales": -7.98}]
    routes.update(_routes(ISO, lines=lines, tickets=[{"rid": "T1", "date": d, "type_sale": 453.64}]))
    routes["menuitem/getbycg"] = routes["menuitem/getbycg"] + [{"mid": "CD", "name": "Cash Discount", "slscat_mid": "K1"}]
    _stub(monkeypatch, routes)
    out = pos_archive.archive_day(rid, DAY)
    assert out["recon_gap"] == 0.0 and "pos_reconcile" not in seen


def test_a_reconcile_finding_is_raised_once_per_night_not_on_every_re_read(db_path, monkeypatch):
    import ops
    rid = _rid(db_path)
    seen = []
    monkeypatch.setattr(ops, "capture", lambda e, job="", **k: seen.append(job))
    checks = {"gap": -12.0, "unknown_type_sales": 0.0, "unknown_type_lines": 0, "lines_by_type": {}, "tickets_by_type": {}}
    for _ in range(3):
        pos_archive._reconcile(rid, ISO, checks)
    assert seen.count("pos_reconcile") == 1
