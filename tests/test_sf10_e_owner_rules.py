"""Scheduler audit 10/10/26, workstream E — RM-9, AI-9, AI-19, AI-10,
contract #5: the owner's words become typed rules a small model proposes,
code validates, the owner confirms, and every pass enforces."""
import json
import schedule_context
import types

import pytest

import models
import owner_rules as orr
import schedule_engine as se
import schedule_note_rules as snr
import schedule_rules as sr
from models import Restaurant, create_restaurant

WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
NAMES = ["Maria Lopez", "Ana Ruiz", "Ben Cole"]
ROLES = ["Server", "Bartender"]


class _Msg:
    def __init__(self, payload):
        self.content = [types.SimpleNamespace(type="text", text=json.dumps(payload))]
        self.stop_reason = "end_turn"
        self.usage = None


def _rule(kind, **kw):
    base = {"kind": kind, "role": "", "person": "", "person2": "", "days": [], "dates": [], "daypart": "",
            "value": 0, "time": "", "quote": ""}
    base.update(kw)
    return base


def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=list(NAMES),
                       active={n.lower() for n in NAMES})
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(d, who, start="4:00pm", end="10:00pm", role="Server"):
    return {"date": d, "day": DAYS[WEEK.index(d)], "employee": who, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": "6", "notes": ""}


# ── validating what a model proposes ──────────────────────────────────────

def test_a_proposed_rule_is_held_only_when_every_part_can_be_stood_on():
    s = "Maria closes Friday"
    ok = orr.validate(_rule("close", person="Maria", days=["Friday"], quote="Maria closes Friday"), s, NAMES, ROLES)
    assert ok["person"] == "Maria Lopez" and ok["days"] == ["Friday"]           # a first name that is one person
    assert orr.validate(_rule("close", person="Zed", days=["Friday"]), s, NAMES, ROLES) is None      # not on roster
    assert orr.validate(_rule("floor", role="Chef", value=2), "2 chefs", NAMES, ROLES) is None        # no such role
    assert orr.validate(_rule("floor", role="Server", value=40), "40 servers", NAMES, ROLES) is None  # out of range
    assert orr.validate(_rule("person_off", person="Ana Ruiz", dates=["2026-12-25"]), "Ana off 12/25", NAMES, ROLES,
                        week_dates=WEEK) is None                                                       # not this week
    # A weight is a direction; the step is code's (blind re-audit 10/11/26 AI-9).
    assert orr.validate(_rule("weight", value=-0.9, days=["Monday"]), "light Monday", NAMES, ROLES)["value"] == \
        -orr.WEIGHT_STEP
    assert orr.validate(_rule("weight", value=0, days=["Monday"]), "light Monday", NAMES, ROLES) is None
    w = orr.validate(_rule("weight", value=-0.2, days=["Monday"], quote="invented words"), "keep Monday light",
                     NAMES, ROLES)
    assert w["value"] == -orr.WEIGHT_STEP and w["quote"] == "keep Monday light"  # never words the owner did not write
    assert orr.validate(_rule("avoid", person="Ana Ruiz", person2="Ana Ruiz"), "x", NAMES, ROLES) is None


# ── reading: the deterministic reader first, then the model, cached ──────

@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return create_restaurant(Restaurant(name="Words Grill", owner_email="w@x.com"), db_path=db_path)


def test_each_sentence_is_read_once_and_cached(rid):
    calls = []

    def call(req):
        calls.append(req)
        # The rule extraction's model honours the T1 canary (re-audit 10/11/26
        # AI-26): Haiku 5.5 inside it, the tier's previous model outside.
        import ai_utils
        assert req["model"] == ai_utils.model_for_run("schedule_rules", rid)
        assert req["output_config"]["format"]["type"] == "json_schema"
        assert "UNTRUSTED_GUEST_TEXT" in req["messages"][0]["content"]       # the owner's words, fenced as data
        return _Msg({"sentences": [{"index": 0, "rules": [_rule("close", person="Maria Lopez", days=["Friday"],
                                                                   quote="Maria closes Friday")]},
                                   {"index": 1, "rules": [_rule("none")]}]})
    got = orr.read(rid, ["Maria closes Friday", "We love our team"], "notes", NAMES, ROLES, call=call)
    assert [r["kind"] for r in got["Maria closes Friday"]] == ["close"] and got["We love our team"] == []
    again = orr.read(rid, ["Maria closes Friday", "We love our team"], "notes", NAMES, ROLES, call=call)
    assert len(calls) == 1 and again["Maria closes Friday"][0]["person"] == "Maria Lopez"


def test_a_plain_minimum_is_still_the_deterministic_readers(rid):
    def call(req):
        raise AssertionError("no model for a sentence the reader holds")
    got = orr.read(rid, ["Keep 2 servers on Friday dinner"], "notes", NAMES, ROLES, call=call)
    (r,) = got["Keep 2 servers on Friday dinner"]
    assert r["kind"] == "floor" and r["value"] == 2 and r["daypart"] == "night" and r["days"] == ["Friday"]


def test_a_failed_read_leaves_the_sentence_prose(rid):
    def call(req):
        raise RuntimeError("provider down")
    assert orr.read(rid, ["Jen and Tom never together"], "notes", NAMES, ROLES, call=call) == {}


def test_the_model_reads_nothing_when_switched_off(rid, monkeypatch):
    monkeypatch.setenv("OWNER_RULES_READ", "0")
    import ai_utils
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read")))
    assert orr.read(rid, ["Jen and Tom never together"], "notes", NAMES, ROLES) == {}


# ── enforcing (contract #5: Constraints → solver, repair, scorer) ─────────

def test_rules_land_in_the_constraints_every_pass_reads():
    c = _c(role_families={"server": "server", "bartender": "bartender"})
    orr.apply(c, [
        dict(_rule("person_off", person="Ana Ruiz", days=["Friday"], quote="Ana off Friday"), confirmed=True),
        dict(_rule("person_off", person="Ben Cole", days=["Saturday"], quote="Ben off Sat"), confirmed=False),
        dict(_rule("avoid", person="Ana Ruiz", person2="Ben Cole", quote="Ana and Ben apart"), confirmed=True),
        dict(_rule("max", role="Server", value=4, daypart="night", quote="no more than 4 servers"), confirmed=True),
        dict(_rule("start", role="Bartender", time="4:30pm", days=["Friday"], daypart="night", quote="bar at 4:30"),
             confirmed=True),
        dict(_rule("close", person="Maria Lopez", days=["Friday"], quote="Maria closes Friday"), confirmed=True),
        dict(_rule("weight", value=-0.2, days=["Monday"], quote="keep Monday light"), confirmed=False),
        dict(_rule("floor", role="Server", value=3, days=["Saturday"], daypart="night", quote="3 servers sat"),
             confirmed=False)])
    ok, why = c.can_work("Ana Ruiz", WEEK[4], "night")
    assert not ok and why.startswith("your rule:")                                  # confirmed: hard
    assert c.can_work("Ben Cole", WEEK[5], "night")[0]                              # a proposal: never hard,
    assert c.fillable("Ben Cole", WEEK[5])[0]                                       # nor a fill exclusion (AI-8)
    assert {p["kind"] for p in c.owner_proposals} == {"person_off", "weight", "floor"}
    assert {"kind": "avoid", "a": "Ana Ruiz", "b": "Ben Cole"}.items() <= c.owner_pairs[0].items()
    assert c.role_caps["server"][0]["max"] == 4
    assert c.role_times[("bartender", "Friday", "night")]["start"] == 16 * 60 + 30
    assert c.owner_person_close[0]["hard"] and c.owner_person_close[0]["dates"] == [WEEK[4]]
    assert not c.owner_weights                                                      # an unconfirmed weight moves nothing
    assert sr.floor_for(c.role_floors, "Server", "Saturday", "night") == 0          # an unconfirmed floor is not held
    assert len(c.typed_rules) == 8 and all(r.get("words") for r in c.typed_rules)


def test_maria_closes_friday_is_a_hard_breach_until_she_does_and_no_pass_may_undo_it():
    c = _c(role_families={"server": "server"})
    orr.apply(c, [dict(_rule("close", person="Maria Lopez", days=["Friday"], quote="Maria closes Friday"),
                       confirmed=True)])
    held = [_row(WEEK[4], "Ana Ruiz", "4:00pm", "9:00pm"), _row(WEEK[4], "Maria Lopez", "5:00pm", "11:00pm")]
    assert not [v for v in sr.violations(held, c) if v["kind"].startswith("owner_")]
    # The solver re-solving the close to Ana — the AI-9 failure — is a new
    # hard breach at the coverage rank: every pass below it refuses it.
    undone = [_row(WEEK[4], "Maria Lopez", "4:00pm", "9:00pm"), _row(WEEK[4], "Ana Ruiz", "5:00pm", "11:00pm")]
    (v,) = [v for v in sr.violations(undone, c) if v["kind"].startswith("owner_")]
    assert v["kind"] == "owner_word_close" and v["hard"] and "Maria closes Friday" in v["detail"]
    worse = sr.regressions(sr.breach_profile(held, c), sr.breach_profile(undone, c), upto=sr.TIER_BUDGET)
    assert any(w["id"][0] == "owner_word_close" for w in worse)


def test_the_repair_stage_hands_the_closing_shift_to_the_person_named():
    c = _c(role_families={"server": "server"})
    orr.apply(c, [dict(_rule("close", person="Maria Lopez", days=["Friday"], quote="Maria closes Friday"),
                       confirmed=True)])
    rows = [_row(WEEK[4], "Ana Ruiz", "5:00pm", "11:00pm"), _row(WEEK[3], "Maria Lopez", "11:00am", "3:00pm")]
    out = orr.repair(rows, c)
    assert out["rows"][0]["employee"] == "Maria Lopez" and out["fixed"][0]["kind"] == "owner_words"
    assert not orr.misses(out["rows"], c)


def test_a_person_on_rule_is_put_right_or_named():
    c = _c(role_families={"server": "server"}, blocked_dates={"maria lopez": {WEEK[5]: "on approved time off"}})
    orr.apply(c, [dict(_rule("person_on", person="Maria Lopez", days=["Saturday"], daypart="night",
                             quote="Maria on Saturday night"), confirmed=True)])
    out = orr.repair([_row(WEEK[5], "Ana Ruiz")], c)
    assert out["rows"][0]["employee"] == "Ana Ruiz" and out["left"]       # she is off: never handed it illegally
    (v,) = [v for v in sr.violations(out["rows"], c) if v["kind"].startswith("owner_")]
    assert v["kind"] == "owner_word_on"


def test_a_weight_moves_the_days_hours_target():
    # Confirmed: Monday moves by the stated step and Tuesday takes the hours,
    # the week's total unchanged (AI-9, BUGSGEN-23); unconfirmed: nothing moves.
    c = _c()
    orr.apply(c, [dict(_rule("weight", value=-1, days=["Monday"], quote="keep Monday light"), confirmed=True)])
    assert orr.apply_weights({WEEK[0]: 50.0, WEEK[1]: 40.0}, c) == {WEEK[0]: 45.0, WEEK[1]: 45.0}
    c2 = _c()
    orr.apply(c2, [dict(_rule("weight", value=-1, days=["Monday"], quote="keep Monday light"), confirmed=False)])
    assert orr.apply_weights({WEEK[0]: 50.0, WEEK[1]: 40.0}, c2) == {WEEK[0]: 50.0, WEEK[1]: 40.0}


def test_the_drafts_instruction_comes_back_as_chips_held_or_not():
    c = _c(role_families={"server": "server"})
    rules = [dict(_rule("close", person="Maria Lopez", days=["Friday"], quote="Maria closes Friday"),
                  confirmed=True),
             dict(_rule("person_off", person="Ben Cole", days=["Monday"], quote="Ben off Monday"), confirmed=True)]
    orr.apply(c, rules)
    rows = [_row(WEEK[4], "Ana Ruiz", "5:00pm", "11:00pm"), _row(WEEK[0], "Ben Cole")]
    chips = orr.chips(rows, c, rules)
    assert chips[0]["words"] == "Maria Lopez closes, Fri" and chips[0]["held"] is False
    assert chips[1]["held"] is False and "still on 10/12/26" in chips[1]["why"]
    assert all(ch["quote"] for ch in chips)


def test_the_repair_loop_runs_the_owner_words_stage_at_the_coverage_rank():
    order = [k for k, *_r in se.REPAIR_STAGES]
    assert se._STAGE_TIER["owner_words"] == sr.TIER_COVERAGE
    assert order.index("floors") < order.index("owner_words") < order.index("overtime") < order.index("solver")


# ── confirming in the existing note-rules screen ──────────────────────────

def test_a_confirmed_rule_is_stored_with_its_kind_and_held_hard(rid, db_path, monkeypatch):
    import staff_settings
    monkeypatch.setattr(staff_settings, "roster", lambda r, db_path=None: [{"name": n, "role": "Server"} for n in NAMES])
    rule = orr.confirm(rid, _rule("avoid", person="Ana Ruiz", person2="Ben Cole", quote="Ana and Ben apart"),
                       source_text="Ana and Ben apart")
    assert rule["kind"] == "avoid" and "Ana Ruiz and Ben Cole never on together" in rule["words"]
    (got,) = orr.confirmed_rules(rid, WEEK)
    assert got["confirmed"] and got["person2"] == "Ben Cole"
    with pytest.raises(ValueError):
        orr.confirm(rid, _rule("avoid", person="Zed", person2="Ben Cole"), source_text="Zed and Ben")
    # build_constraints holds it (schedule_note_rules.apply_note_rules → owner_rules.apply)
    c = _c(role_families={"server": "server"})
    snr.apply_note_rules(c, rid, db_path=db_path)
    assert any(p["kind"] == "avoid" for p in c.owner_pairs)


def test_the_generation_reads_the_instruction_and_holds_it_only_once_confirmed(rid, monkeypatch):
    import staff_settings
    monkeypatch.setattr(staff_settings, "roster", lambda r, db_path=None: [{"name": n, "role": "Server"} for n in NAMES])

    def call(req):
        return _Msg({"sentences": [{"index": 0, "rules": [_rule("close", person="Maria Lopez", dates=[WEEK[4]],
                                                                   quote="Maria closes Friday")]}]})
    got = orr.prepare(rid, WEEK, instruction="Maria closes Friday", call=call)
    (r,) = got["instruction"]
    # Read, never held until the owner confirms the reading (blind re-audit
    # 10/11/26 AI-7): a preference meanwhile.
    assert not r["confirmed"] and r["scope"] == "week" and r["source"] == "instruction" and r["dates"] == [WEEK[4]]
    (t,) = orr.typed_rules(rid, WEEK, instruction="Maria closes Friday")
    assert t["kind"] == "close" and not t["confirmed"]
    # Confirmed for the week (the Generate sheet's tick): hard, from the
    # confirmed rules — and the instruction's reading matches it.
    monkeypatch.setattr(schedule_context, "check_week_start", lambda r_, w: (w, None))
    orr.confirm_for_week(rid, [r], WEEK[0])
    (r2,) = orr.prepare(rid, WEEK, instruction="Maria closes Friday", call=call)["instruction"]
    assert r2["confirmed"]
    (t2,) = orr.typed_rules(rid, WEEK, instruction="Maria closes Friday")
    assert t2["confirmed"] and t2["source"] == "confirmed"


def test_the_note_rules_screen_offers_each_reading_to_confirm(rid, db_path, monkeypatch):
    """GET shows each sentence the plain reader could not hold with the typed
    rules read from it (cache only); "Read my notes" reads the rest with the
    model; confirming stores it hard. Web and phone share these routes."""
    from flask import Flask
    import client_api
    import staff_settings
    import strategy_routes
    monkeypatch.setattr(staff_settings, "roster", lambda r, db_path=None: [{"name": n, "role": "Server"} for n in NAMES])
    monkeypatch.setattr(strategy_routes, "_sees_labor", lambda u: True)
    monkeypatch.setattr(strategy_routes, "_principal", lambda u: True)
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    models.update_restaurant(rid, {"sched_notes": "Jen and Tom never together. Ana Ruiz and Ben Cole apart."},
                             db_path=db_path)
    u = {"restaurant_id": rid, "role": "owner", "id": 1, "email": "w@x.com"}
    app = Flask(__name__)
    with app.test_request_context("/labor/note-rules"):
        body, status = strategy_routes._do_note_rules_get(u)
    assert status == 200 and body["unread"] == 2 and not any(s.get("typed") for s in body["sentences"])
    monkeypatch.setenv("OWNER_RULES_READ", "1")
    import ai_utils

    def fake(client, **req):
        return _Msg({"sentences": [{"index": 0, "rules": [_rule("none")]},
                                   {"index": 1, "rules": [_rule("avoid", person="Ana Ruiz", person2="Ben Cole",
                                                                quote="Ana Ruiz and Ben Cole apart")]}]})
    monkeypatch.setattr(ai_utils, "create_with_retry", fake)
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: None)
    with app.test_request_context("/labor/note-rules/read", method="POST", json={}):
        body, status = strategy_routes._do_note_rules_read(u)
    assert status == 200 and body["unread"] == 0
    (typed,) = [s for s in body["sentences"] if s.get("typed")]
    assert typed["typed"][0]["words"].startswith("Ana Ruiz and Ben Cole never on together")
    with app.test_request_context("/labor/note-rules/typed", method="POST",
                                  json={"rule": typed["typed"][0]["rule"], "source_text": typed["text"],
                                        "scope": "every"}):
        body, status = strategy_routes._do_note_rule_typed_add(u)
    assert status == 200 and body["rule"]["kind"] == "avoid"
    monkeypatch.setattr(strategy_routes, "_principal", lambda u: False)
    with app.test_request_context("/labor/note-rules/typed", method="POST", json={}):
        assert strategy_routes._do_note_rule_typed_add(u)[1] == 403
    routes = {r[0] for r in strategy_routes._ROUTES}
    assert {"/labor/note-rules/read", "/labor/note-rules/typed"} <= routes


def test_a_person_whose_notes_are_all_read_is_the_solvers_to_choose_again():
    import schedule_solver as ss
    s = {"constraints": {"Ana Ruiz": "can't work tuesday", "Ben Cole": "prefers bar"}, "notes_read": ["ana ruiz"],
         "roster": NAMES, "roster_roles": {n: "Server" for n in NAMES}}
    prob = ss.Problem([_row(WEEK[0], "Ben Cole")], _c(), signals=s)
    assert "ana ruiz" not in prob.constrained and "ben cole" in prob.constrained
