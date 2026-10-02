"""event_intel.gameday — Event Intelligence phase 3 (10/1/26): marketing,
prep, ordering, the season's money and the heads-up before a big game.

What games like this one sold here, item by item, against a usual same
weekday; prep and an ordering bump only once two such games were measured
and the item rose on every one; when to reach guests as a starting rule,
said as one; the season's games as measured money that is never Cavnar AI's
value; and one push the afternoon before a game measured big here.
"""
import inspect
from datetime import date, datetime

import pytest

import models
from event_intel import engine, gameday, playbook, store
from test_event_playbook import (GAMES, SAINTS, _outcome, _restaurant, _saints, _staff, _tickets, _usual_sundays,
                                 _world, db)  # noqa: F401  (db is the fixture)


def _lines(db, rid, day, items):
    conn = models.get_conn(db)
    try:
        for name, qty in items.items():
            conn.execute("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, business_date, item_name, "
                         "item_kind, kind, qty, sales) VALUES (?,?,?,?,?,?,?,?,?)",
                         (rid, "rpower", f"{day}-{name}", day, name, "dish", "sale", qty, qty * 10))
        conn.commit()
    finally:
        conn.close()


def _mix_world(db, rid, games=GAMES, wings=(54, 50)):
    """Wings and a game-day shot rise on every game; burgers don't move."""
    for day, w in zip(games, wings):
        _lines(db, rid, day, {"Wings": w, "4th Quarter Shot": 21, "EJ's Smash Burger": 52})
    usual = sorted({d for g in games for d in _usual_sundays(g)} - set(GAMES) - {"2026-09-13"})
    for d in usual:
        _lines(db, rid, d, {"Wings": 28, "4th Quarter Shot": 1, "EJ's Smash Burger": 50})


def _recipe(db, rid, item, ingredient, unit, per):
    conn = models.get_conn(db)
    try:
        mid = conn.execute("INSERT INTO menu_items (restaurant_id, name) VALUES (?,?)", (rid, item)).lastrowid
        iid = conn.execute("INSERT INTO ingredients (restaurant_id, name, unit) VALUES (?,?,?)",
                           (rid, ingredient, unit)).lastrowid
        conn.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
                     (mid, iid, per))
        conn.commit()
    finally:
        conn.close()


def test_the_items_that_rise_on_game_nights_are_read_against_a_usual_night(db):
    r = _restaurant(db)
    _world(db, r.id)
    _mix_world(db, r.id)
    mix = gameday.item_mix(r.id, _saints(db), db_path=db)
    assert mix["n"] == 2 and mix["weekday"] == "Sunday"
    assert [(x["item"], x["game"], x["usual"], x["every_game"]) for x in mix["items"]] == \
        [("Wings", 52.0, 28.0, True), ("4th Quarter Shot", 21.0, 1.0, True)]
    assert mix["text"] == ("Your last 2 home games sold a median 52 Wings (usual 28), 21 4th Quarter Shot (usual 1) "
                           "— against a usual Sunday.")


def test_prep_is_sized_by_what_two_games_sold_and_one_game_is_only_a_fact(db):
    r = _restaurant(db)
    _world(db, r.id)
    _mix_world(db, r.id)
    prep = gameday.prep_lines(r.id, _saints(db), db_path=db)
    assert [p["qty"] for p in prep] == [52, 21]
    assert prep[0]["text"] == "about 52 Wings (your last 2 home games sold 50 and 54; a usual night 28)"
    r2 = _restaurant(db, name="One Game Co")
    _world(db, r2.id, lifts=(20.0, None))
    _mix_world(db, r2.id, games=GAMES[:1], wings=(54,))
    one = gameday.item_mix(r2.id, _saints(db), db_path=db)
    assert one["n"] == 1 and gameday.prep_lines(r2.id, _saints(db), mix=one, db_path=db) == []
    assert one["text"].startswith("Your last home game sold 54 Wings (usual 28)")


