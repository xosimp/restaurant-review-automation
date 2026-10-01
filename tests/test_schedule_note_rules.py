"""The Studio's AI notes, made reliable (owner, 10/1/26: "I don't want them to
say 'I typed something here and it didn't do what I said'").

Each sentence is read deterministically: a minimum code can hold is offered
as a rule (every week or one week) and, once confirmed, is a role floor the
draft is built to and checked against, named in the breach; what code can't
hold says so; a rule about one person points to their scheduling notes.
"""
import inspect
import sys
from datetime import date, timedelta

import pytest

import models
import schedule_note_rules as snr
import schedule_rules
from models import Restaurant, create_restaurant

ROLES = ["Line Cook", "Prep Cook", "Bartender", "Server", "Host"]


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(snr, "restaurant_roles", lambda rid, db_path=None: list(ROLES))
    return db_path


@pytest.fixture
def rid(db):
    return create_restaurant(Restaurant(name="EJ Co", owner_email="e@x.com", timezone="America/Chicago"), db_path=db)


def _next_monday():
    t = date.today()
    return t + timedelta(days=(7 - t.weekday()) % 7 or 7)


# ── reading ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,role,n,parts,days,scope", [
    ("Keep two line cooks on Friday lunch.", "Line Cook", 2, ["morning"], ["Friday"], "every"),
    ("At least 2 bartenders Saturday night", "Bartender", 2, ["night"], ["Saturday"], "every"),
    ("Never cut the host", "Host", 1, ["morning", "night"], None, "every"),
    ("2 servers at dinner on weekends", "Server", 2, ["night"], ["Saturday", "Sunday"], "every"),
    ("Three servers on lunch at all times this week", "Server", 3, ["morning"], None, "week"),
])
def test_a_minimum_is_read_as_a_rule(text, role, n, parts, days, scope):
    got = snr.read_sentence(text, ROLES)
    assert got["kind"] == "rule"
    r = got["rule"]
    assert (r["role"], r["min"], r["dayparts"], r["days"], r["scope"]) == (role, n, parts, days, scope)


def test_two_roles_fit_and_the_owner_picks():
    got = snr.read_sentence("Two cooks on lunch", ROLES)
    assert got["kind"] == "rule" and got["rule"]["role"] is None
    assert set(got["rule"]["role_choices"]) == {"Line Cook", "Prep Cook"} and "pick which" in got["why"]


@pytest.mark.parametrize("text,why", [
    ("No more than 4 servers on Monday", "maximum"),
    ("One more bartender on Fridays", "relative"),
    ("Fewer servers after 9pm Sunday to Thursday", "hour of the day"),
    ("Make sure the closers do side work", "doesn't name one of your roles"),
])
def test_what_code_cannot_hold_says_so(text, why):
    got = snr.read_sentence(text, ROLES)
    assert got["kind"] == "unchecked" and why in got["why"]


def test_game_days_are_a_condition_not_a_standing_rule():
    got = snr.read_sentence("Open with a bartender on game days", ROLES)
    assert got["kind"] == "unchecked" and "depends on something" in got["why"]


@pytest.mark.parametrize("text", [
    "We don't need two line cooks on Monday lunch", "Don't need a host on Monday", "We can't afford 3 servers on Monday",
    "We don't always need 2 servers at lunch", "2 servers is too many on Monday", "By 8 one server can go home",
    "At 5 servers come in on Friday", "2 of our 5 servers should be closers", "We have 6 servers",
    "Send 1 server home after 9pm", "Cut to 1 bartender after 10", "Keep 4 servers on 10/12",
    "When the Bears play, 3 bartenders", "Mother's Day 6 servers", "Ideally 3 servers at lunch",
    "Need a server and a bartender at lunch", "Lunch 1 server, dinner 3 servers", "Max 2 bartenders on Friday",
    "If it rains, 2 servers on the patio", "We used to have 3 hosts",
])
def test_nothing_that_means_something_else_is_offered_as_a_rule(text):
    """Blind audit, 10/1/26: each of these was offered as a minimum."""
    assert snr.read_sentence(text, ROLES, ["Max Ruiz"])["kind"] in ("unchecked", "person")


