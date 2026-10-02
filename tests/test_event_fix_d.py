"""Event re-audit fixes, gameday, push and Ask (D), 10/1/26.

What a plan rests on (P3-01, P3-02, P3-06, A1 handoff 6), the ordering bump's
recipes (P3-03), what counts as played (P3-05, P4-04), Ask's games (P1-04,
P2-05), the push itself (P3-07, P3-08, P3-10), its bell row (P3-09), the
campaign goal (P3-11, P3-12, X-9), who reads the item mix (X-8), the
restaurant's clock (P2-04) and one read per item_mix (X-2).

The clock is pinned: nothing here depends on the real calendar date.
"""
import json
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import models
import event_memory
from event_intel import engine, gameday, playbook, store
from models import update_restaurant, get_restaurant
from test_event_playbook import GAMES, _outcome, _restaurant, _saints, _world, bears_only
from test_event_gameday import _lines, _mix_world

CLOCK = {"now": datetime(2026, 11, 20, 10, 0)}       # Central, between the fixture's games


@pytest.fixture
def db(db_path, monkeypatch, tmp_path):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(event_memory, "record_night", lambda *a, **k: {"recorded": 0})
    import weather
    monkeypatch.setattr(weather, "forecast_for_day", lambda *a, **k: None)
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: [])
    bears_only(db_path, monkeypatch, tmp_path / "seasons")
    # The clock: every "today" and "now" the code reads is CLOCK's.
    import time_utils
    CLOCK["now"] = datetime(2026, 11, 20, 10, 0)

    def now(rest=None, naive=False):
        return CLOCK["now"] if naive else CLOCK["now"].replace(tzinfo=ZoneInfo("America/Chicago"))
    monkeypatch.setattr(time_utils, "restaurant_now", now)
    monkeypatch.setattr(store, "local_today", lambda tz=None: CLOCK["now"].date())
    return db_path


def _event(db, day):
    s = store.series_by_slug("nfl-chicago-bears", db_path=db)
    return store.events_for([s["id"]], day, day, db_path=db)[0]


def _confound(db, rid, day):
    c = models.get_conn(db)
    try:
        c.execute("UPDATE event_outcomes SET confounded=1 WHERE restaurant_id=? AND business_date=?", (rid, day))
        c.commit()
    finally:
        c.close()


def _set(db, eid, **cols):
    c = models.get_conn(db)
    try:
        for k, v in cols.items():
            c.execute(f"UPDATE catalog_events SET {k}=? WHERE id=?", (v, eid))
        c.commit()
    finally:
        c.close()


def _unresolved(db, day):
    """Make a past game an if-necessary game with no result yet."""
    e = _event(db, day)
    _set(db, e["id"], attributes_json=json.dumps({"if_necessary": True}), result=None, status="scheduled")
    return e


# ── P3-01: the clean-nights rule in the item mix ──────────────────────────

def test_a_confounded_game_night_is_left_out_of_the_mix_while_clean_ones_suffice(db):
    r = _restaurant(db)
    games = GAMES + ("2026-11-22",)
    for d in games:
        _outcome(db, r.id, d, 30.0)
    _mix_world(db, r.id, games=games, wings=(54, 50, 200))    # the Saints night was New Year's Eve, say
    _confound(db, r.id, "2026-11-22")
    jags = _event(db, "2026-12-06")                            # home, noon, after all three
    mix = gameday.item_mix(r.id, jags, db_path=db)
    assert mix["n"] == 2 and not mix["confounded"] and [g["date"] for g in mix["games"]] == ["2026-10-04", "2026-09-20"]
    assert [p["qty"] for p in gameday.prep_lines(r.id, jags, mix=mix, db_path=db)][0] == 52