def test_the_ordering_bump_runs_through_the_recipes_in_the_ingredients_own_unit(db):
    r = _restaurant(db)
    _world(db, r.id)
    _mix_world(db, r.id)
    _recipe(db, r.id, "Wings", "Chicken wings", "lb", 0.5)
    bump = gameday.order_bump(r.id, _saints(db), db_path=db)
    assert bump["lines"] == [{"ingredient": "Chicken wings", "unit": "lb", "extra": 12.0, "items": ["Wings"]}]
    assert bump["text"].startswith("Order about 12 lb Chicken wings more than a usual week")
    # no recipe for what rose: no bump, never a guessed ingredient
    r2 = _restaurant(db, name="No Recipe Co")
    _world(db, r2.id)
    _mix_world(db, r2.id)
    assert gameday.order_bump(r2.id, _saints(db), db_path=db) is None


def test_the_send_time_is_a_starting_rule_from_kickoff_inside_the_texting_window(db):
    e = _saints(db)
    plan = gameday.send_plan(e)
    assert plan["text_at"] == "2026-11-22T09:00" and plan["text_words"] == "Sunday around 9am, 3 hours before kickoff"
    assert plan["email_by"] == "2026-11-21" and "hasn't measured send times" in plan["basis"]
    night = gameday.send_plan(dict(e, kickoff_local="19:15"))
    assert night["text_words"] == "Sunday around 4:15pm, 3 hours before kickoff"
    london = gameday.send_plan(dict(e, kickoff_local="08:30"))
    assert london["text_at"] == "2026-11-21T17:00" and london["text_words"] == "Saturday around 5pm, the evening before"
    early = gameday.send_plan(dict(e, kickoff_local="10:00"))
    assert early["text_at"] == "2026-11-22T08:00" and early["text_words"].endswith("2 hours before kickoff")
    assert gameday.send_plan(dict(e, kickoff_local=None)) is None and gameday.send_plan(dict(e, event_date=None)) is None


def test_the_campaign_goal_names_the_game_and_what_sells_never_an_offer(db):
    r = _restaurant(db)
    _world(db, r.id)
    _mix_world(db, r.id)
    goal = gameday.campaign_goal(r.id, _saints(db), db_path=db)
    assert goal == ("Bring guests in to watch Bears vs New Orleans Saints on Sunday 11/22/26 (12pm, FOX) — feature Wings "
                    "and 4th Quarter Shot, what game nights sell here")
    assert "$" not in goal and "%" not in goal and "off" not in goal.split()


def test_the_seasons_money_is_measured_nights_only_and_never_cavnar_ais_value(db):
    import value_delivered
    r = _restaurant(db)
    sid = store.series_by_slug("nfl-chicago-bears", db_path=db)["id"]
    _outcome(db, r.id, "2026-09-28", 106.1, net=11031.0, base=5351.0)
    sv = gameday.season_value(r.id, sid, today=date(2026, 10, 1), db_path=db)
    assert (sv["played"], sv["measured"], sv["incremental"]) == (6, 1, 5680.0)
    assert sv["text"] == ("Bears games so far: 6 played, 1 measured here — +$5,680 over a usual same weekday; 5 without "
                          "a usual night to measure against.")
    assert "not something Cavnar AI did" in sv["basis"]
    # kept apart from value delivered: it never reads the event catalog
    assert "event_intel" not in inspect.getsource(value_delivered)
    assert gameday.season_value(r.id, sid, today=date(2026, 8, 1), db_path=db) is None
    # it rides the owner's follow row
    assert engine.follow_choices(r, today=date(2026, 10, 1), db_path=db)[0]["season"]["measured"] == 1


def test_the_order_screen_hears_about_a_game_this_week(db):
    r = _restaurant(db)
    _world(db, r.id)
    _mix_world(db, r.id)
    _recipe(db, r.id, "Wings", "Chicken wings", "lb", 0.5)
    note = gameday.week_note(r.id, today=date(2026, 11, 17), db_path=db)
    assert note["describe"].startswith("Bears vs New Orleans Saints") and note["order"]["lines"][0]["extra"] == 12.0
    assert gameday.week_note(r.id, today=date(2026, 11, 10), db_path=db) is None        # 12 days out
    r2 = _restaurant(db, name="One Game Co")
    _world(db, r2.id, lifts=(20.0, None))
    _mix_world(db, r2.id, games=GAMES[:1], wings=(54,))
    one = gameday.week_note(r2.id, today=date(2026, 11, 17), db_path=db)
    assert one["order"] is None and one["text"].endswith("One game — not yet a pattern to order on.")


