"""Ticket archives for Toast, Square and Clover, and the POS provider registry
(schedule audit 10/10/26 MARKET-9, MARKET-10). No network: every vendor call
is a stub keyed by URL."""
from datetime import date, datetime, timezone

import pytest

import models
import pos
import pos_archive


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos_archive, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(pos, "PROVIDERS", None)
    yield


def _restaurant(db_path, **cols):
    conn = models.get_conn(db_path)
    names = ["id", "name", "owner_email", "timezone"] + list(cols)
    vals = [1, "Arch Co", "a@x.test", "America/Chicago"] + list(cols.values())
    conn.execute(f"INSERT INTO restaurants ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", vals)
    conn.commit()
    conn.close()
    return 1


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.ok, self.text, self.headers = body, status, status < 400, "", {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ── Toast ────────────────────────────────────────────────────────────────────

_ORDERS = [{
    "guid": "o1", "businessDate": "20261008", "displayNumber": "41", "numberOfGuests": 3,
    "openedDate": "2026-10-09T00:15:00.000+0000", "closedDate": "2026-10-09T01:20:00.000+0000",
    "modifiedDate": "2026-10-09T01:21:00.000+0000", "server": {"guid": "E1"}, "table": {"guid": "T7"},
    "checks": [{"amount": 54.0, "taxAmount": 5.4,
                "appliedDiscounts": [{"guid": "cd1", "name": "Manager comp", "discountAmount": 6.0}],
                "selections": [
                    {"guid": "s1", "displayName": "Burger", "item": {"guid": "I1"}, "quantity": 2, "price": 36.0,
                     "preDiscountPrice": 40.0, "appliedDiscounts": [{"guid": "sd1", "name": "Happy hour",
                                                                     "discountAmount": 4.0}]},
                    {"guid": "s2", "displayName": "Fries", "item": {"guid": "I2"}, "quantity": 1, "price": 24.0},
                    {"guid": "s3", "displayName": "Shake", "item": {"guid": "I3"}, "quantity": 1, "price": 7.0,
                     "voided": True}],
                "payments": [{"guid": "p1", "type": "CREDIT", "amount": 59.4, "tipAmount": 10.0}]}]},
    {"guid": "o2", "businessDate": "20261008", "voided": True, "openedDate": "2026-10-08T23:00:00.000+0000",
     "checks": [{"amount": 12.0, "selections": [{"guid": "s9", "displayName": "Soup", "price": 12.0}]}]}]

_ENTRIES = [{"guid": "te1", "businessDate": "20261008", "employee": {"guid": "E1", "firstName": "Dana",
                                                                      "lastName": "Reyes"},
             "jobReference": {"guid": "J1", "name": "Server"}, "inDate": "2026-10-08T21:00:00.000+0000",
             "outDate": "2026-10-09T03:00:00.000+0000", "regularHours": 5.5, "overtimeHours": 0.5,
             "hourlyWage": 10.0, "declaredCashTips": 20.0, "nonCashTips": 30.0,
             "breaks": [{"inDate": "2026-10-09T00:00:00.000+0000", "outDate": "2026-10-09T00:30:00.000+0000"}]}]


def _toast(monkeypatch, db_path):
    import toast
    rid = _restaurant(db_path, toast_client_id="c", toast_client_secret="s", toast_restaurant_guid="G")
    monkeypatch.setattr(toast, "get_toast_token", lambda r: "tok")
    calls = []

    def fake(fn, url, **kw):
        calls.append((url, kw.get("params") or kw.get("json")))
        if url.endswith("/orders/v2/ordersBulk"):
            return _Resp(_ORDERS if (kw.get("params") or {}).get("page") == 1 else [])
        if url.endswith("/labor/v1/timeEntries"):
            return _Resp({"timeEntries": _ENTRIES})
        if url.endswith("/labor/v1/employees"):
            return _Resp([{"guid": "E1", "firstName": "Dana", "lastName": "Reyes"}])
        if url.endswith("/labor/v1/jobs"):
            return _Resp([{"guid": "J1", "title": "Server"}])
        if url.endswith("/config/v2/tables"):
            return _Resp([{"guid": "T7", "name": "Table 7"}])
        raise AssertionError(url)
    monkeypatch.setattr(toast, "_http", fake)
    return rid, toast, calls


def test_toast_archives_a_day_of_tickets_lines_payments_and_punches(db_path, monkeypatch):
    rid, toast, _calls = _toast(monkeypatch, db_path)
    rows = toast.archive_rows(rid, date(2026, 10, 8))
    t = {x["ticket_id"]: x for x in rows["tickets"]}
    assert set(t) == {"o1", "o2"}
    o1 = t["o1"]
    # Local wall clock (Chicago, -5h in October), the server and table named.
    assert o1["opened_at"] == "2026-10-08T19:15:00" and o1["closed_at"] == "2026-10-08T20:20:00"
    assert (o1["server_name"], o1["table_name"], o1["guest_count"]) == ("Dana Reyes", "Table 7", 3)
    assert o1["net_sales"] == 54.0 and o1["tip"] == 10.0 and o1["cancelled"] == 0
    # Kitchen times are not in Toast's Orders API: never guessed.
    assert o1["fired_at"] is None and o1["bumped_at"] is None
    assert t["o2"]["cancelled"] == 1 and t["o2"]["net_sales"] == 0.0
    o1_lines = [ln for ln in rows["lines"] if ln["ticket_id"] == "o1"]
    # Sale and discount lines sum to the check's net.
    assert round(sum(ln["sales"] for ln in o1_lines), 2) == 54.0
    void = next(ln for ln in o1_lines if ln["line_id"] == "s3")
    assert void["kind"] == "void" and void["loss_amount"] == 7.0
    assert {ln["reason"] for ln in o1_lines if ln["kind"] == "discount"} == {"Manager comp", "Happy hour"}
    p = rows["punches"][0]
    assert (p["employee_name"], p["role"], p["clock_in"]) == ("Dana Reyes", "Server", "2026-10-08T16:00:00")
    assert p["pay"] == 62.5 and p["tips"] == 50.0 and p["break_minutes"] == 30.0
    assert rows["payments"][0]["is_card"] == 1 and rows["max_stamp"].startswith("2026-10-09T01:21")


def test_a_toast_restaurant_now_archives_and_its_curve_reads_the_tickets(db_path, monkeypatch):
    rid, toast, _calls = _toast(monkeypatch, db_path)
    name, mod = pos_archive.provider_for(rid)
    assert name == "toast" and mod is toast
    out = pos_archive.archive_day(rid, date(2026, 10, 8))
    assert (out["tickets"], out["punches"]) == (2, 1)
    import schedule_economics
    conn = models.get_conn(db_path)
    try:
        by = schedule_economics._ticket_days(conn, rid, "2026-10-01", "2026-10-10")
    finally:
        conn.close()
    # The measured sales curve's first source is now this Toast night.
    assert by == {"2026-10-08": {19: 54.0}}


def test_toast_restatements_read_the_modified_window(db_path, monkeypatch):
    rid, toast, calls = _toast(monkeypatch, db_path)
    got = toast.changed_business_dates(rid, datetime(2026, 10, 9, 0, tzinfo=timezone.utc),
                                       datetime(2026, 10, 9, 12, tzinfo=timezone.utc))
    assert got == {"2026-10-08"}
    assert "startDate" in calls[0][1] and "businessDate" not in calls[0][1]


# ── Square ───────────────────────────────────────────────────────────────────

def test_square_archives_orders_and_labor_shifts(db_path, monkeypatch):
    import square
    rid = _restaurant(db_path, square_access_token="sq", square_location_id="L1")

    def fake(fn, url, **kw):
        if url.endswith("/orders/search"):
            return _Resp({"orders": [{
                "id": "SO1", "state": "COMPLETED", "created_at": "2026-10-08T23:30:00Z",
                "closed_at": "2026-10-09T00:10:00Z", "updated_at": "2026-10-09T00:10:00Z",
                "total_money": {"amount": 3240}, "total_tax_money": {"amount": 240},
                "total_tip_money": {"amount": 500}, "total_discount_money": {"amount": 300},
                "line_items": [{"uid": "a", "name": "Pizza", "quantity": "2", "gross_sales_money": {"amount": 2800},
                                "base_price_money": {"amount": 1400}}],
                "discounts": [{"uid": "d", "name": "Loyalty", "applied_money": {"amount": 300}}],
                "tenders": [{"id": "tn", "type": "CARD", "amount_money": {"amount": 3240},
                             "tip_money": {"amount": 500}}]}]})
        if url.endswith("/labor/shifts/search"):
            return _Resp({"shifts": [{"id": "SH1", "team_member_id": "TM1", "start_at": "2026-10-08T21:00:00Z",
                                      "end_at": "2026-10-09T03:00:00Z", "wage": {"title": "Cook",
                                                                                "hourly_rate": {"amount": 1800}},
                                      "breaks": [{"start_at": "2026-10-09T00:00:00Z",
                                                  "end_at": "2026-10-09T00:30:00Z", "is_paid": False}]}]})
        if url.endswith("/team-members/search"):
            return _Resp({"team_members": [{"id": "TM1", "given_name": "Ana", "family_name": "Cruz"}]})
        raise AssertionError(url)
    monkeypatch.setattr(square, "_http", fake)
    rows = square.archive_rows(rid, date(2026, 10, 8))
    t = rows["tickets"][0]
    assert t["opened_at"] == "2026-10-08T18:30:00" and t["net_sales"] == 25.0
    assert round(sum(ln["sales"] for ln in rows["lines"]), 2) == 25.0
    # No server, table or covers in Square's Orders API: None, never 0.
    assert t["guest_count"] is None and t["server_name"] is None
    p = rows["punches"][0]
    assert (p["employee_name"], p["role"], p["reg_hours"], p["pay"]) == ("Ana Cruz", "Cook", 5.5, 99.0)


# ── Clover ───────────────────────────────────────────────────────────────────

def test_clover_archives_orders_and_shifts(db_path, monkeypatch):
    import clover
    rid = _restaurant(db_path, clover_api_token="cl", clover_merchant_id="M1")
    in_ms = int(datetime(2026, 10, 8, 21, tzinfo=timezone.utc).timestamp() * 1000)

    def fake(fn, url, **kw):
        if url.endswith("/employees"):
            return _Resp({"elements": [{"id": "EM1", "name": "Lee Park", "role": "EMPLOYEE"}]})
        if url.endswith("/orders"):
            return _Resp({"elements": [{
                "id": "CO1", "state": "locked", "createdTime": in_ms + 3600000, "modifiedTime": in_ms + 7200000,
                "total": 2160, "employee": {"id": "EM1"},
                "lineItems": {"elements": [{"id": "li1", "name": "Wings", "price": 1000, "unitQty": 2000}]},
                "discounts": {"elements": [{"id": "dd", "name": "Promo", "amount": -0}]},
                "payments": {"elements": [{"id": "pp", "amount": 2160, "tipAmount": 300, "taxAmount": 160,
                                           "createdTime": in_ms + 7000000, "tender": {"label": "Credit Card"}}]}}]})
        if url.endswith("/shifts"):
            return _Resp({"elements": [{"id": "CS1", "inTime": in_ms, "outTime": in_ms + 6 * 3600000,
                                        "employee": {"id": "EM1"}}]})
        raise AssertionError(url)
    monkeypatch.setattr(clover, "_http", fake)
    rows = clover.archive_rows(rid, date(2026, 10, 8))
    t = rows["tickets"][0]
    assert (t["server_name"], t["net_sales"], t["tip"]) == ("Lee Park", 20.0, 3.0)
    assert t["closed_at"] is not None and t["guest_count"] is None
    assert rows["lines"][0]["sales"] == 20.0
    p = rows["punches"][0]
    # Clover carries no wage on a shift: pay is unknown, never invented.
    assert p["reg_hours"] == 6.0 and p["pay"] is None and p["employee_name"] == "Lee Park"


# ── the registry ─────────────────────────────────────────────────────────────

def test_every_provider_now_archives_and_the_matrix_says_who_can_write_a_schedule():
    m = pos.provider_matrix()
    assert set(m) == {"toast", "square", "clover", "rpower"}
    assert all(m[n]["archive"] and m[n]["restatements"] for n in m)
    assert m["rpower"]["schedule_push"] and m["toast"]["schedule_preview"]
    assert not m["square"]["schedule_push"] and not m["clover"]["schedule_push"]


def test_a_new_pos_plugs_in_with_one_registration_and_a_broken_one_is_left_out(monkeypatch):
    import sys
    import types
    good = types.ModuleType("fakepos_good")
    for f in pos.PROVIDER_API:
        setattr(good, f, lambda *a, **k: None)
    bad = types.ModuleType("fakepos_bad")          # no sync_to_db
    bad.is_connected = lambda r: True
    monkeypatch.setitem(sys.modules, "fakepos_good", good)
    monkeypatch.setitem(sys.modules, "fakepos_bad", bad)
    monkeypatch.setattr(pos, "PROVIDER_MODULES", dict(pos.PROVIDER_MODULES))
    pos.register_provider("spoton", "fakepos_good")
    pos.register_provider("broken", "fakepos_bad")
    pos.register_provider("missing", "no_such_module_here")
    got = pos.get_providers()
    assert got["spoton"] is good and "broken" not in got and "missing" not in got and "toast" in got
    assert pos.capabilities(good)["archive"] is False
