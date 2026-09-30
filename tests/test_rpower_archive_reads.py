"""The nightly readers read a closed day from the archive (9/29/26): the
same day's ticketsales were downloaded by four readers every night. For an
archived closed day each reader must return exactly what it returns from
RPOWER — its own code runs on rows rebuilt from the archive."""
from datetime import date, datetime, timedelta, timezone

import pytest

import models
import pos
import pos_archive
import rpower

DAY = date.today() - timedelta(days=10)          # closed, and past the freshness window
ISO = DAY.isoformat()
NULL = "2000-01-01T00:00:00"


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos_archive, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(pos, "PROVIDERS", None)
    pos_archive._change_memo.clear()
    yield


def _rid(db_path):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO restaurants (id, name, owner_email, rpower_token, rpower_cg, rpower_store_mid, timezone) "
                 "VALUES (1,'Reads Co','r@x.test','tok',1280,'1123','America/Chicago')")
    conn.commit()
    conn.close()
    return 1


def _routes(day_iso):
    d = f"{day_iso}T00:00:00"
    return {
        "salestype/getbycg": [{"mid": "S", "name": "Sale", "impacts_sales": 1, "impacts_costs": 1, "type_sale": 1},
                              {"mid": "D", "name": "Discount", "impacts_sales": 1, "type_discnt": 1},
                              {"mid": "C", "name": "Comp", "impacts_sales": 1, "impacts_costs": 1, "type_comp": 1},
                              {"mid": "E", "name": "Error Correct", "type_error": 1},
                              {"mid": "R", "name": "Refund", "type_refund": 1}],
        "salescategory/getbycg": [{"mid": "K1", "name": "Entrees", "slsdep_mid": "DP1"}],
        "salesdepartment/getbycg": [{"mid": "DP1", "name": "Food"}],
        "menuitem/getbycg": [{"mid": "M1", "name": "Brisket", "slscat_mid": "K1"},
                             {"mid": "M2", "name": "Wings", "slscat_mid": "K1"}],
        "voidreason/getbycg": [{"mid": "R1", "name": "Error Made"}, {"mid": "R2", "name": "Wrong Check"}],
        "job/getbycg": [{"mid": "J1", "name": "Server"}],
        "employee/getbycg": [{"mid": "E1", "fname": "Dana", "lname": "Reyes"}],
        "table/getbystore": [], "room/getbystore": [], "mealtime/getbycg": [], "profitcenter/getbycg": [],
        "paymentmethod/getbycg": [], "payoutcategory/getbycg": [], "ticketpayment/getbybusinessdate": [],
        "payout/getbydaterange": [], "timeclock/getbydaterange": [],
        "ticketsales/getbybusinessdate": [
            {"rid": "L1", "ticket_rid": "T1", "date": d, "menuitem_mid": "M1", "slstype_mid": "S", "qty": 2, "sales": 40.0,
             "price": 20.0, "regular_price": 20.0, "item_dttm": f"{day_iso}T18:05:00", "mgr_mid": "0", "voidrsn_mid": "1",
             "shift": 2, "voided": 0},
            {"rid": "L2", "ticket_rid": "T1", "date": d, "slstype_mid": "D", "qty": 1, "sales": -5.0, "voided": 0,
             "voidrsn_mid": "1", "shift": 2},
            {"rid": "L3", "ticket_rid": "T1", "date": d, "menuitem_mid": "M2", "slstype_mid": "C", "qty": 1, "sales": 9.5,
             "price": 0.0, "regular_price": 0.0, "mgr_mid": "E1", "voidrsn_mid": "R1", "shift": 2, "voided": 0},
            {"rid": "L4", "ticket_rid": "T2", "date": d, "menuitem_mid": "M2", "slstype_mid": "E", "qty": 1, "sales": 11.0,
             "voidmgr_mid": "E1", "voidrsn_mid": "R2", "shift": 3, "voided": 0},
            {"rid": "L5", "ticket_rid": "T2", "date": d, "menuitem_mid": "M1", "slstype_mid": "R", "qty": 1, "sales": -20.0,
             "mgr_mid": "E1", "voidrsn_mid": "1", "shift": 3, "voided": 0},
            {"rid": "L6", "ticket_rid": "T2", "date": d, "menuitem_mid": "M1", "slstype_mid": "S", "qty": 1, "sales": 20.0,
             "voided": 1, "voidmgr_mid": "E1", "voidrsn_mid": "R2", "shift": 3}],
        "ticket/getbybusinessdate": [
            {"rid": "T1", "date": d, "open_dttm": f"{day_iso}T18:00:00", "close_dttm": f"{day_iso}T19:00:00",
             "kvs_dttm": NULL, "bump_dttm": NULL, "guest_count": 2, "tax": 3.1, "type_sale": 40.0, "type_discnt": -5.0,
             "type_promo": 0, "type_comp": 9.5, "is_cancelled": 0},
            {"rid": "T2", "date": d, "open_dttm": NULL, "close_dttm": NULL, "guest_count": 1, "tax": 0.0,
             "type_sale": 20.0, "is_cancelled": 0}],
    }


