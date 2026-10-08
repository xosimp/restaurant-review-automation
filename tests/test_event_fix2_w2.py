"""Event Intelligence re-audit 2 fixes, brief and report (W2, 10/1/26).

One rule throughout: a game's effect is said ONCE per brief and per report,
with ONE figure — effect_for's, said by the game's own words (the report's
event item, the brief's game line). A forecast that applied the game's own
label names it without a second figure.

  R2-01  a game two or three days out is never carried into "today"
  R2-02  the forecast's label figure and effect_for's segment figure are
         never both said for one game (report, carried and own today line)
  R2-03  the staffing sentence says no sales lift of its own
  R2-05  the game line drops its effect only when THIS game's was said
  R2-07  the carried today line follows the brief's Labor gate
  R2-08  the report's game card names the other games tonight

Nothing here reads the real calendar: every date is passed and the
restaurant's clock is pinned.
"""
import re
from datetime import date, datetime

import pytest

import dsr
from dsr import access
from dsr import memory as dsr_memory
from dsr import store as dsr_store
from dsr import tomorrow
from event_intel import engine, playbook
from test_event_playbook import GAMES, SAINTS, _restaurant, _saints, _world, db  # noqa: F401

# A lift said as the figure — never the low and high of its own range
# ("(median of 2, +20% to +30%)").
FIGURE = re.compile(r"(?<!, )(?<!to )[+−-]\d+%")


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda rid, naive=False: datetime(2026, 11, 20, 6, 0))


def _game_label(db):
    """The forecast's label for the Saints game, as event_memory keeps it."""
    return sorted(playbook.game_labels(_saints(db)))[0]


def _label_forecast(db, lift=30.0, n=4, extra=()):
    """A forecast that applied the game's LABEL figure (+30%, 4 nights — the
    pooled label, preseason and prime time in it), not effect_for's segment."""
    eff = [{"label": _game_label(db), "display": "Bears home game · Soldier Field", "lift_pct": lift, "n": n,
            "kind": "event"}] + list(extra)
    return {"available": True, "typical_sales": 10400.0, "base_sales": 8000.0, "low": 9000.0, "high": 11800.0,
            "samples": 8, "weekday": "Sunday", "effects": eff, "effect_pct": lift}


def _store_report(db, rid, business_date, snap):
    r = dsr_store.create_report(rid, business_date, trigger="sweep", db_path=db)
    dsr_store.save_block(r["id"], "sales", dsr.block(dsr.READY, source="rpower", metrics={"net": 9100.0}),
                         db_path=db)
    dsr_store.save_section(r["id"], "tomorrow", dict(snap, _preds=[]), db_path=db)
    dsr_store.save_narrative(r["id"], {"executive_summary": {"text": "Net sales were $9,100.",
                                                             "cites": ["sales.net"]},
                                       "actions_tomorrow": []}, db_path=db)
    dsr_store.set_stage(r["id"], "collecting", db_path=db)
    dsr_store.set_stage(r["id"], "final", db_path=db)
    return r


def _figures_about_the_game(lines):
    """Every lift figure the brief's lines state in a sentence about the
    Bears game or its label."""
    out = []
    for l in lines:
        for s in re.split(r"(?<=\.)\s|\s·\s|, with ", l.get("text") or ""):
            if "Bears" in s:
                out += FIGURE.findall(s)
    return out


# ── R2-01: a game two or three days out is not "today" ─────────────────────

def test_the_carry_leaves_games_days_out_to_the_game_line(db):
    r = _restaurant(db)
    snap = {"date": "2026-11-20", "weekday": "Friday", "forecast": None, "confidence": None, "predictions": [],
            "items": [{"kind": "weather", "tone": None, "text": "Sunny, high 50°"},
                      {"kind": "event_ahead", "tone": "warn", "event_id": 9,
                       "text": "Sunday: Bears vs New Orleans Saints · 12pm · FOX — 3 days out"}]}
    _store_report(db, r.id, date(2026, 11, 19), snap)
    c = dsr_memory.morning_carry(r.id, date(2026, 11, 20), db_path=db)
    assert [i["kind"] for i in c["items"]] == ["weather"]


