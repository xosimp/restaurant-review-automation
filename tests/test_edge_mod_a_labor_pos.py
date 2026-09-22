"""POS shift sync and live coverage, as the MOD audit found them: Toast and
Square shifts dated by the UTC clock-in instead of the restaurant's
business date, Toast name formats that never match each other, the silent
2,000-entry cap, Square's role and partial-page sales, Square/Clover syncs
that never archive daily history, a sync overwriting a longer hand upload,
open clock-ins read as no-shows, the nightly loop's scope, and the no-show
coverage job run through the real coverage_gaps.

Every POS call is stubbed at the module function or at requests; nothing
reaches Toast, Square or Clover. Confirmed defects are strict xfails naming
the finding."""
import csv
import io
import os
import sys
from datetime import date, datetime

import pytest

import models
from models import Restaurant, create_restaurant, get_conn



# Imported at collection, while models.get_conn is still the real one, so a
# lazy import inside a test never binds that test's redirect for good.
import cogs, delayed, demand, food_cost_intelligence, intraday, inventory, inventory_ledger  # noqa: E401,F401
import invoices, issues, marketing_signals, notify, ops, pos, push, recipes, reporter  # noqa: E401,F401
import staff_settings, strategy_jobs, toast, square, clover, webhooks  # noqa: E401,F401

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    # Every repo module holding a get_conn: the real one by identity, and any
    # stale redirect an earlier test's lazy import bound (CLAUDE.md "Bound imports").
    for mod in list(sys.modules.values()):
        f = str(getattr(mod, "__file__", None) or "")
        if mod is not None and (getattr(mod, "get_conn", None) is real or
                                (f.startswith(_REPO) and callable(getattr(mod, "get_conn", None)))):
            monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr("notify.send_sms", lambda *a, **k: True)


def _rid(db_path, **kw):
    kw.setdefault("name", "POS Grill")
    kw.setdefault("owner_email", "o@pos.test")
    kw.setdefault("module_labor", 1)
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _set(db_path, rid, **cols):
    c = get_conn(db_path)
    for k, v in cols.items():
        c.execute(f"UPDATE restaurants SET {k}=? WHERE id=?", (v, rid))
    c.commit(); c.close()


def _rows(csv_text):
    return list(csv.DictReader(io.StringIO(csv_text or "")))


def _entry(guid, first, last, in_date, out_date=None, paid=None, sched=None):
    e = {"employee": {"guid": guid, "firstName": first, "lastName": last},
         "jobReference": {"name": "Server"}, "inDate": in_date}
    if out_date:
        e["outDate"] = out_date
    if paid is not None:
        e["paidMinutes"] = paid
    if sched:
        e["scheduledInDate"], e["scheduledOutDate"] = sched
    return e


# ── A3 #35 / MOD-LAB-3: the business date, not the UTC date ─────────────────

@pytest.mark.xfail(strict=True, reason="MOD-LAB-3: Toast shifts are dated and timed by the UTC clock-in, so a 7:30pm CDT shift lands tomorrow at 00:30")
def test_a_toast_evening_shift_keeps_the_restaurants_local_business_date(db_path, monkeypatch):
    import toast
    rid = _rid(db_path)
    monkeypatch.setattr(toast, "fetch_time_entries", lambda *a, **k: [
        _entry("g1", "Maria", "Garcia", "2026-09-19T00:30:00.000+0000", "2026-09-19T06:30:00.000+0000")])
    monkeypatch.setattr(toast, "fetch_business_days", lambda *a, **k: {})
    rows = _rows(toast.build_shifts_csv(rid))
    assert rows[0]["date"] == "2026-09-18" and rows[0]["shift_start"] == "19:30"


@pytest.mark.xfail(strict=True, reason="MOD-LAB-3: Square shifts are dated by start_at's UTC date")
def test_a_square_evening_shift_keeps_the_restaurants_local_business_date(db_path, monkeypatch):
    import square
    rid = _rid(db_path)
    monkeypatch.setattr(square, "_fetch_team_members", lambda rid_: {"t1": {"name": "Ann B", "job_title": "Server"}})
    monkeypatch.setattr(square, "_fetch_shifts", lambda *a, **k: [
        {"team_member_id": "t1", "start_at": "2026-09-19T00:30:00Z", "end_at": "2026-09-19T06:30:00Z"}])
    monkeypatch.setattr(square, "_fetch_daily_sales", lambda *a, **k: {})
    rows = _rows(square.build_shifts_csv(rid))
    assert rows[0]["date"] == "2026-09-18" and rows[0]["shift_start"] == "19:30"


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _square_connected(db_path, rid):
    _set(db_path, rid, square_access_token="sq-test", square_location_id="L1")


