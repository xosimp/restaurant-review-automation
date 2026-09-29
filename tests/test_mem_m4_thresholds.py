"""thresholds: what the engine learned never reached the thresholds that
trigger advice, and a kind that kept making things worse was still proposed
(memory audit 9/29/26, M4). The labor margins are fitted to each
restaurant's own swing (never below the stated one) and stored with their
provenance; a kind whose own record says it does no better than doing
nothing — or keeps making things worse — stops being proposed and the owner
is asked."""
import json
import random
import sys
from datetime import date, datetime, timedelta

import pytest

import models
import notify
import restaurant_thresholds as rt
from models import Restaurant, create_restaurant, save_labor_snapshot, update_restaurant
from thresholds import LABOR_OVER_TARGET_PTS

TODAY = date.today()


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    import auth
    import push
    import webhooks
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


def _rid(db_path, name="Margin Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test",
                                        owner_phone="+15555550100", timezone="America/Chicago"), db_path=db_path)


def _daily(rid, swing, days=84, seed=7):
    rng = random.Random(seed)
    conn = models.get_conn()
    try:
        for i in range(days):
            d = TODAY - timedelta(days=days - i)
            pct = 30.0 + rng.uniform(-swing, swing)
            conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_cost) "
                         "VALUES (?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), 1000.0, pct * 10))
        conn.commit()
    finally:
        conn.close()


def test_a_volatile_restaurant_gets_a_wider_day_margin_and_a_steady_one_the_stated(db_path):
    wild, calm = _rid(db_path, "Wild Co"), _rid(db_path, "Calm Co")
    _daily(wild, swing=8.0)
    _daily(calm, swing=0.5)
    fw = rt.fit(wild)["labor_over_day"]
    fc = rt.fit(calm)["labor_over_day"]
    assert fw["value"] > LABOR_OVER_TARGET_PTS and fw["sigma"] and fw["n"] >= rt.MIN_DAYS_FOR_DAY_SIGMA
    assert "wider than the stated" in fw["basis"]
    assert fc["value"] == LABOR_OVER_TARGET_PTS and "the stated 3-point margin" in fc["basis"]
    assert fw["version"] == rt.THRESHOLDS_VERSION and fw["as_of"] == TODAY.isoformat()


def test_the_margin_is_stored_read_and_never_below_the_stated_one(db_path):
    rid = _rid(db_path)
    assert rt.margin(rid, "labor_over_day") == LABOR_OVER_TARGET_PTS          # nothing stored
    _daily(rid, swing=8.0)
    assert rt.refresh(rid) == 2
    fitted = rt.margin(rid, "labor_over_day")
    assert fitted > LABOR_OVER_TARGET_PTS
    assert rt.margin(rid, "labor_over_day", stated=99.0) == 99.0
    conn = models.get_conn()
    conn.execute("UPDATE restaurant_thresholds SET as_of=? WHERE restaurant_id=?",
                 ((TODAY - timedelta(days=rt.MAX_AGE_DAYS + 1)).isoformat(), rid))
    conn.commit()
    conn.close()
    assert rt.margin(rid, "labor_over_day") == LABOR_OVER_TARGET_PTS          # an old fit is not trusted
    assert rt.margin(None, "labor_over_period") == LABOR_OVER_TARGET_PTS


def test_a_day_inside_the_restaurants_own_swing_is_not_overstaffed():
    import labor
    shifts = [{"date": "2026-09-21", "day": "Monday", "employee": "A", "role": "Server", "shift_start": "9:00am",
               "shift_end": "5:00pm", "scheduled_hours": "8", "actual_hours": "8", "sales": "600"}]
    at_stated = labor.analyse_shifts(shifts, hourly_rate=26.0, labor_target=30.0)
    wide = labor.analyse_shifts(shifts, hourly_rate=26.0, labor_target=30.0, over_margin=10.0)
    # 8h x $26 = $208 on $600 = 34.7%: 4.7 over — past 3, inside 10.
    assert at_stated["overstaffed_days"] and not wide["overstaffed_days"]
    narrow = labor.analyse_shifts(shifts, hourly_rate=26.0, labor_target=30.0, over_margin=1.0)
    assert narrow["overstaffed_days"], "a fitted margin never goes below the stated one"


def test_the_labor_alert_waits_for_the_restaurants_own_margin(db_path, monkeypatch):
    rid = _rid(db_path)
    update_restaurant(rid, {"alert_labor_over": 1, "urgent_via_sms": 1, "labor_target_pct": 30.0,
                            "labor_target_source": "set"}, db_path=db_path)
    end = TODAY - timedelta(days=2)
    save_labor_snapshot(rid, (end - timedelta(days=6)).isoformat(), end.isoformat(), 34.0, 3400, 10000,
                        db_path=db_path)
    texts = []
    monkeypatch.setattr(notify, "send_sms", lambda to, msg, use_case="alert": texts.append(msg) or True)
    monkeypatch.setattr(notify, "_send_alert_email", lambda *a, **k: True)
    import push
    import webhooks
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: None)
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    conn = models.get_conn()
    conn.execute("INSERT INTO restaurant_thresholds (restaurant_id, name, value, stated, n, as_of, version) "
                 "VALUES (?,?,?,?,?,?,?)", (rid, "labor_over_period", 6.0, 3.0, 9, TODAY.isoformat(),
                                             rt.THRESHOLDS_VERSION))
    conn.commit()
    conn.close()
    notify.check_daily_alerts(db_path=db_path)
    conn = models.get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM alert_log WHERE restaurant_id=? AND alert_type='labor_over'",
                         (rid,)).fetchone()[0]
    finally:
        conn.close()
    assert n == 0, "4 points over is inside this restaurant's own 6-point swing"


