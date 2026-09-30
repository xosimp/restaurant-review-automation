"""Arrivals are a clock-in lead before each person's OWN shift (owner,
9/30/26): "this is 5 minutes BEFORE EACH EMPLOYEE'S STARTING SHIFT, not
before open", and "managers don't clock in".

Before: role_arrival_json was minutes relative to the OPEN, the generator
moved any shift starting earlier than open + N, the review flagged it
(before_arrival), and the iOS sheet sent "30 = early" as +30, which the
server read as thirty minutes AFTER open. Now the value is a positive
number of minutes before the person's start, it never moves a shift, it
sets when a clock-in counts as on time, and salaried people are never
judged on a punch they don't make.
"""
import json
from datetime import date, timedelta

import pytest

import attendance
import auth
import intraday
import models
import schedule_engine
import schedule_rules as sr
import shift_facts
import staff_settings
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

DAY = date(2026, 9, 26)


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, staff_settings, intraday, auth):
        monkeypatch.setattr(mod, "get_conn", fake, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(**cols):
    rid = create_restaurant(Restaurant(name="Arrivals Co", owner_email="a@x.test", module_labor=1))
    if cols:
        update_restaurant(rid, cols)
    return rid


def _publish(rid, people):
    """people: [(name, role)] all on 4:00pm-11:00pm on DAY."""
    lines = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"]
    for n, role in people:
        lines.append(f"{DAY.isoformat()},{DAY.strftime('%A')},{n},{role},4:00pm,11:00pm,7,")
    ws = DAY - timedelta(days=DAY.weekday())
    conn = models.get_conn()
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                 "VALUES (?,?,?,?,datetime('now'))",
                 (rid, ws.isoformat(), (ws + timedelta(days=6)).isoformat(), "\n".join(lines) + "\n"))
    conn.commit()
    conn.close()


def _punches(rid, punches):
    """punches: [(name, role, "16:08")] — the POS day, final."""
    rows = [{"date": DAY.isoformat(), "day": DAY.strftime("%A"), "employee": n, "role": role,
             "shift_start": t, "shift_end": "23:00", "scheduled_hours": "7", "actual_hours": "7",
             "sales": "3000", "schedule_known": "0"} for n, role, t in punches]
    shift_facts.ingest(rid, rows, "rpower")
    conn = models.get_conn()
    conn.execute("INSERT OR REPLACE INTO labor_daily_history (restaurant_id, date, day_of_week, labor_cost, sales, "
                 "total_hours, source, provider, final) VALUES (?,?,?,?,?,?,?,?,1)",
                 (rid, DAY.isoformat(), DAY.strftime("%A"), 500, 3000, 21, "rpower", "rpower"))
    conn.commit()
    conn.close()


def _outcomes(rid):
    return {e["employee_name"]: (e["outcome"], e["minutes_late"]) for e in attendance.events(rid)}


def test_the_lead_moves_when_a_clock_in_counts_as_on_time():
    rid = _rid(role_arrival_json=json.dumps({"Server": 5}))
    _publish(rid, [("Ana B.", "Server"), ("Bo C.", "Server"), ("Cy D.", "Bartender")])
    # Due at 3:55 for servers (5 before a 4:00 start); bartenders at 4:00.
    _punches(rid, [("Ana B.", "Server", "16:04"), ("Bo C.", "Server", "16:08"), ("Cy D.", "Bartender", "16:08")])
    attendance.join_published(rid, DAY.isoformat())
    got = _outcomes(rid)
    assert got["Ana B."] == ("on_time", None)          # 9 past 3:55
    assert got["Bo C."] == ("late", 13)                # 13 past 3:55
    assert got["Cy D."] == ("on_time", None)           # no lead: 8 past 4:00


def test_a_salaried_manager_with_no_punch_is_never_a_no_show():
    rid = _rid(salaried_staff_json=json.dumps([{"name": "Anthony Abbot", "annual": 84000}]))
    _publish(rid, [("Anthony Abbot", "Manager FOH"), ("Ana B.", "Server")])
    _punches(rid, [("Ana B.", "Server", "16:00")])
    attendance.join_published(rid, DAY.isoformat())
    assert set(_outcomes(rid)) == {"Ana B."}


def test_the_live_clock_in_check_never_asks_for_a_salaried_punch(monkeypatch):
    import pos
    from datetime import datetime
    rid = _rid(salaried_staff_json=json.dumps([{"name": "Anthony Abbot", "annual": 84000}]))
    _publish(rid, [("Anthony Abbot", "Manager FOH"), ("Ana B.", "Server")])
    monkeypatch.setattr(pos, "fetch_clock_ins_today", lambda *a, **k: ([{"employee": "Ana B."}], "rpower"))
    out = intraday.coverage_gaps(rid, now_local=datetime(2026, 9, 26, 17, 0))
    assert out["available"] and out["missing"] == []


def test_arrivals_never_move_a_shift_or_flag_one():
    assert not hasattr(schedule_engine, "_enforce_arrival_time")
    assert "before_arrival" not in sr.SOFT and "before_arrival" not in sr.LABELS
    rid = _rid(role_arrival_json=json.dumps({"Line Cook": 90}), open_times_json=json.dumps({"Monday": "11:00am"}))
    week = [(date(2026, 9, 21) + timedelta(days=i)) for i in range(7)]
    c = sr.build_constraints(rid, [d.isoformat() for d in week], [d.strftime("%A") for d in week])
    row = {"date": week[0].isoformat(), "day": "Monday", "employee": "Ana", "role": "Line Cook",
           "shift_start": "7:00am", "shift_end": "3:00pm", "scheduled_hours": "8", "notes": ""}
    assert not any("arriv" in v["kind"] for v in sr.violations([row], c))
    assert "Arrival" not in sr.prompt_block(c)


def test_the_rules_screen_stores_minutes_before_the_shift_and_names_salaried_roles(monkeypatch):
    import strategy_routes
    rid = _rid(salaried_staff_json=json.dumps([{"name": "Anthony Abbot", "annual": 84000},
                                               {"name": "Andrew Lane", "annual": 55000}]))
    monkeypatch.setattr(staff_settings, "roster", lambda *a, **k: [
        {"name": "Anthony Abbot", "role": "Manager FOH"}, {"name": "Andrew Lane", "role": "Manager FOH"},
        {"name": "Ana B.", "role": "Server"}, {"name": "Gabriel Huerta", "role": "Kitchen"}])
    owner = {"id": 1, "restaurant_id": rid, "role": "client", "is_admin": False, "username": "o"}
    # A signed "before" value (the old web form) and a plain one (iOS) both
    # store as minutes before; past the ceiling is held to it.
    monkeypatch.setattr(strategy_routes, "_body", lambda: {"role_arrivals": {"Server": -5, "Kitchen": 15,
                                                                              "Host": 500}})
    payload, status = strategy_routes._do_compliance_set(owner)
    assert status == 200 and payload["role_arrivals"] == {"Server": 5, "Kitchen": 15,
                                                          "Host": attendance.CLOCK_IN_LEAD_MAX}
    assert json.loads(get_restaurant(rid).role_arrival_json)["Server"] == 5
    got, status = strategy_routes._do_compliance_get(owner)
    assert got["role_arrivals"]["Server"] == 5
    assert got["roles_without_clock_in"] == ["Manager FOH"]        # Kitchen has hourly cooks too
    assert attendance.clock_in_leads(get_restaurant(rid))["server"] == 5