@pytest.mark.xfail(strict=True, reason="MOD-LAB-3: Square order sales are bucketed by created_at's UTC date")
def test_square_evening_sales_are_counted_on_the_local_business_date(db_path, monkeypatch):
    import square
    rid = _rid(db_path)
    _square_connected(db_path, rid)
    monkeypatch.setattr(square.requests, "post", lambda url, **k: _Resp(200, {
        "orders": [{"created_at": "2026-09-19T01:00:00Z", "total_money": {"amount": 500000}}]}))
    sales = square._fetch_daily_sales(rid, date(2026, 9, 18), date(2026, 9, 19))
    assert sales == {"2026-09-18": 5000}


# ── A3 #50 / MOD-LAB-2: one name for one Toast employee ─────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-LAB-2: shift history says 'Maria G.' while the live clock-in feed says 'Maria Garcia'")
def test_toast_history_and_live_clock_ins_name_the_same_employee_the_same_way(monkeypatch):
    import toast
    e = _entry("g1", "Maria", "Garcia", "2026-09-21T15:00:00.000+0000", "2026-09-21T21:00:00.000+0000")
    monkeypatch.setattr(toast, "fetch_time_entries", lambda *a, **k: [e])
    history_name = toast.normalise_entries([e], {})[0]["employee"]
    live_name = toast.fetch_clock_ins_today(1, date(2026, 9, 21))[0]["employee"]
    assert history_name == live_name


# ── A3 #37 / MOD-LAB-5: same first name and last initial ───────────────────

@pytest.mark.xfail(strict=True, reason="MOD-LAB-5: Toast names are abbreviated to 'First L.', merging two employees")
def test_two_toast_employees_sharing_first_name_and_last_initial_stay_two_people():
    import toast
    rows = toast.normalise_entries([
        _entry("g1", "Maria", "Garcia", "2026-09-21T15:00:00.000+0000", paid=360),
        _entry("g2", "Maria", "Gonzalez", "2026-09-21T15:00:00.000+0000", paid=360),
    ], {})
    assert len({r["employee"] for r in rows}) == 2


def test_nameless_toast_entries_are_kept_not_dropped():
    import toast
    rows = toast.normalise_entries([_entry("g1", "", "", "2026-09-21T15:00:00.000+0000", paid=120)], {})
    assert len(rows) == 1 and rows[0]["actual_hours"] == 2.0


# ── A3 #36 / MOD-LAB-4: the 2,000-entry cap ─────────────────────────────────

def _toast_connected(db_path, rid, monkeypatch):
    import toast
    _set(db_path, rid, toast_client_id="cid", toast_restaurant_guid="guid")
    monkeypatch.setattr(toast, "get_toast_token", lambda rid_: "tok")


def _endless_pages(calls):
    def get(url, **k):
        calls.append(k.get("params", {}).get("pageToken"))
        n = len(calls)
        return _Resp(200, {"timeEntries": [
            _entry(f"g{n}-{i}", "E", str(i), "2026-09-10T15:00:00.000+0000", paid=60) for i in range(100)],
            "nextPageToken": f"p{n}"})
    return get


def test_the_toast_time_entry_fetch_stops_at_a_bounded_number_of_pages(db_path, monkeypatch):
    import toast
    rid = _rid(db_path)
    _toast_connected(db_path, rid, monkeypatch)
    calls = []
    monkeypatch.setattr(toast.requests, "get", _endless_pages(calls))
    out = toast.fetch_time_entries(rid, date(2026, 7, 1), date(2026, 9, 1))
    assert len(calls) == 20 and len(out) == 2000