def test_a_mix_that_must_count_a_confounded_night_says_so_and_plans_nothing(db):
    r = _restaurant(db)
    _world(db, r.id)
    _mix_world(db, r.id)
    _confound(db, r.id, "2026-10-04")
    _recipe_rows(db, r.id, [("Wings", 1, 0.5)])
    saints = _saints(db)
    mix = gameday.item_mix(r.id, saints, db_path=db)
    assert mix["n"] == 2 and mix["confounded"] and mix["mixed"] == 1
    assert mix["text"].endswith("against a usual Sunday, though 1 of those 2 nights had something else on too.")
    assert gameday.prep_lines(r.id, saints, mix=mix, db_path=db) == []
    assert gameday.order_bump(r.id, saints, mix=mix, db_path=db) is None
    note = gameday.week_note(r.id, today=date(2026, 11, 17), db_path=db)
    assert note["order"] is None and note["text"].endswith("had something else on too. Not yet a pattern to order on.")
    assert "feature" not in gameday.campaign_goal(r.id, saints, mix=mix, db_path=db)
    # The brief's alert carries no prep plan on it either.
    assert "Prep for" not in playbook.alert(r.id, date(2026, 11, 20), db_path=db)["text"]


# ── P3-02 / P3-06 / A1 handoff 6: the big-game test ───────────────────────

def test_a_preseason_game_is_never_judged_big_on_regular_season_nights(db):
    r = _restaurant(db)
    _outcome(db, r.id, "2026-09-20", 60.0)
    _outcome(db, r.id, "2026-10-04", 70.0)
    pre = dict(_event(db, "2026-08-15"), id=-1, event_date="2027-08-14")    # next August's home opener
    assert engine.effect_for(r.id, pre, db_path=db) is None
    assert gameday.big_game(r.id, pre, db_path=db) == (False, None)
    # Its own kind decides: one preseason night that ran big.
    _outcome(db, r.id, "2026-08-15", 80.0)
    big, words = gameday.big_game(r.id, pre, db_path=db, tz="America/New_York")
    assert big and words.startswith("Your last one like it, Bears vs Cleveland Browns · Sat 8/15/26 · 1pm")


def test_one_holiday_night_is_never_a_reason_to_push(db):
    r = _restaurant(db)
    _outcome(db, r.id, "2026-09-28", 106.1, net=11031.0, base=5351.0)     # MNF, home, prime time
    pats = _event(db, "2026-10-22")                                         # TNF, home, prime time
    assert gameday.big_game(r.id, pats, db_path=db)[0]
    _confound(db, r.id, "2026-09-28")
    assert gameday.big_game(r.id, pats, db_path=db) == (False, None)


def test_a_home_game_at_another_ground_is_never_planned_from_the_home_grounds_nights(db):
    r = _restaurant(db)
    _world(db, r.id)
    _mix_world(db, r.id)
    saints = _saints(db)
    away_ground = dict(saints, attributes={"alt_venue": True}, venue="SeatGeek Stadium")
    assert gameday.item_mix(r.id, saints, db_path=db)["n"] == 2
    assert gameday.item_mix(r.id, away_ground, db_path=db) is None


# ── P3-03: one recipe per item name ───────────────────────────────────────

def _recipe_rows(db, rid, rows, ingredient="Chicken wings", unit="lb"):
    """rows [(item, is_active, lb per unit)] — each its own menu_items row,
    all on ONE ingredient."""
    c = models.get_conn(db)
    try:
        iid = c.execute("INSERT INTO ingredients (restaurant_id, name, unit) VALUES (?,?,?)",
                        (rid, ingredient, unit)).lastrowid
        for item, active, per in rows:
            mid = c.execute("INSERT INTO menu_items (restaurant_id, name, is_active) VALUES (?,?,?)",
                            (rid, item, active)).lastrowid
            c.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
                      (mid, iid, per))
        c.commit()
    finally:
        c.close()


def test_the_order_bump_takes_one_active_recipe_per_item_never_a_sum_of_rows(db):
    r = _restaurant(db)
    _world(db, r.id)
    _mix_world(db, r.id)                                   # Wings rose by a median 24
    _recipe_rows(db, r.id, [("Wings", 1, 0.5), ("wings ", 0, 0.5), ("Wings", 1, 0.25)])
    bump = gameday.order_bump(r.id, _saints(db), db_path=db)
    # the newest active "Wings" (0.25 lb): 6 lb — not 12 + 12 + 6
    assert bump["lines"] == [{"ingredient": "Chicken wings", "unit": "lb", "extra": 6.0, "items": ["Wings"]}]


# ── P3-05 / P4-04: "played" is store.played everywhere ────────────────────

