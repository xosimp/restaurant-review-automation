"""Scale fix round 10/19/26, F2 — A3-06 / DB-09: the nightly labor read
takes settled days from the archive's punches and reads only the recent
weeks live; the CSV it builds is the one the 60-day live read built; the
live read refreshes the archive's recent punches; the shift facts and the
labor history are written by difference."""
from datetime import date, datetime, timedelta

import pytest

import models
import pos
import pos_archive
import rpower
from time_utils import restaurant_now_by_id


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos_archive, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(pos, "PROVIDERS", None)
    pos_archive._change_memo.clear()
    yield


def _rid():
    conn = models.get_conn()
    conn.execute("INSERT INTO restaurants (id, name, owner_email, rpower_token, rpower_cg, rpower_store_mid, timezone) "
                 "VALUES (1,'Labor Co','l@x.test','tok',1280,'1123','America/Chicago')")
    conn.commit()
    conn.close()
    return 1


def _punches_on(d, tweak=None):
    """Three punches whose clock-in is on calendar day `d`: lunch, a close
    past midnight, and an after-midnight one (last night's business date)."""
    s = d.isoformat()
    n = (d + timedelta(days=1)).isoformat()
    rows = [
        {"rid": f"A-{s}", "emp_mid": "E1", "job_mid": "J1", "in_dttm": f"{s}T10:00:00", "out_dttm": f"{s}T16:00:00",
         "reg_hours": 6.0, "ot_hours": 0, "dt_hours": 0, "reg_rate": 15.0, "payroll_id": "101", "break_minutes": 30},
        {"rid": f"B-{s}", "emp_mid": "E2", "job_mid": "J2", "in_dttm": f"{s}T20:00:00", "out_dttm": f"{n}T01:30:00",
         "reg_hours": 5.5, "ot_hours": 0, "dt_hours": 0, "reg_rate": 9.0, "payroll_id": "102", "break_minutes": 0},
        {"rid": f"C-{s}", "emp_mid": "E2", "job_mid": "J2", "in_dttm": f"{s}T01:00:00", "out_dttm": f"{s}T02:30:00",
         "reg_hours": 1.5, "ot_hours": 0, "dt_hours": 0, "reg_rate": 9.0, "payroll_id": "102", "break_minutes": 0}]
    if tweak:
        tweak(rows)
    return rows


def _routes(state):
    def timeclock(params):
        a = date.fromisoformat(params["startdate"])
        z = date.fromisoformat(params["enddate"])               # exclusive, as RPOWER's is
        out, d = [], a
        while d < z:
            out += _punches_on(d, state.get("tweak", {}).get(d))
            d += timedelta(days=1)
        return out
    lists = {"salestype/getbycg": [], "menuitem/getbycg": [], "salescategory/getbycg": [],
             "salesdepartment/getbycg": [], "voidreason/getbycg": [],
             "job/getbycg": [{"mid": "J1", "name": "Server"}, {"mid": "J2", "name": "Bartender"}],
             "employee/getbycg": [{"mid": "E1", "fname": "Ana", "lname": "Diaz", "payroll_id": "101"},
                                  {"mid": "E2", "fname": "Bo", "lname": "Reyes", "payroll_id": "102"}],
             "table/getbystore": [], "room/getbystore": [], "mealtime/getbycg": [], "profitcenter/getbycg": [],
             "paymentmethod/getbycg": [], "payoutcategory/getbycg": [], "ticketitem/getbybusinessdate": [],
             "ticketpayment/getbybusinessdate": [], "payout/getbydaterange": [], "ticket/getbybusinessdate": [],
             "ticketsales/getbybusinessdate": [], "closeday/getbybusinessdate": []}
    return lists, timeclock


def _stub(monkeypatch, state):
    lists, timeclock = _routes(state)
    calls = []

    def fake(token, path, params=None):
        calls.append(path)
        if path == "timeclock/getbydaterange":
            return timeclock(params or {})
        return lists[path]
    monkeypatch.setattr(rpower, "_request", fake)
    return calls


def _archive(rid, days):
    for d in days:
        pos_archive.archive_day(rid, d)


def test_settled_days_come_from_the_archive_and_the_csv_is_the_live_one(monkeypatch):
    rid = _rid()
    state = {}
    calls = _stub(monkeypatch, state)
    today = restaurant_now_by_id(rid, naive=True).date()
    _archive(rid, [today - timedelta(days=i) for i in range(16, 63)])
    conn = models.get_conn()
    conn.execute("UPDATE pos_archive_days SET punches_read_at='2099-01-01 00:00:00'")    # read once settled
    conn.commit()
    conn.close()
    rpower.clear_caches()
    monkeypatch.setattr(rpower, "fetch_business_days", lambda *a: {})     # sales are not what is measured here
    calls.clear()
    from_archive = rpower.build_shifts_csv(rid, days=60)
    reads = calls.count("timeclock/getbydaterange")
    monkeypatch.setattr(rpower, "LABOR_LIVE_DAYS", 60)
    calls.clear()
    all_live = rpower.build_shifts_csv(rid, days=60)
    assert calls.count("timeclock/getbydaterange") == 9
    assert from_archive == all_live, "the same shifts, names, roles, hours, rates and payroll ids"
    assert reads <= 3, f"{reads} timeclock reads, not 9"
    assert "101" in from_archive and "Bartender" in from_archive


