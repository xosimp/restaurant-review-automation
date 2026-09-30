"""The ticket-level POS archive (RPower endpoint audit, 9/29/26, Critical #2)."""
from datetime import date, datetime, timezone

import pytest

import models
import pos
import pos_archive
import rpower


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos_archive, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(pos, "PROVIDERS", None)
    for c in (rpower._people_cache, rpower._void_reason_cache, rpower._catalog_cache, rpower._menu_cache):
        c.clear()
    yield


def _rid(db_path, rid=1):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO restaurants (id, name, owner_email, rpower_token, rpower_cg, rpower_store_mid, timezone) "
                 "VALUES (?,?,?,?,?,?,?)", (rid, "Arch Co", "a@x.test", "tok", 1280, "1123", "America/Chicago"))
    conn.commit()
    conn.close()
    return rid


class _Fake:
    """A provider that can archive: each day returns what `days` holds."""
    def __init__(self, days, changed=()):
        self.days, self.changed, self.calls = days, set(changed), []

    def archive_rows(self, rid, day):
        self.calls.append(day.isoformat())
        return self.days.get(day.isoformat(), {"tickets": [], "lines": [], "punches": [], "max_stamp": None})

    def changed_business_dates(self, rid, since, until):
        return set(self.changed)


def _day(iso, net=100.0, stamp="2026-09-29T03:00:00", lines=2):
    return {"tickets": [{"ticket_id": f"t-{iso}", "business_date": iso, "net_sales": net, "server_id": "E1",
                         "server_name": "Dana Reyes", "guest_count": 2}],
            "lines": [{"line_id": f"l-{iso}-{i}", "ticket_id": f"t-{iso}", "business_date": iso, "kind": "sale",
                       "qty": 1, "sales": net / lines} for i in range(lines)],
            "punches": [{"punch_id": f"p-{iso}", "business_date": iso, "employee_id": "E1", "tips": 12.5}],
            "max_stamp": stamp}


def _count(db_path, table, rid=1):
    conn = models.get_conn(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE restaurant_id=?", (rid,)).fetchone()[0]
    finally:
        conn.close()


def test_a_day_is_stored_whole_and_replaced_whole(db_path, monkeypatch):
    rid = _rid(db_path)
    fake = _Fake({"2026-09-28": _day("2026-09-28", lines=3)})
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    out = pos_archive.archive_day(rid, date(2026, 9, 28))
    assert (out["tickets"], out["lines"], out["punches"], out["net_sales"]) == (1, 3, 1, 100.0)
    fake.days["2026-09-28"] = _day("2026-09-28", lines=2, stamp="2026-09-30T01:00:00")
    out = pos_archive.archive_day(rid, date(2026, 9, 28))
    assert out["restated"] is True and _count(db_path, "pos_ticket_lines") == 2   # no stale line survives
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT restated FROM pos_archive_days WHERE restaurant_id=?", (rid,)).fetchone()[0] == 1
    conn.close()


def test_a_night_stores_yesterday_restated_days_and_a_week_of_backfill(db_path, monkeypatch):
    rid = _rid(db_path)
    fake = _Fake({}, changed={"2026-09-20"})
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    now = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)
    first = pos_archive.run_for(rid, today=date(2026, 9, 30), now_utc=now)
    # Yesterday first, then the week before it; 9/20 was not archived yet, so it is not "restated".
    assert fake.calls[:8] == ["2026-09-29", "2026-09-28", "2026-09-27", "2026-09-26", "2026-09-25",
                              "2026-09-24", "2026-09-23", "2026-09-22"]
    assert first["backfill_left"] == pos_archive.BACKFILL_DAYS - 1 - pos_archive.BACKFILL_DAYS_PER_NIGHT
    fake.calls.clear()
    fake.changed = {"2026-09-25", "2026-09-30"}                 # a re-post of an archived day; today is not closed
    second = pos_archive.run_for(rid, today=date(2026, 10, 1), now_utc=now)
    assert fake.calls[0] == "2026-09-30" and "2026-09-25" in fake.calls
    assert second["ok"] and pos_archive._changes_checked_to(rid, "rpower", models.DB_PATH) == now


