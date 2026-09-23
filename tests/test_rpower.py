"""rpower.py — the RPOWER Core API client.

Built before the real token arrived, against the response shapes RPOWER
publishes in their own Postman collection (saved at
docs/integrations/rpower_core_api.postman.json). Every fixture below is that
documented shape, so the day the token lands the only unknowns are the ones
RPOWER does not document: rate limits, non-200 responses, and which reading
of their self-contradictory pagination description is the true one.

Three of RPOWER's own documented warnings are the reason most of this file
exists, and each is a trap that would otherwise produce a confidently wrong
number rather than an error.
"""
import pytest

import models
import pos
import rpower


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    # A fresh provider registry per test so one test's stubs can't leak.
    monkeypatch.setattr(pos, "PROVIDERS", None)
    yield


def _restaurant(db_path, rid=1, **kw):
    conn = models.get_conn(db_path)
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test",
            "module_inventory": 1}
    cols.update(kw)
    keys = ",".join(cols)
    marks = ",".join("?" for _ in cols)
    conn.execute(f"INSERT INTO restaurants ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


def _connected(db_path, rid=1, **kw):
    return _restaurant(db_path, rid, rpower_token="tok", rpower_cg=1280,
                       rpower_store_mid="1123000884211712110", **kw)


# RPOWER's documented salestype shape, trimmed to the flags that matter.
def _sales_type(mid, name, **flags):
    base = {"mid": mid, "cg": 1280, "name": name, "impacts_sales": 0, "impacts_costs": 0,
            "type_sale": 0, "type_comp": 0, "type_waste": 0, "type_error": 0,
            "type_return": 0, "type_refund": 0, "type_grat": 0, "type_hidgrat": 0,
            "type_housegrat": 0, "type_tax": 0, "type_fee": 0}
    base.update(flags)
    return base


SALE = _sales_type("100", "Sale", impacts_sales=1, impacts_costs=1, type_sale=1)
COMP = _sales_type("200", "Comp", impacts_costs=1, type_comp=1)
GRAT = _sales_type("300", "Gratuity", impacts_sales=1, type_grat=1)
REFUND = _sales_type("400", "Refund", type_refund=1)


def _stub_api(monkeypatch, routes):
    """Stand in for the HTTP layer. `routes` maps an endpoint path to either a
    list (returned whole) or a callable taking the request params."""
    calls = []

    def fake_request(token, path, params=None):
        calls.append({"token": token, "path": path, "params": dict(params or {})})
        handler = routes.get(path)
        if handler is None:
            raise AssertionError(f"unexpected call to {path}")
        return handler(params or {}) if callable(handler) else handler

    monkeypatch.setattr(rpower, "_request", fake_request)
    return calls


# ── connection state ────────────────────────────────────────────────────────

def test_a_token_alone_is_not_a_connection(db_path):
    """cg and store_mid come back from /store/get and every subsequent call
    needs both. Treating a pasted-but-unverified token as connected is how a
    sync job starts failing silently every night."""
    rid = _restaurant(db_path, rpower_token="tok")
    assert rpower.has_token(rid) is True
    assert rpower.is_connected(rid) is False
    status = rpower.get_connection_status(rid)
    assert status["state"] == "needs_bootstrap"


def test_a_fully_bound_restaurant_is_connected(db_path):
    rid = _connected(db_path)
    assert rpower.is_connected(rid) is True
    assert rpower.get_connection_status(rid)["state"] == "connected"


def test_no_token_reports_not_configured(db_path):
    rid = _restaurant(db_path)
    assert rpower.is_connected(rid) is False
    assert rpower.get_connection_status(rid)["state"] == "not_configured"


# ── bootstrap ───────────────────────────────────────────────────────────────

_STORE = {"mid": "1123000884211712110", "cg": 1280, "name": "Simple EJ's",
          "serial_number": 1280, "timezone": "America/Chicago",
          "week_dow": 1, "ot_dow": 1, "open_days": "NYYYYYY"}


def test_bootstrap_binds_a_single_store_and_adopts_its_settings(db_path, monkeypatch):
    """RPOWER already knows the timezone and the payroll-week anchor — the two
    settings an admin was being asked to type in by hand."""
    rid = _restaurant(db_path, rpower_token="tok")
    _stub_api(monkeypatch, {"store/get": [_STORE]})
    out = rpower.bootstrap(rid)
    assert out["ok"] is True
    r = models.get_restaurant(rid)
    assert r.rpower_cg == 1280
    assert r.rpower_store_mid == "1123000884211712110"
    assert r.rpower_store_name == "Simple EJ's"
    assert r.rpower_verified_at
    assert r.timezone == "America/Chicago"
    # RPOWER's ot_dow is 1-based from Monday; Cavnar's week_start_day is
    # Python's weekday(), 0-based from Monday. Off by one, and silently off
    # by one is a payroll week starting on the wrong day.
    assert r.week_start_day == 0, "RPOWER ot_dow 1 (Monday) is Cavnar week_start_day 0"


def test_bootstrap_refuses_to_guess_between_several_stores(db_path, monkeypatch):
    """Silently binding a restaurant to the wrong store produces a module full
    of somebody else's numbers that looks entirely normal."""
    rid = _restaurant(db_path, rpower_token="tok")
    other = dict(_STORE, mid="999", cg=99, name="Other Concept")
    _stub_api(monkeypatch, {"store/get": [_STORE, other]})
    out = rpower.bootstrap(rid)
    assert out["ok"] is False
    assert out["needs_choice"] is True
    assert len(out["stores"]) == 2
    assert models.get_restaurant(rid).rpower_store_mid is None


def test_bootstrap_binds_the_store_that_was_chosen(db_path, monkeypatch):
    rid = _restaurant(db_path, rpower_token="tok")
    other = dict(_STORE, mid="999", cg=99, name="Other Concept")
    _stub_api(monkeypatch, {"store/get": [_STORE, other]})
    out = rpower.bootstrap(rid, store_mid="999")
    assert out["ok"] is True
    assert models.get_restaurant(rid).rpower_cg == 99


def test_bootstrap_never_overrides_a_timezone_someone_chose(db_path, monkeypatch):
    """An operator's explicit setting outranks the POS."""
    rid = _restaurant(db_path, rpower_token="tok", timezone="America/New_York")
    _stub_api(monkeypatch, {"store/get": [_STORE]})
    rpower.bootstrap(rid)
    assert models.get_restaurant(rid).timezone == "America/New_York"


def test_a_token_with_no_stores_is_reported_as_such(db_path, monkeypatch):
    _stub_api(monkeypatch, {"store/get": []})
    out = rpower.test_token("tok")
    assert out["ok"] is False
    assert "not authorised for any store" in out["error"]


# ── net sales: the flags that separate revenue from activity ───────────────

def _ticketsale(mid, sales, qty=1, day="2026-09-10", voided=0, menuitem="item-a"):
    return {"cg": 1280, "date": f"{day}T00:00:00", "slstype_mid": mid,
            "menuitem_mid": menuitem, "qty": qty, "sales": sales, "voided": voided,
            "rid": f"r{mid}{sales}{qty}{day}{menuitem}{voided}"}


def test_net_sales_excludes_comps_gratuities_refunds_and_voids(db_path, monkeypatch):
    """RPOWER models each sales type with impacts_sales plus explicit comp /
    waste / refund flags, and a ticket line carries the type it was rung
    under. Summing sales without consulting them returns gross activity, and
    every food cost percentage computed against it is wrong by whatever the
    restaurant comps in a week."""
    rid = _connected(db_path)
    _stub_api(monkeypatch, {
        "salestype/getbycg": [SALE, COMP, GRAT, REFUND],
        "ticketsales/getbybusinessdate": [
            _ticketsale("100", 100.0),            # real sale
            _ticketsale("200", 40.0),             # comp — costs money, earns none
            _ticketsale("300", 18.0),             # gratuity — not revenue
            _ticketsale("400", 25.0),             # refund
            _ticketsale("100", 60.0, voided=1),   # voided sale
        ],
    })
    out = rpower.fetch_business_days(rid, "2026-09-10", "2026-09-10")
    assert out == {"2026-09-10": 100.0}


def test_an_unknown_sales_type_is_not_assumed_to_be_revenue(db_path, monkeypatch):
    """Counting it in would inflate sales, which is the direction that makes
    food cost % look better than it is."""
    rid = _connected(db_path)
    _stub_api(monkeypatch, {
        "salestype/getbycg": [SALE],
        "ticketsales/getbybusinessdate": [_ticketsale("999", 500.0)],
    })
    assert rpower.fetch_business_days(rid, "2026-09-10", "2026-09-10") == {}


def test_net_sales_sums_per_business_date(db_path, monkeypatch):
    rid = _connected(db_path)
    _stub_api(monkeypatch, {
        "salestype/getbycg": [SALE],
        "ticketsales/getbybusinessdate": [
            _ticketsale("100", 100.0, day="2026-09-10"),
            _ticketsale("100", 50.0, day="2026-09-10", menuitem="item-b"),
            _ticketsale("100", 75.0, day="2026-09-11"),
        ],
    })
    out = rpower.fetch_business_days(rid, "2026-09-10", "2026-09-11")
    assert out == {"2026-09-10": 150.0, "2026-09-11": 75.0}


# ── item-level sales: the depletion feed ───────────────────────────────────

def test_selections_read_ticketsales_not_ticketitem(db_path, monkeypatch):
    """RPOWER states plainly that ticket items are for reconstructing a
    receipt and must not be used for totals — their modifier rows would count
    a burger with four modifiers as five items sold."""
    import inspect
    src = inspect.getsource(rpower.fetch_order_selections)
    assert "ticketsales/getbybusinessdate" in src
    assert "ticketitem" not in src.replace("ticketitem's", "").replace("`ticketitem`", "")


def test_a_comped_plate_still_depletes_stock(db_path, monkeypatch):
    """A comp earns nothing but the food still left the kitchen. Depletion
    deliberately keeps what net sales drops."""
    rid = _connected(db_path)
    _stub_api(monkeypatch, {
        "salestype/getbycg": [SALE, COMP, REFUND],
        "ticketsales/getbybusinessdate": [
            _ticketsale("100", 20.0, qty=2, menuitem="burger"),
            _ticketsale("200", 0.0, qty=1, menuitem="burger"),   # comped burger
            _ticketsale("400", 0.0, qty=3, menuitem="burger"),   # refunded — never served
        ],
    })
    out = rpower.fetch_order_selections(rid, "2026-09-10")
    assert out == [{"item": {"guid": "burger"}, "quantity": 3.0}], \
        "2 sold + 1 comped = 3 depleted; the refund is excluded"


def test_voided_lines_never_deplete(db_path, monkeypatch):
    rid = _connected(db_path)
    _stub_api(monkeypatch, {
        "salestype/getbycg": [SALE],
        "ticketsales/getbybusinessdate": [
            _ticketsale("100", 20.0, qty=2, menuitem="burger"),
            _ticketsale("100", 20.0, qty=9, menuitem="burger", voided=1),
        ],
    })
    assert rpower.fetch_order_selections(rid, "2026-09-10") == [
        {"item": {"guid": "burger"}, "quantity": 2.0}]


def test_selections_match_the_shape_the_ledger_already_reads(db_path, monkeypatch):
    """inventory_ledger.compute_daily_depletion reads exactly this shape, so
    matching it means recipe depletion works for RPOWER with no change there."""
    rid = _connected(db_path)
    _stub_api(monkeypatch, {
        "salestype/getbycg": [SALE],
        "ticketsales/getbybusinessdate": [_ticketsale("100", 20.0, qty=2, menuitem="g1")],
    })
    rows = rpower.fetch_order_selections(rid, "2026-09-10")
    assert rows and set(rows[0]) == {"item", "quantity"}
    assert set(rows[0]["item"]) == {"guid"}


# ── pagination: the trap RPOWER's own docs set ─────────────────────────────

def test_omitting_pagenumber_is_never_relied_on(db_path, monkeypatch):
    """RPOWER: "If this parameter is not specified, you will only get back the
    first 1000 records." Silently. Every paged read must send one."""
    rid = _connected(db_path)
    calls = _stub_api(monkeypatch, {
        "salestype/getbycg": [SALE],
        "ticketsales/getbybusinessdate": [_ticketsale("100", 5.0)],
    })
    rpower.fetch_business_days(rid, "2026-09-10", "2026-09-10")
    assert all("pagenumber" in c["params"] for c in calls), \
        "a request without pagenumber is silently capped at 1000 rows"


def test_paging_walks_until_a_short_page(db_path, monkeypatch):
    rid = _connected(db_path)
    full = [_ticketsale("100", 1.0, menuitem=f"i{n}") for n in range(rpower.PAGE_SIZE)]
    tail = [_ticketsale("100", 1.0, menuitem="tail")]

    def handler(params):
        return {1: full, 2: tail}.get(int(params.get("pagenumber", 1)), [])

    _stub_api(monkeypatch, {"salestype/getbycg": [SALE],
                            "ticketsales/getbybusinessdate": handler})
    out = rpower.fetch_business_days(rid, "2026-09-10", "2026-09-10")
    assert out == {"2026-09-10": float(rpower.PAGE_SIZE + 1)}


def test_an_overlapping_page_is_deduplicated_not_double_counted(db_path, monkeypatch):
    """RPOWER's description of page 2 as "records 2000-2999" skips 1000-1999,
    so either the prose or the API is wrong. Rows are keyed rather than
    trusted, so an off-by-one-page server cannot double a day's sales."""
    rid = _connected(db_path)
    page1 = [_ticketsale("100", 1.0, menuitem=f"i{n}") for n in range(rpower.PAGE_SIZE)]
    # Page 2 repeats the last 500 of page 1 and adds 500 genuinely new rows.
    page2 = page1[-500:] + [_ticketsale("100", 1.0, menuitem=f"n{n}") for n in range(500)]

    def handler(params):
        return {1: page1, 2: page2}.get(int(params.get("pagenumber", 1)), [])

    _stub_api(monkeypatch, {"salestype/getbycg": [SALE],
                            "ticketsales/getbybusinessdate": handler})
    out = rpower.fetch_business_days(rid, "2026-09-10", "2026-09-10")
    assert out == {"2026-09-10": float(rpower.PAGE_SIZE + 500)}


def test_a_fully_repeating_page_stops_rather_than_looping(db_path, monkeypatch):
    """A server that ignores pagenumber would otherwise page forever."""
    rid = _connected(db_path)
    page = [_ticketsale("100", 1.0, menuitem=f"i{n}") for n in range(rpower.PAGE_SIZE)]
    _stub_api(monkeypatch, {"salestype/getbycg": [SALE],
                            "ticketsales/getbybusinessdate": lambda p: page})
    out = rpower.fetch_business_days(rid, "2026-09-10", "2026-09-10")
    assert out == {"2026-09-10": float(rpower.PAGE_SIZE)}


# ── business date, never time_stamp ────────────────────────────────────────

def test_every_analytic_read_uses_a_business_date_endpoint():
    """RPOWER: time_stamp "will be changed any time data is reposted from
    RPOWER at the store... rely on date values instead." The ByTimeStamp
    variants are legitimate as a sync cursor and must never back a period."""
    import inspect
    for fn in (rpower.fetch_business_days, rpower.fetch_order_selections,
               rpower.fetch_time_entries):
        src = inspect.getsource(fn)
        assert "getbytimestamp" not in src.lower(), \
            f"{fn.__name__} windows on time_stamp, which is a write time not a business time"


def test_dates_are_sent_without_a_time_component():
    assert rpower._d("2026-09-10T00:00:00") == "2026-09-10"
    from datetime import date as _date
    assert rpower._d(_date(2026, 9, 10)) == "2026-09-10"


# ── labor ───────────────────────────────────────────────────────────────────

def _punch(**kw):
    base = {"in_dttm": "2026-09-10T08:00:00", "out_dttm": "2026-09-10T16:00:00",
            "payroll_id": "20CY2G", "job_id": "COOK", "reg_hours": 7.5,
            "ot_hours": 0, "dt_hours": 0, "break_minutes": 30}
    base.update(kw)
    return base


def test_labor_rows_use_rpowers_own_payroll_hours(db_path):
    """RPOWER's hours come from the engine that actually pays these people. A
    derived number that disagrees with the paycheck is worse than none."""
    rows = rpower.normalise_entries([_punch(reg_hours=7.5, ot_hours=1.5)],
                                    {"2026-09-10": 4200.0})
    assert len(rows) == 1
    r = rows[0]
    assert r["actual_hours"] == 9.0, "reg + ot + dt, not wall clock"
    assert r["role"] == "COOK"
    assert r["employee"] == "20CY2G"
    assert r["day"] == "Thursday"
    assert r["sales"] == 4200.0
    assert "OT 1.5h" in r["notes"]
    assert "break 30m" in r["notes"]


def test_an_open_shift_is_skipped_not_counted_as_zero(db_path):
    """A punch with no out time is still running. Including it as a zero-hour
    shift would drag every average down."""
    assert rpower.normalise_entries([_punch(out_dttm=None)], {}) == []


def test_hours_fall_back_to_the_clock_only_when_payroll_reported_none(db_path):
    rows = rpower.normalise_entries(
        [_punch(reg_hours=0, ot_hours=0, dt_hours=0,
                in_dttm="2026-09-10T08:00:00", out_dttm="2026-09-10T12:00:00")], {})
    assert rows[0]["actual_hours"] == 4.0


def test_scheduled_hours_is_never_invented(db_path):
    """RPOWER's timeclock is what was WORKED and carries no schedule. A
    fabricated variance is worse than none."""
    rows = rpower.normalise_entries([_punch(reg_hours=6)], {})
    assert rows[0]["scheduled_hours"] == rows[0]["actual_hours"]


def test_the_shifts_csv_matches_what_labor_already_parses(db_path, monkeypatch):
    rid = _connected(db_path)
    monkeypatch.setattr(rpower, "fetch_time_entries", lambda r, s, e: [_punch()])
    monkeypatch.setattr(rpower, "fetch_business_days", lambda r, s, e: {"2026-09-10": 4200.0})
    csv_str = rpower.build_shifts_csv(rid, days=7)
    header = csv_str.splitlines()[0]
    assert header == ("date,day,employee,role,shift_start,shift_end,"
                      "scheduled_hours,actual_hours,sales,notes")


# ── pos.py integration ──────────────────────────────────────────────────────

def test_rpower_is_registered_as_a_provider():
    assert "rpower" in pos.get_providers()


def test_pos_routes_data_reads_to_the_connected_provider(db_path, monkeypatch):
    rid = _connected(db_path)
    _stub_api(monkeypatch, {
        "salestype/getbycg": [SALE],
        "ticketsales/getbybusinessdate": [_ticketsale("100", 90.0)],
    })
    for other in ("toast", "square", "clover"):
        monkeypatch.setattr(f"{other}.is_connected", lambda r: False)
    days, provider = pos.fetch_business_days(rid, "2026-09-10", "2026-09-10")
    assert provider == "rpower"
    assert days == {"2026-09-10": 90.0}


def test_a_provider_without_item_detail_raises_rather_than_returning_empty(db_path, monkeypatch):
    """An empty selection list looks identical to "sold nothing", which would
    zero every ingredient's usage and quietly empty the reorder list."""
    rid = _restaurant(db_path, square_access_token="t", square_location_id="l")
    for other in ("toast", "clover", "rpower"):
        monkeypatch.setattr(f"{other}.is_connected", lambda r: False)
    monkeypatch.setattr("square.is_connected", lambda r: True)
    with pytest.raises(pos.POSCapabilityError, match="item-level"):
        pos.fetch_order_selections(rid, "2026-09-10")


def test_food_cost_percent_is_no_longer_toast_only(db_path, monkeypatch):
    """cogs.net_sales_in_window imported toast directly, so a Square, Clover
    or RPOWER restaurant was told "no POS connected" while pos.py knew
    perfectly well which POS they were on — and the one number the module is
    named after could never be computed for them."""
    import cogs
    rid = _connected(db_path)
    _stub_api(monkeypatch, {
        "salestype/getbycg": [SALE],
        "ticketsales/getbybusinessdate": [_ticketsale("100", 1000.0)],
    })
    for other in ("toast", "square", "clover"):
        monkeypatch.setattr(f"{other}.is_connected", lambda r: False)
    from datetime import date
    total, why = cogs.net_sales_in_window(rid, date(2026, 9, 10), date(2026, 9, 10))
    assert why is None
    assert total == 1000.0


def test_no_pos_still_reports_unknown_never_zero(db_path, monkeypatch):
    import cogs
    rid = _restaurant(db_path)
    for other in ("toast", "square", "clover", "rpower"):
        monkeypatch.setattr(f"{other}.is_connected", lambda r: False)
    from datetime import date
    total, why = cogs.net_sales_in_window(rid, date(2026, 9, 10), date(2026, 9, 10))
    assert total is None
    assert "no POS connected" in why


# ── schedule push ───────────────────────────────────────────────────────────

def test_push_groups_shifts_by_payroll_id(db_path, monkeypatch):
    rid = _connected(db_path)
    captured = {}

    class _Resp:
        ok, status_code, text = True, 200, "{}"

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"], captured["body"] = url, json
        return _Resp()

    monkeypatch.setattr("requests.post", fake_post)
    out = rpower.push_labor_schedule(rid, [
        {"employee_payroll_id": "A1", "job": "Bartender",
         "start": "2026-09-15T17:00:00", "end": "2026-09-15T21:45:00"},
        {"employee_payroll_id": "A1", "job": "Bartender",
         "start": "2026-09-16T17:00:00", "end": "2026-09-16T21:45:00"},
        {"employee_payroll_id": "B2", "job": "Cook",
         "start": "2026-09-15T09:00:00", "end": "2026-09-15T17:00:00"},
    ])
    assert out["ok"] is True
    assert out["employees"] == 2 and out["shifts"] == 3
    body = captured["body"]
    assert body["storeMid"] == "1123000884211712110"
    a1 = next(s for s in body["schedules"] if s["payrollid"] == "A1")
    assert len(a1["schedules"]) == 2
    assert a1["schedules"][0] == {"inTime": "2026-09-15T17:00:00",
                                  "jobCode": "Bartender",
                                  "outTime": "2026-09-15T21:45:00"}


def test_a_shift_with_no_payroll_id_is_reported_not_dropped(db_path, monkeypatch):
    """An employee with no payroll id cannot be matched to anyone at the
    store; pushing them under a blank id would attach hours to nobody."""
    rid = _connected(db_path)

    class _Resp:
        ok, status_code, text = True, 200, "{}"
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp())
    out = rpower.push_labor_schedule(rid, [
        {"employee_payroll_id": "A1", "job": "Cook",
         "start": "2026-09-15T09:00:00", "end": "2026-09-15T17:00:00"},
        {"employee": "New Hire", "job": "Cook",
         "start": "2026-09-15T09:00:00", "end": "2026-09-15T17:00:00"},
    ])
    assert out["ok"] is True
    assert len(out["skipped"]) == 1
    assert out["skipped"][0]["employee"] == "New Hire"


