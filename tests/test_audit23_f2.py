"""Audit #23 fix round (10/21/26), F2 — the RPOWER archive and sync.

A first read is judged before it counts as held (A4-04 / A6-07 / A1-09); the
archive is keyed on the store it was read from (A4-05 / A6-18); a night that
will not archive backs off and is quarantined (A4-06); punches, payments and
payouts are guarded against a short re-read (A4-07); one run per restaurant
(A4-09); the change read's margin, refusal, lookback, feeds and its own rows
(A1-03 / A1-15 / A4-14 / A4-18 / A1-04); weekly verification and a doubted
change answer (A4-03); a recent gap owed without a slot (A6-03); older
layouts re-read apart from the backfill (A4-13); payments reconciled and open
punches counted (A6-06); the night's net from its lines (A2-22); re-dated
punches (A2-16 / A3-17 / A3-16); payout end date (A1-16); a multi-page gap
re-read (A1-11); a good read kept over a failed write (A4-22); the sync's
cross-process claim and local window (A4-20 / A4-15); backfill bytes (A5-08);
a nightly reader waiting for the archive (A5-12)."""
import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest

import job_queue
import models
import ops
import pos
import pos_archive
import pos_io
import rpower

PG = (os.getenv("CAVNAR_TEST_PG_URL") or "").strip()


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos_archive, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(pos, "PROVIDERS", None)
    pos_archive._change_memo.clear()
    pos_archive._WRITE_RETRY.clear()
    rpower.clear_caches()
    pos_io.reset_for_tests()
    CAPTURED.clear()
    monkeypatch.setattr(ops, "capture", lambda exc, **k: CAPTURED.append((str(exc), k.get("kind", "job"))))
    yield
    pos_io.reset_for_tests()


CAPTURED = []


def _restaurant(rid=1, **kw):
    conn = models.get_conn()
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test", "rpower_token": "tok",
            "rpower_cg": 1280, "rpower_store_mid": "1123", "timezone": "America/Chicago"}
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


def _night(iso, tickets=10, net_each=50.0, gap=0.0, unknown=0, punches=None, payments=None, payouts=None,
           stamp="2026-10-01T03:00:00"):
    ts = [{"ticket_id": f"t{i}", "business_date": iso, "net_sales": net_each, "tax": 0.0, "grat": 0.0}
          for i in range(tickets)]
    ls = [{"line_id": f"l{i}", "ticket_id": f"t{i}", "business_date": iso, "kind": "sale", "qty": 1,
           "sales": net_each, "item_id": "M1"} for i in range(tickets)]
    return {"tickets": ts, "lines": ls,
            "punches": punches if punches is not None else [{"punch_id": f"p-{iso}", "business_date": iso,
                                                             "clock_in": f"{iso}T16:00:00",
                                                             "clock_out": f"{iso}T22:00:00"}],
            "payments": payments if payments is not None else [
                {"payment_id": f"y{i}", "ticket_id": f"t{i}", "business_date": iso, "amount": net_each}
                for i in range(tickets)],
            "payouts": payouts if payouts is not None else [],
            "max_stamp": stamp, "links_ok": True,
            "checks": {"gap": gap, "unknown_type_sales": 0.0, "unknown_type_lines": unknown}}


class _Prov:
    """A provider that archives: `days` per ISO date, a close read, a store."""

    def __init__(self, days=None, closed=True, key="1280:1123", changed=None):
        self.days, self.closed, self.key, self.calls = dict(days or {}), closed, key, []
        self.changed = changed

    def archive_rows(self, rid, day):
        self.calls.append(day.isoformat())
        got = self.days.get(day.isoformat())
        if isinstance(got, Exception):
            raise got
        if callable(got):
            return got()
        return json.loads(json.dumps(got)) if got else _night(day.isoformat(), tickets=0)

    def day_closed_cached(self, rid, day):
        return self.closed

    def store_key(self, rid):
        return self.key


class _ProvChanges(_Prov):
    def changed_business_stamps(self, rid, since, until):
        self.asked = (since, until)
        if isinstance(self.changed, Exception):
            raise self.changed
        return dict(self.changed or {})


def _use(monkeypatch, prov, name="rpower"):
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: (name, prov))
    return prov


def _first(rid, iso):
    row = _q("SELECT first_read FROM pos_archive_days WHERE restaurant_id=? AND business_date=?", (rid, iso))
    return json.loads(row[0]["first_read"]) if row and row[0]["first_read"] else None


def _age_first_read(rid, iso, minutes):
    fr = _first(rid, iso)
    at = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    fr["at"] = fr["read_at"] = at
    _x("UPDATE pos_archive_days SET first_read=? WHERE restaurant_id=? AND business_date=?",
       (json.dumps(fr), rid, iso))


# ── A4-04 / A6-07 / A1-09: a first read is judged before it is held ──────────

