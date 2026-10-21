"""Blind re-audit 10/20/26, refix G3 (RPOWER / POS): live reads ahead of
bulk work and a bucket lock with a time limit (R3-01 / R3-07 / R3-08); a
page overlap read again and never stored short (R3-04, in test_rpower); an
empty or short re-read never replaces a stored night (R3-05); an open punch
is on the clock and a new hire never blanks live labor (R3-06); one punch
boundary, the schedule's (R3-10 / R9-09); a closed day settles (R3-14); a
night read once (R3-02) and its re-check not repeated (R3-03); the claim
kept while a slow read runs (R3-15); orders due kept per store (R3-16); the
loss window on the restaurant's own date (R3-09); the 3am sync at each
store's own hour (R3-12); catalogs and prices written only where they moved
(R3-11 / R3-18); a provider's "wait" spends no try (R3-13); the showcase's
simulated archive kept to its window (R3-17)."""
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

import models
import ops
import pos
import pos_archive
import pos_io
import rpower


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos_archive, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(pos, "PROVIDERS", None)
    pos_archive._change_memo.clear()
    pos_io.reset_for_tests()
    yield
    pos_io.reset_for_tests()


def _restaurant(rid=1, **kw):
    conn = models.get_conn()
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test", "rpower_token": "tok",
            "rpower_cg": 1280, "rpower_store_mid": f"store-{rid}", "timezone": "America/Chicago"}
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


# ── R3-01 / R3-07 / R3-08: the request budget ───────────────────────────────

def test_a_background_request_never_takes_a_slot_while_a_live_one_waits(monkeypatch):
    monkeypatch.setattr(rpower, "_take_shared_slot", lambda t, s, key=None: 0.0)
    key = "rpower:test-bucket"
    rpower._priority_waiting[key] = 1                      # a live read is waiting on this bucket
    try:
        with pytest.raises(rpower.RPowerBudgetExceeded):
            rpower._wait_for_bucket("tok", key, 0.01, 0.2, lane="nightly")
        rpower._wait_for_bucket("tok", key, 0.01, 0.2, lane="live")      # the live read goes
    finally:
        rpower._priority_waiting.pop(key, None)


def test_bulk_work_always_passes_the_background_share_and_nightly_work_only_beside_live_reads(monkeypatch):
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0.001)
    monkeypatch.setattr(rpower, "FLEET_RPS", 0)
    taken = []
    monkeypatch.setattr(rpower, "_take_shared_slot", lambda t, s, key=None: (taken.append((key, s)), 0.0)[1])
    key = rpower._bucket_key("tok")
    with pos_io.lane("bulk"):
        rpower._sleep_for_spacing("tok")
    # RPOWER audit #23 (A4-12 / A5-01): lanes BORROW an idle share — bulk
    # alone on the bucket has all of it; beside live reads it keeps to its
    # share of the window (pos_capacity.background_share).
    assert [k for k, _ in taken] == [key], "nothing above bulk is active: it borrows the whole token"
    taken.clear()
    with pos_io.lane("nightly"):
        rpower._sleep_for_spacing("tok")
    assert [k for k, _ in taken] == [key], "no live reads: the nightly work has the whole token"
    with pos_io.lane("live"):
        rpower._sleep_for_spacing("tok")              # marks the bucket live, for every process
    taken.clear()
    rpower._live_seen.clear()
    with pos_io.lane("nightly"):
        rpower._sleep_for_spacing("tok")
    assert [k for k, _ in taken] == [key + ":bg:nightly", key], "beside live reads the nightly work keeps to its share"
    taken.clear()
    with pos_io.lane("bulk"):
        rpower._sleep_for_spacing("tok")
    import pos_capacity
    assert (key + ":bg:bulk", 0.001 / pos_capacity.share("bulk")) in taken, "beside live reads bulk keeps to its share"