def _stub(monkeypatch, routes):
    calls = []

    def fake(token, path, params=None):
        calls.append(path)
        return routes[path]
    monkeypatch.setattr(rpower, "_request", fake)
    return calls


def _all(rid):
    return {"days": rpower.fetch_business_days(rid, DAY, DAY),
            "selections": sorted((s["item"]["guid"], s["quantity"]) for s in rpower.fetch_order_selections(rid, DAY)),
            "losses": sorted((l["kind"], l["amount"], l["reason"], l["approver"], str(l["shift"]))
                             for l in rpower.fetch_loss_lines(rid, DAY, DAY)),
            "day_sales": rpower.fetch_day_sales(rid, DAY)}


def test_every_reader_returns_the_same_from_the_archive_without_downloading_the_day(db_path, monkeypatch):
    rid = _rid(db_path)
    calls = _stub(monkeypatch, _routes(ISO))
    monkeypatch.setenv("POS_ARCHIVE_READS", "0")
    from_pos = _all(rid)
    monkeypatch.setenv("POS_ARCHIVE_READS", "1")
    pos_archive.archive_day(rid, DAY)
    rpower.clear_caches()
    calls.clear()
    from_archive = _all(rid)
    assert from_archive == from_pos
    assert "ticketsales/getbybusinessdate" not in calls and "ticket/getbybusinessdate" not in calls


def test_a_closed_day_not_yet_archived_is_archived_on_first_ask_then_shared(db_path, monkeypatch):
    rid = _rid(db_path)
    calls = _stub(monkeypatch, _routes(ISO))
    pos_archive.archive_day(rid, DAY - timedelta(days=1))       # the store's archive has started
    calls.clear()
    rpower.fetch_business_days(rid, DAY, DAY)                    # archives DAY on the spot
    assert calls.count("ticketsales/getbybusinessdate") == 1
    calls.clear()
    rpower.fetch_order_selections(rid, DAY)
    rpower.fetch_loss_lines(rid, DAY, DAY)
    rpower.fetch_day_sales(rid, DAY)
    assert "ticketsales/getbybusinessdate" not in calls        # one download served all four


def test_a_recent_day_the_pos_changed_is_stored_again_before_it_is_read(db_path, monkeypatch):
    rid = _rid(db_path)
    recent = date.today() - timedelta(days=1)
    routes = _routes(recent.isoformat())
    calls = _stub(monkeypatch, routes)
    pos_archive.archive_day(rid, recent)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE pos_archive_days SET archived_at=datetime('now','-2 hours')")
    conn.commit()
    conn.close()
    routes["ticketsales/getbytimestamp"] = [{"date": f"{recent.isoformat()}T00:00:00",
                                             "time_stamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")}]
    routes["ticketsales/getbybusinessdate"] = routes["ticketsales/getbybusinessdate"] + [
        {"rid": "L9", "ticket_rid": "T3", "date": f"{recent.isoformat()}T00:00:00", "menuitem_mid": "M1",
         "slstype_mid": "S", "qty": 1, "sales": 100.0, "voided": 0}]           # a tab closed after the archive
    calls.clear()
    got = rpower.fetch_business_days(rid, recent, recent)
    assert got[recent.isoformat()] == 135.0 and "ticketsales/getbytimestamp" in calls


def test_the_live_read_never_asks_the_archive(db_path, monkeypatch):
    rid = _rid(db_path)
    _stub(monkeypatch, _routes(date.today().isoformat()))
    monkeypatch.setattr(pos_archive, "ready_dates", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    assert rpower.fetch_sales_today(rid, date.today()) == 35.0