def test_the_brief_alert_carries_prep_the_send_time_and_a_goal_that_names_it(db):
    r = _restaurant(db)
    _world(db, r.id)
    _mix_world(db, r.id)
    line = playbook.alert(r.id, date(2026, 11, 20), marketing=True, db_path=db)
    assert "Prep for about 52 Wings (your last 2 home games sold 50 and 54; a usual night 28)" in line["text"]
    assert "Text your guests Sunday around 9am, 3 hours before kickoff (a starting rule" in line["text"]
    assert "send=Sunday+around+9am" in line["action"]["nav"] and "feature+Wings" in line["action"]["nav"]
    assert line["prep"][0]["qty"] == 52
    # a manager without the Labor view: no item counts, no item names in the goal
    quiet = playbook.alert(r.id, date(2026, 11, 20), sees_sales=False, sees_labor=False, marketing=True, db_path=db)
    assert "Wings" not in quiet["text"] and "Wings" not in quiet["action"]["nav"]


def test_the_day_after_says_what_to_prep(db):
    from dsr import tomorrow
    r = _restaurant(db)
    _world(db, r.id)
    _mix_world(db, r.id)
    snap = tomorrow.build(r, date(2026, 11, 21), facts={}, db_path=db)
    prep = [i for i in snap["items"] if i["kind"] == "game_prep"]
    assert prep and prep[0]["text"].startswith("Prep for about 52 Wings") and prep[0]["tone"] == "warn"


# ── the heads-up before a big game ─────────────────────────────────────────

def test_a_big_game_is_one_measured_big_never_a_guess(db):
    r = _restaurant(db)
    _world(db, r.id)                                   # home day games +20 and +30: median +25
    big, words = gameday.big_game(r.id, _saints(db), db_path=db)
    assert big and words.startswith("Bears home games have run +25%")
    s = store.series_by_slug("nfl-chicago-bears", db_path=db)
    pats = store.events_for([s["id"]], "2026-10-22", "2026-10-22", db_path=db)[0]      # home, prime time
    _outcome(db, r.id, "2026-09-28", 106.1, net=11031.0, base=5351.0)
    big, words = gameday.big_game(r.id, pats, db_path=db)
    # the effect segment for 10/22 is home games (+25, n=3 incl. 9/28): below? median of 20,30,106 = 30
    assert big and "+30%" in words
    road = store.events_for([s["id"]], "2026-11-26", "2026-11-26", db_path=db)[0]      # at Detroit
    assert gameday.big_game(r.id, road, db_path=db) == (False, None)


def test_one_measured_game_is_big_only_when_it_was_the_same_kind(db):
    r = _restaurant(db)
    _outcome(db, r.id, "2026-09-28", 106.1, net=11031.0, base=5351.0)      # MNF, home, prime time
    s = store.series_by_slug("nfl-chicago-bears", db_path=db)
    pats = store.events_for([s["id"]], "2026-10-22", "2026-10-22", db_path=db)[0]      # TNF, home, prime time
    big, words = gameday.big_game(r.id, pats, db_path=db)
    assert big and words.endswith("ran +106% against a usual Monday — one night.")
    jets = store.events_for([s["id"]], "2026-10-04", "2026-10-04", db_path=db)[0]      # home, noon
    assert gameday.big_game(r.id, jets, db_path=db) == (False, None)