def test_a_request_thread_never_waits_past_its_limit_behind_a_held_bucket_lock(monkeypatch):
    key = "rpower:held"
    lock = rpower._bucket_lock(key)
    lock.acquire()
    try:
        t0 = time.monotonic()
        with pytest.raises(rpower.RPowerBudgetExceeded):
            rpower._wait_for_bucket("tok", key, 0.01, 0.3, lane="live")
        assert time.monotonic() - t0 < 2.0, "the lock is taken with the request's own time limit"
    finally:
        lock.release()


def test_the_fleet_ceiling_stands_when_the_per_token_spacing_is_off(monkeypatch):
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0)
    monkeypatch.setattr(rpower, "FLEET_RPS", 10.0)
    taken = []
    monkeypatch.setattr(rpower, "_take_shared_slot", lambda t, s, key=None: (taken.append(key), 0.0)[1])
    rpower._sleep_for_spacing("tok")
    assert rpower.FLEET_BUCKET in taken


def test_a_read_during_service_is_the_live_lane(monkeypatch):
    seen = []
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("rpower", type("M", (), {
        "fetch_sales_today": staticmethod(lambda r, d: seen.append(pos_io.current_lane()) or 12.0)})))
    _restaurant()
    assert pos.fetch_sales_today(1, date(2026, 10, 20))[0] == 12.0
    assert seen == ["live"] and pos_io.current_lane() == "nightly"


# ── R3-06: an open punch, a new hire ────────────────────────────────────────

_OPEN = "2000-01-01T00:00:00"


def _punch(mid, start="2026-10-20T10:00:00", out=_OPEN, **kw):
    return dict({"rid": f"p{mid}", "emp_mid": mid, "job_mid": "J1", "in_dttm": start, "out_dttm": out,
                 "reg_rate": 15.0}, **kw)


def test_an_open_punch_is_on_the_clock_not_a_finished_one():
    people = {"employees": {"1": "Ana"}}
    rpower._require_resolved([_punch("1"), _punch("9")], people)     # only open punches: nothing refused
    with pytest.raises(rpower.RPowerUnresolved):
        rpower._require_resolved([_punch("9", out="2026-10-20T15:00:00")], people)


def test_a_new_hires_punch_reads_the_people_list_afresh_and_never_blanks_live_labor(monkeypatch):
    _restaurant()
    emps = [{"mid": str(i), "fname": f"P{i}", "lname": "X", "payroll_id": f"Y{i}"} for i in range(1, 10)]
    calls = []
    routes = {"job/getbycg": [{"mid": "J1", "name": "Server"}], "employee/getbycg": lambda: list(emps)}

    def fake(token, path, params=None):
        calls.append(path)
        return routes[path]() if callable(routes[path]) else routes[path]
    monkeypatch.setattr(rpower, "_request", fake)
    rpower.fetch_people(1)                                   # kept before the hire
    emps.append({"mid": "10", "fname": "New", "lname": "Hire", "payroll_id": "Y10"})
    punches = [_punch(str(i)) for i in range(1, 11)]
    monkeypatch.setattr(rpower, "fetch_time_entries", lambda r, s, e: punches)
    rpower.clear_caches()
    calls.clear()
    rows = rpower.fetch_clock_ins_today(1, date(2026, 10, 20))
    assert len(rows) == 10 and "New Hire" in {r["employee"] for r in rows}
    assert calls.count("employee/getbycg") == 1, "the list was read afresh once, for the hire"
    # Still unknown after the fresh read: counted, never a refused read.
    punches.append(_punch("77"))
    rows = rpower.fetch_clock_ins_today(1, date(2026, 10, 20))
    unknown = [r for r in rows if r.get("unknown")]
    assert len(rows) == 11 and unknown and unknown[0]["employee"].startswith("Unknown employee")
    assert calls.count("employee/getbycg") == 1, "not read afresh again within PEOPLE_REFRESH_SECONDS"


# ── R3-10 / R9-09: one punch boundary ───────────────────────────────────────

def test_a_punch_is_filed_by_the_schedules_own_4am_boundary():
    assert rpower.punch_business_date(datetime(2026, 10, 20, 4, 30)) == date(2026, 10, 20)
    assert rpower.punch_business_date(datetime(2026, 10, 20, 3, 59)) == date(2026, 10, 19)
    assert rpower.punch_business_date(datetime(2026, 10, 20, 0, 10)) == date(2026, 10, 19)


