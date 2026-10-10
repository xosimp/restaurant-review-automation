"""Schedule fix round 10/10/26, workstream J2: live labor (MARKET-8, MARKET-24).

Labor to date from the punches and the scheduled rest of the day, against
sales so far and the re-forecast close; the cut time from the restaurant's own
curve and close, never a fixed 8pm; legal, priced cuts, extensions and
call-ins; earned autopilot that never cuts alone. No network: the POS is a
stub."""
import datetime as dt
import json

import pytest

import demand
import floor_settings
import labor_live
import models
import pos
import push
import shift_requests
import strategy_jobs
import schedule_versions as sv
import staff_settings as ss
from tests.test_sf10_j2_floor import db, _restaurant, _publish, _rows, W1  # noqa: F401 — db is the fixture

FRI = dt.date.fromisoformat(W1[4])
HOURS = {d: "11:00am" for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")}
CLOSE = {d: "10:00pm" for d in HOURS}


def _live(db, monkeypatch, rows, sales_so_far=1200.0, typical=5000.0, punches=None, **cols):
    rid = _restaurant(db, [("Ana", "Server"), ("Bo", "Server"), ("Cy", "Server"), ("Di", "Cook"), ("Ed", "Server"),
                           ("Max", "Manager")],
                      open_times_json=json.dumps(HOURS), close_times_json=json.dumps(CLOSE),
                      role_rates_json=json.dumps({"Server": 15.0, "Cook": 20.0}), cut_floor_default=1,
                      salaried_staff_json=json.dumps([{"name": "Max", "annual": 60000}]), **cols)
    ss.upsert(rid, "Max", floor_manager=True)
    _publish(db, rid, rows)
    conn = models.get_conn(db)
    for w in range(1, 9):            # eight past Fridays: 2000 by 6pm of a 5000 day
        d = (FRI - dt.timedelta(weeks=w)).isoformat()
        for hour, net in ((18, 2000.0), (22, 5000.0)):
            conn.execute("INSERT INTO pos_intraday (restaurant_id, business_date, captured_hour, weekday, net_sales, "
                         "provider) VALUES (?,?,?,?,?,?)", (rid, d, hour, "Friday", net, "rpower"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(pos, "fetch_sales_today", lambda r, d: (sales_so_far, "rpower"))
    monkeypatch.setattr(pos, "fetch_clock_ins_today", lambda r, d: (punches if punches is not None else [
        {"employee": "Ana", "role": "Server", "clocked_in_at": "2026-10-09T15:55:00"},
        {"employee": "Bo", "role": "Server", "clocked_in_at": "2026-10-09T16:58:00"},
        {"employee": "Cy", "role": "Server", "clocked_in_at": "2026-10-09T17:02:00"},
        {"employee": "Di", "role": "Cook", "clocked_in_at": "2026-10-09T15:58:00"},
        {"employee": "Max", "role": "Manager", "clocked_in_at": "2026-10-09T14:50:00"}], "rpower"))
    monkeypatch.setattr(demand, "forecast_day", lambda *a, **k: {"available": True, "sales": typical})
    return rid, models.get_restaurant(rid)


SLOW = [(W1[4], "Ana", "Server", "4:00pm", "10:00pm", 6), (W1[4], "Bo", "Server", "5:00pm", "10:00pm", 5),
        (W1[4], "Cy", "Server", "5:00pm", "10:00pm", 5), (W1[4], "Di", "Cook", "4:00pm", "10:30pm", 6.5),
        (W1[4], "Max", "Manager", "3:00pm", "10:30pm", 7.5)]
SIX = dt.datetime(2026, 10, 9, 18, 0)


def test_the_live_read_sets_labor_to_date_and_the_rest_of_the_day_against_the_projected_close(db, monkeypatch):
    rid, r = _live(db, monkeypatch, SLOW)
    rd = labor_live.read(r, now_local=SIX)
    s, lab, p = rd["sales"], rd["labor"], rd["pct"]
    # 1200 by 6pm, where a typical Friday has done 40%: 3000 at close.
    assert s["so_far"] == 1200.0 and s["share"] == 0.4 and s["projected"] == 3000.0 and s["pace_pct"] == -40.0
    # The salaried manager is out of the hourly figure; the four hourly
    # people's punches to now, then their scheduled rest at the role rates.
    assert lab["to_date"] == pytest.approx(31.25 + 15.5 + 14.5 + 40.67, abs=0.05)
    assert lab["rest"] == pytest.approx(60 + 60 + 60 + 90, abs=0.01)
    assert p["hourly_so_far"] == pytest.approx(lab["to_date"] / 1200 * 100, abs=0.1)
    # All-in for the owner: the day's share of the salary on top (60000/52/7).
    assert p["all_in_projected"] == pytest.approx((lab["projected"] + 60000 / 52 / 7) / 3000 * 100, abs=0.1)
    assert "projected" in rd["sentence"] and "behind" in rd["sentence"]


def test_the_cut_time_is_the_restaurants_own_close_and_curve_never_one_8pm(db, monkeypatch):
    rid, r = _live(db, monkeypatch, SLOW)
    m, basis = labor_live.cut_minute(r, FRI)
    assert m == 20 * 60 and "two hours before your close" in basis
    models.update_restaurant(rid, {"close_times_json": json.dumps({d: "9:00pm" for d in CLOSE})})
    m, _b = labor_live.cut_minute(models.get_restaurant(rid), FRI)
    assert m == 19 * 60                                   # a 9pm close cuts at 7pm, not 8pm
    import schedule_economics
    # A measured curve: the evening peaks 7-8pm and halves by 9pm.
    monkeypatch.setattr(schedule_economics, "hourly_profile",
                        lambda *a, **k: {"Friday": {17: 0.15, 18: 0.25, 19: 0.3, 20: 0.12, 21: 0.05}})
    m, basis = labor_live.cut_minute(models.get_restaurant(rid), FRI)
    assert basis.startswith("the first half hour after tonight's peak") and m == 20 * 60


def test_the_pre_dinner_pulse_names_the_restaurants_own_cut_time(db, monkeypatch):
    rid, _r = _live(db, monkeypatch, SLOW)
    models.update_restaurant(rid, {"close_times_json": json.dumps({d: "9:00pm" for d in CLOSE})})
    r = models.get_restaurant(rid)
    move = strategy_jobs.staffing_move(r, dt.datetime(2026, 10, 9, 16, 30),
                                       {"direction": "behind", "pct": -30.0, "samples": 6})
    assert move and move["cut_at"] == "7pm" and "go at 7pm" in move["text"]


def test_a_slow_night_suggests_one_legal_priced_cut_at_the_earliest_safe_half_hour(db, monkeypatch):
    rid, r = _live(db, monkeypatch, SLOW)
    ms = labor_live.moves(r, now_local=SIX)
    assert ms and ms[0]["kind"] == "cut"
    cut = ms[0]
    # Pace 60%: three servers on need two, so one can go from 6:30pm; the
    # cook (alone, at the floor) and the manager are never offered.
    assert cut["employee"] in ("Bo", "Cy") and cut["start_label"] == "6:30pm" and cut["hours"] == 3.5
    assert cut["dollars"] == 52.5 and "stays at or above" in cut["text"]
    assert all(m["employee"] not in ("Di", "Max") for m in ms)
    assert sum(1 for m in ms if m["kind"] == "cut") == 1


def test_a_busy_night_asks_the_one_ending_inside_the_rush_to_stay_and_else_calls_someone_in(db, monkeypatch):
    rows = [(W1[4], "Ana", "Server", "4:00pm", "9:00pm", 5), (W1[4], "Bo", "Server", "5:00pm", "10:00pm", 5),
            (W1[4], "Cy", "Server", "5:00pm", "10:00pm", 5), (W1[4], "Di", "Cook", "4:00pm", "10:30pm", 6.5),
            (W1[4], "Max", "Manager", "3:00pm", "10:30pm", 7.5)]
    rid, r = _live(db, monkeypatch, rows, sales_so_far=3000.0)
    ms = labor_live.moves(r, now_local=SIX, limit=5)
    ext = next(m for m in ms if m["kind"] == "extend")
    assert ext["employee"] == "Ana" and ext["end_label"] == "10pm" and ext["hours"] == 1.0
    rid2 = rid
    # Nobody ending inside the rush: the best legal standby is called in.
    conn = models.get_conn(db)
    conn.execute("DELETE FROM schedule_history WHERE restaurant_id=?", (rid2,))
    conn.commit()
    conn.close()
    _publish(db, rid2, SLOW)
    ms = labor_live.moves(models.get_restaurant(rid2), now_local=SIX, limit=5)
    call = next(m for m in ms if m["kind"] == "call_in")
    assert call["employee"] == "Ed" and call["start_label"] == "6:30pm" and call["end_label"] == "10pm"


def test_autopilot_offers_alone_only_when_on_and_earned_and_never_cuts(db, monkeypatch):
    rows = [(W1[4], "Ana", "Server", "4:00pm", "9:00pm", 5), (W1[4], "Bo", "Server", "5:00pm", "10:00pm", 5),
            (W1[4], "Cy", "Server", "5:00pm", "10:00pm", 5), (W1[4], "Di", "Cook", "4:00pm", "10:30pm", 6.5),
            (W1[4], "Max", "Manager", "3:00pm", "10:30pm", 7.5)]
    rid, r = _live(db, monkeypatch, rows, sales_so_far=3000.0)
    offers, pushes = [], []
    monkeypatch.setattr(shift_requests, "post_open_shift",
                        lambda *a, **k: offers.append((a, k)) or {"request": {"id": 99}, "offer": {"via": "push"}})
    monkeypatch.setattr(labor_live, "_offer_to_managers", lambda r_, mid, m, db_path=None: pushes.append(m) or True)
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: 1)
    out = labor_live.run_for(r, now_local=SIX)
    assert out["offered"] and not out["acted"] and pushes[0]["kind"] == "extend" and offers == []
    # Earned: six of the managers' own approvals of extensions — and the
    # owner's switch on.
    conn = models.get_conn(db)
    for i in range(labor_live.EARN_APPROVALS):
        conn.execute("INSERT INTO live_labor_moves (restaurant_id, business_date, kind, move_key, status, how, "
                     "decided_at) VALUES (?,?,?,?,?,?,datetime('now'))", (rid, W1[3], "extend", f"old{i}", "offered",
                                                                         "manager"))
        conn.execute("INSERT INTO live_labor_moves (restaurant_id, business_date, kind, move_key, status, how, "
                     "decided_at) VALUES (?,?,?,?,?,?,datetime('now'))", (rid, W1[3], "cut", f"oldc{i}", "acted",
                                                                         "manager"))
    conn.execute("DELETE FROM live_labor_moves WHERE business_date=?", (W1[4],))
    conn.commit()
    conn.close()
    assert labor_live.trust(rid, "extend")["earned"] and not labor_live.trust(rid, "extend")["acts_alone"]
    floor_settings.save(rid, {"autopilot": True})
    assert labor_live.trust(rid, "extend")["acts_alone"]
    assert not labor_live.trust(rid, "cut")["acts_alone"]                # a cut never acts alone
    out = labor_live.run_for(models.get_restaurant(rid), now_local=SIX)
    assert out["acted"] and offers and offers[0][1]["offer_to"] == "Ana" and offers[0][1]["actor"] == labor_live.AUTO_ACTOR
    mid = labor_live.todays_moves(rid, W1[4])[0]["id"]
    # One reversal lapses it at once.
    monkeypatch.setattr(shift_requests, "cancel_open", lambda *a, **k: True)
    assert labor_live.reverse(rid, mid, user={"username": "gm"})["ok"]
    t = labor_live.trust(rid, "extend")
    assert not t["acts_alone"] and t["lapse_reason"].startswith("you reversed")


def test_a_cut_the_manager_taps_is_saved_through_the_studio_and_sent_to_that_person(db, monkeypatch):
    rid, r = _live(db, monkeypatch, SLOW)
    import client_api
    sent = []
    monkeypatch.setattr(client_api, "send_schedule_changes",
                        lambda rid_, hid, actor=None, **k: sent.append(hid) or ({"ok": True}, 200))
    best = labor_live.moves(r, now_local=SIX)[0]
    mid, new = labor_live._store_move(rid, best)
    out = labor_live.act(rid, mid, user={"id": 3, "username": "gm", "role": "owner", "restaurant_id": rid})
    assert out["ok"] and out["status"] == "acted" and sent
    hid = sent[0]
    who = best["employee"]
    row = next(x for x in _rows(db, hid) if x["employee"] == who and x["date"] == W1[4])
    assert row["shift_end"] == "6:30pm"
    conn = models.get_conn(db)
    origins = conn.execute("SELECT row_origins_json FROM schedule_versions WHERE history_id=? ORDER BY version DESC "
                           "LIMIT 1", (hid,)).fetchone()[0]
    conn.close()
    assert "live_move" in (origins or "")                       # Cavnar AI's suggestion, never the manager's habit
    assert labor_live.act(rid, mid)["ok"] is False              # answered once


def test_the_managers_hear_a_move_at_most_three_times_a_night(db, monkeypatch):
    rid, r = _live(db, monkeypatch, SLOW)
    import morning_brief
    import notify
    fired = []
    monkeypatch.setattr(notify, "briefing_allowed", lambda *a, **k: True)
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: [{"id": 1}])
    monkeypatch.setattr(strategy_jobs, "deliverable_audience", lambda *a, **k: {1})
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: fired.append(a) or 1)
    for i in range(5):
        m = dict(labor_live.moves(r, now_local=SIX)[0], key=f"k{i}")
        mid, _new = labor_live._store_move(rid, m)
        labor_live._offer_to_managers(r, mid, m)
    assert len(fired) == labor_live.MAX_MOVE_PUSHES_PER_DAY
    assert fired[0][1] == "intraday_pulse"


def test_the_rain_forecast_moves_the_close_only_while_it_comes_from_the_forecast(db, monkeypatch):
    rid, r = _live(db, monkeypatch, SLOW, sales_so_far=200.0)
    import schedule_economics
    import weather
    monkeypatch.setattr(weather, "cached_forecast_for_week",
                        lambda rest, dates: [{"date": dates[0], "precip_pct": 80, "stale": False}])
    monkeypatch.setattr(schedule_economics, "_rain_effect", lambda *a, **k: {"median_lift_pct": -20.0})
    # Early in the day (11am: none of a typical Friday's sales done yet) the
    # close is the forecast, moved by what rain measurably does here.
    s = labor_live.read(r, now_local=dt.datetime(2026, 10, 9, 11, 0))["sales"]
    assert "rain forecast" in s["projected_basis"] and s["projected"] == pytest.approx(200 + 4800 * 0.8)
    assert s["weather"] == {"precip_pct": 80, "rain_lift_pct": -20.0}
    # From the day's own pace, today's weather is already in it.
    s = labor_live.read(r, now_local=SIX)["sales"]
    assert "rain" not in s["projected_basis"]
