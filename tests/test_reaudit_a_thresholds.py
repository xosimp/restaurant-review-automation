"""Thresholds every surface agrees on (re-audit A-2, A-3, A-12, A-25, A-29).

The rating alert compared Google's lifetime rating with a floor derived
from recent reviews and called it "your threshold"; the labor alert and the
manager's issue measured against an eight-week band while Home measured
against 30; every labor figure averaged the whole accumulated shifts file;
and Home used `>` where the alert used `>=`. Nothing is sent: SMS, email and
push are stubs.
"""
import os
import re
import sys
from datetime import date, timedelta

import pytest

import auth
import models
import notify
import push
import webhooks
from models import Restaurant, create_restaurant, get_conn, save_labor_snapshot, update_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, notify, auth, push, webhooks):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    auth.init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    webhooks.init_webhooks(db_path)
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)


@pytest.fixture
def sent(monkeypatch):
    out = {"sms": [], "email": [], "push": []}
    monkeypatch.setattr(notify, "send_sms", lambda to, msg, use_case="alert": out["sms"].append(msg) or True)
    monkeypatch.setattr(notify, "_send_alert_email",
                        lambda to, subject, html, restaurant_id=None: out["email"].append(subject) or True)
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: out["push"].append(a))
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    return out


def _rid(db_path, **kw):
    kw.setdefault("name", "Trust Co")
    kw.setdefault("owner_email", "o@x.test")
    kw.setdefault("owner_phone", "+15555550100")
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _log(db_path, rid, alert_type):
    conn = get_conn(db_path)
    rows = conn.execute("SELECT id FROM alert_log WHERE restaurant_id=? AND alert_type=?",
                        (rid, alert_type)).fetchall()
    conn.close()
    return rows


def _raw_set(db_path, rid, **cols):
    conn = get_conn(db_path)
    for k, v in cols.items():
        conn.execute(f"UPDATE restaurants SET {k}=? WHERE id=?", (v, rid))
    conn.commit()
    conn.close()


# ── A-2: the rating floor is the owner's, compared like with like ────────────

def test_an_improving_restaurant_is_not_told_its_rating_dropped(db_path, sent, monkeypatch):
    """Recent reviews average 4.9 (a derived floor of 4.7); Google's lifetime
    rating is 4.5. Nothing dropped, and the owner's floor is 4.0."""
    import metrics
    rid = _rid(db_path)
    update_restaurant(rid, {"alert_rating_threshold": 1, "urgent_via_sms": 1}, db_path=db_path)
    _raw_set(db_path, rid, gbp_rating=4.5, alert_rating_floor=None)
    monkeypatch.setattr(metrics, "trailing", lambda r, key, **k: {"value": 4.9})
    notify.check_daily_alerts(db_path=db_path)
    assert _log(db_path, rid, "rating_threshold") == []


def test_the_owners_floor_still_alerts(db_path, sent, monkeypatch):
    raised = []
    monkeypatch.setattr(notify, "raise_alert", lambda rid_, t, sms, subj, **k: raised.append(sms))
    rid = _rid(db_path)
    update_restaurant(rid, {"alert_rating_threshold": 1, "urgent_via_sms": 1, "alert_rating_floor": 4.6},
                      db_path=db_path)
    _raw_set(db_path, rid, gbp_rating=4.5)
    notify.check_daily_alerts(db_path=db_path)
    assert len(raised) == 1 and "4.6" in raised[0]


# ── A-3: one labor target ───────────────────────────────────────────────────

def _labor_period(db_path, rid, pct):
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=6)
    save_labor_snapshot(rid, start.isoformat(), end.isoformat(), pct, pct * 100, 10000, db_path=db_path)
    return start, end


def test_the_alert_and_home_measure_against_the_same_target(db_path, sent, monkeypatch):
    """No target stored, trailing 24% (the old band: 26%), current 29.5%:
    Home calls that under a 30% target, so the alert must not call it
    3.5 points over a 26% one."""
    import labor, metrics
    rid = _rid(db_path)
    update_restaurant(rid, {"alert_labor_over": 1, "urgent_via_sms": 1}, db_path=db_path)
    _raw_set(db_path, rid, labor_target_pct=None)
    monkeypatch.setattr(metrics, "trailing", lambda r, key, **k: {"value": 24.0})
    _labor_period(db_path, rid, 29.5)
    notify.check_daily_alerts(db_path=db_path)
    assert _log(db_path, rid, "labor_over") == []
    r = models.get_restaurant(rid, db_path)
    assert notify.labor_target_for(r) == 30.0
    assert notify.labor_target_for({"labor_target_pct": None}) == 30.0
    assert notify.labor_target_for({"labor_target_pct": 26.0}) == 26.0