def test_a_day_not_read_once_settled_is_read_live():
    rid = _rid()
    d = date.today() - timedelta(days=30)
    conn = models.get_conn()
    conn.execute("INSERT INTO pos_archive_days (restaurant_id, provider, business_date, version, punches_read_at) "
                 "VALUES (?, 'rpower', ?, 4, ?)", (rid, d.isoformat(), (d + timedelta(days=1)).isoformat() + " 05:00:00"))
    conn.execute("INSERT INTO pos_archive_days (restaurant_id, provider, business_date, version, punches_read_at) "
                 "VALUES (?, 'rpower', ?, 4, ?)", (rid, (d - timedelta(days=1)).isoformat(),
                                                   (d + timedelta(days=20)).isoformat() + " 05:00:00"))
    conn.commit()
    conn.close()
    got = pos_archive.settled_punch_dates(rid, "rpower", (d - timedelta(days=5)).isoformat(), d.isoformat(), 14)
    assert got == {(d - timedelta(days=1)).isoformat()}, "archived the morning after: not settled"


def test_the_live_read_refreshes_the_archived_punches_it_covers(monkeypatch):
    rid = _rid()
    state = {}
    _stub(monkeypatch, state)
    today = restaurant_now_by_id(rid, naive=True).date()
    day = today - timedelta(days=5)
    _archive(rid, [day])                                   # archived the next morning
    # A manager corrects the lunch punch afterwards.
    state["tweak"] = {day: lambda rows: rows[0].update(out_dttm=f"{day.isoformat()}T17:00:00", reg_hours=7.0)}
    rpower.clear_caches()
    rpower.build_shifts_csv(rid, days=60)
    conn = models.get_conn()
    row = conn.execute("SELECT reg_hours, clock_out FROM pos_punches WHERE restaurant_id=? AND punch_id=?",
                       (rid, f"A-{day.isoformat()}")).fetchone()
    stamp = conn.execute("SELECT punches_read_at FROM pos_archive_days WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.close()
    assert (row["reg_hours"], row["clock_out"]) == (7.0, f"{day.isoformat()}T17:00:00")
    assert stamp


def test_shift_facts_are_written_by_difference(db_path):
    import shift_facts
    rid = _rid()
    conn = models.get_conn()

    def fact(day, name, hours):
        return {"business_date": day, "person_id": None, "employee_name": name, "employee_key": name.lower(),
                "role": "Server", "shift_start": "10:00", "shift_end": "16:00", "scheduled_hours": None,
                "actual_hours": hours, "pay_rate": 15.0, "source": "rpower", "external_id": None, "row_json": "{}"}
    shift_facts._write_facts(conn, rid, [fact("2026-10-01", "Ana", 6.0), fact("2026-10-02", "Bo", 5.0)],
                             "2026-10-01", "2026-10-02")
    conn.commit()
    ids = {r[0]: r[1] for r in conn.execute("SELECT employee_key, id FROM shift_facts").fetchall()}
    shift_facts._write_facts(conn, rid, [fact("2026-10-01", "Ana", 6.0), fact("2026-10-02", "Bo", 5.5)],
                             "2026-10-01", "2026-10-02")
    conn.commit()
    after = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT employee_key, id, actual_hours FROM shift_facts")}
    conn.close()
    assert after["ana"][0] == ids["ana"], "an unchanged shift is left alone"
    assert after["bo"][1] == 5.5


def test_the_labor_history_save_is_the_window_and_the_days_costed_on_other_rates(monkeypatch):
    rid = _rid()
    key = models.labor_rates_key(rid)
    conn = models.get_conn()
    for d, basis in (("2026-08-01", f"{key}:rpower"), ("2026-08-02", "oldrates:rpower")):
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, basis) VALUES (?,?,?)", (rid, d, basis))
    conn.commit()
    conn.close()
    by_day = {d: {"labor_cost": 1} for d in ("2026-08-01", "2026-08-02", "2026-08-03", "2026-10-01")}
    kept = pos._history_to_save(rid, by_day, ["2026-09-20", "2026-10-01"])
    assert set(kept) == {"2026-08-02", "2026-08-03", "2026-10-01"}