def test_a_baker_at_430am_is_this_mornings_clock_in(monkeypatch):
    _restaurant()
    monkeypatch.setattr(rpower, "fetch_time_entries", lambda r, s, e: [
        _punch("1", start="2026-10-20T04:30:00"), _punch("2", start="2026-10-21T03:30:00")])
    monkeypatch.setattr(rpower, "_people_for", lambda r, e: {"employees": {"1": "Baker", "2": "Closer"},
                                                             "jobs": {"J1": "Prep"}})
    rows = rpower.fetch_clock_ins_today(1, date(2026, 10, 20))
    assert {r["employee"] for r in rows} == {"Baker", "Closer"}


def test_an_after_midnight_job_change_is_the_night_befores_labor():
    rows = rpower.normalise_entries([_punch("1", start="2026-10-17T00:10:00", out="2026-10-17T02:30:00",
                                            reg_hours=2.33)], {}, {"employees": {"1": "Ana"}})
    assert rows[0]["date"] == "2026-10-16" and rows[0]["day"] == "Friday"


# ── R3-14: a closed day settles ─────────────────────────────────────────────

def _archived_day(rid, iso, punches=0, max_stamp="2026-10-01T03:00:00", tickets=1, lines=1, net=10.0,
                  archived_at=None):
    conn = models.get_conn()
    conn.execute("INSERT INTO pos_archive_days (restaurant_id, provider, business_date, tickets, lines, punches, "
                 "net_sales, max_stamp, version, links_ok, archived_at) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                 (rid, "rpower", iso, tickets, lines, punches, net, max_stamp, pos_archive.ARCHIVE_VERSION,
                  archived_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()


def test_an_empty_archived_day_read_empty_is_stamped_read_and_a_stored_one_is_never_wiped():
    _restaurant()
    _archived_day(1, "2026-10-05")                      # closed: no punches
    _archived_day(1, "2026-10-06", punches=1)
    conn = models.get_conn()
    conn.execute("INSERT INTO pos_punches (restaurant_id, provider, punch_id, business_date) "
                 "VALUES (1, 'rpower', 'k1', '2026-10-06')")
    conn.execute("UPDATE pos_archive_days SET punches_read_at=NULL")
    conn.commit()
    conn.close()
    out = pos_archive.refresh_punches(1, "rpower", {"2026-10-05": [], "2026-10-06": []})
    got = {r["business_date"]: r for r in _q("SELECT business_date, punches_read_at, punches FROM pos_archive_days")}
    assert got["2026-10-05"]["punches_read_at"], "the closed day settles"
    assert not got["2026-10-06"]["punches_read_at"] and got["2026-10-06"]["punches"] == 1, "never wiped"
    assert _q("SELECT COUNT(*) n FROM pos_punches")[0]["n"] == 1 and out["checked"] == 1


# ── R3-05: a stored night is never replaced short ───────────────────────────

class _Prov:
    def __init__(self, tickets=3, lines=6, stamp="2026-10-01T03:00:00", gap=0.0):
        self.tickets, self.lines, self.stamp, self.gap, self.calls, self.changed = tickets, lines, stamp, gap, [], {}

    def archive_rows(self, rid, day):
        iso = day.isoformat()
        self.calls.append(iso)
        tickets = [{"ticket_id": f"t{i}", "business_date": iso, "net_sales": 10.0} for i in range(self.tickets)]
        lines = [{"line_id": f"l{i}", "ticket_id": "t0", "business_date": iso, "kind": "sale", "qty": 1,
                  "sales": 5.0, "item_id": "M1"} for i in range(self.lines)]
        return {"tickets": tickets, "lines": lines, "punches": [], "payments": [], "payouts": [],
                "max_stamp": self.stamp, "links_ok": True, "checks": {"gap": self.gap, "unknown_type_sales": 0}}

    def changed_business_stamps(self, rid, since, until):
        return dict(self.changed)


def test_an_empty_re_read_keeps_the_stored_night_and_says_so(monkeypatch):
    _restaurant()
    prov = _Prov()
    day = date(2026, 10, 10)
    pos_archive.archive_day(1, day, provider=("rpower", prov))
    prov.tickets = prov.lines = 0
    with pytest.raises(pos_archive.ArchiveShrunk):
        pos_archive.archive_day(1, day, provider=("rpower", prov))
    row = _q("SELECT tickets, lines, net_sales, suspect FROM pos_archive_days")[0]
    assert (row["tickets"], row["lines"], row["net_sales"]) == (3, 6, 30.0), "the stored night stands"
    assert json.loads(row["suspect"])["lines"] == 0
    assert _q("SELECT COUNT(*) n FROM pos_ticket_lines")[0]["n"] == 6


def test_a_short_re_read_is_taken_only_when_it_comes_back_the_same_later_and_reconciles(monkeypatch):
    _restaurant()
    prov = _Prov(tickets=40, lines=200)
    day = date(2026, 10, 10)
    pos_archive.archive_day(1, day, provider=("rpower", prov))
    prov.tickets, prov.lines = 30, 150
    with pytest.raises(pos_archive.ArchiveShrunk):
        pos_archive.archive_day(1, day, provider=("rpower", prov))
    with pytest.raises(pos_archive.ArchiveShrunk):             # the same short read, at once: still kept
        pos_archive.archive_day(1, day, provider=("rpower", prov))
    conn = models.get_conn()
    s = json.loads(conn.execute("SELECT suspect FROM pos_archive_days").fetchone()[0])
    s["at"] = (datetime.now(timezone.utc) - timedelta(minutes=pos_archive.SHRINK_CONFIRM_MINUTES + 1)
               ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE pos_archive_days SET suspect=?", (json.dumps(s),))
    conn.commit()
    conn.close()
    prov.gap = 25.0                                            # it does not reconcile: still kept
    with pytest.raises(pos_archive.ArchiveShrunk):
        pos_archive.archive_day(1, day, provider=("rpower", prov))
    prov.gap = 0.0
    out = pos_archive.archive_day(1, day, provider=("rpower", prov))
    assert out["tickets"] == 30 and _q("SELECT suspect FROM pos_archive_days")[0]["suspect"] is None


def test_a_small_drop_is_a_restatement_not_a_short_read():
    _restaurant()
    prov = _Prov(tickets=40, lines=200)
    day = date(2026, 10, 10)
    pos_archive.archive_day(1, day, provider=("rpower", prov))
    prov.tickets, prov.lines = 39, 198                         # one ticket re-dated at the store
    assert pos_archive.archive_day(1, day, provider=("rpower", prov))["lines"] == 198


def test_a_reader_is_answered_from_the_stored_night_when_the_re_read_is_short(monkeypatch):
    _restaurant()
    prov = _Prov()
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", prov))
    monkeypatch.setattr(pos_archive, "_business_today", lambda r: date(2026, 10, 12))
    day = date(2026, 10, 10)
    pos_archive.archive_day(1, day, provider=("rpower", prov))
    conn = models.get_conn()
    conn.execute("UPDATE pos_archive_days SET archived_at=datetime('now', '-2 hours')")
    conn.commit()
    conn.close()
    prov.changed = {day.isoformat(): "2026-10-11 09:00:00"}     # a write after the stored copy
    prov.tickets = prov.lines = 0
    assert pos_archive.ready_dates(1, "rpower", [day]) == {day.isoformat()}
    assert _q("SELECT lines FROM pos_archive_days")[0]["lines"] == 6


def test_a_night_still_filling_is_not_settled_and_the_report_waits(monkeypatch):
    _restaurant()
    now = datetime.now(timezone.utc)
    _archived_day(1, "2026-10-19", max_stamp=(now - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%S"),
                  archived_at=now.strftime("%Y-%m-%d %H:%M:%S"))
    settled, why = pos_archive.night_settled(1, "rpower", "2026-10-19")
    assert not settled and "still writing" in why
    conn = models.get_conn()
    conn.execute("UPDATE pos_archive_days SET checked_at=?",
                 ((now + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S"),))
    conn.commit()
    conn.close()
    assert pos_archive.night_settled(1, "rpower", "2026-10-19")[0], "a later check found nothing new"


def test_the_sales_block_waits_on_an_unsettled_night(monkeypatch):
    import dsr
    from dsr import block_sales
    _restaurant()
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("rpower", object()))
    monkeypatch.setattr(pos, "fetch_day_sales", lambda rid, d: ({"transactions": 5, "gross": 100.0}, "rpower"))
    monkeypatch.setattr(pos_archive, "night_settled", lambda *a, **k: (False, "still writing"))
    ctx = dsr.Context(models.get_restaurant(1), date(2026, 10, 19))
    ctx.day_closed = "pos"
    blk = block_sales.collect(ctx)
    assert blk["status"] == dsr.AWAITING and blk["detail"]["waiting_for"] == "tickets_settling"


# ── R3-02 / R3-03: a night read once, its re-check not repeated ─────────────

def test_last_night_named_by_the_change_read_is_not_read_again_unless_written_after_it(monkeypatch):
    _restaurant()
    prov = _Prov(stamp="2026-10-19T04:00:00")
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", prov))
    monkeypatch.setattr(pos_archive, "sync_catalog", lambda *a, **k: None)
    monkeypatch.setattr(pos_archive, "sync_roles", lambda *a, **k: None)
    monkeypatch.setattr(pos_archive, "BACKFILL_DAYS", 1)
    today = date(2026, 10, 20)
    y = today - timedelta(days=1)
    pos_archive.archive_day(1, y, provider=("rpower", prov))         # the report's read after the close
    prov.calls.clear()
    prov.changed = {y.isoformat(): "2026-10-19 04:00:00"}           # its own lines, nothing newer
    out = pos_archive.run_for(1, today=today, now_utc=datetime.now(timezone.utc))
    assert prov.calls == [] and out["unchanged"] == [y.isoformat()]
    pos_archive._change_memo.clear()
    conn = models.get_conn()
    conn.execute("UPDATE pos_archive_state SET change_read_at=NULL")
    conn.commit()
    conn.close()
    prov.changed = {y.isoformat(): "2026-10-19 05:30:00"}           # a tip adjusted after the read
    pos_archive.run_for(1, today=today, now_utc=datetime.now(timezone.utc) + timedelta(seconds=5))
    assert prov.calls == [y.isoformat()]


def test_the_report_takes_the_stored_night_without_reading_it_again():
    _restaurant()
    prov = _Prov()
    day = date(2026, 10, 10)
    pos_archive.archive_day(1, day, provider=("rpower", prov))
    prov.calls.clear()
    out = pos_archive.ensure_day(1, day, provider=("rpower", prov))
    assert out["stored"] and out["lines"] == 6 and prov.calls == []


def test_a_recheck_that_finds_nothing_is_not_repeated_by_the_next_reader(monkeypatch):
    _restaurant()
    prov = _Prov()
    reads = []
    prov.changed_business_stamps = lambda rid, since, until: reads.append(since) or {}
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", prov))
    monkeypatch.setattr(pos_archive, "_business_today", lambda r: date(2026, 10, 12))
    day = date(2026, 10, 10)
    _archived_day(1, day.isoformat(), archived_at=(datetime.now(timezone.utc) - timedelta(hours=2)
                                                   ).strftime("%Y-%m-%d %H:%M:%S"))
    assert pos_archive.ready_dates(1, "rpower", [day]) == {day.isoformat()} and len(reads) == 1
    assert _q("SELECT checked_at FROM pos_archive_days")[0]["checked_at"]
    pos_archive._change_memo.clear()
    conn = models.get_conn()
    conn.execute("UPDATE pos_archive_state SET change_read_at=NULL")
    conn.commit()
    conn.close()
    pos_archive.ready_dates(1, "rpower", [day])
    assert len(reads) == 1, "checked just now: the next reader does not ask again"


def test_the_change_read_asks_with_a_time_and_names_each_dates_newest_write(monkeypatch):
    _restaurant()
    asked = []

    def req(token, path, params=None):
        asked.append(params)
        if path == "ticketsales/getbytimestamp":
            return [{"rid": "a", "date": "2026-10-19T00:00:00", "time_stamp": "2026-10-20T04:00:00"},
                    {"rid": "b", "date": "2026-10-19T00:00:00", "time_stamp": "2026-10-20T04:20:00"}]
        return []
    monkeypatch.setattr(rpower, "_request", req)
    monkeypatch.setitem(rpower._change_datetime_refused, "at", None)
    since = datetime(2026, 10, 20, 4, 10, tzinfo=timezone.utc)
    out = rpower.changed_business_stamps(1, since, datetime(2026, 10, 20, 4, 40, tzinfo=timezone.utc))
    assert out == {"2026-10-19": "2026-10-20 04:20:00"}
    assert all("T" in p["startdate"] and "T" in p["enddate"] for p in asked)


# ── R3-15: the claim is kept while a slow read runs ────────────────────────

def test_a_slow_read_keeps_its_claim(monkeypatch):
    _restaurant()
    monkeypatch.setattr(pos_archive, "ARCHIVE_CLAIM_RENEW_SECONDS", 0.05)
    renewed = []
    monkeypatch.setattr(ops, "renew_cooldown", lambda key: renewed.append(key) or True)
    prov = _Prov()
    real = prov.archive_rows
    prov.archive_rows = lambda rid, day: (time.sleep(0.3), real(rid, day))[1]
    pos_archive.archive_day(1, date(2026, 10, 10), provider=("rpower", prov))
    assert renewed and renewed[0] == "pos_archive_day:1:rpower:2026-10-10"


def test_renew_cooldown_moves_a_held_claim():
    assert ops.claim_cooldown("g3-claim", 10)
    conn = models.get_conn()
    conn.execute("UPDATE job_period_claims SET claimed_at=datetime('now', '-9 minutes') "
                 "WHERE job_key='cooldown:g3-claim'")
    conn.commit()
    conn.close()
    assert ops.renew_cooldown("g3-claim")
    assert not ops.claim_cooldown("g3-claim", 10), "renewed: still held"


# ── R3-16 / R3-07: orders due ───────────────────────────────────────────────

def test_orders_due_are_one_row_per_store_and_an_owners_read_never_reaches_rpower(monkeypatch):
    from flask import Flask
    _restaurant()
    calls = []
    monkeypatch.setattr(rpower, "_paged", lambda t, p, params, max_pages=200: calls.append(p) or [
        {"need_dttm": "2026-10-21T12:00:00", "guest_count": 20, "ticket": 5, "items": 400.0, "is_cancelled": 0}])
    conn = models.get_conn()
    conn.execute("INSERT INTO pos_live_snapshot (restaurant_id, kind, business_date, value_json, read_at) "
                 "VALUES (1, 'orders_due:2026-10-25', '2026-10-19', '[]', 0)")
    conn.commit()
    conn.close()
    rpower.fetch_orders_due(1, date(2026, 10, 20), date(2026, 11, 3))
    rpower.fetch_orders_due(1, date(2026, 10, 20), date(2026, 11, 2))
    kinds = [r["kind"] for r in _q("SELECT kind FROM pos_live_snapshot WHERE restaurant_id=1")]
    assert kinds == ["orders_due"] and calls == ["ticket/getbyneeddatetime"]
    conn = models.get_conn()
    conn.execute("UPDATE pos_live_snapshot SET read_at=0")
    conn.commit()
    conn.close()
    with Flask("t").test_request_context("/"):
        assert rpower.fetch_orders_due(1, date(2026, 10, 20), date(2026, 10, 26))[0]["guests"] == 20
        with pytest.raises(rpower.RPowerArchiveMiss):
            rpower.fetch_orders_due(1, date(2026, 10, 20), date(2026, 12, 1))
    assert calls == ["ticket/getbyneeddatetime"], "an owner's read used the kept window at any age"


# ── R3-07: the push leaves the request thread ───────────────────────────────

def test_send_now_queues_the_push_off_the_request_thread(monkeypatch):
    import job_queue
    import pos_schedule_push
    _restaurant()
    queued = []
    monkeypatch.setattr(job_queue, "enqueue", lambda kind, payload, **k: queued.append((kind, payload)) or (1, True))
    monkeypatch.setattr(pos_schedule_push, "run_push", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    assert pos_schedule_push.send_now(1, 42)
    assert queued == [("pos_schedule_push", {"push_id": 42})]
    assert job_queue.HANDLERS["pos_schedule_push"] == "pos_schedule_push:run_queued_push"


# ── R3-09: the loss window on the restaurant's own date ─────────────────────

def test_the_loss_window_ends_on_the_restaurants_last_complete_night(monkeypatch):
    import loss_detection
    _restaurant()
    asked = []
    monkeypatch.setattr(pos, "local_complete_through", lambda rid: date(2026, 10, 18))
    monkeypatch.setattr(pos, "fetch_loss_lines", lambda rid, s, e: asked.append((s, e)) or ([], "rpower"))
    loss_detection.sync(1, days=7, db_path=models.DB_PATH)
    assert asked and asked[0][1] == date(2026, 10, 18)


# ── R3-12: the nightly sync at each store's own hour ───────────────────────

def test_a_store_still_trading_at_3am_chicago_is_synced_at_its_own_hour(monkeypatch):
    import scheduler
    _restaurant(1, timezone="America/Chicago")
    _restaurant(2, timezone="America/Los_Angeles")
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("rpower", object()))
    synced = []
    monkeypatch.setattr(pos, "sync_restaurant", lambda rid, **k: synced.append(rid) or {"ok": True})
    monkeypatch.setattr(pos, "note_sync_failure", lambda rid: None)
    hour = {1: 3, 2: 1}
    monkeypatch.setattr(pos, "sync_due_locally", lambda r, now_local=None: hour[r.id] >= pos.POS_SYNC_LOCAL_HOUR)
    pos.sync_all()
    assert synced == [1] and pos.deferred_syncs() == [2]
    hour[2] = 3                                               # 3am in Los Angeles
    # The retry pass reads each store's own clock: pinned to 3:30am, or the
    # test passed only when run at night (R3-12).
    import time_utils
    from datetime import datetime as _dt
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=True: _dt(2026, 10, 20, 3, 30))
    monkeypatch.setattr(scheduler.data_health if hasattr(scheduler, "data_health") else __import__("data_health"),
                        "due_retries", lambda kind: [])
    scheduler.run_pos_retry()
    scheduler.run_pos_retry()
    assert synced == [1, 2], "synced once, at its own hour"


# ── R3-11 / R3-18: catalogs and prices written where they moved ─────────────

def test_an_unchanged_list_moves_only_its_read_time():
    pos_io.catalog_put("rpower", "g", "menuitem/getbycg", [{"mid": "1"}])
    pos_io.catalog_put("rpower", "g", "menuitem/getbycg", [{"mid": "1"}])
    assert pos_io.catalog_get("rpower", "g", "menuitem/getbycg")[1] == [{"mid": "1"}]
    pos_io.catalog_put("rpower", "g", "menuitem/getbycg", [{"mid": "2"}])
    assert pos_io.catalog_get("rpower", "g", "menuitem/getbycg")[1] == [{"mid": "2"}]


def test_the_catalog_sync_writes_only_rows_that_changed(monkeypatch):
    _restaurant()
    cat = {"categories": [{"category_id": "K1", "name": "Beer", "is_bar": 1}],
           "items": [{"item_id": "M1", "name": "Lager", "category_id": "K1"},
                     {"item_id": "M2", "name": "Stout", "category_id": "K1"}]}
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", type("M", (), {
        "fetch_sales_catalog": staticmethod(lambda rid: cat), "archive_rows": staticmethod(lambda *a: None)})))
    assert pos_archive.sync_catalog(1)["changed"] == {"categories": 1, "items": 2, "removed": 0}
    conn = models.get_conn()
    conn.execute("UPDATE pos_menu_catalog SET seen_at='2026-01-01 00:00:00'")
    conn.commit()
    conn.close()
    cat["items"] = [{"item_id": "M1", "name": "Lager", "category_id": "K1"},
                    {"item_id": "M3", "name": "IPA", "category_id": "K1"}]
    assert pos_archive.sync_catalog(1)["changed"] == {"categories": 0, "items": 1, "removed": 1}
    rows = {r["item_id"]: r["seen_at"] for r in _q("SELECT item_id, seen_at FROM pos_menu_catalog")}
    assert set(rows) == {"M1", "M3"} and rows["M1"] == "2026-01-01 00:00:00", "an unchanged row is not rewritten"


