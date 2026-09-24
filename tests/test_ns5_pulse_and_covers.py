"""NS5 H6 and M9 — the staffing suggestions Cavnar pushes are checked
against the same rules a schedule is. Each test replays a probe from
scratchpad/ns5/ (probe_pulse.py, probe_pulse2.py, probe_cover.py) and
failed before its fix.

H6: the pre-dinner pulse suggested sending home the only keyholder, the
closer the owner's own "stays after close" rule keeps, on any night
including holidays and booked ones, with a saving stated as fact under a
notice rule. M9: a suggested cover skipped replacement_is_legal.
"""
import datetime as dt
import json

import pytest

import auth
import intraday
import labor_replacements
import models
import schedule_engine
import schedule_rules as sr
import staff_settings as ss
import strategy_jobs
import time_off
from models import create_restaurant, Restaurant, update_restaurant, get_conn, get_restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
DAY = dt.date(2026, 9, 21)                 # a Monday, no holiday
LOCAL = dt.datetime(2026, 9, 21, 16, 30)
PULSE = {"direction": "behind", "pct": -30, "samples": 6}


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, auth, intraday, schedule_engine, sr, ss, time_off, strategy_jobs):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(sr, "DB_PATH", db_path, raising=False)
    return db_path


def _published(db, rid, csv_text, start=DAY, end=None):
    conn = get_conn(db)
    models._ensure_history_columns(conn)
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, summary_json, "
                 "published_at) VALUES (?,?,?,?,'[]',datetime('now'))",
                 (rid, start.isoformat(), (end or start).isoformat(), csv_text))
    conn.commit()
    conn.close()


def _rest(db, name, **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.lower()}@x.com"), db_path=db)
    update_restaurant(rid, dict({"hourly_rate": 15, "module_labor": 1}, **cols), db_path=db)
    return rid


def _row(day, name, role, start, end, hours):
    return f"{day.isoformat()},{day.strftime('%A')},{name},{role},{start},{end},{hours},\n"


# ── H6: the pulse cut is simulated against the rules ───────────────────

def test_the_only_keyholder_is_never_the_one_sent_home(db):
    """Probe A: Ana is the only keyholder and the latest starter."""
    rid = _rest(db, "A")
    for n in ("Bob", "Ana"):
        models.add_manual_team_member(rid, n, role="Server", db_path=db)
    ss.upsert(rid, "Ana", certifications=["keyholder", "food_handler"], db_path=db)
    sr.save_compliance(rid, {"manager_on_duty": True}, db_path=db)
    _published(db, rid, HEADER + _row(DAY, "Bob", "Server", "3:00pm", "10:00pm", 7.0)
               + _row(DAY, "Ana", "Server", "5:00pm", "11:30pm", 6.5))
    move = strategy_jobs.staffing_move(get_restaurant(rid, db), LOCAL, PULSE, db_path=db)
    assert move and move["employee"] == "Bob", move


def test_the_closer_the_stays_after_close_rule_keeps_is_not_cut(db):
    """Probe B: 'last bartender stays 60 min after close', close at midnight."""
    rid = _rest(db, "B", close_times_json=json.dumps({"Monday": "12:00am"}),
                role_close_min_json=json.dumps({"Bartender": 60}))
    for n in ("Bart1", "Bart2"):
        models.add_manual_team_member(rid, n, role="Bartender", db_path=db)
    _published(db, rid, HEADER + _row(DAY, "Bart1", "Bartender", "4:00pm", "11:00pm", 7.0)
               + _row(DAY, "Bart2", "Bartender", "6:00pm", "1:00am", 7.0))
    move = strategy_jobs.staffing_move(get_restaurant(rid, db), LOCAL, PULSE, db_path=db)
    assert move is None or move["employee"] != "Bart2", move


def test_no_cut_is_suggested_on_a_holiday(db):
    xmas_eve = dt.date(2026, 12, 24)
    rid = _rest(db, "H")
    _published(db, rid, HEADER + _row(xmas_eve, "Ana", "Server", "4:00pm", "10:00pm", 6.0)
               + _row(xmas_eve, "Bo", "Server", "5:00pm", "11:00pm", 6.0), start=xmas_eve)
    assert strategy_jobs.staffing_move(get_restaurant(rid, db), dt.datetime(2026, 12, 24, 16, 30), PULSE,
                                       db_path=db) is None


def test_no_cut_is_suggested_with_reservations_on_the_book(db):
    import demand_signals
    rid = _rest(db, "R")
    _published(db, rid, HEADER + _row(DAY, "Ana", "Server", "4:00pm", "10:00pm", 6.0)
               + _row(DAY, "Bo", "Server", "5:00pm", "11:00pm", 6.0))
    assert strategy_jobs.staffing_move(get_restaurant(rid, db), LOCAL, PULSE, db_path=db)   # a move, before
    demand_signals.save(rid, [{"date": DAY.isoformat(), "kind": "reservations", "covers": 60}], db_path=db)
    assert strategy_jobs.staffing_move(get_restaurant(rid, db), LOCAL, PULSE, db_path=db) is None


def test_the_saving_is_hedged_when_a_notice_rule_is_set(db):
    rid = _rest(db, "N")
    _published(db, rid, HEADER + _row(DAY, "Ana", "Server", "4:00pm", "10:00pm", 6.0)
               + _row(DAY, "Bo", "Server", "5:00pm", "11:00pm", 6.0))
    plain = strategy_jobs.staffing_move(get_restaurant(rid, db), LOCAL, PULSE, db_path=db)
    assert plain and not plain["pay_caveat"] and "predictability" not in plain["text"]
    sr.save_compliance(rid, {"notice_days": 14}, db_path=db)
    move = strategy_jobs.staffing_move(get_restaurant(rid, db), LOCAL, PULSE, db_path=db)
    assert move["pay_caveat"] and "before any predictability pay" in move["text"] and "check with counsel" in move["text"]


# ── M9: suggested covers pass the claim's own legality check ───────────

def test_a_minor_is_not_suggested_for_a_shift_past_the_minor_curfew(db):
    """probe_cover.py: Kid (a minor) was 'best placed to cover' Mia's
    6pm-11:30pm shift that replacement_is_legal refuses him."""
    rid = _rest(db, "Cov")
    for n in ("Ana", "Kid", "Zed", "Mia"):
        models.add_manual_team_member(rid, n, role="Server", db_path=db)
        models.set_staff_contact(rid, n, f"{n.lower()}@x.com", None, db_path=db)
    ss.upsert(rid, "Kid", is_minor=True, db_path=db)
    y = DAY - dt.timedelta(days=1)
    _published(db, rid, HEADER + _row(y, "Zed", "Server", "5:00pm", "1:30am", 8.5)
               + _row(DAY, "Mia", "Server", "6:00pm", "11:30pm", 5.5), start=y, end=DAY + dt.timedelta(days=5))
    fits = labor_replacements.for_gap(rid, "Server", DAY.strftime("%A"), exclude={"Mia"}, on_date=DAY.isoformat(),
                                      limit=5, db_path=db)
    names = [f["name"] for f in fits]
    assert "Kid" not in names and "Ana" in names, names
