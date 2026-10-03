"""Staff notes and the owner's own rules, held by code (schedule audit 10/3/26
D-34, D-38).

D-34: a note was "priority 1" in the prompt and nothing in code read it —
c.notes was written and never read, only an owner-confirmed hold became a
rule, and a deterministic backstop could put Ana on the Friday close her
unconfirmed "can't close Fridays" ruled out after the model obeyed it.

D-38: the owner's staffing rules were read as the team reads memory, so an
owner-only rule constrained nothing, and a rule about two people ("never
schedule Ana with Ben") was not parsed at all.
"""
import sys

import pytest

import models
import owner_memory
import person_note_holds as pnh
import schedule_engine as se
import schedule_rules as sr
import shift_quality as sq
import staff_settings

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
FRI, THU, TUE = "2026-10-09", "2026-10-08", "2026-10-06"
OWNER = {"id": 11, "role": "client", "is_admin": 0, "username": "erik"}


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_restaurant_today", lambda rid: __import__("datetime").date(2026, 10, 3))
    yield


def _rid(names=("Ana", "Ben", "Cy")):
    rid = models.create_restaurant(models.Restaurant(name="Notes Co", owner_email="n@x.test", module_labor=1))
    for n in names:
        models.add_manual_team_member(rid, n, role="Server")
    return rid


# ── D-34: every note is read; an unconfirmed one keeps the fills off its days ──

def test_an_unconfirmed_note_keeps_the_fills_off_its_day_and_still_reaches_the_model():
    rid = _rid()
    models.save_staff_note(rid, "Ana", "can't close Fridays", today="2026-10-01")
    c = sr.build_constraints(rid, WEEK, list(sr.DAYS))
    assert FRI in c.note_caution["ana"]["dates"]
    ok, why = c.fillable("Ana", FRI)
    assert not ok and why.startswith("their note: can't close Fridays")
    assert c.fillable("Ana", THU)[0]
    assert c.can_work("Ana", FRI)[0]                   # unconfirmed: the owner may still write her in
    assert "can't close Fridays" in c.notes["ana"]     # and the model is still told


def test_a_confirmed_hold_is_a_hard_rule_not_a_caution():
    rid = _rid()
    models.save_staff_note(rid, "Ana", "no Fridays", today="2026-10-01")
    pnh.add_hold(rid, "Ana", "no Fridays", days=["Friday"], start="2026-10-01")
    c = sr.build_constraints(rid, WEEK, list(sr.DAYS))
    assert "ana" not in c.note_caution
    assert not c.can_work("Ana", FRI)[0]


def test_a_note_the_reader_cannot_hold_still_cautions_the_days_it_rules_out():
    assert pnh.caution("not with Mike on Fridays", noted="2026-10-01", week_dates=WEEK)["dates"] == {FRI}
    assert pnh.caution("out 10/6-10/7 probably", noted="2026-10-01", week_dates=WEEK)["dates"] == {TUE, "2026-10-07"}
    assert pnh.caution("Tuesdays only for now", noted="2026-10-01", week_dates=WEEK)["dates"] == set(WEEK) - {TUE}
    # Nothing about a day, or nothing that rules a day out: no caution.
    assert pnh.caution("max 25 hours", noted="2026-10-01", week_dates=WEEK) is None
    assert pnh.caution("prefers Tuesday mornings", noted="2026-10-01", week_dates=WEEK) is None
    assert pnh.caution("great with guests", noted="2026-10-01", week_dates=WEEK) is None


def test_a_dinner_only_note_leaves_lunch_to_a_fill_that_says_so():
    rid = _rid()
    models.save_staff_note(rid, "Ben", "no nights", today="2026-10-01")
    c = sr.build_constraints(rid, WEEK, list(sr.DAYS))
    assert c.note_caution["ben"]["parts"] == {"night"}
    assert c.fillable("Ben", TUE, daypart="morning")[0]
    assert not c.fillable("Ben", TUE, daypart="night")[0]
    assert not c.fillable("Ben", TUE)[0]               # a fill that doesn't say keeps clear


def test_an_availability_note_is_read_too():
    rid = _rid()
    conn = models.get_conn()
    try:
        conn.execute("INSERT INTO staff_availability (restaurant_id, employee_name, available_days, notes, updated_at) "
                     "VALUES (?,?,?,?,?)", (rid, "Cy", "[]", "can't do Tuesdays this month", "2026-10-01 09:00:00"))
        conn.commit()
    finally:
        conn.close()
    c = sr.build_constraints(rid, WEEK, list(sr.DAYS))
    assert TUE in c.note_caution["cy"]["dates"]


# ── D-38: owner-only rules constrain the draft without being shown ──────────

def _rule(rid, text, audience="team"):
    owner_memory.remember(rid, text, kind="constraint", modules=["schedule"], user=OWNER, audience=audience,
                          audience_chosen=True)