def test_a_refused_push_says_the_feature_needs_enabling(db_path, monkeypatch):
    """RPOWER: "Labor schedule push is not enabled by default." A 403 here is
    a different problem from a bad token and has a different fix."""
    rid = _connected(db_path)

    class _Resp:
        ok, status_code, text = False, 403, "forbidden"
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp())
    out = rpower.push_labor_schedule(rid, [
        {"employee_payroll_id": "A1", "job": "Cook",
         "start": "2026-09-15T09:00:00", "end": "2026-09-15T17:00:00"}])
    assert out["ok"] is False
    assert "integrations@rpower.com" in out["error"]


# ── failure handling for the responses RPOWER never documents ──────────────

def test_a_rejected_token_raises_a_distinct_error(db_path, monkeypatch):
    """RPOWER documents only 200s across all 120 endpoints. A bad token must
    not be retried on a loop — it is bad on the third attempt too."""
    class _Resp:
        status_code, ok, text = 401, False, "unauthorized"
        headers = {}
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0)
    with pytest.raises(rpower.RPowerAuthError):
        rpower._request("bad-token", "store/get")


def test_a_server_error_is_retried_then_reported(db_path, monkeypatch):
    attempts = []

    class _Resp:
        status_code, ok, text = 503, False, "unavailable"
        headers = {}

    def fake_get(*a, **k):
        attempts.append(1)
        return _Resp()

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0)
    monkeypatch.setattr(rpower, "RETRY_BACKOFF", 1.0)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(rpower.RPowerError):
        rpower._request("tok", "store/get")
    assert len(attempts) == rpower.MAX_RETRIES