def test_the_brief_says_a_game_two_days_out_once_and_counts_from_the_morning(db):
    import morning_brief
    r = _restaurant(db)
    _world(db, r.id)
    snap = tomorrow.build(r, date(2026, 11, 19), facts={}, db_path=db)
    ahead = [i for i in snap["items"] if i["kind"] == "event_ahead"]
    assert ahead and "days out" in ahead[0]["text"]            # the report itself still says it
    _store_report(db, r.id, date(2026, 11, 19), snap)
    brief = morning_brief.build(r.id, today=date(2026, 11, 20), db_path=db)
    texts = [l.get("text") or "" for l in brief["lines"]]
    assert not [t for t in texts if "days out" in t]
    game = [l for l in brief["lines"] if str(l.get("key")).startswith("event_ahead:")]
    assert game and game[0]["text"].startswith("Sunday 11/22/26: Bears vs New Orleans Saints")
    assert sum(t.count("Bears home games have run") for t in texts) == 1


# ── R2-02: one figure for one game, effect_for's ───────────────────────────

def test_the_reports_tomorrow_says_the_game_with_one_figure(db, monkeypatch):
    r = _restaurant(db)
    _world(db, r.id)                                     # effect_for: +25%, median of 2
    monkeypatch.setattr(tomorrow, "_forecast", lambda rid, day, db_path: _label_forecast(db))
    snap = tomorrow.build(r, date(2026, 11, 21), facts={}, db_path=db)
    (ev,) = [i for i in snap["items"] if i["kind"] == "event"]
    assert "Bears home games have run +25%" in ev["text"] and ev["has_effect"] is True
    fc = snap["forecast"]
    assert fc["effects"][0]["game_event_id"] == _saints(db)["id"]
    assert "+30%" not in fc["basis"] and "Bears home game · Soldier Field's measured effect" in fc["basis"]
    # the effect still moved the forecast's number: the data keeps its figure
    assert fc["effects"][0]["lift_pct"] == 30.0
    assert tomorrow.effect_words({"display": "Cubs", "lift_pct": 9.0, "n": 1}, figure=False) == "Cubs' measured effect"
    assert tomorrow.effect_words({"display": "Payday", "lift_pct": 8.0, "n": 1}) == "Payday +8% (measured 1 time here)"


def test_the_carried_today_line_says_the_game_with_one_figure(db, monkeypatch):
    import morning_brief
    r = _restaurant(db)
    _world(db, r.id)
    monkeypatch.setattr(tomorrow, "_forecast", lambda rid, day, db_path: _label_forecast(db))
    snap = tomorrow.build(r, date(2026, 11, 21), facts={}, db_path=db)
    _store_report(db, r.id, date(2026, 11, 21), snap)
    brief = morning_brief.build(r.id, today=date(2026, 11, 22), db_path=db)
    (today,) = [l for l in brief["lines"] if l["key"] == "today"]
    assert today["source"] == "dsr" and "Field +30%" not in today["text"]
    assert "Bears home game · Soldier Field's measured effect" in today["text"]
    # the game is said once, on its own line (owner, 10/8/26): the report's
    # item about it leaves the today line and the game line says it
    (game,) = [l for l in brief["lines"] if str(l["key"]).startswith("event_ahead:")]
    assert "have run +25%" not in today["text"] and "+25%" in game["text"]
    assert _figures_about_the_game(brief["lines"]) == ["+25%"]
    assert ".." not in today["text"]                    # a carried sentence's own stop is not doubled
    assert not [k for l in brief["lines"] for k in l if k in ("_items", "_said")]     # never sent