def test_an_if_necessary_game_with_no_result_is_never_counted_as_played(db):
    r = _restaurant(db)
    sid = store.series_by_slug("nfl-chicago-bears", db_path=db)["id"]
    _outcome(db, r.id, "2026-09-20", 20.0, net=9600.0, base=8000.0)
    before = gameday.season_value(r.id, sid, today=date(2026, 10, 1), db_path=db)
    _unresolved(db, "2026-09-28")
    sv = gameday.season_value(r.id, sid, today=date(2026, 10, 1), db_path=db)
    assert (before["played"], sv["played"], sv["waiting"]) == (6, 5, 1)
    assert sv["text"].endswith("; 1 if-necessary game not counted until a result is in.")


def test_review_windows_open_only_on_played_games(db):
    from event_intel import reviews
    from test_event_phase4 import _review
    r = _restaurant(db)
    _review(db, r.id, "2026-09-29", ["service"], 10)
    played = reviews.game_night_reviews(r.id, today=date(2026, 10, 1), days=60, db_path=db)
    _unresolved(db, "2026-09-28")
    after = reviews.game_night_reviews(r.id, today=date(2026, 10, 1), days=60, db_path=db)
    assert after["games"] == played["games"] - 1
    assert (played["game_reviews"], after["game_reviews"], after["other_reviews"]) == (10, 0, 10)


# ── P1-04 / P2-05: Ask's games ────────────────────────────────────────────

def test_ask_lists_played_games_only_and_says_each_upcoming_games_status(db, monkeypatch):
    import ask_cavnar_tools as tools
    r = _restaurant(db)
    CLOCK["now"] = datetime(2026, 10, 1, 10, 0)
    monkeypatch.setattr(tools, "_local_today_of", lambda rid: date(2026, 10, 1))
    _unresolved(db, "2026-09-28")                                  # Eagles: if necessary, no result
    _set(db, _event(db, "2026-09-20")["id"], status="postponed")   # Vikings
    store.dismiss(r.id, _event(db, "2026-09-13")["id"], db_path=db)   # Panthers: removed
    _set(db, _event(db, "2026-10-11")["id"], status="cancelled")   # Packers: off the list ahead
    out = tools._read_events(r.id, days=14, past=4)
    said = " ".join(x["what"] for x in out["recent"])
    assert out["recent"] and not any(w in said for w in ("Eagles", "Vikings", "Panthers"))
    ahead = {u["what"].split(" · ")[0]: u for u in out["upcoming"]}
    assert "Bears at Green Bay Packers" not in ahead and ahead["Bears vs New York Jets"]["status"] == "scheduled"


def test_ask_says_kickoffs_on_the_restaurants_clock(db, monkeypatch):
    import ask_cavnar_tools as tools
    r = _restaurant(db)
    update_restaurant(r.id, {"timezone": "America/New_York"}, db_path=db)
    monkeypatch.setattr(tools, "_local_today_of", lambda rid: date(2026, 11, 20))
    out = tools._read_events(r.id, days=3)
    saints = [u for u in out["upcoming"] if "Saints" in u["what"]][0]
    assert saints["kickoff"] == "13:00" and "· 1pm" in saints["what"]
    assert saints["reach_guests"]["text"] == "Sunday around 10am, 3 hours before kickoff"


# ── X-8: one rule for who reads the item mix ──────────────────────────────