def test_a_restaurant_whose_pos_cannot_archive_is_skipped(db_path):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO restaurants (id, name, owner_email) VALUES (2, 'No POS', 'n@x.test')")
    conn.commit()
    conn.close()
    assert pos_archive.run_for(2, today=date(2026, 9, 30))["ok"] is False


# ── rpower.archive_rows against RPOWER's documented shapes ──────────────────

NULL = "2000-01-01T00:00:00"          # RPOWER's "no value"


def test_rpower_archive_rows_names_everything_and_drops_the_null_date(db_path, monkeypatch):
    rid = _rid(db_path)
    routes = {
        "salestype/getbycg": [{"mid": "S", "name": "Sale", "impacts_sales": 1, "type_sale": 1},
                              {"mid": "D", "name": "Discount", "impacts_sales": 1, "type_discnt": 1},
                              {"mid": "C", "name": "Comp", "impacts_sales": 1, "impacts_costs": 1, "type_comp": 1}],
        "salescategory/getbycg": [{"mid": "K1", "name": "Entrees"}],
        "menuitem/getbycg": [{"mid": "M1", "name": "Brisket`", "slscat_mid": "K1"}],
        "voidreason/getbycg": [{"mid": "R1", "name": "Error Made"}],
        "job/getbycg": [{"mid": "J1", "name": "Server PM"}],
        "employee/getbycg": [{"mid": "E1", "fname": "Dana", "lname": "Reyes"}],
        "table/getbystore": [{"mid": "T1", "name": "60", "room_mid": "RM1"}],
        "room/getbystore": [{"mid": "RM1", "name": "Bar Room", "is_bar": 1}],
        "mealtime/getbycg": [{"mid": "MT", "name": "Dinner"}],
        "profitcenter/getbycg": [{"mid": "PC", "name": "Bar"}],
        "ticketsales/getbybusinessdate": [
            {"rid": "L1", "ticket_rid": "TK", "date": "2026-09-28T00:00:00", "menuitem_mid": "M1", "slstype_mid": "S",
             "qty": 1, "sales": 24.0, "price": 24.0, "item_dttm": "2026-09-28T18:02:00", "mealtime_mid": "MT",
             "pcenter_mid": "PC", "time_stamp": "2026-09-28T23:10:00"},
            {"rid": "L2", "ticket_rid": "TK", "date": "2026-09-28T00:00:00", "slstype_mid": "D", "sales": -4.0,
             "time_stamp": "2026-09-28T23:10:00"},
            {"rid": "L3", "ticket_rid": "TK", "date": "2026-09-28T00:00:00", "menuitem_mid": "M1", "slstype_mid": "C",
             "qty": 1, "sales": 6.5, "voidrsn_mid": "R1", "mgr_mid": "E1", "time_stamp": "2026-09-28T23:11:00"}],
        "ticket/getbybusinessdate": [
            {"rid": "TK", "date": "2026-09-28T00:00:00", "ticket": 1001, "open_dttm": "2026-09-28T18:00:00",
             "close_dttm": "2026-09-28T19:05:00", "kvs_dttm": NULL, "bump_dttm": NULL, "need_dttm": NULL,
             "main_server": "E1", "table_mid": "T1", "guest_count": 3, "entree_count": 2, "bev_count": 4,
             "tip": 7.0, "mealtime_mid": "MT", "pcenter_mid": "PC", "time_stamp": "2026-09-29T00:05:00"}],
        "paymentmethod/getbycg": [{"mid": "PM1", "name": "Visa", "is_cc": 1}, {"mid": "PM2", "name": "Cash", "is_cash": 1}],
        "payoutcategory/getbycg": [{"mid": "PC1", "name": "Misc. Payout"}],
        "ticketpayment/getbybusinessdate": [
            {"rid": "Y1", "ticket_rid": "TK", "date": "2026-09-28T00:00:00", "paymeth_mid": "PM1", "paid_ticket": 20.0,
             "paid_tip": 5.0},
            {"rid": "Y2", "ticket_rid": "TK", "date": "2026-09-28T00:00:00", "paymeth_mid": "PM2", "paid_ticket": 5.0}],
        "payout/getbydaterange": [
            {"rid": "PO1", "date": "2026-09-28T00:00:00", "pocat_mid": "PC1", "net": 42.5, "mgr_mid": "E1",
             "dttm": "2026-09-28T15:00:00", "reference": "produce run"}],
        "timeclock/getbydaterange": [
            {"rid": "P1", "emp_mid": "E1", "job_mid": "J1", "in_dttm": "2026-09-28T16:00:00",
             "out_dttm": "2026-09-28T23:00:00", "reg_hours": 7, "reg_rate": 2.13, "reg_pay": 14.91, "tips_total": 88,
             "editmgr_mid": "X9", "edit_dttm": NULL, "time_stamp": "2026-09-29T04:00:00"}],
    }
    monkeypatch.setattr(rpower, "_request", lambda token, path, params=None: routes[path])
    out = rpower.archive_rows(rid, date(2026, 9, 28))
    (t,) = out["tickets"]
    assert (t["server_name"], t["table_name"], t["room_name"], t["is_bar"]) == ("Dana Reyes", "60", "Bar Room", 1)
    assert (t["fired_at"], t["bumped_at"], t["need_at"]) == (None, None, None)       # no kitchen screens here
    assert (t["net_sales"], t["mealtime"], t["profit_center"], t["guest_count"]) == (20.0, "Dinner", "Bar", 3)
    kinds = {l["line_id"]: (l["kind"], l["item_name"], l["reason"], l["loss_amount"]) for l in out["lines"]}
    assert kinds == {"L1": ("sale", "Brisket", None, None), "L2": ("discount", None, None, None),
                     "L3": ("comp", "Brisket", "Error Made", 6.5)}
    (p,) = out["punches"]
    assert (p["employee_name"], p["role"], p["pay"], p["tips"], p["edited_by"]) == ("Dana Reyes", "Server PM", 14.91, 88.0, None)
    assert out["max_stamp"] == "2026-09-29T04:00:00"
    assert [(y["method"], y["is_card"], y["amount"], y["tip"]) for y in out["payments"]] == [
        ("Visa", 1, 20.0, 5.0), ("Cash", 0, 5.0, 0.0)]
    (po,) = out["payouts"]
    assert (po["category"], po["amount"], po["manager_name"], po["reference"]) == ("Misc. Payout", 42.5, "Dana Reyes", "produce run")