def test_every_margin_reader_goes_through_the_fitted_margin():
    import inspect
    import home_brief
    import issues
    import labor
    import mobile_api
    for mod, name in ((home_brief, "labor_over_period"), (issues, "labor_over_period"),
                      (notify, "labor_over_period"), (mobile_api, "labor_over_period"),
                      (labor, "labor_over_day")):
        assert f'"{name}"' in inspect.getsource(mod), mod.__name__


# ── stop proposing and ask ───────────────────────────────────────────────────

def _episodes(rid, key, verdicts):
    import rec_ledger
    now = datetime.utcnow()
    conn = models.get_conn()
    try:
        for i, v in enumerate(verdicts):
            start = TODAY - timedelta(days=300 - 20 * i)
            cur = conn.execute(
                "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, baseline_value, "
                "started_on, evaluate_on, status, verdict, after_start, after_end, concurrent) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, "recommendation", key, "t", "labor_pct", 30.0, start.isoformat(),
                 (start + timedelta(days=7)).isoformat(), "evaluated", v, start.isoformat(),
                 (start + timedelta(days=6)).isoformat(), "[]"))
            created = (now - timedelta(days=40 + i)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT INTO rec_instances (rec_id, restaurant_id, key, module, kind, title, status, tags, "
                         "tracker_id, created_at, last_event_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (f"h{rid}{i}", rid, key, "labor", rec_ledger.kind_of(key), key, "completed",
                          json.dumps(rec_ledger.tags_for(key)), cur.lastrowid, created, created))
            conn.execute("INSERT INTO rec_events (rec_id, restaurant_id, key, event, dedupe, at) VALUES (?,?,?,?,?,?)",
                         (f"h{rid}{i}", rid, key, "shown", f"s{i}", created))
        conn.commit()
    finally:
        conn.close()


def test_a_kind_that_keeps_making_things_worse_stops_and_the_owner_is_asked(db_path):
    import home_brief
    import rec_learning
    rid = _rid(db_path)
    _episodes(rid, "trim_day:Tuesday", ["worsened"] * 4 + ["no_clear_change"])
    learned = rec_learning.effectiveness(rid)
    held = learned.held("trim_day")
    assert held and held["worsened"] == 4 and "measured worse 4 of 5 times here" in held["why"]
    recs = [{"key": "trim_day:Tuesday", "title": "Trim Tuesday", "timeframe": "This week", "dollars_monthly": 400},
            {"key": "stock_low:Salmon", "title": "Order salmon", "timeframe": "Today", "dollars_monthly": None},
            {"key": "trim_day:Friday", "title": "Trim Friday", "severity": "critical", "timeframe": "Today"}]
    out = home_brief.order_recommendations(recs, learned=learned)
    keys = [r["key"] for r in out]
    assert "trim_day:Tuesday" not in keys and "stock_low:Salmon" in keys
    assert "trim_day:Friday" in keys, "a critical item is never held back"
    asks = home_brief._kind_hold_asks(rid, learned, present=True)
    assert asks[0]["key"] == "kind_hold:trim_day" and asks[0]["answerable"]
    assert "Keep suggesting trim day?" == asks[0]["title"] and "before and after, not proof" in asks[0]["why"]
    # The owner keeps it: it is proposed again.
    import rec_ledger
    rec_ledger.record(rid, "kind_hold:trim_day", "completed", surface="home")
    assert rec_learning.effectiveness(rid).held("trim_day") is None
    # The question is bookkeeping, never a recommendation in the rates.
    assert not rec_ledger.counts_in_acceptance("kind_hold:trim_day")


def test_a_kind_with_a_mixed_record_is_still_proposed(db_path):
    import rec_learning
    rid = _rid(db_path)
    _episodes(rid, "trim_day:Tuesday", ["improved", "worsened", "no_clear_change", "improved", "no_clear_change"])
    assert rec_learning.effectiveness(rid).held("trim_day") is None


def test_the_nightly_pass_refits_the_margins(db_path):
    import learning_memory
    rid = _rid(db_path)
    _daily(rid, swing=8.0)
    out = learning_memory.nightly(rid)
    assert out["steps"]["thresholds"]["written"] == 2
    assert rt.detail(rid, "labor_over_day")["value"] > LABOR_OVER_TARGET_PTS