def test_one_rule_says_who_reads_the_item_mix_on_every_surface(db, monkeypatch):
    import ask_cavnar_tools as tools
    import morning_brief
    from permissions import FOOD_COST_VIEW
    vis = gameday.item_mix_visible
    assert vis() and vis(denied=set()) and vis(denied={"labor"}) and vis(denied={"inventory"})
    assert not vis(denied={"labor", "inventory"}) and not vis(denied={"labor", "food"})
    assert vis(user={"role": "manager"}) and vis(user={"role": "manager", "grants": [FOOD_COST_VIEW]})
    assert not vis(user={"role": "employee"}) and vis(user={"role": "employee", "is_admin": True})
    r = _restaurant(db)
    _world(db, r.id)
    _mix_world(db, r.id)
    _recipe_rows(db, r.id, [("Wings", 1, 0.5)])
    # Food Cost without Labor: the brief and Ask carry the items (and Ask the
    # ordering bump, Food Cost's as on the game-week screen), never the dollars.
    line = morning_brief._game_line(r, r.id, date(2026, 11, 20), {"labor"}, [], db_path=db)
    assert "Prep for about 52 Wings" in line["text"] and "$" not in line["text"]
    monkeypatch.setattr(tools, "_local_today_of", lambda rid: date(2026, 11, 20))

    class V:
        _ask_denied = frozenset({"labor"})
    saints = [u for u in tools._read_events(r.id, days=7, _viewer=V())["upcoming"] if "Saints" in u["what"]][0]
    assert saints["items_sold"]["prep"] and saints["order_more"]["lines"] and "staffing" not in saints
    V._ask_denied = frozenset({"labor", "inventory"})
    saints = [u for u in tools._read_events(r.id, days=7, _viewer=V())["upcoming"] if "Saints" in u["what"]][0]
    assert "items_sold" not in saints
    line = morning_brief._game_line(r, r.id, date(2026, 11, 20), {"labor", "inventory"}, [], db_path=db)
    assert "Wings" not in line["text"]


# ── the push ───────────────────────────────────────────────────────────────

def _push_world(db, monkeypatch, marketing=1):
    import morning_brief
    import ops
    import push
    import strategy_jobs
    r = _restaurant(db, module_marketing=marketing)
    _world(db, r.id)                                     # home noon games +20, +30: the Saints game is big
    sent = []
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: sent.append((a, k)))
    monkeypatch.setattr(morning_brief, "recipients", lambda rid, db_path=None, include_opted_out=False: [
        {"id": 1, "role": "owner"}])
    # Every login has a phone here; the login's own choices still apply
    # (deliverable_audience's per-type mute test, preferences.push_allowed).
    import preferences
    monkeypatch.setattr(strategy_jobs, "deliverable_audience",
                        lambda rid, ids, db_path=None, alert_type=None: {
                            u for u in ids if not alert_type
                            or preferences.push_allowed(u, rid, alert_type, db_path=db_path)})
    claims = set()
    monkeypatch.setattr(ops, "period_claimed", lambda k, p: (k, p) in claims)
    monkeypatch.setattr(ops, "claim_period", lambda k, p: not ((k, p) in claims or claims.add((k, p))))
    return r, sent, claims


def test_the_guest_text_is_a_starting_rule_and_never_a_time_already_gone(db, monkeypatch):
    r, _, _ = _push_world(db, monkeypatch)
    london = dict(_saints(db), kickoff_local="08:30")       # text "the evening before": Saturday 5pm
    CLOCK["now"] = datetime(2026, 11, 21, 14, 0)
    (_, _, body), = gameday.push_for(get_restaurant(r.id, db_path=db), date(2026, 11, 21), db_path=db, events=[london])
    assert "Guest text, as a starting rule: Saturday around 5pm, the evening before." in body
    CLOCK["now"] = datetime(2026, 11, 21, 17, 20)
    (_, _, body), = gameday.push_for(get_restaurant(r.id, db_path=db), date(2026, 11, 21), db_path=db, events=[london])
    assert "Guest text" not in body and body.startswith("Bears home games have run +25%")


def test_the_briefing_budget_is_checked_before_each_push(db, monkeypatch):
    import notify
    r, sent, claims = _push_world(db, monkeypatch)
    saints = _saints(db)
    other = dict(saints, id=saints["id"] + 100000)
    monkeypatch.setattr(gameday, "tomorrows_games", lambda rest, today, db_path=None: [saints, other])
    left = {"n": 1}

    def allowed(rid, t, db_path=None):
        left["n"] -= 1
        return left["n"] >= 0
    monkeypatch.setattr(notify, "briefing_allowed", allowed)
    CLOCK["now"] = datetime(2026, 11, 21, 15, 0)
    out = gameday.run_event_push(db_path=db, restaurants=[get_restaurant(r.id, db_path=db)])
    assert out["sent"] == 1 and len(sent) == 1 and out["skipped"] == 1
    # the held game stays unclaimed: a later run with budget may send it
    assert len(claims) == 1


