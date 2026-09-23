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


# ── shared DB harness ─────────────────────────────────────────────────────

import sys as _sys


@pytest.fixture
def db(db_path, monkeypatch):
    import ops
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(_sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    claimed = set()

    def _claim(job, period):
        if (job, period) in claimed:
            return False
        claimed.add((job, period))
        return True
    monkeypatch.setattr(ops, "claim_period", _claim)
    return db_path


# ── A-9: a safety recommendation is never hidden by a key ───────────────

def test_not_for_us_on_a_coverage_gap_does_not_hide_next_weeks_gap(db, monkeypatch):
    import rec_ledger
    import schedule_engine as se
    import schedule_intel as si
    import shift_quality as sq
    rid = create_restaurant(Restaurant(name="Gap Co", owner_email="g@x.com"), db_path=db)
    gap = "Fill the gap on Friday night: Server short 1 of 3."
    pair = "Pair Ana with Ben on Saturday night."
    monkeypatch.setattr(se, "_quality_signals", lambda r, result, **extra: ({}, None))
    monkeypatch.setattr(sq, "score_rows", lambda rows, **k: {"checked": False, "recommendations": [gap, pair]})
    silenced = {si.schedule_rec_key("coverage", gap), si.schedule_rec_key(sq.recommendation_kind(pair), pair)}
    monkeypatch.setattr(rec_ledger, "silenced_keys", lambda *a, **k: silenced)
    quality, _w = se._score_schedule_quality(rid, [_row(_week()[4], "Ana")], {})
    assert quality["recommendations"] == [gap], "the gap is shown; the answered pair is not"


# ── A-19: clean only counts when somebody was watching ───────────────────

def _published_week(db, rid, rows, week):
    import schedule_versions as sv
    header = ",".join(sv.COLS) + "\n"
    csv_text = header + "".join(",".join(str(r.get(c, "")) for c in sv.COLS) + "\n" for r in rows)
    c = models.get_conn(db)
    hid = c.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                    "VALUES (?,?,?,?, datetime('now'))", (rid, week[0], week[6], csv_text)).lastrowid
    c.commit(); c.close()
    return hid


def test_pairs_are_not_suggested_from_nights_nobody_watched(db, monkeypatch):
    import schedule_intel as si
    rid = create_restaurant(Restaurant(name="Pair Co", owner_email="p@x.com", module_labor=1), db_path=db)
    outs = []
    for i in range(3):
        w = _week(i)
        week_rows = [_row(w[d], n, start="5:00pm", end="10:00pm", hours=5) for d in range(5) for n in ("Ana", "Ben")]
        hid = _published_week(db, rid, week_rows, w)
        outs += [(hid, w[d]) for d in range(5)]
    c = models.get_conn(db)
    for hid, d in outs:
        c.execute("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, issues) VALUES (?,?,?,?,0)",
                  (rid, hid, d, "night"))
    c.commit(); c.close()
    assert si.chemistry_suggestions(rid, db_path=db) == [], "no clock-in feed: nothing was watching"
    monkeypatch.setattr(si, "watched_dates", lambda rid_, start, end, db_path=None: {d for _h, d in outs})
    got = si.chemistry_suggestions(rid, db_path=db)
    assert got and {got[0]["a"], got[0]["b"]} == {"Ana", "Ben"}


def test_an_unwatched_clean_night_is_not_an_improved_outcome(db):
    import schedule_intel as si
    rid = create_restaurant(Restaurant(name="Gap Co", owner_email="g@x.com"), db_path=db)
    text = "Fill the gap on Friday night: nobody on Bartender."
    si.record_recommendation(rid, "coverage", text, "accepted")
    fri = dt.date.today() + dt.timedelta(days=(4 - dt.date.today().weekday()) % 7 or 7)
    c = models.get_conn(db)
    hid = c.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end) VALUES (?,?,?)",
                    (rid, (fri - dt.timedelta(days=4)).isoformat(), (fri + dt.timedelta(days=2)).isoformat())).lastrowid
    c.execute("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, issues) VALUES (?,?,?,?,0)",
              (rid, hid, fri.isoformat(), "night"))
    c.commit(); c.close()
    assert si.measure_accepted_recommendations(rid, db_path=db, today=fri + dt.timedelta(days=5)) == 0