@pytest.mark.parametrize("text,days", [
    ("Keep 2 servers Fri-Sun at dinner", ["Friday", "Saturday", "Sunday"]),
    ("At least 2 bartenders Thursday through Saturday night", ["Thursday", "Friday", "Saturday"]),
    ("Keep 3 servers every day except Monday at dinner", ["Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
                                                           "Sunday"]),
    ("Keep 2 servers on Tues and Thurs at lunch", ["Tuesday", "Thursday"]),
    ("Keep 2 servers Sun to Tue at dinner", ["Monday", "Tuesday", "Sunday"]),
])
def test_day_ranges_abbreviations_and_exceptions(text, days):
    got = snr.read_sentence(text, ROLES)
    assert got["kind"] == "rule" and got["rule"]["days"] == days


def test_weeknights_are_dinners_monday_to_thursday_and_said_so():
    got = snr.read_sentence("Keep 2 servers on weeknights", ROLES)
    assert got["rule"]["dayparts"] == ["night"] and got["rule"]["days"] == ["Monday", "Tuesday", "Wednesday", "Thursday"]
    assert "Monday to Thursday" in got["why"]


def test_a_rule_about_one_person_goes_to_their_notes():
    got = snr.read_sentence("Marcus only closes on weekends", ROLES, ["Marcus Lee"])
    assert got["kind"] == "person" and got["person"] == "Marcus Lee"
    # "Will" opening a sentence is a verb, not Will Smith.
    assert snr.read_sentence("Will need two line cooks on lunch", ROLES, ["Will Smith"])["kind"] == "rule"
    # Everyday words are never a single-name person (Max, May, Grant).
    assert snr.read_sentence("Keep 2 servers on Saturday, servers may leave early", ROLES, ["May"])["kind"] != "person"


def test_guidance_is_labelled_as_unchecked_guidance():
    assert snr.read_sentence("Play the game on every TV", ROLES)["kind"] == "guidance"


# ── the rules ──────────────────────────────────────────────────────────────

def test_a_rule_is_validated(rid, db):
    with pytest.raises(ValueError, match="your roles"):
        snr.add_rule(rid, "Juggler", 2, ["morning"], db_path=db)
    with pytest.raises(ValueError, match="from 1 to"):
        snr.add_rule(rid, "Server", 11, ["morning"], db_path=db)
    with pytest.raises(ValueError, match="lunch, dinner or both"):
        snr.add_rule(rid, "Server", 2, [], db_path=db)
    with pytest.raises(ValueError, match="already happened"):
        snr.add_rule(rid, "Server", 2, ["night"], scope="week", week_start="2020-01-08", db_path=db)


def _constraints(rid, monday):
    dates = [(monday + timedelta(days=i)).isoformat() for i in range(7)]
    return schedule_rules.Constraints(restaurant_id=rid, week_dates=dates, week_days=list(snr.DAYS))


def test_a_confirmed_rule_is_a_floor_for_its_week_only(rid, db):
    mon = _next_monday()
    every = snr.add_rule(rid, "line cook", 2, ["morning"], days=["Friday"], scope="every",
                         source_text="Keep two line cooks on Friday lunch.", db_path=db)
    week = snr.add_rule(rid, "Bartender", 3, ["night"], scope="week", week_start=(mon + timedelta(days=2)).isoformat(),
                        source_text="Three bartenders every night this week", db_path=db)
    assert every["role"] == "Line Cook" and week["week_start"] == mon.isoformat()
    assert "the week of" in week["words"] and "every week" in every["words"]
    c = _constraints(rid, mon)
    snr.apply_note_rules(c, rid, db_path=db)
    assert schedule_rules.floor_for(c.role_floors, "Line Cook", "Friday", "morning") == 2
    assert schedule_rules.floor_for(c.role_floors, "Line Cook", "Thursday", "morning") == 0
    assert schedule_rules.floor_for(c.role_floors, "Bartender", "Tuesday", "night") == 3
    assert c.rule_floor_sources[("line cook", "Friday", "morning")] == "Keep two line cooks on Friday lunch."
    later = _constraints(rid, mon + timedelta(days=7))
    snr.apply_note_rules(later, rid, db_path=db)
    assert schedule_rules.floor_for(later.role_floors, "Bartender", "Tuesday", "night") == 0
    assert schedule_rules.floor_for(later.role_floors, "Line Cook", "Friday", "morning") == 2
    assert snr.remove_rule(rid, every["id"], db_path=db) and not snr.remove_rule(rid, every["id"], db_path=db)
    gone = _constraints(rid, mon)
    snr.apply_note_rules(gone, rid, db_path=db)
    assert schedule_rules.floor_for(gone.role_floors, "Line Cook", "Friday", "morning") == 0