def test_the_labor_tab_reads_the_same_resolver(db_path, monkeypatch):
    import labor
    rid = _rid(db_path)
    update_restaurant(rid, {"labor_target_pct": 27.0}, db_path=db_path)
    monkeypatch.setattr(models, "get_restaurant", lambda *a, **k: Restaurant(name="x", owner_email="y",
                                                                               labor_target_pct=27.0))
    assert labor.get_labor_target(rid) == 27.0


def test_no_surface_carries_its_own_labor_target_fallback():
    """The target is read in one place (notify.labor_target_for). A second
    `labor_target_pct or 30` anywhere is a second definition."""
    offenders = []
    for name in os.listdir(ROOT):
        if not name.endswith(".py") or name == "notify.py":
            continue
        text = open(os.path.join(ROOT, name), encoding="utf-8").read()
        if re.search(r"labor_target_pct\"?\)?\s*or\s*30", text) or \
                re.search(r"baseline_labor_target\(", text):
            offenders.append(name)
    assert offenders == []


# ── A-29: one comparison ────────────────────────────────────────────────────

def test_every_over_target_check_uses_the_same_comparison():
    """At exactly +3.0 the alert fires; Home used `>` and showed neither
    'over' nor a win."""
    offenders = []
    for name in ("home_brief.py", "notify.py", "issues.py", "labor.py"):
        text = open(os.path.join(ROOT, name), encoding="utf-8").read()
        for m in re.finditer(r"[^>=<]>\s*(LABOR_OVER_TARGET_PTS|OVERSTAFF_THRESHOLD)", text):
            offenders.append((name, text[max(0, m.start() - 40):m.end()]))
    assert offenders == []


def test_exactly_three_points_over_is_an_alert(db_path, sent):
    rid = _rid(db_path)
    update_restaurant(rid, {"alert_labor_over": 1, "urgent_via_sms": 1, "labor_target_pct": 30.0}, db_path=db_path)
    _labor_period(db_path, rid, 33.0)
    notify.check_daily_alerts(db_path=db_path)
    assert len(_log(db_path, rid, "labor_over")) == 1


# ── A-25: the labor SMS period is M/D/YY ───────────────────────────────────

def test_the_labor_sms_names_its_period_in_mdy(db_path, sent, monkeypatch):
    from time_utils import mdy
    raised = []
    monkeypatch.setattr(notify, "raise_alert", lambda rid_, t, sms, subj, **k: raised.append(sms))
    rid = _rid(db_path)
    update_restaurant(rid, {"alert_labor_over": 1, "urgent_via_sms": 1, "labor_target_pct": 30.0}, db_path=db_path)
    start, end = _labor_period(db_path, rid, 36.0)
    notify.check_daily_alerts(db_path=db_path)
    sms = " ".join(raised)
    assert mdy(start) in sms and mdy(end) in sms
    assert start.strftime("%b") not in sms


def test_short_period_is_mdy():
    assert notify._short_period("2026-08-25", "2026-08-31") == "8/25/26 – 8/31/26"


# ── A-12: current-state labor reads a trailing window ────────────────────────

def _shifts_csv(days):
    lines = ["date,day_of_week,employee,role,shift_start,shift_end,hours_scheduled,hours_worked,daily_sales,covers"]
    end = date(2026, 9, 20)
    for i in range(days):
        d = end - timedelta(days=i)
        # Old months ran at 50% labor, the last four weeks at 20%.
        sales = 400 if i >= 28 else 1000
        lines.append(f"{d.isoformat()},{d.strftime('%A')},Ana,Server,10:00,18:00,8,8,{sales},")
    return "\n".join(lines)


def test_six_months_of_synced_shifts_are_not_a_six_month_average(db_path, monkeypatch):
    import labor
    rid = _rid(db_path)
    monkeypatch.setattr(labor, "get_hourly_rate", lambda rid_: 25.0)
    monkeypatch.setattr("models.get_role_rates", lambda *a, **k: {})
    stored = {"shifts_csv": _shifts_csv(180)}
    current = labor.analyse_shifts_for_restaurant(rid, client_data=stored)
    whole = labor.analyse_shifts_for_restaurant(rid, client_data=stored, window_days=None)
    assert current["date_range"]["start"] == (date(2026, 9, 20) - timedelta(days=27)).isoformat()
    assert whole["date_range"]["start"] < current["date_range"]["start"]
    assert current["overall_labor_pct"] < 25
    assert whole["overall_labor_pct"] > 30


def test_a_short_hand_upload_is_read_whole():
    import labor
    rows = [{"date": "2026-06-01"}, {"date": "2026-06-10"}, {"date": ""}]
    assert labor.current_window(rows) == rows