def test_auto_publish_trust_needs_watched_weeks(db, monkeypatch):
    import schedule_intel as si
    rid = create_restaurant(Restaurant(name="Trust Co", owner_email="t@x.com", module_labor=1), db_path=db)
    for i in range(3):
        _published_week(db, rid, [_row(_week(i)[0], "Ana")], _week(i))
    assert models.schedule_publish_trust(rid, db_path=db) == 0
    monkeypatch.setattr(si, "watched_dates", lambda *a, **k: {"watched"})
    assert models.schedule_publish_trust(rid, db_path=db) == 3


def test_the_watch_needs_the_feed_the_manager_and_a_reading(db, monkeypatch):
    import issues, pos
    import schedule_intel as si
    rid = create_restaurant(Restaurant(name="Watch Co", owner_email="w@x.com", module_labor=1), db_path=db)
    assert si.coverage_check_possible(rid, db_path=db) is False
    monkeypatch.setattr(issues, "get_routing", lambda *a, **k: {"manager": {"contact_id": 1}})

    class _Toast:
        @staticmethod
        def fetch_clock_ins_today(rid_, day):
            return []
    monkeypatch.setattr(pos, "connected_provider", lambda rid_: ("toast", _Toast))
    assert si.coverage_check_possible(rid, db_path=db) is True
    assert si.watched_dates(rid, "2026-10-05", "2026-10-11", db_path=db) == set()
    c = models.get_conn(db)
    c.execute("INSERT INTO pos_intraday (restaurant_id, business_date, captured_hour, weekday, net_sales, provider) "
              "VALUES (?, '2026-10-09', 18, 'Friday', 900, 'toast')", (rid,))
    c.commit(); c.close()
    assert si.watched_dates(rid, "2026-10-05", "2026-10-11", db_path=db) == {"2026-10-09"}


# ── A-26: the weekly suggestion is what Apply applies ────────────────────

def test_the_weekly_suggestion_is_calibrate_weights(db, monkeypatch):
    import json
    import schedule_learning
    import strategy_jobs
    rid = create_restaurant(Restaurant(name="Cal Co", owner_email="c@x.com", module_labor=1), db_path=db)
    _published_week(db, rid, [_row(_week()[0], "Ana")], _week())
    suggestion = {"coverage": 26.0, "fairness": 7.0}
    monkeypatch.setattr(schedule_learning, "calibrate_weights",
                        lambda rid_, db_path=None: {"ready": True, "suggested_weights": suggestion})
    assert strategy_jobs.run_quality_calibration(db_path=db)["restaurants_changed"] == 1
    change = [c for c in models.get_capability_changes(rid, db_path=db) if c["kind"] == "quality_weights_suggested"][0]
    after = change["after"] if isinstance(change["after"], dict) else json.loads(change["after"])
    assert after["coverage"] == 26.0 and after["fairness"] == 7.0
    assert strategy_jobs.run_quality_calibration(db_path=db)["restaurants_changed"] == 0, "once per new week"


# ── A-30: a week that starts today still gets its reminder ──────────────

def test_a_draft_starting_today_is_still_reminded(db):
    import strategy_jobs
    rid = create_restaurant(Restaurant(name="Draft Co", owner_email="d@x.com", module_labor=1), db_path=db)
    today = dt.date(2026, 10, 5)
    c = models.get_conn(db)
    c.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv) VALUES (?,?,?,?)",
              (rid, today.isoformat(), (today + dt.timedelta(days=6)).isoformat(), "x"))
    c.commit(); c.close()
    w = strategy_jobs.labor_waiting(rid, db_path=db, today=today)
    assert w["history_id"] and "10/5/26" in w["lines"][-1]