def test_an_empty_first_read_on_a_closed_day_is_suspect_and_not_held(monkeypatch):
    rid = _restaurant()
    prov = _use(monkeypatch, _Prov(closed=True))
    out = pos_archive.archive_day(rid, date(2026, 10, 1))
    assert out["suspect"] and "no checks" in out["first_read"]["reasons"][0]
    assert "2026-10-01" not in pos_archive.archived_dates(rid, "rpower")
    trusted, why = pos_archive.night_trusted(rid, "2026-10-01", provider="rpower")
    assert not trusted and "incomplete" in why
    # Read again after half an hour: an empty night needs EMPTY_CONFIRM_MINUTES.
    _age_first_read(rid, "2026-10-01", 40)
    assert pos_archive.archive_day(rid, date(2026, 10, 1))["suspect"]
    _age_first_read(rid, "2026-10-01", pos_archive.EMPTY_CONFIRM_MINUTES + 5)
    out = pos_archive.archive_day(rid, date(2026, 10, 1))
    assert not out["suspect"] and out["first_read"]["verdict"] == "confirmed"
    assert "2026-10-01" in pos_archive.archived_dates(rid, "rpower") and len(prov.calls) == 3


def test_an_empty_night_the_owner_marked_closed_or_a_provider_without_a_close_is_not_suspect(monkeypatch):
    rid = _restaurant(compliance_json=json.dumps({"closed_dates": ["2026-10-01"]}))
    _use(monkeypatch, _Prov(closed=True))
    assert not pos_archive.archive_day(rid, date(2026, 10, 1))["suspect"]

    class _NoClose(_Prov):
        day_closed_cached = None
    _use(monkeypatch, _NoClose())
    assert not pos_archive.archive_day(rid, date(2026, 10, 2))["suspect"]


def test_a_short_first_read_against_the_weekday_median_is_suspect_until_a_whole_read(monkeypatch):
    rid = _restaurant()
    d = date(2026, 10, 15)
    days = {(d - timedelta(days=7 * k)).isoformat(): _night((d - timedelta(days=7 * k)).isoformat(), tickets=40)
            for k in range(1, 6)}
    days[d.isoformat()] = _night(d.isoformat(), tickets=12)          # 30% of a usual Thursday
    prov = _use(monkeypatch, _Prov(days))
    for k in range(1, 6):
        assert not pos_archive.archive_day(rid, d - timedelta(days=7 * k))["suspect"]
    out = pos_archive.archive_day(rid, d)
    assert out["suspect"] and "usual 40" in out["first_read"]["reasons"][0]
    assert not pos_archive.night_trusted(rid, d, provider="rpower")[0]
    # The rest of the night reached RPOWER's cloud: the re-read is whole.
    prov.days[d.isoformat()] = _night(d.isoformat(), tickets=38)
    out = pos_archive.archive_day(rid, d)
    assert not out["suspect"] and out["first_read"]["verdict"] == "ok"
    assert pos_archive.night_trusted(rid, d, provider="rpower")[0]


def test_an_unreconciled_first_read_is_confirmed_by_the_same_read_after_the_wait(monkeypatch):
    rid = _restaurant()
    _use(monkeypatch, _Prov({"2026-10-02": _night("2026-10-02", gap=-20.0)}))
    out = pos_archive.archive_day(rid, date(2026, 10, 2))
    assert out["suspect"] and "off the POS" in out["first_read"]["reasons"][0]
    assert pos_archive.ensure_day(rid, date(2026, 10, 2))["stored"]          # re-read not due yet: no read
    _age_first_read(rid, "2026-10-02", pos_archive.SUSPECT_REREAD_MINUTES + 1)
    out = pos_archive.archive_day(rid, date(2026, 10, 2))
    assert out["first_read"]["verdict"] == "confirmed" and not out["suspect"]
    assert any(k == "data_quality" for _j, k in CAPTURED)


def test_a_suspect_night_is_not_served_to_readers_until_its_re_read(monkeypatch):
    rid = _restaurant()
    day = date.today() - timedelta(days=10)
    prov = _use(monkeypatch, _Prov({day.isoformat(): _night(day.isoformat(), gap=-30.0)}))
    pos_archive.archive_day(rid, day - timedelta(days=1))                     # the archive has started
    pos_archive.archive_day(rid, day)
    prov.calls.clear()
    assert day.isoformat() not in pos_archive.ready_dates(rid, "rpower", [day])
    assert prov.calls == [], "its re-read is not due: the caller reads it this time"
    _age_first_read(rid, day.isoformat(), pos_archive.SUSPECT_REREAD_MINUTES + 1)
    assert day.isoformat() in pos_archive.ready_dates(rid, "rpower", [day]) and prov.calls == [day.isoformat()]


def test_the_dsr_sales_block_waits_on_a_suspect_night():
    import inspect
    from dsr import block_sales
    assert "pos_archive.night_trusted(" in inspect.getsource(block_sales)


# ── A4-05 / A6-18: the archive is the bound store's ─────────────────────────

def test_a_rebind_sets_the_old_stores_nights_aside_and_restarts_the_cursor(monkeypatch):
    rid = _restaurant()
    prov = _use(monkeypatch, _Prov({"2026-10-01": _night("2026-10-01")}, key="1280:1123"))
    pos_archive.archive_day(rid, date(2026, 10, 1))
    pos_archive._set_state(rid, "rpower", models.DB_PATH, changes_checked_to="2026-10-02 10:00:00",
                           restate_pending='["2026-09-30"]')
    assert pos_archive.archived_dates(rid, "rpower") == {"2026-10-01"}
    prov.key = "1280:7777"                                     # corrected to another store
    assert pos_archive.archived_dates(rid, "rpower") == set()
    assert not pos_archive.archive_started(rid, "rpower")
    assert pos_archive.bind_store(rid, "rpower", prov) == "1280:7777"
    st = pos_archive._state(rid, "rpower", models.DB_PATH)
    assert st["store_key"] == "1280:7777" and st["changes_checked_to"] is None and st["restate_pending"] is None
    assert _q("SELECT store_key FROM pos_archive_days WHERE restaurant_id=?", (rid,))[0]["store_key"] == "1280:1123"
    assert _q("SELECT 1 FROM admin_events WHERE event_type='pos_archive.store_rebound' AND restaurant_id=?", (rid,))
    # The new store's read of that night replaces the old one, as its first.
    out = pos_archive.archive_day(rid, date(2026, 10, 1))
    assert out["first_read"]["verdict"] == "ok"
    assert pos_archive.archived_dates(rid, "rpower") == {"2026-10-01"}
    assert _q("SELECT store_key FROM pos_archive_days WHERE restaurant_id=?", (rid,))[0]["store_key"] == "1280:7777"