def test_the_carried_line_keeps_the_figure_when_the_games_item_is_not_shown(monkeypatch):
    import morning_brief
    fx = {"label": "bears soldier field", "display": "Bears home game · Soldier Field", "lift_pct": 30.0, "n": 4,
          "kind": "event", "game_event_id": 7}
    carry = {"forecast": {"typical": 10400.0, "low": 9000.0, "high": 11800.0, "effects": [fx]},
             "items": [{"kind": "weather", "text": "Rain forecast (80% chance)"}]}
    line = morning_brief._carry_today_line(carry, date(2026, 11, 22))
    assert "Bears home game · Soldier Field +30% (measured 4 times here)" in line["text"]
    assert line["_said"] == ["bears soldier field"]


def test_the_brief_own_forecast_names_todays_game_and_the_game_line_says_its_figure(db, monkeypatch):
    import demand
    import morning_brief
    r = _restaurant(db)
    _world(db, r.id)
    monkeypatch.setattr(demand, "forecast_day", lambda rid, day=None, db_path=None: _label_forecast(db))
    brief = morning_brief.build(r.id, today=date(2026, 11, 22), db_path=db)
    (today,) = [l for l in brief["lines"] if l["key"] == "today"]
    assert "Bears home game · Soldier Field's measured effect" in today["text"] and "+30%" not in today["text"]
    game = [l for l in brief["lines"] if str(l.get("key")).startswith("event_ahead:")]
    assert game and "Bears home games have run +25%" in game[0]["text"]
    assert _figures_about_the_game(brief["lines"]) == ["+25%"]


# ── R2-03: the staffing sentence carries no lift of its own ────────────────

def test_the_staffing_plan_says_no_second_lift_in_the_brief_or_the_report(db):
    r = _restaurant(db)
    _world(db, r.id)
    st = playbook.staffing(r.id, _saints(db), db_path=db)
    assert st["recommend"] and "%" not in st["text"] and st["lift_pct"] == 25.0
    line = playbook.alert(r.id, date(2026, 11, 20), db_path=db)
    assert "Staff above a usual Sunday" in line["text"] and FIGURE.findall(line["text"]) == ["+25%"]
    snap = tomorrow.build(r, date(2026, 11, 21), facts={}, db_path=db)
    (gs,) = [i for i in snap["items"] if i["kind"] == "game_staffing"]
    assert "%" not in gs["text"] and gs["plan"] is True


# ── R2-05: "said" only when THIS game's effect was said ────────────────────

def test_another_measured_effect_on_the_today_line_leaves_the_games_effect_said(db, monkeypatch):
    import demand
    import morning_brief
    r = _restaurant(db)
    _world(db, r.id)
    payday = {"label": "payday", "display": "Payday", "lift_pct": 8.0, "n": 6, "kind": "payday"}
    monkeypatch.setattr(demand, "forecast_day", lambda rid, day=None, db_path=None: {
        "available": True, "typical_sales": 8640.0, "base_sales": 8000.0, "low": 7000.0, "high": 9900.0,
        "samples": 8, "weekday": "Sunday", "effects": [payday]})
    brief = morning_brief.build(r.id, today=date(2026, 11, 22), db_path=db)
    (today,) = [l for l in brief["lines"] if l["key"] == "today"]
    assert "Payday +8% (measured 6 times here)" in today["text"]
    game = [l for l in brief["lines"] if str(l.get("key")).startswith("event_ahead:")]
    assert game and "Bears home games have run +25%" in game[0]["text"]


def test_the_games_own_label_said_with_its_figure_is_not_said_again(db):
    r = _restaurant(db)
    _world(db, r.id)
    day = date(2026, 11, 22)
    assert "have run" in playbook.alert(r.id, day, said_labels={"payday"}, db_path=db)["text"]
    assert "have run" not in playbook.alert(r.id, day, said_labels={_game_label(db)}, db_path=db)["text"]