# ── who can work which job (High ROI #5) ─────────────────────────────────────

def _person(db_path, rid, name, ext):
    import people
    conn = models.get_conn(db_path)
    pid = conn.execute("INSERT INTO people (restaurant_id, display_name, name_key) VALUES (?,?,?)",
                       (rid, name, people._nk(name))).lastrowid
    conn.execute("INSERT INTO person_aliases (person_id, restaurant_id, source, external_id, name_key, display_name) "
                 "VALUES (?,?,?,?,?,?)", (pid, rid, "rpower", ext, people._nk(name), name))
    conn.commit()
    conn.close()


def test_pos_jobs_become_roles_for_known_people_only_and_never_override_the_owner(db_path, monkeypatch):
    import people
    rid = _rid(db_path)
    _person(db_path, rid, "Dana Reyes", "E1")
    _person(db_path, rid, "Bo Park", "E2")
    people.add_role(rid, "Bo Park", "Server PM", primary=True, db_path=db_path)        # the owner's own
    jobs = [{"external_id": "E1", "role": "Server PM", "primary": True},
            {"external_id": "E1", "role": "Bartender PM", "primary": False},
            {"external_id": "E2", "role": "Host PM", "primary": True},                  # POS says primary
            {"external_id": "GONE", "role": "Server AM", "primary": True}]            # a former employee
    fake = type("P", (), {"fetch_employee_jobs": staticmethod(lambda r: list(jobs)),
                          "archive_rows": staticmethod(lambda r, d: None)})
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    before = _count(db_path, "change_log")
    out = pos_archive.sync_roles(rid, db_path=db_path)
    assert (out["people"], out["added"], out["removed"]) == (2, 3, 0)
    held = {(h["name"], h["role"], h["primary"], h["source"]) for h in people.held_roles(rid, db_path=db_path)}
    assert held == {("Dana Reyes", "Server PM", True, "sync"), ("Dana Reyes", "Bartender PM", False, "sync"),
                    ("Bo Park", "Server PM", True, "owner"), ("Bo Park", "Host PM", False, "sync")}
    assert _count(db_path, "change_log") == before                 # a POS mirror is not a roster change
    jobs[:] = [j for j in jobs if j["role"] != "Bartender PM"]    # the POS drops a job
    out = pos_archive.sync_roles(rid, db_path=db_path)
    assert out["removed"] == 1 and ("Dana Reyes", "Bartender PM") not in {
        (h["name"], h["role"]) for h in people.held_roles(rid, db_path=db_path)}