def test_unstamped_nights_from_before_the_key_are_adopted_by_the_bound_store(monkeypatch):
    rid = _restaurant()
    prov = _use(monkeypatch, _Prov({"2026-10-01": _night("2026-10-01")}))
    pos_archive.archive_day(rid, date(2026, 10, 1))
    _x("UPDATE pos_archive_days SET store_key=NULL")
    _x("UPDATE pos_archive_state SET store_key=NULL")
    assert pos_archive.archived_dates(rid, "rpower") == {"2026-10-01"}            # legacy rows still count
    pos_archive.bind_store(rid, "rpower", prov)
    assert _q("SELECT store_key FROM pos_archive_days")[0]["store_key"] == "1280:1123"


def test_rpower_store_key_is_the_binding_without_a_request(monkeypatch):
    rid = _restaurant(rpower_cg=1280, rpower_store_mid="9")
    monkeypatch.setattr(rpower, "_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    assert rpower.store_key(rid) == "1280:9"


# ── A4-06: a night that will not archive backs off, then is quarantined ─────

def test_a_failing_night_backs_off_then_is_quarantined_and_stops_counting_as_missing(monkeypatch):
    rid = _restaurant()
    bad = pos_archive.ArchiveCountMismatch("pos_tickets 2026-09-25: 2 records from the POS, 1 stored")
    prov = _use(monkeypatch, _Prov({"2026-09-25": bad}))
    for n in range(1, pos_archive.DAY_QUARANTINE_AFTER + 1):
        with pytest.raises(pos_archive.ArchiveCountMismatch):
            pos_archive.archive_day(rid, date(2026, 9, 25))
        led = pos_archive._failures(rid, "rpower")
        assert led["2026-09-25"]["n"] == n
    assert led["2026-09-25"]["quarantined"]
    assert pos_archive.quarantined_days(rid)[0]["date"] == "2026-09-25"
    # A provider asking us to wait costs the night nothing.
    err = rpower.RPowerUnavailable("paused", retry_after=60)
    prov.days["2026-09-26"] = err
    with pytest.raises(rpower.RPowerUnavailable):
        pos_archive.archive_day(rid, date(2026, 9, 26))
    assert "2026-09-26" not in pos_archive._failures(rid, "rpower")
    # The nightly run neither reads nor counts the quarantined night.
    monkeypatch.setattr(pos_archive, "sync_catalog", lambda *a, **k: None)
    monkeypatch.setattr(pos_archive, "sync_roles", lambda *a, **k: None)
    monkeypatch.setattr(pos_archive, "_prices_due", lambda *a, **k: False)
    prov.days.pop("2026-09-26")
    prov.calls.clear()
    out = pos_archive.run_for(rid, today=date(2026, 9, 30), now_utc=datetime(2026, 9, 30, 10, tzinfo=timezone.utc))
    assert "2026-09-25" not in prov.calls and "2026-09-25" in out["quarantined"]
    assert pos_archive.release_quarantine(rid, "rpower", "2026-09-25")
    assert "2026-09-25" not in pos_archive._failures(rid, "rpower")


def test_the_backoff_doubles(monkeypatch):
    rid = _restaurant()
    _use(monkeypatch, _Prov())
    now = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    nexts = []
    for _ in range(4):
        pos_archive._note_failure(rid, "rpower", "2026-09-20", RuntimeError("boom"), now=now)
        nexts.append(pos_archive._utc(pos_archive._failures(rid, "rpower")["2026-09-20"]["next"]) - now)
    assert nexts == [timedelta(0), timedelta(hours=24), timedelta(hours=48), timedelta(hours=96)]


# ── A4-07: punches, payments and payouts are guarded too ────────────────────

@pytest.mark.parametrize("table", ("punches", "payments", "payouts"))
def test_an_empty_re_read_of_punches_payments_or_payouts_never_replaces_them(monkeypatch, table):
    rid = _restaurant()
    full = _night("2026-10-03", payouts=[{"payout_id": "po1", "business_date": "2026-10-03", "amount": 5.0}])
    prov = _use(monkeypatch, _Prov({"2026-10-03": full}))
    pos_archive.archive_day(rid, date(2026, 10, 3))
    short = _night("2026-10-03", payouts=[{"payout_id": "po1", "business_date": "2026-10-03", "amount": 5.0}],
                   stamp="2026-10-03T09:00:00")
    short[table] = []
    prov.days["2026-10-03"] = short
    with pytest.raises(pos_archive.ArchiveShrunk):
        pos_archive.archive_day(rid, date(2026, 10, 3))
    tbl = {"punches": "pos_punches", "payments": "pos_payments", "payouts": "pos_payouts"}[table]
    assert _q(f"SELECT COUNT(*) AS n FROM {tbl} WHERE restaurant_id=?", (rid,))[0]["n"] > 0


# ── A4-09: one run per restaurant; a lost lease stops between days ──────────

def test_a_second_run_of_the_same_restaurant_is_busy(monkeypatch):
    rid = _restaurant()
    _use(monkeypatch, _Prov())
    assert ops.claim_cooldown(f"pos_run_for:{rid}:rpower", 10)
    out = pos_archive.run_for(rid, today=date(2026, 9, 30))
    assert out["busy"] and out["retry_after"] and not out["ok"]


def test_a_run_whose_lease_was_lost_stops_before_the_next_day(monkeypatch):
    rid = _restaurant()
    prov = _use(monkeypatch, _Prov())
    monkeypatch.setattr(pos_archive, "sync_catalog", lambda *a, **k: None)
    calls = {"n": 0}

    def lost(job=None):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise job_queue.LeaseLost("lost")
    monkeypatch.setattr(job_queue, "raise_if_lost", lost)
    with pytest.raises(job_queue.LeaseLost):
        pos_archive.run_for(rid, today=date(2026, 9, 30), now_utc=datetime(2026, 9, 30, 10, tzinfo=timezone.utc))
    assert len(prov.calls) == 1
    assert ops.claim_cooldown(f"pos_run_for:{rid}:rpower", 10), "the claim was given back"


# ── A1-03 / A1-15 / A4-18 / A4-14: the change read ──────────────────────────

def test_the_change_margin_is_zero_and_the_payment_feed_is_watched():
    assert rpower.CHANGE_MARGIN == timedelta(0)
    assert "ticketpayment/getbytimestamp" in rpower.CHANGE_FEEDS


def test_a_datetime_refusal_is_matched_by_status_and_body_and_expires(monkeypatch):
    rid = _restaurant()
    asked = []

    def req(token, path, params=None):
        if "T" in str(params["startdate"]):
            raise rpower.RPowerError("HTTP 404 from ticketsales/getbytimestamp: not found")
        asked.append(params["startdate"])
        return []
    monkeypatch.setattr(rpower, "_request", req)
    with pytest.raises(rpower.RPowerError):
        rpower.changed_business_stamps(rid, datetime(2026, 10, 9, 3, tzinfo=timezone.utc),
                                       datetime(2026, 10, 10, 3, tzinfo=timezone.utc))
    assert rpower._change_datetime_refused["at"] is None, "a 404 is no refusal of a time"

    def req400(token, path, params=None):
        if "T" in str(params["startdate"]):
            raise rpower.RPowerError("HTTP 400 from ticketsales/getbytimestamp: The startdate is not a valid date")
        return []
    monkeypatch.setattr(rpower, "_request", req400)
    rpower.changed_business_stamps(rid, datetime(2026, 10, 9, 3, tzinfo=timezone.utc),
                                   datetime(2026, 10, 10, 3, tzinfo=timezone.utc))
    assert rpower._change_datetime_refused["at"] is not None and "valid date" in rpower._change_datetime_refused["why"]
    rpower._change_datetime_refused["at"] -= rpower.CHANGE_DATETIME_REFUSAL_SECONDS + 1
    assert not rpower._datetime_refusal_active() and rpower._change_datetime_refused["at"] is None


def test_the_change_read_looks_back_seven_days_at_most_and_an_old_cursor_is_reported(monkeypatch):
    rid = _restaurant()
    prov = _use(monkeypatch, _ProvChanges(changed={}))
    monkeypatch.setattr(pos_archive, "sync_catalog", lambda *a, **k: None)
    monkeypatch.setattr(pos_archive, "sync_roles", lambda *a, **k: None)
    monkeypatch.setattr(pos_archive, "_prices_due", lambda *a, **k: False)
    now = datetime(2026, 10, 30, 10, tzinfo=timezone.utc)
    pos_archive._set_state(rid, "rpower", models.DB_PATH, changes_checked_to="2026-09-01 10:00:00")
    out = pos_archive.run_for(rid, today=date(2026, 10, 30), now_utc=now)
    assert prov.asked[0] >= now - timedelta(days=pos_archive.CHANGE_LOOKBACK_MAX_DAYS, hours=2)
    assert out["cursor_age_hours"] > pos_archive.CURSOR_STALE_HOURS
    assert any("change cursor" in m and k == "data_quality" for m, k in CAPTURED)


# ── A4-03: a doubted change answer; the weekly verification ────────────────

def test_an_empty_change_answer_over_our_own_reads_does_not_advance_the_cursor(monkeypatch):
    rid = _restaurant()
    prov = _use(monkeypatch, _ProvChanges({"2026-10-19": _night("2026-10-19", stamp="2026-10-20T04:30:00")},
                                          changed={}))
    pos_archive.archive_day(rid, date(2026, 10, 19))            # the DSR read it after its close
    _x("UPDATE pos_archive_days SET archived_at='2026-10-20 05:00:00'")
    monkeypatch.setattr(pos_archive, "sync_catalog", lambda *a, **k: None)
    monkeypatch.setattr(pos_archive, "sync_roles", lambda *a, **k: None)
    monkeypatch.setattr(pos_archive, "_prices_due", lambda *a, **k: False)
    pos_archive._set_state(rid, "rpower", models.DB_PATH, changes_checked_to="2026-10-19 10:00:00")
    out = pos_archive.run_for(rid, today=date(2026, 10, 20), now_utc=datetime(2026, 10, 20, 10, tzinfo=timezone.utc))
    assert out["feed_suspect"] == ["2026-10-19"]
    assert pos_archive._state(rid, "rpower", models.DB_PATH)["changes_checked_to"] == "2026-10-19 10:00:00"
    # An answer that names the night moves the cursor on.
    prov.changed = {"2026-10-19": "2026-10-20 04:30:00"}
    pos_archive._change_memo.clear()
    _x("UPDATE pos_archive_state SET change_read_at=NULL")
    out = pos_archive.run_for(rid, today=date(2026, 10, 20), now_utc=datetime(2026, 10, 20, 11, tzinfo=timezone.utc))
    assert out["feed_suspect"] == []
    assert pos_archive._state(rid, "rpower", models.DB_PATH)["changes_checked_to"] == "2026-10-20 11:00:00"


def test_the_weekly_verification_re_archives_a_night_the_feed_never_named(monkeypatch):
    rid = _restaurant()
    today = date(2026, 10, 20)
    night = today - timedelta(days=14)                          # two weeks old: tonight's turn
    prov = _use(monkeypatch, _ProvChanges({night.isoformat(): _night(night.isoformat(), tickets=10)}, changed={}))
    pos_archive.archive_day(rid, night)
    _x("UPDATE pos_tickets SET type_sale=50.0")
    theirs = {"tickets": 11, "type_sale": 550.0}                # a check re-posted at the store
    prov.verify_day_totals = lambda r, d: dict(theirs) if d == night else {"tickets": 0, "type_sale": 0}
    prov.days[night.isoformat()] = _night(night.isoformat(), tickets=11, stamp="2026-10-01T03:00:00")
    for m in ("sync_catalog", "sync_roles"):
        monkeypatch.setattr(pos_archive, m, lambda *a, **k: None)
    monkeypatch.setattr(pos_archive, "_prices_due", lambda *a, **k: False)
    prov.calls.clear()
    out = pos_archive.run_for(rid, today=today, now_utc=datetime(2026, 10, 20, 10, tzinfo=timezone.utc))
    assert out["verify_moved"] == [night.isoformat()] and night.isoformat() in prov.calls
    assert _q("SELECT tickets FROM pos_archive_days WHERE business_date=?", (night.isoformat(),))[0]["tickets"] == 11


def test_rpower_verify_reads_one_business_date_of_tickets(monkeypatch):
    rid = _restaurant()
    asked = []

    def req(token, path, params=None):
        asked.append(path)
        return [{"rid": "1", "date": "2026-10-01T00:00:00", "type_sale": 20.0},
                {"rid": "2", "date": "2026-10-01T00:00:00", "type_sale": 5.0, "is_cancelled": 1}]
    monkeypatch.setattr(rpower, "_request", req)
    assert rpower.verify_day_totals(rid, date(2026, 10, 1)) == {"tickets": 2, "type_sale": 20.0}
    assert asked == ["ticket/getbybusinessdate"]


# ── A6-03 / A4-13: recent gaps owed; older layouts apart from the backfill ──

def test_a_recent_gap_is_owed_without_a_slot_and_an_old_layout_is_re_read_a_few_a_night(monkeypatch):
    import onboarding
    rid = _restaurant()
    today = date(2026, 10, 20)
    prov = _use(monkeypatch, _Prov({(today - timedelta(days=i)).isoformat():
                                    _night((today - timedelta(days=i)).isoformat()) for i in range(1, 40)}))
    for i in range(1, 40):
        if i != 4:                                          # 10/16 never made it
            pos_archive.archive_day(rid, today - timedelta(days=i))
    _x("UPDATE pos_archive_days SET version=3 WHERE business_date < '2026-09-25'")    # an older layout
    monkeypatch.setattr(onboarding, "backfill_admission", lambda *a, **k: {"admit": False, "reason": "waiting"})
    for m in ("sync_catalog", "sync_roles"):
        monkeypatch.setattr(pos_archive, m, lambda *a, **k: None)
    monkeypatch.setattr(pos_archive, "_prices_due", lambda *a, **k: False)
    prov.calls.clear()
    out = pos_archive.run_for(rid, today=today, now_utc=datetime(2026, 10, 20, 10, tzinfo=timezone.utc))
    assert out["recent_gaps"] == ["2026-10-16"] and "2026-10-16" in prov.calls
    assert len(out["stale_layout"]) == pos_archive.STALE_LAYOUT_PER_NIGHT
    assert all(d in prov.calls for d in out["stale_layout"])
    # Last night too: this provider has no change read to say it is unchanged.
    assert set(prov.calls) == {"2026-10-19", "2026-10-16", *out["stale_layout"]}


# ── A6-06 / A2-22 / open punches: what the night records ───────────────────

def test_the_night_records_its_pay_gap_open_punches_line_net_and_store(monkeypatch):
    rid = _restaurant()
    n = _night("2026-10-05", tickets=4, punches=[
        {"punch_id": "a", "business_date": "2026-10-05", "clock_in": "2026-10-05T16:00:00", "clock_out": None},
        {"punch_id": "b", "business_date": "2026-10-05", "clock_in": "2026-10-05T10:00:00",
         "clock_out": "2026-10-05T15:00:00"}])
    n["payments"] = n["payments"][:3]                        # one check's payment lost
    n["net_sales"] = 212.5                                   # rpower's line-based net (a ticketless line)
    _use(monkeypatch, _Prov({"2026-10-05": n}))
    pos_archive.archive_day(rid, date(2026, 10, 5))
    row = _q("SELECT pay_gap, open_punches, net_sales, store_key, attempts FROM pos_archive_days")[0]
    assert (row["pay_gap"], row["open_punches"], row["net_sales"], row["store_key"]) == (-50.0, 1, 212.5,
                                                                                         "1280:1123")
    assert any("payments for 2026-10-05" in m and k == "data_quality" for m, k in CAPTURED)


def test_rpower_archive_rows_nets_from_the_lines_and_asks_payouts_through_the_next_day(monkeypatch):
    rid = _restaurant()
    asked = {}
    d = "2026-10-01T00:00:00"
    routes = {
        "salestype/getbycg": [{"mid": "S", "name": "Sale", "impacts_sales": 1, "type_sale": 1}],
        "ticketsales/getbybusinessdate": [
            {"rid": "L1", "ticket_rid": "T1", "date": d, "slstype_mid": "S", "sales": 30.0,
             "time_stamp": "2026-10-02T01:00:00"},
            {"rid": "L2", "ticket_rid": "T9", "date": d, "slstype_mid": "S", "sales": 7.0}],   # no ticket row
        "ticket/getbybusinessdate": [{"rid": "T1", "date": d, "type_sale": 30.0}],
    }

    def req(token, path, params=None):
        asked[path] = params
        return routes.get(path, [])
    monkeypatch.setattr(rpower, "_request", req)
    out = rpower.archive_rows(rid, date(2026, 10, 1))
    assert out["net_sales"] == 37.0 and sum(t["net_sales"] for t in out["tickets"]) == 30.0
    assert asked["payout/getbydaterange"]["enddate"] == "2026-10-02"
    assert out["source"] == "business_date"


# ── A1-04: a night from the change read's own rows ─────────────────────────

def test_a_night_the_change_window_covered_is_built_from_its_rows(monkeypatch):
    rid = _restaurant()
    d = "2026-10-01T00:00:00"
    feed_lines = [{"rid": "L1", "ticket_rid": "T1", "date": d, "slstype_mid": "S", "sales": 30.0,
                   "time_stamp": "2026-10-01T23:00:00"}]
    feed_tickets = [{"rid": "T1", "date": d, "type_sale": 30.0, "time_stamp": "2026-10-01T23:05:00"}]
    asked = []

    def req(token, path, params=None):
        asked.append(path)
        if params.get("pagenumber", 1) > 1:
            return []
        return {"ticketsales/getbytimestamp": feed_lines, "ticket/getbytimestamp": feed_tickets,
                "salestype/getbycg": [{"mid": "S", "name": "Sale", "impacts_sales": 1, "type_sale": 1}]}.get(path, [])
    monkeypatch.setattr(rpower, "_request", req)
    # From 6pm UTC the day before: before the Chicago midnight starting 10/1.
    rpower.changed_business_stamps(rid, datetime(2026, 9, 30, 18, tzinfo=timezone.utc),
                                   datetime(2026, 10, 2, 10, tzinfo=timezone.utc))
    asked.clear()
    out = rpower.archive_rows(rid, date(2026, 10, 1))
    assert out["source"] == "feed" and out["net_sales"] == 30.0
    assert "ticketsales/getbybusinessdate" not in asked and "ticket/getbybusinessdate" not in asked
    # A window that started after the night began proves nothing: by business date.
    rpower.changed_business_stamps(rid, datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
                                   datetime(2026, 10, 2, 10, tzinfo=timezone.utc))
    asked.clear()
    assert rpower.archive_rows(rid, date(2026, 10, 1))["source"] == "business_date"
    assert "ticketsales/getbybusinessdate" in asked


def test_feed_rows_that_do_not_reconcile_fall_back_to_the_business_date_read(monkeypatch):
    rid = _restaurant()
    d = "2026-10-01T00:00:00"
    sale = {"mid": "S", "name": "Sale", "impacts_sales": 1, "type_sale": 1}
    routes = {"ticketsales/getbytimestamp": [{"rid": "L1", "ticket_rid": "T1", "date": d, "slstype_mid": "S",
                                              "sales": 30.0, "time_stamp": "2026-10-01T23:00:00"}],
              "ticket/getbytimestamp": [{"rid": "T1", "date": d, "type_sale": 80.0,
                                         "time_stamp": "2026-10-01T23:05:00"}],
              "ticketsales/getbybusinessdate": [{"rid": "L1", "ticket_rid": "T1", "date": d, "slstype_mid": "S",
                                                 "sales": 80.0}],
              "ticket/getbybusinessdate": [{"rid": "T1", "date": d, "type_sale": 80.0}],
              "salestype/getbycg": [sale]}
    monkeypatch.setattr(rpower, "_request", lambda t, path, params=None: [] if params.get("pagenumber", 1) > 1
                        else routes.get(path, []))
    rpower.changed_business_stamps(rid, datetime(2026, 9, 30, 18, tzinfo=timezone.utc),
                                   datetime(2026, 10, 2, 10, tzinfo=timezone.utc))
    out = rpower.archive_rows(rid, date(2026, 10, 1))
    assert out["source"] == "business_date" and out["net_sales"] == 80.0


def test_a_kept_change_read_is_extended_not_repeated(monkeypatch):
    rid = _restaurant()
    prov = _use(monkeypatch, _ProvChanges(changed={"2026-10-19": "2026-10-20 03:00:00"}))
    t1 = datetime(2026, 10, 20, 7, tzinfo=timezone.utc)
    pos_archive._changed_stamps(rid, prov, datetime(2026, 10, 19, 4, tzinfo=timezone.utc), t1, provider="rpower")
    pos_archive._change_memo.clear()
    prov.changed = {"2026-10-12": "2026-10-20 09:00:00"}
    t2 = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
    out, at = pos_archive._changed_stamps(rid, prov, datetime(2026, 10, 19, 10, tzinfo=timezone.utc), t2,
                                          provider="rpower")
    assert prov.asked[0] == t1, "only the writes after the kept read were asked for"
    assert out == {"2026-10-19": "2026-10-20 03:00:00", "2026-10-12": "2026-10-20 09:00:00"} and at == t2


# ── A1-11 / A4-22: a multi-page gap read again; a good read kept ────────────

def test_a_multi_page_read_that_does_not_reconcile_is_read_once_more(monkeypatch):
    rid = _restaurant()
    reads = []

    class _Big(_Prov):
        PAGE_SIZE = 5

        def archive_rows(self, rid_, day):
            reads.append(1)
            return _night(day.isoformat(), tickets=6, gap=(-12.0 if len(reads) == 1 else 0.0))
    _use(monkeypatch, _Big())
    out = pos_archive.archive_day(rid, date(2026, 10, 6))
    assert len(reads) == 2 and out["recon_gap"] == 0.0 and not out["suspect"]


def test_a_good_read_whose_write_failed_is_stored_by_the_retry_without_a_download(monkeypatch):
    rid = _restaurant()
    prov = _use(monkeypatch, _Prov({"2026-10-07": _night("2026-10-07")}))
    real = pos_archive._drop_neighbour_punches
    boom = {"on": True}

    def fail_once(*a, **k):                       # a write inside the night's transaction fails
        if boom["on"]:
            raise RuntimeError("pool timeout")
        return real(*a, **k)
    monkeypatch.setattr(pos_archive, "_drop_neighbour_punches", fail_once)
    with pytest.raises(RuntimeError):
        pos_archive.archive_day(rid, date(2026, 10, 7))
    assert not _q("SELECT 1 FROM pos_archive_days")
    boom["on"] = False
    assert len(prov.calls) == 1
    pos_archive.archive_day(rid, date(2026, 10, 7))
    assert len(prov.calls) == 1, "the kept read was stored, not downloaded again"


# ── A2-16 / A3-17 / A3-16: punches re-dated, and a day's punches all deleted ─

def test_refresh_punches_takes_an_empty_day_when_the_read_was_serving(monkeypatch):
    rid = _restaurant()
    _use(monkeypatch, _Prov({"2026-10-08": _night("2026-10-08"), "2026-10-09": _night("2026-10-09")}))
    pos_archive.archive_day(rid, date(2026, 10, 8))
    pos_archive.archive_day(rid, date(2026, 10, 9))
    # The whole read came back empty: nothing is wiped.
    pos_archive.refresh_punches(rid, "rpower", {"2026-10-08": [], "2026-10-09": []})
    assert _q("SELECT COUNT(*) AS n FROM pos_punches")[0]["n"] == 2
    # 10/9's punches came back and 10/8's lone punch was voided at the store.
    keep = _q("SELECT * FROM pos_punches WHERE business_date='2026-10-09'")
    out = pos_archive.refresh_punches(rid, "rpower", {"2026-10-08": [], "2026-10-09": keep})
    assert out["changed"] == 1
    assert _q("SELECT punches FROM pos_archive_days WHERE business_date='2026-10-08'")[0]["punches"] == 0


def test_refresh_punches_drops_the_copy_on_the_neighbouring_night(monkeypatch):
    rid = _restaurant()
    _use(monkeypatch, _Prov({"2026-10-08": _night("2026-10-08"), "2026-10-09": _night("2026-10-09")}))
    pos_archive.archive_day(rid, date(2026, 10, 8))
    pos_archive.archive_day(rid, date(2026, 10, 9))
    moved = dict(_q("SELECT * FROM pos_punches WHERE business_date='2026-10-09'")[0])
    moved.update(business_date="2026-10-08", clock_in="2026-10-09T03:50:00")      # edited across 4am
    pos_archive.refresh_punches(rid, "rpower", {"2026-10-08": [moved] + _q(
        "SELECT * FROM pos_punches WHERE business_date='2026-10-08'"), "2026-10-09": []})
    got = sorted((r["punch_id"], r["business_date"]) for r in _q("SELECT punch_id, business_date FROM pos_punches"))
    assert got == [("p-2026-10-08", "2026-10-08"), ("p-2026-10-09", "2026-10-08")]


def test_the_re_dated_punch_cleanup_runs_once_per_restaurant(monkeypatch):
    rid = _restaurant()
    _use(monkeypatch, _Prov())
    first = pos_archive.dedupe_redated_punches(rid, "rpower")
    assert first.get("skipped") is None
    assert pos_archive.dedupe_redated_punches(rid, "rpower")["skipped"] == "done before"


@pytest.mark.skipif(not PG, reason="CAVNAR_TEST_PG_URL names no scratch Postgres")
def test_the_re_dated_punch_cleanup_keeps_the_punchs_own_date_on_postgres(monkeypatch):
    # The suite's Postgres mode keys pos_punches as production does (the
    # business date in the key), so a punch can sit on two dates.
    rid = _restaurant()
    _x("INSERT INTO pos_punches (restaurant_id, provider, punch_id, business_date, clock_in) "
       "VALUES (?, 'rpower', 'p1', '2026-10-09', '2026-10-10T04:30:00')", (rid,))
    try:
        _x("INSERT INTO pos_punches (restaurant_id, provider, punch_id, business_date, clock_in) "
           "VALUES (?, 'rpower', 'p1', '2026-10-10', '2026-10-10T04:30:00')", (rid,))
    except Exception:
        pytest.skip("this database keys pos_punches without the business date")
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", rpower))
    out = pos_archive.dedupe_redated_punches(rid, "rpower", force=True)
    assert out["deleted"] == 1
    assert [r["business_date"] for r in _q("SELECT business_date FROM pos_punches WHERE punch_id='p1'")] == [
        "2026-10-10"]


# ── A4-20 / A4-15: the sync ────────────────────────────────────────────────

def test_a_sync_running_in_another_process_makes_this_one_busy(monkeypatch):
    rid = _restaurant()
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", rpower))
    monkeypatch.setattr(rpower, "sync_to_db", lambda r: (_ for _ in ()).throw(AssertionError("synced twice")))
    assert ops.claim_cooldown(f"pos_sync:{rid}", 10)
    out = pos.sync_restaurant(rid)
    assert out["busy"] and not out["ok"]
    ops.release_period("cooldown", f"pos_sync:{rid}")
    monkeypatch.setattr(rpower, "sync_to_db", lambda r: {"ok": True})
    assert pos.sync_restaurant(rid)["ok"]
    assert ops.claim_cooldown(f"pos_sync:{rid}", 10), "the claim was given back"


def test_the_nightly_sync_window_is_local_and_never_mid_service():
    r = models.Restaurant(name="x", owner_email="x@x")
    at = lambda h: datetime(2026, 10, 21, h, 0)          # noqa: E731
    assert pos.sync_due_locally(r, at(3)) and pos.sync_due_locally(r, at(10))
    assert not pos.sync_due_locally(r, at(11)) and not pos.sync_due_locally(r, at(22))
    assert not pos.sync_due_locally(r, at(2))


def test_a_hawaii_store_waits_for_its_own_boundary_and_archives_that_night(monkeypatch):
    import strategy_jobs
    import time_utils
    from zoneinfo import ZoneInfo
    r = models.Restaurant(name="Kona", owner_email="k@x", timezone="Pacific/Honolulu")
    now = datetime(2026, 10, 21, 23, 0, tzinfo=ZoneInfo("Pacific/Honolulu"))          # 4am Chicago
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: now.replace(tzinfo=None)
                        if k.get("naive") else now)
    night, not_before = strategy_jobs._archive_night(r)
    assert night == "2026-10-21"
    assert datetime.fromtimestamp(not_before, ZoneInfo("Pacific/Honolulu")).strftime("%m-%d %H:%M") == "10-22 05:00"
    east = datetime(2026, 10, 21, 5, 0, 10, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: east.replace(tzinfo=None)
                        if k.get("naive") else east)
    r2 = models.Restaurant(name="NY", owner_email="n@x", timezone="America/New_York")
    assert strategy_jobs._archive_night(r2) == ("2026-10-20", 0)