@pytest.mark.xfail(strict=True, reason="MOD-LAB-4: a sync that hit the 2,000-entry cap reports ok and overwrites the complete CSV")
def test_a_truncated_toast_sync_is_not_reported_ok_and_keeps_the_previous_csv(db_path, monkeypatch):
    import toast
    rid = _rid(db_path)
    _toast_connected(db_path, rid, monkeypatch)
    previous = "date,employee,role,actual_hours,sales\n2026-07-01,Ann,Server,8,1000\n"
    models.save_client_data(rid, "shifts", previous, db_path=db_path)
    monkeypatch.setattr(toast.requests, "get", _endless_pages([]))
    monkeypatch.setattr(toast, "fetch_business_days", lambda *a, **k: {})
    result = toast.sync_to_db(rid)
    assert result["ok"] is False
    assert models.get_client_data(rid, db_path=db_path)["shifts_csv"] == previous


# ── A3 #38 / MOD-LAB-6: Square role ─────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-LAB-6: Square role is read from assigned_locations.assignment_type, not the shift's job")
def test_a_square_shift_carries_the_job_title_as_its_role(db_path, monkeypatch):
    import square
    rid = _rid(db_path)
    _square_connected(db_path, rid)
    monkeypatch.setattr(square.requests, "post", lambda url, **k: _Resp(200, {"team_members": [
        {"id": "t1", "given_name": "Ann", "family_name": "B",
         "assigned_locations": {"assignment_type": "ALL_CURRENT_AND_FUTURE_LOCATIONS"}}]}))
    monkeypatch.setattr(square, "_fetch_shifts", lambda *a, **k: [
        {"team_member_id": "t1", "start_at": "2026-09-18T16:00:00Z", "end_at": "2026-09-18T22:00:00Z",
         "wage": {"title": "Server", "hourly_rate": {"amount": 1500, "currency": "USD"}}}])
    monkeypatch.setattr(square, "_fetch_daily_sales", lambda *a, **k: {})
    rows = _rows(square.build_shifts_csv(rid))
    assert rows[0]["role"] == "Server"


# ── A3 #39 / MOD-LAB-7: a Square orders page failing mid-range ──────────────

@pytest.mark.xfail(strict=True, reason="MOD-LAB-7: a 429 on a later orders page is a silent `break`, and partial sales are saved as whole days")
def test_a_square_orders_page_failure_fails_the_sync(db_path, monkeypatch):
    import square
    rid = _rid(db_path)
    _square_connected(db_path, rid)
    monkeypatch.setattr(square, "_fetch_team_members", lambda rid_: {"t1": {"name": "Ann B", "job_title": "Server"}})
    monkeypatch.setattr(square, "_fetch_shifts", lambda *a, **k: [
        {"team_member_id": "t1", "start_at": "2026-09-18T16:00:00Z", "end_at": "2026-09-18T22:00:00Z"}])
    pages = iter([_Resp(200, {"orders": [{"created_at": "2026-09-18T17:00:00Z", "total_money": {"amount": 100000}}],
                              "cursor": "c2"}),
                  _Resp(429, {"errors": [{"code": "RATE_LIMITED"}]})])
    monkeypatch.setattr(square.requests, "post", lambda url, **k: next(pages))
    assert square.sync_to_db(rid)["ok"] is False


# ── A3 #40 / MOD-LAB-8: every provider archives labor_daily_history ─────────

_POS_CSV = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes\n"
            "2026-09-18,Friday,Ann B,Server,11:00,17:00,6,6,4000,\n")


def _history_rows(db_path, rid):
    c = get_conn(db_path)
    n = c.execute("SELECT COUNT(*) FROM labor_daily_history WHERE restaurant_id=?", (rid,)).fetchone()[0]
    c.close()
    return n


def test_a_toast_sync_writes_labor_daily_history(db_path, monkeypatch):
    import toast
    rid = _rid(db_path)
    monkeypatch.setattr(toast, "build_shifts_csv", lambda rid_, days=60: _POS_CSV)
    monkeypatch.setattr("inventory_ledger.discover_menu_items", lambda *a, **k: None)
    assert toast.sync_to_db(rid)["ok"] is True
    assert _history_rows(db_path, rid) == 1