# ── menu prices (High ROI #7) ────────────────────────────────────────────────

def _sold_at(db_path, rid, level, n=1):
    conn = models.get_conn(db_path)
    for i in range(n):
        conn.execute("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, business_date, kind, price_level_id) "
                     "VALUES (?,?,?,?,?,?)", (rid, "rpower", f"s-{level}-{i}", date.today().isoformat(), "sale", level))
    conn.commit()
    conn.close()


def test_a_pos_price_change_is_logged_as_a_price_change_after_the_baseline(db_path, monkeypatch):
    import change_log
    rid = _rid(db_path)
    _sold_at(db_path, rid, "L1", 5)
    _sold_at(db_path, rid, "L2", 1)
    prices = [{"item_id": "M1", "item_name": "Brisket", "level_id": "L1", "level": "Level 1", "price": 18.0},
              {"item_id": "M1", "item_name": "Brisket", "level_id": "L2", "level": "Level 2", "price": 15.0},
              {"item_id": "M2", "item_name": "Wings", "level_id": "L9", "level": "Level 9", "price": 9.0}]  # never rung
    fake = type("P", (), {"fetch_menu_prices": staticmethod(lambda r: [dict(p) for p in prices]),
                          "archive_rows": staticmethod(lambda r, d: None)})
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    first = pos_archive.sync_prices(rid, db_path=db_path)
    assert first == {"ok": True, "baseline": True, "tracked": 2, "changes": 0}
    prices[0]["price"], prices[1]["price"] = 19.0, 16.0
    out = pos_archive.sync_prices(rid, db_path=db_path)
    assert (out["changes"], out["logged"]) == (2, 2)
    rows = change_log.history(rid, kinds="price", db_path=db_path)
    assert {(r["subject"], r["new_value"], r["source"]) for r in rows} == {
        ("Brisket", 19.0, "sync"), ("Brisket (Level 2)", 16.0, "sync")}


def test_a_mass_change_is_stored_but_not_logged(db_path, monkeypatch):
    import change_log
    rid = _rid(db_path)
    _sold_at(db_path, rid, "L1")
    prices = [{"item_id": f"M{i}", "item_name": f"Dish {i}", "level_id": "L1", "level": "Level 1", "price": 10.0}
              for i in range(pos_archive.MAX_PRICE_CHANGES_LOGGED + 5)]
    fake = type("P", (), {"fetch_menu_prices": staticmethod(lambda r: [dict(p) for p in prices]),
                          "archive_rows": staticmethod(lambda r, d: None)})
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    pos_archive.sync_prices(rid, db_path=db_path)
    for p in prices:
        p["price"] = 11.0
    out = pos_archive.sync_prices(rid, db_path=db_path)
    assert out["logged"] == 0 and change_log.history(rid, kinds="price", db_path=db_path) == []


def test_a_day_stored_under_an_older_layout_is_stored_again(db_path, monkeypatch):
    rid = _rid(db_path)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO pos_archive_days (restaurant_id, provider, business_date, version) VALUES (?,?,?,1)",
                 (rid, "rpower", "2026-09-28"))
    conn.commit()
    conn.close()
    assert "2026-09-28" not in pos_archive.archived_dates(rid, "rpower")
    fake = _Fake({})
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    pos_archive.run_for(rid, today=date(2026, 9, 30), now_utc=datetime(2026, 9, 30, 9, tzinfo=timezone.utc))
    assert "2026-09-28" in fake.calls and "2026-09-28" in pos_archive.archived_dates(rid, "rpower")