def test_a_draft_short_of_the_rule_is_a_breach_that_names_the_note(rid, db):
    mon = _next_monday()
    snr.add_rule(rid, "Line Cook", 2, ["morning"], days=["Friday"], source_text="Keep two line cooks on Friday lunch.",
                 db_path=db)
    c = _constraints(rid, mon)
    snr.apply_note_rules(c, rid, db_path=db)
    friday = (mon + timedelta(days=4)).isoformat()
    rows = [{"date": friday, "day": "Friday", "employee": "Ana", "role": "Line Cook", "shift_start": "10:00",
             "shift_end": "16:00", "scheduled_hours": 6}]
    v = [x for x in schedule_rules._coverage_violations(rows, c) if x["kind"] == "coverage_floor"]
    assert v and "Keep two line cooks on Friday lunch." in v[0]["detail"]


def test_build_constraints_applies_the_rules():
    src = inspect.getsource(schedule_rules.build_constraints)
    assert "schedule_note_rules.apply_note_rules(c, restaurant_id" in src


def test_read_notes_marks_the_sentence_a_rule_came_from(rid, db):
    snr.add_rule(rid, "Line Cook", 2, ["morning"], days=["Friday"], source_text="Keep two line cooks on Friday lunch.",
                 db_path=db)
    got = snr.read_notes(rid, "Keep two line cooks on Friday lunch. Play the game on every TV.", db_path=db)
    assert got[0]["rule_id"] and "at least 2 Line Cook at lunch, Fri" in got[0]["enforced"]
    assert "rule_id" not in got[1]


def test_the_draft_ranks_the_owners_notes():
    import labor
    src = inspect.getsource(labor.generate_optimized_schedule)
    assert "Quality preferences — the owner's ADDITIONAL SCHEDULING NOTES first" in src and "Follow each one" in src


# ── the routes ─────────────────────────────────────────────────────────────

def test_the_routes(rid, db, monkeypatch):
    from flask import Flask
    import strategy_routes
    monkeypatch.setattr(strategy_routes, "_sees_labor", lambda u: True)
    monkeypatch.setattr(strategy_routes, "_principal", lambda u: True)
    import client_api
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    models.update_restaurant(rid, {"sched_notes": "Keep two line cooks on Friday lunch."}, db_path=db)
    u = {"restaurant_id": rid, "role": "owner", "id": 1, "email": "e@x.com"}
    app = Flask(__name__)
    with app.test_request_context("/labor/note-rules"):
        body, status = strategy_routes._do_note_rules_get(u)
    assert status == 200 and body["sentences"][0]["kind"] == "rule" and body["week_of"]
    with app.test_request_context("/labor/note-rules", method="POST",
                                  json={"role": "Line Cook", "min": 2, "dayparts": ["morning"], "days": ["Friday"],
                                        "scope": "every", "source_text": "Keep two line cooks on Friday lunch."}):
        body, status = strategy_routes._do_note_rule_add(u)
    assert status == 200 and body["rule"]["min_people"] == 2
    with app.test_request_context("/labor/note-rules", method="POST", json={"role": "Juggler", "min": 2,
                                                                             "dayparts": ["night"]}):
        assert strategy_routes._do_note_rule_add(u)[1] == 400
    with app.test_request_context("/labor/note-rules/1/remove", method="POST", json={}):
        assert strategy_routes._do_note_rule_remove(u, body["rule"]["id"] if "rule" in body else 1)[1] in (200, 404)
    # A manager who may draft still cannot set or remove the owner's floors.
    monkeypatch.setattr(strategy_routes, "_principal", lambda u: False)
    monkeypatch.setattr(strategy_routes, "_may_draft", lambda u: True)
    with app.test_request_context("/labor/note-rules", method="POST", json={}):
        assert strategy_routes._do_note_rule_add(u)[1] == 403
    with app.test_request_context("/labor/note-rules/1/remove", method="POST", json={}):
        assert strategy_routes._do_note_rule_remove(u, 1)[1] == 403