def test_one_override_does_not_read_the_price_list_again():
    _restaurant()
    _archived_day(1, "2026-10-19")
    conn = models.get_conn()
    for item, price, regular in (("M1", 12.0, 12.0), ("M2", 3.0, 6.0)):
        conn.execute("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, business_date, item_id, kind, "
                     "price, regular_price, price_level_id) VALUES (1, 'rpower', ?, '2026-10-19', ?, 'sale', ?, ?, '1')",
                     (item, item, price, regular))
        conn.execute("INSERT INTO pos_menu_prices (restaurant_id, provider, item_id, level_id, price) "
                     "VALUES (1, 'rpower', ?, '1', 6.0)", (item,))
    conn.execute("INSERT INTO pos_archive_state (restaurant_id, provider, prices_read_at) "
                 "VALUES (1, 'rpower', datetime('now'))")
    conn.commit()
    conn.close()
    # M1 rang at a new price, M2 at a manager's override: one moved item.
    assert not pos_archive._prices_due(1, "rpower", "2026-10-19", models.DB_PATH)


# ── R3-13: a provider's "wait" spends no try ───────────────────────────────

def test_a_provider_wait_does_not_use_up_a_try_before_the_deadline():
    import job_queue
    qid, _ = job_queue.enqueue("pos_archive", {"restaurant_id": 1}, restaurant_id=1, wake=False,
                               deadline=time.time() + 22 * 3600)
    for _ in range(4):                                       # four outages: still queued
        conn = models.get_conn()
        conn.execute("UPDATE jobs SET state='running', lease_owner='w', attempts=attempts+1 WHERE id=?", (qid,))
        conn.commit()
        conn.close()
        assert job_queue.fail(qid, "w", rpower.RPowerUnavailable("open", retry_after=1800)) == "queued"
    assert _q("SELECT attempts FROM jobs WHERE id=?", (qid,))[0]["attempts"] == 0
    conn = models.get_conn()
    conn.execute("UPDATE jobs SET state='running', lease_owner='w', attempts=2 WHERE id=?", (qid,))
    conn.commit()
    conn.close()
    assert job_queue.fail(qid, "w", RuntimeError("a bug")) == "failed", "an ordinary failure still counts"


# ── R3-17: the showcase's simulated archive ────────────────────────────────

def test_the_showcases_archive_is_kept_to_its_window(monkeypatch):
    import demo_store
    from demo_store import api, build
    _restaurant()
    monkeypatch.setattr(demo_store, "is_simulated", lambda rid: True)
    today = api.business_today()
    for days in (10, build.ARCHIVE_KEEP_DAYS + 5, 400):
        _archived_day(1, (today - timedelta(days=days)).isoformat())
    assert build._prune(1) == 2
    assert [r["business_date"] for r in _q("SELECT business_date FROM pos_archive_days")] == [
        (today - timedelta(days=10)).isoformat()]
    monkeypatch.setattr(demo_store, "is_simulated", lambda rid: False)
    _archived_day(1, (today - timedelta(days=400)).isoformat())
    assert build._prune(1) == 0, "never a real store's archive"
