"""pos.fetch_day_sales / pos.fetch_day_closed — the DSR's two POS reads.

RPOWER fixtures are the documented response shapes from RPOWER's own Postman
collection (docs/integrations/rpower_core_api.postman.json: ticketsales,
ticket, menuitem, salescategory, salesdepartment, salestype, closeday),
trimmed to the fields that matter. Toast fixtures are the Orders API's
documented ordersBulk shape. No network: the transport is stubbed.

What these pin:
  * gross and net are defined once — pos.NET_DEDUCTIONS decides the
    subtraction for the day, every department, every hour and every item;
  * on RPOWER, net is exactly fetch_business_days' figure (the same lines
    through the same sales-type rules);
  * voids, refunds, tax, gratuities and unknown sales types never reach
    gross; an unknown type is counted in source_checks, never guessed;
  * a POS that cannot answer raises, never returns an empty day.
"""
import json
from datetime import date

import pytest

import models
import pos
import rpower
import toast


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0)
    yield


def _restaurant(db_path, rid=1, **kw):
    conn = models.get_conn(db_path)
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test", "timezone": "America/Chicago"}
    cols.update(kw)
    conn.execute(f"INSERT INTO restaurants ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                 tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


# ── RPOWER, in its documented shapes ────────────────────────────────────────

def _sales_type(mid, name, **flags):
    base = {"mid": mid, "cg": 1280, "name": name, "impacts_sales": 0, "impacts_costs": 0,
            "type_sale": 0, "type_comp": 0, "type_waste": 0, "type_error": 0, "type_return": 0,
            "type_refund": 0, "type_grat": 0, "type_hidgrat": 0, "type_housegrat": 0, "type_tax": 0,
            "type_fee": 0, "type_discnt": 0, "type_promo": 0}
    base.update(flags)
    return base


TYPES = [
    _sales_type("100", "Sale", impacts_sales=1, impacts_costs=1, type_sale=1),
    _sales_type("200", "Comp", impacts_costs=1, type_comp=1),
    _sales_type("300", "Gratuity", impacts_sales=1, type_grat=1),
    _sales_type("400", "Refund", type_refund=1),
    _sales_type("500", "Discount", impacts_sales=1, type_discnt=1),
]
DEPARTMENTS = [{"mid": "D1", "cg": 1280, "name": "Food"}, {"mid": "D2", "cg": 1280, "name": "Beer"},
               {"mid": "D3", "cg": 1280, "name": "Discounts"}]
CATEGORIES = [{"mid": "C1", "cg": 1280, "name": "1 Entrees", "slsdep_mid": "D1"},
              {"mid": "C2", "cg": 1280, "name": "Draft", "slsdep_mid": "D2"},
              {"mid": "C3", "cg": 1280, "name": "Discount items", "slsdep_mid": "D3"}]
MENU = [{"mid": "M1", "cg": 1280, "name": "Burger", "slscat_mid": "C1", "is_mod": 0},
        {"mid": "M2", "cg": 1280, "name": "IPA", "slscat_mid": "C2", "is_mod": 0},
        {"mid": "M3", "cg": 1280, "name": "Add Bacon", "slscat_mid": "C1", "is_mod": 1},
        {"mid": "M4", "cg": 1280, "name": "10% Off", "slscat_mid": "C3", "is_mod": 0},
        {"mid": "M5", "cg": 1280, "name": "Salad", "slscat_mid": "C1", "is_mod": 0}]
DAY = "2019-06-23"


def _ticket(rid, opened, guests, tax, cancelled=0, **kw):
    t = {"rid": rid, "cg": 1280, "store_mid": "S", "date": f"{DAY}T00:00:00", "shift": "A", "ticket": 101,
         "open_dttm": f"{DAY}T{opened}:00", "close_dttm": f"{DAY}T23:00:00", "is_cancelled": cancelled,
         "guest_count": guests, "tax": tax, "type_sale": 0, "type_discnt": 0, "type_comp": 0, "type_promo": 0}
    t.update(kw)
    return t


def _line(rid, ticket, menuitem, stype, sales, qty=1, voided=0, at="12:00"):
    return {"cg": 1280, "store_mid": "S", "date": f"{DAY}T00:00:00", "shift": "A", "ticket": 101,
            "atom": hash(rid) & 0xffff, "menuitem_mid": menuitem, "slstype_mid": stype,
            "item_dttm": f"{DAY}T{at}:00", "qty": qty, "price": 0, "sales": sales, "regular_price": 0,
            "voided": voided, "rid": rid, "ticket_rid": ticket}


TICKETS = [_ticket("t1", "11:16", 2, 3.00, type_sale=29, type_discnt=-2.9),
           _ticket("t2", "18:30", 3, 2.00, type_sale=15, type_comp=-11),
           _ticket("t3", "19:00", 4, 9.99, cancelled=1)]
LINES = [
    _line("l1", "t1", "M1", "100", 15),                 # Burger
    _line("l2", "t1", "M2", "100", 12, qty=2),          # 2 x IPA
    _line("l3", "t1", "M3", "100", 2),                  # a modifier: money, not an item
    _line("l4", "t1", "M4", "500", -2.9),               # a discount line, rung negative
    _line("l5", "t2", "M1", "100", 15),                 # Burger
    _line("l6", "t2", "M5", "200", 11),                 # a comped Salad
    _line("l7", "t2", "M2", "100", 6, voided=1),        # voided IPA
    _line("l8", "t2", "M1", "300", 5),                  # gratuity: never sales
    _line("l9", "t2", "M1", "400", -8),                 # a refund
    _line("l10", "t2", "M1", "999", 3),                 # a sales type the lookup doesn't know
]


def _stub(monkeypatch, lines=LINES, tickets=TICKETS, closeday=()):
    calls = []

    def fake_request(token, path, params=None):
        calls.append(path)
        page = (params or {}).get("pagenumber", 1)
        data = {"salestype/getbycg": TYPES, "salesdepartment/getbycg": DEPARTMENTS,
                "salescategory/getbycg": CATEGORIES, "menuitem/getbycg": MENU,
                "ticketsales/getbybusinessdate": list(lines), "ticket/getbybusinessdate": list(tickets),
                "closeday/getbybusinessdate": list(closeday)}.get(path)
        if data is None:
            raise AssertionError(f"unexpected call to {path}")
        return data if page == 1 else []
    monkeypatch.setattr(rpower, "_request", fake_request)
    return calls


def _rpower(db_path):
    return _restaurant(db_path, rpower_token="tok", rpower_cg=1280, rpower_store_mid="1123000884211712110")


def test_rpower_day_sales_gross_and_net_are_exactly_defined(db_path, monkeypatch):
    rid = _rpower(db_path)
    _stub(monkeypatch)
    data, provider = pos.fetch_day_sales(rid, date(2019, 6, 23))
    assert provider == "rpower"
    # gross = sale lines (15+12+2+15) + the comped Salad's value (11)
    assert data["gross"] == 55.0
    assert data["discounts"] == 2.9 and data["comps"] == 11.0
    # net = gross − discounts − comps
    assert data["net"] == 41.1
    assert data["voids"] == 6.0 and data["refunds"] == 8.0
    assert data["tax"] == 5.0                     # the cancelled ticket's tax is not the night's
    assert data["transactions"] == 2 and data["guests"] == 5
    assert data["net_deductions"] == ["discounts", "comps"]
    # A line whose sales type is unknown is counted aside, never guessed into sales.
    assert data["source_checks"]["unknown_type_lines"] == 1
    assert data["source_checks"]["ticket_type_sale"] == 44.0


def test_rpower_net_is_the_same_figure_every_other_surface_reads(db_path, monkeypatch):
    rid = _rpower(db_path)
    _stub(monkeypatch)
    data, _ = pos.fetch_day_sales(rid, DAY)
    assert rpower.fetch_business_days(rid, DAY, DAY) == {DAY: data["net"]}


def test_rpower_departments_hours_and_items_all_net_by_the_same_rule(db_path, monkeypatch):
    rid = _rpower(db_path)
    _stub(monkeypatch)
    data, _ = pos.fetch_day_sales(rid, DAY)
    assert data["by_department"] == {"Food": 32.0, "Beer": 12.0, "Discounts": -2.9}
    assert round(sum(data["by_department"].values()), 2) == data["net"]
    assert data["by_hour"] == {"11": 26.1, "18": 15.0}
    assert round(sum(data["by_hour"].values()), 2) == data["net"]
    items = {i["name"]: i for i in data["items"]}
    # Modifiers and discount lines are money but not items; an item only
    # ever comped was served, not sold.
    assert set(items) == {"Burger", "IPA"}
    assert items["Burger"]["qty"] == 2 and items["Burger"]["net"] == 30.0
    assert items["IPA"] == {"name": "IPA", "department": "Beer", "qty": 2.0, "net": 12.0}


def test_the_subtraction_is_one_tuple(db_path, monkeypatch):
    """The owner's definition is open (plan §11 Q2): change NET_DEDUCTIONS
    and the day, its departments and its hours all follow."""
    rid = _rpower(db_path)
    _stub(monkeypatch)
    monkeypatch.setattr(pos, "NET_DEDUCTIONS", ("discounts",))
    data, _ = pos.fetch_day_sales(rid, DAY)
    assert data["net"] == 52.1                    # comps stay in
    assert data["by_department"]["Food"] == 43.0
    assert round(sum(data["by_hour"].values()), 2) == 52.1


def test_rpower_closeday_covers_the_date_or_not(db_path, monkeypatch):
    rid = _rpower(db_path)

    def close(frm, thru):
        return {"rid": f"c{frm}{thru}", "cg": 1280, "store_mid": "S", "from_date": f"{frm}T00:00:00",
                "from_shift": "A", "thru_date": f"{thru}T00:00:00", "thru_shift": "A",
                "dttm": f"{thru}T02:58:32", "zdttm": f"{thru}T09:58:32", "time_stamp": f"{thru}T09:58:39"}
    _stub(monkeypatch, closeday=[close("2019-06-22", "2019-06-22")])
    assert pos.fetch_day_closed(rid, date(2019, 6, 23)) == (False, "rpower")
    _stub(monkeypatch, closeday=[close("2019-06-22", "2019-06-22"), close("2019-06-23", "2019-06-23")])
    assert pos.fetch_day_closed(rid, date(2019, 6, 23)) == (True, "rpower")
    # A store that skipped a close closes two days in one record.
    _stub(monkeypatch, closeday=[close("2019-06-22", "2019-06-23")])
    assert pos.fetch_day_closed(rid, date(2019, 6, 23)) == (True, "rpower")


def test_a_rejected_rpower_token_is_an_auth_error_not_a_blip(db_path, monkeypatch):
    rid = _rpower(db_path)

    def rejected(token, path, params=None):
        raise rpower.RPowerAuthError("RPOWER rejected the token (401).")
    monkeypatch.setattr(rpower, "_request", rejected)
    with pytest.raises(pos.POSAuthError):
        pos.fetch_day_sales(rid, DAY)


# ── Toast, in the Orders API's shape ────────────────────────────────────────

def _sel(name, price, cat, qty=1, voided=False, discount=0.0, pre=None, guid=None):
    s = {"guid": f"s-{name}", "item": {"guid": guid or f"i-{name}"}, "displayName": name, "quantity": qty,
         "price": price, "preDiscountPrice": pre if pre is not None else price, "voided": voided,
         "salesCategory": {"guid": cat, "entityType": "SalesCategory"} if cat else None,
         "appliedDiscounts": [{"discountAmount": discount}] if discount else []}
    return s


ORDERS = [
    {"guid": "A", "openedDate": "2026-09-22T23:15:00.000+0000", "numberOfGuests": 2, "voided": False,
     "checks": [{"amount": 45.0, "taxAmount": 3.6, "totalAmount": 48.6, "voided": False,
                 "appliedDiscounts": [{"discountAmount": 5.0}],
                 "selections": [_sel("Burger", 30.0, "cat-food", qty=2), _sel("IPA", 20.0, "cat-beer", qty=2),
                                _sel("Fries", 0.0, "cat-food", voided=True, pre=6.0)]}]},
    {"guid": "B", "openedDate": "2026-09-22T17:05:00.000+0000", "numberOfGuests": 0, "voided": False,
     "checks": [{"amount": 18.0, "taxAmount": 1.44, "voided": False, "appliedDiscounts": [],
                 "selections": [_sel("Salad", 18.0, "cat-food", discount=2.0, pre=20.0)]}]},
    {"guid": "C", "openedDate": "2026-09-22T19:00:00.000+0000", "numberOfGuests": 1, "voided": True,
     "checks": [{"amount": 9.0, "taxAmount": 0.7, "selections": [_sel("Soup", 9.0, "cat-food")]}]},
]


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _toast(db_path, monkeypatch, orders=ORDERS):
    rid = _restaurant(db_path, toast_restaurant_guid="guid-1", toast_client_id="cid", toast_client_secret="sec")
    monkeypatch.setattr(toast, "get_toast_token", lambda _rid: "tok")
    seen = []

    def fake_get(url, headers=None, params=None, timeout=None):
        assert timeout, "every outbound call names a timeout"
        seen.append(url)
        if url.endswith("/orders/v2/ordersBulk"):
            return _Resp(list(orders) if params.get("page") == 1 else [])
        if url.endswith("/config/v2/salesCategories"):
            return _Resp([{"guid": "cat-food", "name": "Food"}, {"guid": "cat-beer", "name": "Beer"}])
        raise AssertionError(url)
    monkeypatch.setattr(toast.requests, "get", fake_get)
    return rid, seen


def test_toast_day_sales_from_orders_bulk(db_path, monkeypatch):
    rid, _seen = _toast(db_path, monkeypatch)
    data, provider = pos.fetch_day_sales(rid, date(2026, 9, 22))
    assert provider == "toast"
    assert data["net"] == 63.0                    # Toast's own check.amount, after discounts, before tax
    assert data["discounts"] == 7.0 and data["gross"] == 70.0
    assert data["comps"] is None                  # Toast rings comps as discounts: unknown, not zero
    assert data["tax"] == 5.04
    assert data["voids"] == 15.0                  # a voided selection and a voided order
    assert data["transactions"] == 2 and data["guests"] == 2


def test_toast_departments_hours_and_items(db_path, monkeypatch):
    rid, _seen = _toast(db_path, monkeypatch)
    data, _ = pos.fetch_day_sales(rid, date(2026, 9, 22))
    # Category names come from the config API; the $5 check-level discount
    # is on no selection, so the departments sum to $5 more than net.
    assert data["by_department"] == {"Food": 48.0, "Beer": 20.0}
    # openedDate is UTC; the hour is the restaurant's (Chicago, CDT).
    assert data["by_hour"] == {"12": 18.0, "18": 45.0}
    items = {i["name"]: i for i in data["items"]}
    assert items["Burger"] == {"name": "Burger", "department": "Food", "qty": 2.0, "net": 30.0}
    assert items["Salad"]["net"] == 18.0 and "Fries" not in items


def test_toast_demo_mode_cannot_report_a_real_day(db_path, monkeypatch):
    rid = _restaurant(db_path, is_demo=1, toast_restaurant_guid="demo", toast_client_id="demo", toast_client_secret="demo")
    with pytest.raises(pos.POSCapabilityError):
        pos.fetch_day_sales(rid, date(2026, 9, 22))


def test_a_pos_without_a_closeday_record_says_so(db_path, monkeypatch):
    rid, _seen = _toast(db_path, monkeypatch)
    with pytest.raises(pos.POSCapabilityError):
        pos.fetch_day_closed(rid, date(2026, 9, 22))


def test_no_pos_or_a_pos_that_cannot_report_raises_rather_than_returning_an_empty_day(db_path):
    rid = _restaurant(db_path)
    with pytest.raises(pos.POSCapabilityError):
        pos.fetch_day_sales(rid, date(2026, 9, 22))
    sq = _restaurant(db_path, rid=2, square_access_token="sq", square_location_id="L1")
    if pos.connected_provider(sq)[0] == "square":
        with pytest.raises(pos.POSCapabilityError):
            pos.fetch_day_sales(sq, date(2026, 9, 22))
