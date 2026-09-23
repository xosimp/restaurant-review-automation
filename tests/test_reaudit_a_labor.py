"""Labor and scheduling recommendations (re-audit A-4, A-9, A-19, A-26, A-30)."""
import datetime as dt

import pytest

import models
import schedule_learning as sl
import schedule_rules as sr
from models import create_restaurant, Restaurant, get_conn

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _week(i=0):
    start = dt.date(2026, 10, 5) + dt.timedelta(weeks=i)
    return [(start + dt.timedelta(days=k)).isoformat() for k in range(7)]


def _row(date, emp, start="9:00am", end="5:00pm", role="Server", hours=8.0):
    day = dt.date.fromisoformat(date).strftime("%A")
    return {"date": date, "day": day, "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(hours), "notes": ""}


# ── A-4: a personal cap is not overtime ──────────────────────────────────

def test_hours_past_a_personal_cap_are_not_priced_as_overtime():
    """Ana is capped at 25h and scheduled 32h. Nobody is past 40, so there is
    no overtime premium to save — the move is still worth flagging."""
    w = _week()
    c = sr.Constraints(restaurant_id=1, week_dates=w, week_days=DAYS)
    c.hours_limits = {"ana": (None, 25)}
    rows = [_row(w[k], "Ana") for k in range(4)]            # 32h
    rows += [_row(w[4], "Ben")]
    out = sl.overtime_forecast(rows, constraints=c)
    assert len(out) == 1 and out[0]["over"] == 7.0 and out[0]["overtime_hours"] == 0
    sl.price_overtime_moves(out, {"_default": 20.0})
    assert out[0]["candidate"]["saves"] is None
    assert "overtime" not in out[0]["text"].lower()
    assert "25h limit" in out[0]["text"]


def test_hours_past_forty_are_priced_on_the_overtime_hours_only():
    w = _week()
    c = sr.Constraints(restaurant_id=1, week_dates=w, week_days=DAYS)
    c.hours_limits = {"ana": (None, 38)}
    rows = [_row(w[k], "Ana") for k in range(5)] + [_row(w[5], "Ana", hours=4.0, end="1:00pm")]   # 44h
    rows += [_row(w[6], "Ben")]
    out = sl.overtime_forecast(rows, constraints=c)
    f = out[0]
    assert f["over"] == 6.0 and f["overtime_hours"] == 4.0
    sl.price_overtime_moves(out, {"_default": 20.0})
    # The shift moved is 4h or 8h; only the 4h past 40 carry the half-time.
    assert f["candidate"]["overtime_hours_avoided"] == 4.0
    assert f["candidate"]["saves"] == 40
    assert "4h of it overtime" in f["text"]


def test_the_restaurants_own_ceiling_is_the_overtime_line():
    w = _week()
    c = sr.Constraints(restaurant_id=1, week_dates=w, week_days=DAYS)
    c.compliance = dict(c.compliance or {}, weekly_hours_ceiling=36)
    rows = [_row(w[k], "Ana") for k in range(5)] + [_row(w[5], "Ben")]
    out = sl.overtime_forecast(rows, constraints=c)
    assert out[0]["overtime_line"] == 36.0 and out[0]["overtime_hours"] == 4.0
    assert "36h overtime line" in out[0]["text"]