def test_an_owner_only_staffing_rule_is_a_floor_whose_words_stay_private(monkeypatch):
    rid = _rid()
    _rule(rid, "Always two servers on Saturday night", audience="principals")
    _rule(rid, "At least 1 server every day", audience="principals")
    c = sr.Constraints(restaurant_id=rid, week_dates=WEEK, week_days=list(sr.DAYS))
    sr.apply_owner_rules(c, rid)
    assert sr.floor_for(c.role_floors, "Server", "Saturday", "night") == 2       # it constrains
    assert not c.rule_floor_sources                                               # its words don't travel
    assert all(r["private"] for r in c.owner_rules)
    sat = [{"date": "2026-10-10", "day": "Saturday", "employee": "Ana", "role": "Server", "shift_start": "4:00pm",
            "shift_end": "10:00pm"}]
    floor = next(v for v in sr._coverage_violations(sat, c) if v["kind"] == "coverage_floor")
    assert "your rule" not in floor["detail"] and "Always two" not in floor["detail"]
    tue = [{"date": TUE, "day": "Tuesday", "employee": "Ana", "role": "Host", "shift_start": "4:00pm",
            "shift_end": "10:00pm"}]
    rule = next(v for v in sr._coverage_violations(tue, c) if v["kind"] == "owner_rule")
    assert rule["detail"].endswith("below a staffing rule you set")


def test_an_unreadable_owner_only_rule_stays_off_the_shared_review():
    rid = _rid()
    _rule(rid, "Keep the rotation fair for the new hires", audience="principals")
    _rule(rid, "Give the new hires a fair rotation please", audience="team")
    c = sr.Constraints(restaurant_id=rid, week_dates=WEEK, week_days=list(sr.DAYS))
    sr.apply_owner_rules(c, rid)
    assert c.owner_rules_unchecked == ["Give the new hires a fair rotation please"]
    assert c.owner_rules_unchecked_private == ["Keep the rotation fair for the new hires"]


def test_a_rule_about_two_people_is_a_pairing():
    names = ["Ana Lopez", "Ben Ruiz", "Cy Park", "Will Hart"]
    avoid = sr.parse_pair_rule("Never schedule Ana with Ben", names)
    assert (avoid["kind"], avoid["a"], avoid["b"]) == ("avoid", "Ana Lopez", "Ben Ruiz")
    assert sr.parse_pair_rule("keep Ana Lopez and Cy apart", names)["kind"] == "avoid"
    assert sr.parse_pair_rule("Ana and Ben shouldn't be on together", names)["kind"] == "avoid"
    assert sr.parse_pair_rule("Always pair Cy with Ben", names)["kind"] == "prefer"
    assert sr.parse_pair_rule("Ana and Ben never work Sundays", names) is None         # about Sundays
    assert sr.parse_pair_rule("we will never schedule Ana with nobody", names) is None  # "will" is a word
    assert sr.parse_pair_rule("never put Ana with Ben on Fridays", names)["days"] == ("Friday",)


def test_a_team_pairing_is_printed_and_a_private_one_is_held_unprinted(monkeypatch):
    rid = _rid()
    _rule(rid, "Never schedule Ana with Ben", audience="team")
    _rule(rid, "Keep Ben and Cy apart", audience="principals")
    _rule(rid, "Never put Ana with Cy on Fridays", audience="team")
    c = sr.Constraints(restaurant_id=rid, week_dates=WEEK, week_days=list(sr.DAYS))
    sr.apply_owner_rules(c, rid)
    pairs = sr.pairs_with_rules({"prefer": set(), "avoid": set()}, c)
    ab, bc = frozenset({"ana", "ben"}), frozenset({"ben", "cy"})
    assert {ab, bc} <= pairs["avoid"] and pairs["private"] == {bc}
    assert c.owner_rules_unchecked == ["Never put Ana with Cy on Fridays"]   # a day-only pairing is named
    block = se._pairs_block(pairs, [("Ana", "Server"), ("Ben", "Server"), ("Cy", "Server")])
    assert "Ana and Ben" in block and "Cy" not in block
    # Scored against, never named.
    ctx = sq.ShiftContext(date=TUE, day="Tuesday", daypart="night",
                          rows=[{"employee": n, "role": "Server", "date": TUE, "shift_start": "4:00pm",
                                 "shift_end": "10:00pm"} for n in ("Ben", "Cy")],
                          profile=sq.BUILTIN_PROFILES[0], pairs=pairs)
    res = sq.dim_pairings(ctx)
    assert res.score < sq.SCORE_MAX
    assert not any("Ben" in w or "Cy" in w for w in res.weaknesses)
    assert res.facts["clashes"] == [] and res.facts["private"] == 1