def test_the_push_goes_once_the_afternoon_before_to_the_labor_view_only(db, monkeypatch):
    import morning_brief
    import notify
    import ops
    import push
    import strategy_jobs
    import time_utils
    r = _restaurant(db)
    _outcome(db, r.id, "2026-09-28", 106.1, net=11031.0, base=5351.0)
    sent = []
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: sent.append((a, k)))
    monkeypatch.setattr(notify, "briefing_allowed", lambda *a, **k: True)
    monkeypatch.setattr(morning_brief, "recipients", lambda rid, db_path=None, include_opted_out=False: [
        {"id": 1, "role": "owner"}, {"id": 2, "role": "employee"}])
    monkeypatch.setattr(strategy_jobs, "deliverable_audience", lambda rid, ids, db_path=None, alert_type=None: set(ids))
    claims = set()
    monkeypatch.setattr(ops, "period_claimed", lambda k, p: (k, p) in claims)
    monkeypatch.setattr(ops, "claim_period", lambda k, p: not ((k, p) in claims or claims.add((k, p))))
    clock = {"now": datetime(2026, 10, 21, 15, 0)}
    monkeypatch.setattr(time_utils, "restaurant_now", lambda rest, naive=False: clock["now"])
    out = gameday.run_event_push(db_path=db, restaurants=[r])
    assert out["sent"] == 1 and len(sent) == 1
    args, kw = sent[0]
    assert args[1] == "event_ahead" and args[2] == "Tomorrow: Bears vs New England Patriots · 7:15pm (prime time) · Prime Video"
    assert "ran +106% against a usual Monday — one night." in args[3]
    assert kw["user_ids"] == {1} and kw["data"]["nav"].startswith("ask?q=How+should+we+get+ready")
    # once per game
    assert gameday.run_event_push(db_path=db, restaurants=[r])["sent"] == 0 and len(sent) == 1
    # outside the afternoon window: nothing
    clock["now"] = datetime(2026, 10, 21, 9, 0)
    claims.clear()
    assert gameday.run_event_push(db_path=db, restaurants=[r])["sent"] == 0


def test_the_push_is_registered_everywhere_an_alert_type_must_be(db):
    import client_api
    import jobs_registry
    import nav
    import push
    import scheduler
    assert jobs_registry.JOBS["event_push"]["target"] == ("event_intel.gameday", "run_event_push")
    assert '_ops.run_job("event_push", run_event_push' in inspect.getsource(scheduler.scheduler_loop)
    assert push.NOTIFICATION_MODULE["event_ahead"] == "ask" and push.PRIORITY["event_ahead"] == push.P3_INFO
    assert "event_ahead" in models.NON_ALERT_TYPES
    assert nav.for_notification("event_ahead").startswith("ask")
    import os
    swift = open(os.path.join(os.path.dirname(__file__), "..", "ios", "CavnarAI", "CavnarAI", "Push",
                              "DeepLinkRouter.swift")).read()
    assert '"milestone", "event_ahead":' in swift


def test_the_game_week_route_is_the_food_views(db):
    from flask import Flask
    import strategy_routes
    r = _restaurant(db)
    app = Flask(__name__)
    with app.test_request_context("/food-cost/game-week"):
        body, status = strategy_routes._do_game_week({"restaurant_id": r.id, "role": "owner", "id": 1})
    assert status == 200 and "game" in body
    with app.test_request_context("/food-cost/game-week"):
        body, status = strategy_routes._do_game_week({"restaurant_id": r.id, "role": "manager", "id": 2})
    assert status == 403


# ── blind audit, 10/1/26 ───────────────────────────────────────────────────

def test_a_game_older_than_the_sync_window_keeps_its_measured_night(db, monkeypatch):
    import demand_signals
    import event_memory
    calls = []
    monkeypatch.setattr(event_memory, "record_night", lambda rid, d, db_path=None: calls.append(d) or {"recorded": 0})
    r = _restaurant(db)
    engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    before = [s for s in demand_signals.upcoming(r.id, "2026-08-15", "2026-08-15", db_path=db) if s.get("ref")]
    calls.clear()
    engine.sync_restaurant(r, today=date(2027, 9, 25), db_path=db)          # 8/15/26 is 406 days back
    after = [s for s in demand_signals.upcoming(r.id, "2026-08-15", "2026-08-15", db_path=db) if s.get("ref")]
    assert before and after == before and "2026-08-15" not in calls