def test_the_push_is_silent_inside_the_locations_quiet_hours(db, monkeypatch):
    import notify
    r, sent, claims = _push_world(db, monkeypatch, marketing=0)
    monkeypatch.setattr(notify, "briefing_allowed", lambda *a, **k: True)
    quiet = {"on": True}
    monkeypatch.setattr(models, "is_in_quiet_hours", lambda rid, db_path=None: quiet["on"])
    CLOCK["now"] = datetime(2026, 11, 21, 15, 0)
    gameday.run_event_push(db_path=db, restaurants=[get_restaurant(r.id, db_path=db)])
    assert sent and sent[-1][1]["data"]["quiet"] is True
    quiet["on"] = False
    claims.clear()
    gameday.run_event_push(db_path=db, restaurants=[get_restaurant(r.id, db_path=db)])
    assert len(sent) == 2 and "quiet" not in sent[-1][1]["data"]


def test_the_bell_opens_ask_on_the_game_the_push_was_about(db, monkeypatch):
    import client_api
    import nav
    import notify
    r, sent, _ = _push_world(db, monkeypatch, marketing=0)
    monkeypatch.setattr(notify, "briefing_allowed", lambda *a, **k: True)
    update_restaurant(r.id, {"timezone": "America/New_York"}, db_path=db)
    CLOCK["now"] = datetime(2026, 11, 21, 15, 0)
    gameday.run_event_push(db_path=db, restaurants=[get_restaurant(r.id, db_path=db)])
    pushed = sent[-1][1]["data"]["nav"]
    assert "New+Orleans+Saints" in pushed and "1pm" in pushed            # the restaurant's clock
    CLOCK["now"] = datetime(2026, 11, 23, 9, 0)                           # tapped two days later
    body, _ = client_api._do_get_notifications(r.id)
    row = [x for x in body["notifications"] if x["type"] == "event_ahead"][0]
    assert row["nav"] == pushed
    assert "tomorrow" not in nav.for_notification("event_ahead")


# ── P3-11 / P3-12 / X-9: the campaign goal ────────────────────────────────

def test_the_campaign_goal_says_what_sells_only_past_the_plan_floor(db):
    r = _restaurant(db)
    _world(db, r.id, lifts=(20.0, None))
    _mix_world(db, r.id, games=GAMES[:1], wings=(54,))
    one = gameday.campaign_goal(r.id, _saints(db), db_path=db)
    assert one.startswith("Bring guests in to watch Bears vs New Orleans Saints on Sunday 11/22/26 (12pm, FOX)")
    assert one.endswith("— feature Wings and 4th Quarter Shot, what your last game sold")
    r2 = _restaurant(db, name="Not Every Game Co")
    _world(db, r2.id)
    for day, w in zip(GAMES, (54, 30)):
        _lines(db, r2.id, day, {"Wings": w})
    from test_event_playbook import _usual_sundays
    for d in sorted({d for g in GAMES for d in _usual_sundays(g)} - set(GAMES) - {"2026-09-13"}):
        _lines(db, r2.id, d, {"Wings": 28})
    mix = gameday.item_mix(r2.id, _saints(db), db_path=db)
    assert mix["items"] and not mix["items"][0]["every_game"]
    assert "feature" not in gameday.campaign_goal(r2.id, _saints(db), mix=mix, db_path=db)


# ── P2-04: the send time on the restaurant's clock ────────────────────────

def test_the_send_time_is_on_the_restaurants_clock(db):
    e = _saints(db)                                          # 12:00 Central
    assert gameday.send_plan(e)["text_words"] == "Sunday around 9am, 3 hours before kickoff"
    east = gameday.send_plan(e, tz="America/New_York")       # 1pm in South Bend
    assert east["text_at"] == "2026-11-22T10:00" and east["text_words"] == "Sunday around 10am, 3 hours before kickoff"


# ── X-2: one read of item lines per item_mix ──────────────────────────────

def test_item_mix_reads_the_item_lines_in_two_queries_whatever_the_games(db, monkeypatch):
    r = _restaurant(db)
    _world(db, r.id)
    _mix_world(db, r.id)
    calls = []
    real = gameday._items_by_date
    monkeypatch.setattr(gameday, "_items_by_date", lambda *a: calls.append(a[1]) or real(*a))
    assert gameday.item_mix(r.id, _saints(db), db_path=db)["n"] == 2
    assert len(calls) == 2