@pytest.mark.parametrize("provider", [
    pytest.param("square", marks=pytest.mark.xfail(strict=True, reason="MOD-LAB-8: square.sync_to_db never writes labor_daily_history")),
    pytest.param("clover", marks=pytest.mark.xfail(strict=True, reason="MOD-LAB-8: clover.sync_to_db never writes labor_daily_history")),
])
def test_every_pos_sync_writes_labor_daily_history(db_path, monkeypatch, provider):
    mod = __import__(provider)
    rid = _rid(db_path)
    monkeypatch.setattr(mod, "build_shifts_csv", lambda rid_, days=60: _POS_CSV)
    assert mod.sync_to_db(rid)["ok"] is True
    assert _history_rows(db_path, rid) == 1


# ── A3 #41: a failed sync keeps the previous data and records the error ─────

def test_a_failed_toast_sync_keeps_the_previous_csv_and_records_the_error(db_path, monkeypatch):
    import toast
    rid = _rid(db_path)
    previous = "date,employee,role,actual_hours,sales\n2026-07-01,Ann,Server,8,1000\n"
    models.save_client_data(rid, "shifts", previous, db_path=db_path)

    def boom(*a, **k):
        raise RuntimeError("Toast 503")
    monkeypatch.setattr(toast, "fetch_time_entries", boom)
    result = toast.sync_to_db(rid)
    assert result["ok"] is False and "503" in result["error"]
    assert models.get_client_data(rid, db_path=db_path)["shifts_csv"] == previous
    assert "503" in (models.get_restaurant(rid, db_path=db_path).toast_sync_error or "")


# ── MOD-LAB-18: a POS sync after a longer hand upload ───────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-LAB-18: save_client_data replaces shifts_csv, so a 60-day POS sync erases a year of uploaded history")
def test_a_pos_sync_keeps_uploaded_dates_outside_the_pos_window(db_path, monkeypatch):
    import toast
    rid = _rid(db_path)
    models.save_client_data(rid, "shifts", "date,employee,role,actual_hours,sales\n"
                                           "2026-01-15,Ann,Server,8,1000\n", db_path=db_path)
    monkeypatch.setattr(toast, "build_shifts_csv", lambda rid_, days=60: _POS_CSV)
    monkeypatch.setattr("inventory_ledger.discover_menu_items", lambda *a, **k: None)
    toast.sync_to_db(rid)
    assert "2026-01-15" in models.get_client_data(rid, db_path=db_path)["shifts_csv"]


# ── MOD-LAB-21: an open Toast clock-in ──────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-LAB-21: an open Toast entry (no outDate, no paidMinutes) becomes actual_hours 0.0 — a $0 shift")
def test_an_open_toast_clock_in_is_not_a_zero_hour_shift():
    import toast
    rows = toast.normalise_entries([_entry("g1", "Sam", "Close", "2026-09-21T23:00:00.000+0000",
                                           sched=("2026-09-21T23:00:00.000+0000", "2026-09-22T07:00:00.000+0000"))], {})
    assert not rows or rows[0]["actual_hours"] in (None, "")


@pytest.mark.xfail(strict=True, reason="MOD-LAB-21: an open Toast clock-in is counted as a no-show by staff_settings.reliability")
def test_an_open_toast_clock_in_is_not_a_no_show(db_path):
    import toast
    import staff_settings
    rid = _rid(db_path)
    sched = lambda d: (f"2026-09-{d:02d}T23:00:00.000+0000", f"2026-09-{d + 1:02d}T05:00:00.000+0000")
    entries = [_entry("g1", "Sam", "Close", f"2026-09-{d:02d}T23:00:00.000+0000", paid=360, sched=sched(d))
               for d in range(10, 16)]
    entries.append(_entry("g1", "Sam", "Close", "2026-09-21T23:00:00.000+0000", sched=sched(21)))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["date", "day", "employee", "role", "shift_start", "shift_end",
                                        "scheduled_hours", "actual_hours", "sales", "notes"])
    w.writeheader()
    w.writerows(toast.normalise_entries(entries, {}))
    models.save_client_data(rid, "shifts", buf.getvalue(), db_path=db_path)
    rel = staff_settings.reliability(rid, db_path=db_path)
    assert rel["Sam C."]["no_show_rate"] == 0.0


# ── A3 #42 / MOD-LAB-9: the nightly POS loop ────────────────────────────────