# ── blind audit regressions (10/1/26) ─────────────────────────────────────

def test_the_same_rule_twice_is_one_rule(rid, db):
    a = snr.add_rule(rid, "Server", 2, ["night"], days=["Friday"], db_path=db)
    b = snr.add_rule(rid, "Server", 2, ["night"], days=["Friday"], db_path=db)
    assert b["id"] == a["id"] and b.get("existing")
    with pytest.raises(ValueError, match="whole number"):
        snr.add_rule(rid, "Server", 2.7, ["night"], db_path=db)


def test_a_dinner_only_day_gets_no_lunch_floor(rid, db):
    mon = _next_monday()
    snr.add_rule(rid, "Host", 1, ["morning", "night"], source_text="Never cut the host", db_path=db)
    c = _constraints(rid, mon)
    c.open_times = {d: "16:00" for d in snr.DAYS}
    snr.apply_note_rules(c, rid, db_path=db)
    assert schedule_rules.floor_for(c.role_floors, "Host", "Friday", "morning") == 0
    assert schedule_rules.floor_for(c.role_floors, "Host", "Friday", "night") == 1


def test_a_rule_for_a_role_nobody_holds_is_named_not_a_floor(rid, db, monkeypatch):
    snr.add_rule(rid, "Server", 2, ["night"], source_text="Keep two servers at dinner", db_path=db)
    monkeypatch.setattr(snr, "restaurant_roles", lambda r, db_path=None: ["Bartender"])
    c = _constraints(rid, _next_monday())
    snr.apply_note_rules(c, rid, db_path=db)
    assert schedule_rules.floor_for(c.role_floors, "Server", "Friday", "night") == 0
    assert any("nobody on the roster is a Server" in x for x in c.owner_rules_unchecked)


def test_a_one_week_rule_never_leaks_into_a_two_week_check(rid, db):
    mon = _next_monday()
    snr.add_rule(rid, "Bartender", 3, ["night"], scope="week", week_start=mon.isoformat(), db_path=db)
    dates = [(mon + timedelta(days=i)).isoformat() for i in range(14)]
    c = schedule_rules.Constraints(restaurant_id=rid, week_dates=dates, week_days=list(snr.DAYS))
    snr.apply_note_rules(c, rid, db_path=db)
    assert schedule_rules.floor_for(c.role_floors, "Bartender", "Friday", "night") == 0


def test_every_cut_surface_reads_the_floors_with_the_note_rules(rid, db):
    snr.add_rule(rid, "Line Cook", 2, ["morning"], days=["Friday"], db_path=db)
    r = models.get_restaurant(rid, db_path=db)
    floors = schedule_rules.effective_role_floors(r, _next_monday(), db_path=db)
    assert schedule_rules.floor_for(floors, "Line Cook", "Friday", "morning") == 2
    import ask_cavnar_tools
    import strategy_jobs
    assert "effective_role_floors(restaurant)" in inspect.getsource(schedule_rules.cut_policy)
    assert "_sr.effective_role_floors(r)" in inspect.getsource(ask_cavnar_tools)
    assert "_sr.effective_role_floors(restaurant, local.date())" in inspect.getsource(strategy_jobs.staffing_move)


def test_cavnar_ais_questions_never_ride_as_the_owners_instructions():
    import labor
    import schedule_engine
    src = inspect.getsource(labor.generate_optimized_schedule)
    assert "partition(SCHED_FINDINGS_HEADER)" in src
    assert "SCHED_FINDINGS_HEADER" in inspect.getsource(schedule_engine._sched_notes_with_findings)
    assert "never instructions" in labor.SCHED_FINDINGS_HEADER