def test_clearing_a_correction_restores_the_season_files_status_and_result(db):
    s = store.series_by_slug("nfl-chicago-bears", db_path=db)
    wk6 = [e for e in store.events_for([s["id"]], "2026-10-18", "2026-10-18", db_path=db)][0]
    store.edit_event(wk6["id"], {"status": "cancelled", "result": "W 30-3"}, db_path=db)
    got = store.edit_event(wk6["id"], {}, clear=["status", "result"], db_path=db)
    assert got["after"]["result"] is None and got["after"]["status"] in ("scheduled", "completed")
    store.load_bundled(db_path=db)
    again = store.event_by_id(wk6["id"], db_path=db)
    assert again["status"] != "cancelled" and again["result"] is None


def test_twelve_hour_shift_times_are_read(db):
    assert [playbook._minutes(t) for t in ("15:50", "4:00pm", "04:00 PM", "9:00", "12:15am", "bad")] == \
        [950, 960, 960, 540, 15, None]


def test_a_rush_pattern_needs_the_games_own_peaks_within_an_hour(db, monkeypatch):
    r = _restaurant(db)
    _world(db, r.id)
    real = playbook._hourly

    def shifted(rid, iso, db_path):
        got = real(rid, iso, db_path)
        if iso == GAMES[1] and got:                       # the second game peaks two hours later
            return {10: 300, 11: 500, 12: 900, 13: 1900, 17: 700}
        return got
    monkeypatch.setattr(playbook, "_hourly", shifted)
    ru = playbook.rush(r.id, _saints(db), db_path=db)
    assert ru["pattern_offset"] is None


def test_ask_gives_a_manager_without_food_cost_no_ordering_and_without_labor_no_dollars(db, monkeypatch):
    import ask_cavnar_tools as tools
    r = _restaurant(db)
    _world(db, r.id)
    _mix_world(db, r.id)
    _recipe(db, r.id, "Wings", "Chicken wings", "lb", 0.5)
    monkeypatch.setattr(tools, "_local_today_of", lambda rid: date(2026, 11, 20))

    class V:
        _ask_denied = frozenset({"inventory"})
    out = tools._read_events(r.id, days=7, _viewer=V())
    saints = [u for u in out["upcoming"] if "Saints" in u["what"]][0]
    assert "order_more" not in saints and saints["staffing"]["plan"]
    V._ask_denied = frozenset({"labor", "inventory"})
    out = tools._read_events(r.id, days=7, _viewer=V())
    saints = [u for u in out["upcoming"] if "Saints" in u["what"]][0]
    assert not {"staffing", "rush", "items_sold", "last_like_it"} & set(saints) and "season_so_far" not in out


def test_the_brief_does_not_repeat_what_the_reports_carried_line_already_said(db):
    r = _restaurant(db)
    _world(db, r.id)
    _mix_world(db, r.id)
    eid = _saints(db)["id"]
    carried = [{"kind": "event", "event_id": eid}, {"kind": "game_staffing", "event_id": eid},
               {"kind": "game_prep", "event_id": eid}]
    line = playbook.alert(r.id, date(2026, 11, 22), carried=carried, db_path=db)
    assert "Staff above" not in line["text"] and "Prep for" not in line["text"] and "have run" not in line["text"]
    assert line["text"].startswith("Today: Bears vs New Orleans Saints")


def test_the_seasons_money_leaves_out_postponed_games_and_mixed_nights(db):
    r = _restaurant(db)
    sid = store.series_by_slug("nfl-chicago-bears", db_path=db)["id"]
    _outcome(db, r.id, "2026-09-28", 106.1, net=11031.0, base=5351.0)
    _outcome(db, r.id, "2026-09-20", 20.0, net=9600.0, base=8000.0)
    conn = models.get_conn(db)
    conn.execute("UPDATE event_outcomes SET confounded=1 WHERE restaurant_id=? AND business_date='2026-09-20'", (r.id,))
    conn.commit()
    conn.close()
    sv = gameday.season_value(r.id, sid, today=date(2026, 10, 1), db_path=db)
    assert sv["measured"] == 1 and sv["mixed"] == 1 and sv["incremental"] == 5680.0
    assert sv["text"].endswith("; 1 left out with something else on that night.")
