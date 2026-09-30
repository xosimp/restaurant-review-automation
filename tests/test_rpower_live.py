"""RPOWER during service (RPower endpoint audit, 9/29/26, Critical #1).

A read-only check on Simple EJ's found RPOWER's above-store data posted
within minutes of each sale, so the live view (intraday pacing, the no-show
check) now reads RPOWER through the two functions pos.py looks up by name.
"""
from datetime import date

import pytest

import intraday
import models
import pos
import rpower

DAY = date(2026, 9, 29)


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(intraday, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    rpower._people_cache.clear()
    yield


def _connected(db_path, rid=1):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO restaurants (id, name, owner_email, rpower_token, rpower_cg, rpower_store_mid, "
                 "timezone) VALUES (?,?,?,?,?,?,?)",
                 (rid, "Live Co", "l@x.test", "tok", 1280, "1123", "America/Chicago"))
    conn.commit()
    conn.close()
    return rid


SALE = {"mid": "100", "name": "Sale", "impacts_sales": 1, "impacts_costs": 1, "type_sale": 1}
COMP = {"mid": "200", "name": "Comp", "impacts_sales": 0, "impacts_costs": 1, "type_comp": 1}


def _stub(monkeypatch, routes):
    calls = []

    def fake_request(token, path, params=None):
        calls.append(path)
        h = routes[path]
        return h(params or {}) if callable(h) else h
    monkeypatch.setattr(rpower, "_request", fake_request)
    return calls


def test_sales_so_far_use_the_same_revenue_rules_as_the_nights_total(db_path, monkeypatch):
    rid = _connected(db_path)
    _stub(monkeypatch, {
        "salestype/getbycg": [SALE, COMP],
        "ticketsales/getbybusinessdate": [
            {"date": "2026-09-29T00:00:00", "slstype_mid": "100", "sales": 40.0, "rid": "a"},
            {"date": "2026-09-29T00:00:00", "slstype_mid": "100", "sales": 22.5, "rid": "b"},
            {"date": "2026-09-29T00:00:00", "slstype_mid": "200", "sales": 15.0, "rid": "c"}]})
    assert rpower.fetch_sales_today(rid, DAY) == 62.5
    assert pos.fetch_sales_today(rid, DAY) == (62.5, "rpower")


def test_nothing_posted_yet_is_not_zero_sales(db_path, monkeypatch):
    rid = _connected(db_path)
    _stub(monkeypatch, {"salestype/getbycg": [SALE], "ticketsales/getbybusinessdate": []})
    with pytest.raises(rpower.RPowerError):
        rpower.fetch_sales_today(rid, DAY)
    assert intraday.capture(rid, now_local=__import__("datetime").datetime(2026, 9, 29, 10, 5))["ok"] is False


def test_the_hourly_capture_now_files_an_rpower_reading(db_path, monkeypatch):
    rid = _connected(db_path)
    _stub(monkeypatch, {"salestype/getbycg": [SALE], "ticketsales/getbybusinessdate": [
        {"date": "2026-09-29T00:00:00", "slstype_mid": "100", "sales": 1812.4, "rid": "a"}]})
    from datetime import datetime
    out = intraday.capture(rid, now_local=datetime(2026, 9, 29, 18, 10))
    assert out["ok"] and out["provider"] == "rpower" and out["net_sales"] == 1812.4


def _punch(emp, job, at, out=None, rate=15.0):
    return {"rid": f"{emp}-{at}", "emp_mid": emp, "job_mid": job, "in_dttm": at, "out_dttm": out,
            "reg_rate": rate, "reg_hours": 0, "ot_hours": 0, "dt_hours": 0}


def test_clock_ins_cover_the_business_day_by_name_and_rpower_id(db_path, monkeypatch):
    rid = _connected(db_path)
    punches = [
        _punch("E1", "J1", "2026-09-29T10:58:00"),                         # today, still on
        _punch("E2", "J2", "2026-09-29T16:02:00", "2026-09-29T22:10:00"),  # today, done
        _punch("E3", "J1", "2026-09-30T00:30:00"),                         # after midnight: still tonight
        _punch("E4", "J1", "2026-09-29T02:15:00", "2026-09-29T03:00:00"),  # before 5am: last night's
        _punch("S1", "J1", "2026-09-29T11:00:00", rate=0),                 # a station login ("To Go AM", unpaid)
    ]
    calls = _stub(monkeypatch, {
        "timeclock/getbydaterange": punches,
        "job/getbycg": [{"mid": "J1", "name": "Server"}, {"mid": "J2", "name": "Line Cook"}],
        "employee/getbycg": [{"mid": "E1", "fname": "Dana", "lname": "Reyes", "name": "Reyes, Dana"},
                             {"mid": "E2", "fname": "Bo", "lname": "Park", "name": "Park, Bo"},
                             {"mid": "E3", "fname": "Ana", "lname": "Lee", "name": "Lee, Ana"},
                             {"mid": "E4", "fname": "Cy", "lname": "Moe", "name": "Moe, Cy"},
                             {"mid": "S1", "fname": "To", "mname": "Go", "lname": "AM", "name": "To Go AM"}]})
    got = rpower.fetch_clock_ins_today(rid, DAY)
    assert [(r["employee"], r["role"], r["external_id"]) for r in got] == [
        ("Dana Reyes", "Server", "E1"), ("Bo Park", "Line Cook", "E2"), ("Ana Lee", "Server", "E3")]
    assert got[0]["clocked_in_at"] == "2026-09-29T10:58:00"
    # The staff lists are read once and reused through the evening's checks.
    rpower.fetch_clock_ins_today(rid, DAY)
    assert calls.count("employee/getbycg") == 1 and calls.count("timeclock/getbydaterange") >= 2


def test_pos_reports_rpower_as_able_to_answer_during_service(db_path):
    rid = _connected(db_path)
    assert pos.supports(rid, "fetch_sales_today") and pos.supports(rid, "fetch_clock_ins_today")