class _FakeProvider:
    def __init__(self, fail_for=()):
        self.synced, self.fail_for = [], set(fail_for)

    def is_connected(self, rid):
        return True

    def sync_to_db(self, rid):
        self.synced.append(rid)
        if rid in self.fail_for:
            raise RuntimeError("provider down")
        return {"ok": True, "rows": 1}


def _fake_pos(monkeypatch, provider):
    import pos
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("toast", provider))
    monkeypatch.setattr("ops.capture", lambda *a, **k: None)
    return pos


def test_one_restaurant_failing_its_pos_sync_does_not_stop_the_rest(db_path, monkeypatch):
    a, b, c = (_rid(db_path, name=n) for n in ("A", "B", "C"))
    provider = _FakeProvider(fail_for={b})
    pos = _fake_pos(monkeypatch, provider)
    results = pos.sync_all()
    assert provider.synced == [a, b, c]
    assert [r["ok"] for r in results] == [True, False, True]


@pytest.mark.xfail(strict=True, reason="MOD-LAB-9: the nightly POS sync has no billing filter; churned restaurants are still synced")
def test_the_nightly_pos_sync_skips_a_churned_restaurant(db_path, monkeypatch):
    live = _rid(db_path, name="Live")
    gone = _rid(db_path, name="Gone")
    _set(db_path, gone, billing_status="churned")
    provider = _FakeProvider()
    pos = _fake_pos(monkeypatch, provider)
    pos.sync_all()
    assert provider.synced == [live]


@pytest.mark.xfail(strict=True, reason="MOD-LAB-9: the nightly POS sync is unbounded and keeps no cursor in job_cursors")
def test_the_nightly_pos_sync_records_where_it_stopped(db_path, monkeypatch):
    for n in ("A", "B", "C"):
        _rid(db_path, name=n)
    pos = _fake_pos(monkeypatch, _FakeProvider())
    pos.sync_all()
    c = get_conn(db_path)
    keys = [r[0] for r in c.execute("SELECT key FROM job_cursors").fetchall()]
    c.close()
    assert any("pos" in k or "toast" in k for k in keys), keys


# ── A3 #49 / MOD-LAB-1: the coverage job through the real coverage_gaps ─────

def _coverage_world(db_path, monkeypatch):
    import issues
    import time_utils
    import pos
    rid = _rid(db_path)
    models.update_restaurant(rid, {"open_times_json": '{"Monday": "10:00am"}',
                                   "close_times_json": '{"Monday": "10:00pm"}'}, db_path=db_path)
    c = get_conn(db_path)
    cid = c.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) "
                    "VALUES (?, 'GM', '+15555550100', 1)", (rid,)).lastrowid
    c.commit(); c.close()
    issues.set_routing(rid, "manager", cid, db_path=db_path)
    day = date(2026, 9, 21)
    sched_csv = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                 f"{day},Monday,Dana K,Server,11:00am,10:00pm,8,\n")
    models.save_schedule_history(rid, day.isoformat(), day.isoformat(), 40, 40, 30, sched_csv, [], db_path=db_path)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: datetime(2026, 9, 21, 11, 30))
    monkeypatch.setattr(pos, "fetch_clock_ins_today", lambda rid_, d: ([], "toast"))
    return rid


def test_the_real_coverage_gaps_names_the_late_employee(db_path, monkeypatch):
    import intraday
    rid = _coverage_world(db_path, monkeypatch)
    gaps = intraday.coverage_gaps(rid, now_local=datetime(2026, 9, 21, 11, 30), db_path=db_path)
    assert [m["employee"] for m in gaps["missing"]] == ["Dana K"]


@pytest.mark.xfail(strict=True, reason="MOD-LAB-1: coverage_gaps returns scheduled as an int; run_coverage_check iterates it and crashes")
def test_the_coverage_job_opens_one_issue_for_a_late_employee(db_path, monkeypatch):
    import strategy_jobs
    import issues
    rid = _coverage_world(db_path, monkeypatch)
    captured = []
    monkeypatch.setattr("ops.capture", lambda e, **k: captured.append(repr(e)))
    assert strategy_jobs.run_coverage_check(db_path=db_path)["opened"] == 1
    assert "Dana K" in issues.list_issues(rid, db_path=db_path)[0]["title"]
    assert captured == []