def test_a_non_json_body_is_an_error_not_a_guess(db_path, monkeypatch):
    class _Resp:
        status_code, ok, text = 200, True, "<html>maintenance</html>"
        headers = {}
        def json(self):
            raise ValueError("not json")
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0)
    with pytest.raises(rpower.RPowerError, match="non-JSON"):
        rpower._request("tok", "store/get")


def test_sync_records_an_auth_failure_against_the_restaurant(db_path, monkeypatch):
    rid = _connected(db_path)
    monkeypatch.setattr(rpower, "build_shifts_csv",
                        lambda r, days=60: (_ for _ in ()).throw(
                            rpower.RPowerAuthError("token rejected")))
    out = rpower.sync_to_db(rid)
    assert out["ok"] is False and out["auth"] is True
    assert "token rejected" in models.get_restaurant(rid).rpower_sync_error


def test_the_base_url_is_https(db_path):
    """RPOWER's intro links these endpoints over http://. A bearer token over
    plaintext is not acceptable regardless of what their docs link."""
    assert rpower.BASE_URL.startswith("https://")


# ── long ranges ─────────────────────────────────────────────────────────────

def test_a_long_range_is_chunked(db_path):
    """A year-long backfill in one call would be one enormous paged read
    against an API with no documented rate limit or timeout behaviour."""
    from datetime import date
    chunks = list(rpower._chunk_range(date(2026, 1, 1), date(2026, 12, 31)))
    assert len(chunks) == 12
    assert chunks[0][0] == date(2026, 1, 1)
    assert chunks[-1][1] == date(2026, 12, 31)
    # Contiguous, no gaps and no overlap.
    for (a_start, a_end), (b_start, _b_end) in zip(chunks, chunks[1:]):
        assert (b_start - a_end).days == 1


def test_a_backwards_range_yields_nothing(db_path):
    from datetime import date
    assert list(rpower._chunk_range(date(2026, 5, 1), date(2026, 4, 1))) == []


def test_the_owner_card_syncs_and_disconnects_but_never_takes_a_token():
    """The account card's RPower row is the shared POS card: Sync and a
    principal-only Disconnect are client routes; saving a token stays
    admin-only, because RPOWER issues it to Cavnar."""
    import rpower_routes
    src = open(rpower_routes.__file__).read()
    assert '"/api/rpower/sync", methods=["POST"]' in src
    assert '"/api/rpower/disconnect", methods=["POST"]' in src
    assert 'principal_only(current_user, "the RPower connection")' in src
    assert "/api/rpower/save" not in src and "/admin/rpower/save/" in src