# ── A5-08 / A5-12 ──────────────────────────────────────────────────────────

def test_backfill_bytes_default_and_per_store_measure(monkeypatch):
    import onboarding
    rid = _restaurant()
    assert onboarding.BACKFILL_BYTES_PER_STORE_DAY == 1_250_000
    assert onboarding.bytes_per_store_day(rid, "rpower") == 1_250_000
    for i in range(10):
        _x("INSERT INTO pos_archive_days (restaurant_id, provider, business_date, lines) VALUES (?, 'rpower', ?, 2000)",
           (rid, f"2026-10-{i + 1:02d}"))
    assert onboarding.bytes_per_store_day(rid, "rpower") == 2000 * onboarding.BACKFILL_BYTES_PER_LINE


def test_a_nightly_reader_waits_for_a_night_being_archived_elsewhere(monkeypatch):
    rid = _restaurant()
    day = date.today() - timedelta(days=10)
    prov = _use(monkeypatch, _Prov({day.isoformat(): _night(day.isoformat()),
                                    (day - timedelta(days=1)).isoformat(): _night((day - timedelta(days=1)).isoformat())}))
    pos_archive.archive_day(rid, day - timedelta(days=1))
    monkeypatch.setattr(pos_archive, "archive_day", lambda *a, **k: (_ for _ in ()).throw(pos_archive.ArchiveBusy("x")))
    stored = {"n": 0}

    def wait(rid_, name, isos, since, seconds, db_path=None):
        stored["n"] += 1
        return set(isos)
    monkeypatch.setattr(pos_archive, "_wait_for_nights", wait)
    with pos_io.lane("nightly"):
        assert day.isoformat() in pos_archive.ready_dates(rid, "rpower", [day])
    assert stored["n"] == 1
    with pos_io.lane("close"):                                   # the nightly report never waits
        assert day.isoformat() not in pos_archive.ready_dates(rid, "rpower", [day])
    assert stored["n"] == 1