def test_a_carried_item_without_the_effect_leaves_it_to_the_game_line(db):
    r = _restaurant(db)
    _world(db, r.id)
    sid = _saints(db)["id"]
    bare = [{"kind": "event", "event_id": sid, "has_effect": False}]
    said = [{"kind": "event", "event_id": sid, "has_effect": True}]
    assert "have run +25%" in playbook.alert(r.id, date(2026, 11, 21), carried=bare, db_path=db)["text"]
    assert "have run" not in playbook.alert(r.id, date(2026, 11, 21), carried=said, db_path=db)["text"]
    # "what was staffed" carried is no plan: the plan is still said
    staffed = [{"kind": "game_staffing", "event_id": sid, "plan": False}]
    line = playbook.alert(r.id, date(2026, 11, 21), carried=staffed, db_path=db)
    assert "Staff above a usual Sunday" in line["text"] and line["claim_kind"] == "inferred"


# ── R2-07: the carried today line follows the Labor gate ───────────────────

def test_a_login_without_the_labor_view_reads_the_carried_game_without_its_lift(db):
    import morning_brief
    r = _restaurant(db)
    _world(db, r.id)
    snap = tomorrow.build(r, date(2026, 11, 21), facts={}, db_path=db)
    # As the carry hands it to this login (access.tomorrow_for: the
    # staffing item is the Labor view's)
    t = access.tomorrow_for({"tomorrow": snap}, {"role": "manager"},
                                access.view_for({"role": "manager"}), withheld=["labor"])
    carry = {"forecast": t["forecast"], "items": t["items"]}
    assert [i["kind"] for i in carry["items"]] == ["event"]
    hidden = morning_brief._carry_today_line(carry, date(2026, 11, 22), show_forecast=False)
    assert "Bears vs New Orleans Saints" in hidden["text"] and "%" not in hidden["text"]
    shown = morning_brief._carry_today_line(carry, date(2026, 11, 22), show_forecast=True)
    assert "Bears home games have run +25%" in shown["text"]
    # a report stored before `plain`: the game is left to the game line
    old = {"items": [{"kind": "event", "text": "Bears vs X. Bears home games have run +25% …", "event_id": 1}]}
    assert morning_brief._carry_today_line(old, date(2026, 11, 22), show_forecast=False) is None


# ── R2-08: the report's game card names the others tonight ─────────────────

def test_the_report_card_names_the_other_games_tonight(db, monkeypatch):
    import os
    from event_intel import store
    r = _restaurant(db)
    s = store.series_by_slug("nfl-chicago-bears", db_path=db)
    other = store.upsert_series({"slug": "test-fc", "name": "Test FC", "short_name": "FC", "category": "sports",
                                 "league": "MLS", "home_venue": "Field", "timezone": "America/Chicago",
                                 "lat": None, "lng": None, "radius_km": None}, db_path=db)
    store.upsert_events(other, [{"external_id": "fc1", "date": SAINTS, "kickoff": "19:30", "home_away": "home",
                                 "opponent": "Rivals", "venue": "Field", "season": 2026, "season_type": "regular"}],
                        timezone="America/Chicago", db_path=db)
    store.set_follow(r.id, other, True, db_path=db)
    assert s
    monkeypatch.setattr(playbook, "usual_net", lambda rid, day, db_path=None: {"median": 8000.0, "n": 6})
    g = playbook.game_night(r.id, SAINTS, net=9000.0, db_path=db)
    assert g["also"] and len(g["also"]) == len(g["others"]) == 1
    assert {"Rivals" in g["describe"], "Rivals" in g["also"][0]} == {True, False}     # the other game is named
    assert g["text"].endswith(f"Also tonight: {g['also'][0]}.")
    html = open(os.path.join(os.path.dirname(__file__), "..", "templates", "dashboard.html")).read()
    body = html[html.index("function drGameHtml(g){"):]
    body = body[:body.index("\n  }\n")]
    assert "g.also" in body and "h+=note('Also tonight: '+prose(also)" in body
