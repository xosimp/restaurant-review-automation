"""Schedule re-audit fix round 10/4/26, workstream LEARN — the closed
learning loop: the owner's word is what comes out of the memory, and no
learned fix undoes itself.

LEARN-1   the owner's Keep on a learned keep-apart keeps the two APART in
          the memory's meaning, the solver, the optimizer, the scorer and
          the prompt (never "together", never named)
LEARN-2   a padded close and an overtime headroom keep their evidence
LEARN-3   a manager's answer is the manager's: it never undoes the owner's
          Keep or Let go, never reads "The owner said"
LEARN-4   the owner's Keep on a habit under the line is applied
LEARN-5   a rule the owner removed is no longer "A rule"
LEARN-6   a learned keep-apart reaches no team-visible screen or prompt
LEARN-7   teams learned from noise are not findings (Holm over every group)
LEARN-8   the cover record credits who covered, blames nobody not needed
LEARN-9   an admin's uncounted answer never locks the owner out
LEARN-10  a learned closer is one of the closers the owner chose
LEARN-11  what the floor shows keeps an opener alive under Cavnar AI drafts
LEARN-12  an approved drop nobody claimed still says "keeps dropping"
PROMPT-2  the rotation plan hands closes only to chosen closers
"""
import json
import random
from datetime import date, timedelta
from itertools import combinations

import pytest

from tests.test_sched_fix_h2_memory import (  # noqa: F401  (_db: the autouse fixture)
    OWNER, VIEW_AS, _db, _hist, _mem, _monday, _punch, _q, _rid, _row, _sat, _sql, _team_world, _week)
import schedule_intel as si
import schedule_learning as sl
import schedule_memory as sm
import schedule_optimizer as so
import schedule_rules as sr
import schedule_solver as ss
import schedule_versions as sv
import shift_quality as sq
import staff_settings
from sched_d2_week import WEEK, DAYS, row

MANAGER = {"id": 2, "username": "mgr", "role": "manager", "restaurant_id": None}
MON, TUE, WED, THU, FRI, SAT, SUN = WEEK


def _fri():
    return (_monday(-1) + timedelta(days=4)).isoformat()


def _kept_apart(rid, monkeypatch):
    """The owner keeps the learned keep-apart of Ana and Bo; returns its key."""
    _team_world(rid, 6, 8, monkeypatch, good_together=False)
    sm.consolidate(rid)
    key = next(p["memory_key"] for p in _mem(rid, kind="pair") if p["value"].get("kind") == "avoid")
    out = sm.owner_answer(rid, key, "keep", user=OWNER)
    assert out["message"] == "Kept — every draft keeps them on different shifts."
    sm.consolidate(rid)
    return key


def _avoid_signal(conf=0.7):
    return {"kind": "pair", "key": "pair|avoid|ana+bo", "person": "Ana", "day": None, "daypart": None,
            "role": None, "value": {"kind": "avoid", "with": ["Bo"]}, "confidence": conf, "enforcement": "soft",
            "source": "outcomes_and_punches"}


def cons(names, **kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=list(names),
                       active={n.lower() for n in names})
    c.compliance = dict(sr.DEFAULTS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _sig(names, role="Server", **kw):
    out = {"roster": list(names), "roster_roles": {n: role for n in names}}
    out.update(kw)
    return out


# ══ LEARN-1: Keep on a keep-apart keeps them apart, everywhere ═══════════

def test_the_owners_keep_on_a_keep_apart_binds_apart_in_the_memorys_meaning(monkeypatch):
    rid = _rid()
    key = _kept_apart(rid, monkeypatch)
    m = _mem(rid, key=key)[0]
    assert (m["status"], m["enforcement"], m["value"]["kind"], m["owner_said_authority"]) == \
        ("active", "soft", "avoid", "principal")
    sig = [s for s in sm.enforced_signals(rid, [_fri()], ["Ana", "Bo", "Cy", "Di"]) if s["kind"] == "pair"]
    assert [(s["key"], s["value"]["kind"]) for s in sig] == [(key, "avoid")]
    fri = _fri()
    split = [_row(fri, "Ana", "10:00am", "3:00pm"), _row(fri, "Bo", "5:00pm", "10:00pm")]
    together = [_row(fri, "Ana", "5:00pm", "10:00pm"), _row(fri, "Bo", "5:00pm", "10:00pm")]
    assert sm.learned_cost(split, sig) == 0
    assert sm.learned_cost(together, sig) > 0
    assert sorted(sm.misses(together, sig)[0]["indexes"]) == [0, 1]


def test_a_pair_with_no_polarity_or_a_managers_keep_apart_never_binds():
    rid = _rid()
    for k, value, said, auth in (("pair|x|ana+bo", {"with": ["Bo"]}, None, None),
                                 ("pair|avoid|cy+di", {"kind": "avoid", "with": ["Di"]}, "keep", "delegate")):
        _sql("INSERT INTO schedule_memory (restaurant_id, memory_key, kind, fact_class, person, status, enforcement, "
             "confidence, value_json, owner_said, owner_said_authority) VALUES (?,?,?,?,?,'active','soft',0.8,?,?,?)",
             rid, k, "pair", "team", k.split("|")[2].split("+")[0].title(), json.dumps(value), said, auth)
    assert sm.enforced_signals(rid, [_fri()], ["Ana", "Bo", "Cy", "Di"]) == []
    # misses itself never reads a pair without a polarity as "together"
    assert sm.misses([_row(_fri(), "Ana", "10:00am", "3:00pm"), _row(_fri(), "Bo")],
                     [dict(_avoid_signal(), value={"with": ["Bo"]})]) == []


def test_the_solver_pays_to_keep_two_the_owner_keeps_apart_on_different_shifts():
    names = ["Ana", "Bo", "Cal"]
    c = cons(names)
    rows = [row(SAT, "Ana", "4:00pm", "11:00pm"), row(SAT, "Bo", "4:00pm", "11:00pm"),
            row(SAT, "Cal", "10:00am", "4:00pm")]
    sig = _sig(names, learned=[_avoid_signal()],
               typical_headcount={("Saturday", "night"): {"Server": 2}, ("Saturday", "morning"): {"Server": 1}})
    prob = ss.Problem(rows, c, signals=sig)
    assert [p for _m, _w, p in prob.mem_pairs] == ["avoid"]
    key = ("L", SAT)
    together = [u.draft for u in prob.units]
    apart = [prob.pidx["ana"] if prob.units[i].draft == prob.pidx["ana"] else
             prob.pidx["cal"] if prob.units[i].draft == prob.pidx["bo"] else prob.pidx["bo"]
             for i in range(len(prob.units))]
    assert prob.team_cost(key, together) > 0 and prob.team_cost(key, apart) == 0
    out = ss.improve(rows, {}, signals=sig, constraints=c)["rows"]
    night = {r["employee"] for r in out if r["date"] == SAT and r["shift_start"] == "4:00pm"}
    assert not {"Ana", "Bo"} <= night


def test_a_preferred_team_still_costs_the_solver_when_split():
    names = ["Ana", "Bo", "Cal"]
    c = cons(names)
    rows = [row(SAT, "Ana", "4:00pm", "11:00pm"), row(SAT, "Bo", "10:00am", "4:00pm")]
    prefer = dict(_avoid_signal(), key="pair|prefer|ana+bo", value={"kind": "prefer", "with": ["Bo"]})
    prob = ss.Problem(rows, c, signals=_sig(names, learned=[prefer]))
    assert prob.team_cost(("L", SAT), [u.draft for u in prob.units]) > 0


def test_the_optimizer_moves_them_apart_and_the_review_never_names_them():
    names = ["Ana", "Bo", "Kim"]
    c = cons(names)
    rows = [row(TUE, "Ana", "4:00pm", "10:00pm"), row(TUE, "Bo", "4:00pm", "10:00pm"),
            row(TUE, "Kim", "10:00am", "3:00pm")]
    sig = _sig(names, learned=[_avoid_signal()],
               typical_headcount={("Tuesday", "night"): {"Server": 2}, ("Tuesday", "morning"): {"Server": 1}})
    res = so.optimize(rows, {}, signals=sig, constraints=c)
    night = {r["employee"] for r in res["rows"] if r["date"] == TUE and r["shift_start"] == "4:00pm"}
    assert not {"Ana", "Bo"} <= night
    assert so.memory_misses(res["quality"]) == []
    for r in res["rows"]:
        assert "apart" not in (r.get("notes") or "")


def test_the_score_counts_a_kept_apart_pair_together_and_names_nobody():
    names = ["Ana", "Bo", "Kim"]
    sig = _sig(names, typical_headcount={("Tuesday", "night"): {"Server": 2}})
    together = [row(TUE, "Ana", "4:00pm", "10:00pm"), row(TUE, "Bo", "4:00pm", "10:00pm")]
    q = sq.score_rows(together, **dict(sig, learned=[_avoid_signal()]))
    week = {d["key"]: d for d in q["week_dimensions"]}["learned"]
    assert week["score"] < 100
    assert week["weaknesses"] == ["Two people you chose to keep apart are on the same shift."]
    facts = json.dumps(week["facts"])
    assert "Ana" not in facts and "Bo" not in facts
    assert week["facts"]["misses"][0]["slots"] == [[TUE, "night"]] or \
        week["facts"]["misses"][0]["slots"] == [(TUE, "night")]
    apart = [row(TUE, "Ana", "4:00pm", "10:00pm"), row(TUE, "Bo", "10:00am", "3:00pm")]
    q2 = sq.score_rows(apart, **dict(sig, learned=[_avoid_signal()]))
    assert {d["key"]: d for d in q2["week_dimensions"]}["learned"]["score"] == 100


def test_the_prompt_never_tells_the_model_to_put_a_kept_apart_pair_together(monkeypatch):
    rid = _rid()
    _team_world(rid, 6, 8, monkeypatch, good_together=False)
    sm.consolidate(rid)
    before = sm.prompt_lines(rid, [_fri()], roster_names=["Ana", "Bo", "Cy", "Di"], budget_chars=6000)
    assert "Ana" not in before or "Bo" not in before.split("Ana", 1)[1].split("\n")[0]
    key = next(p["memory_key"] for p in _mem(rid, kind="pair") if p["value"].get("kind") == "avoid")
    sm.owner_answer(rid, key, "keep", user=OWNER)
    sm.consolidate(rid)
    after = sm.prompt_lines(rid, [_fri()], roster_names=["Ana", "Bo", "Cy", "Di"], budget_chars=6000)
    assert "Ana with Bo" not in after and "Ana and Bo" not in after
    assert "keeps apart" not in after


# ══ LEARN-3: the owner's word is the owner's ═════════════════════════════

def _opener(rid):
    for w in (2, 1):
        d = _sat(w)
        _punch(rid, d, "Ana", "08:55", "16:00", 7, role="Kitchen")
        _punch(rid, d, "Bo", "10:00", "18:00", 8, role="Kitchen")
    sm.consolidate(rid)
    return next(m for m in _mem(rid, kind="opener") if m["person"] == "Ana")["memory_key"]


def test_a_manager_cannot_let_go_what_the_owner_kept():
    rid = _rid()
    key = _opener(rid)
    sm.owner_answer(rid, key, "keep", user=OWNER)
    mgr = dict(MANAGER, restaurant_id=rid)
    with pytest.raises(ValueError, match="The owner kept this one"):
        sm.owner_answer(rid, key, "let_go", user=mgr)
    sm.consolidate(rid)
    m = _mem(rid, key=key)[0]
    assert (m["status"], m["owner_said"], m["owner_said_by"], m["owner_said_authority"]) == \
        ("active", "keep", "erik", "principal")
    # the same answer is no change, and never re-attributed
    assert sm.owner_answer(rid, key, "keep", user=mgr)["unchanged"] is True
    assert _mem(rid, key=key)[0]["owner_said_by"] == "erik"
    item = next(i for i in sm.memory_view(rid, principal=False)["items"] if i["key"] == key)
    assert item["can_let_go"] is False and item["answered"] == "Kept by the owner"


def test_a_managers_let_go_is_theirs_and_the_owner_can_bring_it_back():
    rid = _rid()
    key = _opener(rid)
    mgr = dict(MANAGER, restaurant_id=rid)
    sm.owner_answer(rid, key, "let_go", user=mgr)
    sm.consolidate(rid)
    m = _mem(rid, key=key)[0]
    assert (m["status"], m["retired_reason"], m["owner_said_authority"]) == ("retired", "manager", "delegate")
    item = next(i for i in sm.memory_view(rid)["items"] if i["key"] == key)
    assert item["retired_words"] == "a manager let it go" and item["answered"] == "Let go by a manager"
    sm.owner_answer(rid, key, "keep", user=OWNER)
    sm.consolidate(rid)
    assert _mem(rid, key=key)[0]["status"] == "active"
    # and once the owner let it go, a manager cannot keep it
    sm.owner_answer(rid, key, "let_go", user=OWNER)
    with pytest.raises(ValueError, match="The owner let this one go"):
        sm.owner_answer(rid, key, "keep", user=mgr)


def test_a_managers_keep_is_a_hand_confirmation_not_the_owners_word():
    rid = _rid()
    key = _opener(rid)
    out = sm.owner_answer(rid, key, "keep", user=dict(MANAGER, restaurant_id=rid))
    assert "once Cavnar AI is sure enough" in out["message"]
    sm.consolidate(rid)
    m = _mem(rid, key=key)[0]
    assert (m["status"], m["owner_said_authority"]) == ("candidate", "delegate")
    assert not sm.enforced_signals(rid, [_sat(-1).isoformat()], ["Ana", "Bo"])


def test_only_the_owner_answers_a_keep_apart_and_view_as_changes_nothing(monkeypatch):
    rid = _rid()
    _team_world(rid, 6, 8, monkeypatch, good_together=False)
    sm.consolidate(rid)
    key = next(p["memory_key"] for p in _mem(rid, kind="pair") if p["value"].get("kind") == "avoid")
    with pytest.raises(ValueError, match="Only the owner"):
        sm.owner_answer(rid, key, "keep", user=dict(MANAGER, restaurant_id=rid))
    with pytest.raises(ValueError, match="view-as"):
        sm.owner_answer(rid, key, "keep", user=VIEW_AS)
    assert _mem(rid, key=key)[0]["status"] == "candidate"


def _standing(rid, key="moved_off|ana|Friday|night", status="active", hits=3, opps=4, **extra):
    cols = {"restaurant_id": rid, "pattern_key": key, "kind": key.split("|")[0], "employee": "Ana", "day": "Friday",
            "daypart": "night", "text": "The manager has taken Ana off Friday dinner.", "status": status, "hits": hits,
            "opportunities": opps, "confidence": 0.39}
    cols.update(extra)
    _sql("INSERT INTO schedule_standing_patterns (" + ", ".join(cols) + ", first_learned, last_confirmed, last_hand) "
         "VALUES (" + ",".join("?" * len(cols)) + ", datetime('now','-30 days'), datetime('now','-30 days'), "
         "date('now','-30 days'))", *cols.values())
    return key


def test_a_manager_never_lets_go_dismisses_or_restores_against_the_owner(monkeypatch):
    rid = _rid()
    key = _standing(rid)
    sv.confirm_standing(rid, key, user=OWNER, keep=True)
    mgr = dict(MANAGER, restaurant_id=rid)
    with pytest.raises(sv.OwnerAnswered):
        sv.confirm_standing(rid, key, user=mgr, keep=False)
    with pytest.raises(sv.OwnerAnswered):
        si.dismiss_pattern(rid, key, actor="mgr", authority="delegate")
    assert si.dismissed_patterns(rid) == set()
    import strategy_routes as sr_
    monkeypatch.setattr(sr_, "_rid", lambda u: rid)
    monkeypatch.setattr(sr_, "_may_draft", lambda u: True)
    monkeypatch.setattr(sr_, "_body", lambda: {"key": key, "dismissed": True})
    body, code = sr_._do_learned_pattern_set(mgr)
    assert code == 400 and "only the owner" in body["error"]
    # the owner's dismissal: a manager (or an admin through view-as) cannot restore it
    other = _standing(rid, key="moved_off|bo|Friday|night", employee="Bo")
    si.dismiss_pattern(rid, other, actor="erik", authority="principal")
    with pytest.raises(sv.OwnerAnswered):
        si.restore_pattern(rid, other, authority="delegate")
    with pytest.raises(sv.OwnerAnswered):
        si.restore_pattern(rid, other, authority="admin")
    assert other in si.dismissed_patterns(rid)


def test_a_managers_always_is_theirs_and_never_brings_back_what_the_owner_let_go():
    rid = _rid()
    mon = _monday(1)
    hid = _hist(rid, mon, [_row((mon + timedelta(days=4)).isoformat(), "Ana")])
    subj = {"kind": "moved_off", "employee": "Ana", "day": "Friday", "daypart": "night",
            "options": ["always", "this_week", "call_off"], "text": "Why?"}
    _sql("INSERT INTO schedule_edit_answers (restaurant_id, history_id, question_key, kind, subject_json, keys_json, "
         "phase) VALUES (?,?,?,?,?,?,?)", rid, hid, "q1", "moved_off", json.dumps(subj), "[]", "pre_publish")
    sl.answer_edit_question(rid, hid, "q1", "always", user=dict(MANAGER, restaurant_id=rid))
    row_ = _q("SELECT * FROM schedule_standing_patterns WHERE restaurant_id=?", rid)[0]
    assert row_["source"] == "learned" and row_["text"].startswith("A manager said always")
    sm.consolidate(rid, only=("patterns",))
    assert _mem(rid, key="pattern:" + row_["pattern_key"])[0]["status"] == "candidate"
    # the owner lets it go; the manager's next "always" leaves it let go
    sv.confirm_standing(rid, row_["pattern_key"], user=OWNER, keep=False)
    _sql("INSERT INTO schedule_edit_answers (restaurant_id, history_id, question_key, kind, subject_json, keys_json, "
         "phase) VALUES (?,?,?,?,?,?,?)", rid, hid, "q2", "moved_off", json.dumps(subj), "[]", "pre_publish")
    sl.answer_edit_question(rid, hid, "q2", "always", user=dict(MANAGER, restaurant_id=rid))
    again = _q("SELECT * FROM schedule_standing_patterns WHERE restaurant_id=?", rid)[0]
    assert (again["status"], again["retired_reason"]) == ("retired", "owner")
    # the owner's own "always" is theirs
    _sql("INSERT INTO schedule_edit_answers (restaurant_id, history_id, question_key, kind, subject_json, keys_json, "
         "phase) VALUES (?,?,?,?,?,?,?)", rid, hid, "q3", "moved_off", json.dumps(subj), "[]", "pre_publish")
    sl.answer_edit_question(rid, hid, "q3", "always", user=OWNER)
    mine = _q("SELECT * FROM schedule_standing_patterns WHERE restaurant_id=?", rid)[0]
    assert (mine["status"], mine["source"]) == ("active", "owner_said")


# ══ LEARN-4: the owner's Keep applies whatever the confidence ════════════

def test_the_owners_keep_on_a_habit_under_the_line_is_applied():
    rid = _rid()
    key = _standing(rid)
    sm.consolidate(rid, only=("patterns",))
    mkey = "pattern:" + key
    assert _mem(rid, key=mkey)[0]["status"] == "candidate"
    sm.owner_answer(rid, mkey, "keep", user=dict(MANAGER, restaurant_id=rid))
    sv.refresh_standing_patterns(rid)
    sm.consolidate(rid, only=("patterns",))
    assert _mem(rid, key=mkey)[0]["status"] == "candidate", "a manager's keep is not the owner's"
    sm.owner_answer(rid, mkey, "keep", user=OWNER)
    sv.refresh_standing_patterns(rid)
    sm.consolidate(rid, only=("patterns",))
    m = _mem(rid, key=mkey)[0]
    assert m["status"] == "active" and m["confidence"] < sm.ACTIVE_CONFIDENCE
    assert [s["kind"] for s in sm.enforced_signals(rid, [_fri()], ["Ana"])] == ["moved_off"]


# ══ LEARN-2: a fix never erases its own evidence ═════════════════════════

def test_a_padded_close_that_runs_to_its_padded_end_keeps_the_pad():
    rid = _rid()

    def week(w, end, punch_out, hours):
        mon = _monday(w)
        fri = (mon + timedelta(days=4)).isoformat()
        _hist(rid, mon, [_row(fri, "Ana", "4:00pm", end, hours=hours)])
        _punch(rid, fri, "Ana", "16:00", punch_out, hours)
    for w in range(10, 2, -1):
        week(w, "10:00pm", "22:30", 6.5)
    sm.consolidate(rid)
    m = _mem(rid, kind="end_overrun")[0]
    assert m["status"] == "active" and m["value"]["ends_at"] == "10:00pm"
    for w in (2, 1):
        week(w, "10:30pm", "22:30", 6.5)               # the padded close, worked as padded
    sm.consolidate(rid)
    m = _mem(rid, kind="end_overrun")[0]
    assert m["status"] == "active" and m["value"]["ends_at"] == "10:00pm" and m["value"]["padded_end"] == "10:30pm"
    assert "end_overrun" in [s["kind"] for s in sm.enforced_signals(rid, [_fri()], ["Ana"])]


def test_a_padded_close_that_really_ends_early_again_counts_as_on_time():
    rid = _rid()

    def week(w, end, punch_out, hours):
        mon = _monday(w)
        fri = (mon + timedelta(days=4)).isoformat()
        _hist(rid, mon, [_row(fri, "Ana", "4:00pm", end, hours=hours)])
        _punch(rid, fri, "Ana", "16:00", punch_out, hours)
    for w in range(14, 10, -1):
        week(w, "10:00pm", "22:30", 6.5)
    sm.consolidate(rid)
    for w in range(10, 0, -1):
        week(w, "10:30pm", "22:00", 6.0)               # padded, but they leave at 10 now
    sm.consolidate(rid)
    m = _mem(rid, kind="end_overrun")[0]
    assert m["hits"] == 4 and m["opportunities"] == 14 and m["status"] != "active"


def test_overtime_headroom_the_passes_kept_still_counts_as_evidence():
    rid = _rid()
    from labor import _week_key

    def week(w, planned, worked):
        mon = _monday(w)
        days = [(mon + timedelta(days=i)).isoformat() for i in range(4)]
        _hist(rid, mon, [_row(d, "Ana", "8:00am", "5:00pm", hours=planned / 4.0) for d in days])
        for d in days:
            _punch(rid, d, "Ana", "08:00", "18:00", worked / 4.0)
    for w in range(9, 3, -1):
        week(w, 40, 44)                                # ran into overtime
    sm.consolidate(rid)
    assert _mem(rid, kind="ot_risk")[0]["status"] == "active"
    for w in (3, 2, 1):
        week(w, 36, 40)                                # kept 4h under the line: no overtime, still ran 4h over
    sm.consolidate(rid)
    m = _mem(rid, kind="ot_risk")[0]
    assert m["status"] == "active" and m["hits"] == m["opportunities"]
    assert m["value"]["headroom_hours"] >= 4
    assert _week_key


# ══ LEARN-5: a rule the owner removed is no longer a rule ════════════════

def test_a_rule_the_owner_removed_goes_back_to_the_evidence(monkeypatch):
    rid = _rid()
    _team_world(rid, 8, 6, monkeypatch)
    sm.consolidate(rid)
    key = next(p["memory_key"] for p in _mem(rid, kind="pair") if p["value"].get("kind") == "prefer"
               and len(p["value"]["with"]) == 1)
    sm.owner_answer(rid, key, "rule", user=OWNER)
    with pytest.raises(ValueError, match="rule now"):
        sm.owner_answer(rid, key, "let_go", user=OWNER)
    sm.consolidate(rid)
    assert _mem(rid, key=key)[0]["status"] == "rule"
    for p in staff_settings.pairs(rid):
        staff_settings.delete_pair(rid, p["id"])
    sm.consolidate(rid)
    m = _mem(rid, key=key)[0]
    assert m["status"] == "active" and m["rule_ref"] is None and m["owner_said"] is None
    item = next(i for i in sm.memory_view(rid)["items"] if i["key"] == key)
    assert item["status_label"] == "Applied"
    assert [s["key"] for s in sm.enforced_signals(rid, [_fri()], ["Ana", "Bo", "Cy", "Di"]) if s["key"] == key]


def test_a_rule_whose_rule_is_gone_and_no_longer_learned_retires():
    rid = _rid()
    _sql("INSERT INTO schedule_memory (restaurant_id, memory_key, kind, fact_class, person, day, status, enforcement, "
         "value_json, rule_ref, owner_said) VALUES (?,?,?,?,?,?,'rule','hard',?,?,'keep')",
         rid, "opener|kitchen|Saturday|ana", "opener", "ownership", "Ana", "Saturday",
         json.dumps({"start": "9:00am", "end": "4:00pm", "role": "Kitchen"}), "Ana works Saturdays 9-4")
    sm.consolidate(rid)
    m = _mem(rid, key="opener|kitchen|Saturday|ana")[0]
    assert (m["status"], m["retired_reason"], m["rule_ref"]) == ("retired", "rule_removed", None)
    # while the standing shift is there, the rule holds
    _sql("UPDATE schedule_memory SET status='rule', retired_reason=NULL WHERE restaurant_id=?", rid)
    staff_settings.upsert(rid, "Ana", standing_shifts=[{"day": "Saturday", "start": "9:00am", "end": "4:00pm",
                                                        "role": "Kitchen"}], updated_by="erik")
    sm.consolidate(rid)
    assert _mem(rid, key="opener|kitchen|Saturday|ana")[0]["status"] == "rule"


def test_a_pattern_rule_whose_availability_the_owner_gave_back_is_a_pattern_again():
    rid = _rid()
    key = _standing(rid, hits=8, opps=8, confidence=0.8)
    staff_settings.upsert(rid, "Ana", updated_by="erik")
    sv.make_rule(rid, key, user=OWNER)
    sm.consolidate(rid, only=("patterns",))
    assert _mem(rid, key="pattern:" + key)[0]["status"] == "rule"
    staff_settings.upsert(rid, "Ana", daypart_availability={}, updated_by="erik")      # the owner gives it back
    sm.consolidate(rid, only=("patterns",))
    assert _mem(rid, key="pattern:" + key)[0]["status"] in ("active", "candidate")
    assert _q("SELECT status FROM schedule_standing_patterns WHERE restaurant_id=?", rid)[0]["status"] == "active"


# ══ LEARN-6: a keep-apart is the owner's alone ═══════════════════════════

def test_a_keep_apart_reaches_no_team_screen_and_no_prompt(monkeypatch):
    rid = _rid()
    _team_world(rid, 6, 8, monkeypatch, good_together=False)
    sm.consolidate(rid)
    team = sm.memory_view(rid, principal=False)
    assert not [i for i in team["items"] if i["kind"] == "pair" and i["value"].get("kind") == "avoid"]
    own = sm.memory_view(rid, principal=True)
    avoid = [i for i in own["items"] if i["kind"] == "pair" and i["value"].get("kind") == "avoid"]
    assert avoid and avoid[0]["keep_label"] == "Keep them apart"
    # its words count what it counts: "6 of 6 … did not run well" beside 6 of 6
    assert "6 of 6 shared shifts did not run well" in avoid[0]["text"] and avoid[0]["hits"] == 6
    blk = sm.prompt_lines(rid, [_fri()], roster_names=["Ana", "Bo", "Cy", "Di"], budget_chars=6000)
    assert "did not run well" not in blk
    import strategy_routes as sr_
    monkeypatch.setattr(sr_, "_rid", lambda u: rid)
    monkeypatch.setattr(sr_, "_sees_labor", lambda u: True)
    from flask import Flask
    with Flask(__name__).test_request_context("/api/labor/schedule-memory"):
        body, code = sr_._do_schedule_memory(dict(MANAGER, restaurant_id=rid))
    assert code == 200 and not [i for i in body["items"] if i["value"].get("kind") == "avoid"]


# ══ LEARN-7: teams from noise are not findings ═══════════════════════════

def _world(seed, effect=None):
    rnd = random.Random(seed)
    staff = list(range(14))
    nights = []
    for _n in range(48):
        on = set(rnd.sample(staff[:5], 4)) | set(rnd.sample(staff[5:], 3))
        p = 0.95 if effect and set(effect) <= on else 0.45
        nights.append((on, rnd.random() < p))
    base = sum(g for _o, g in nights) / len(nights)
    ev, nights_of = {}, {}
    for i, (on, good) in enumerate(nights):
        for k in on:
            nights_of.setdefault(k, set()).add(i)
        for g in list(combinations(sorted(on), 2)) + list(combinations(sorted(on), 3)):
            e = ev.setdefault(g, [0, 0])
            e[0] += 1
            e[1] += good
    stats = {}
    for g, (n, h) in ev.items():
        if n < sm.PAIR_MIN_SHARED:
            continue
        every = set.intersection(*(nights_of[k] for k in g))
        apart = set().union(*(nights_of[k] for k in g)) - every
        other = [i for i in range(len(nights)) if i not in every]
        stats[g] = {"n": n, "hits": h, "apart_n": len(apart), "apart_hits": sum(1 for i in apart if nights[i][1]),
                    "other_n": len(other), "other_hits": sum(1 for i in other if nights[i][1])}
    return sm.significant_teams(stats, base)


def test_teams_are_never_learned_from_outcomes_that_ignore_who_worked():
    found = [_world(s) for s in range(60)]
    assert sum(1 for f in found if any(v[0] == "prefer" and len(g) == 2 for g, v in f.items())) <= 3
    real = [(0, 1) in _world(1000 + s, effect=(0, 1)) for s in range(40)]
    assert sum(real) >= 20


def test_a_trio_is_never_enforced(monkeypatch):
    rid = _rid()
    monkeypatch.setattr(si, "watched_dates", lambda r, a, b, db_path=None: set())
    w = 1
    for i in range(14):
        mon = _monday(w)
        fri = (mon + timedelta(days=4)).isoformat()
        team = ("Ana", "Bo", "Cy") if i < 8 else ("Di", "Ed", "Flo")
        hid = _hist(rid, mon, [_row(fri, p) for p in team])
        _sql("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, people, sales, issues, "
             "split_basis, actual_hours, actual_people, actual_basis) VALUES (?,?,?,?,15,3,?,0,'measured',15,3,"
             "'punches')", rid, hid, fri, "night", 900.0 if i < 8 else 400.0)
        for p in team:
            _punch(rid, fri, p, "17:00", "22:00", 5)
        w += 1
    sm.consolidate(rid)
    trios = [p for p in _mem(rid, kind="pair") if p["value"].get("size") == 3]
    assert trios and all(p["status"] == "candidate" for p in trios)
    assert all(p["value"].get("p_value") is not None for p in _mem(rid, kind="pair"))


# ══ LEARN-8: the cover record ════════════════════════════════════════════

def test_only_who_covered_is_credited_and_nobody_not_needed_is_blamed():
    import people
    rid = _rid()
    day = (date.today() - timedelta(days=2)).isoformat()
    meta = {"missing": "Ana", "shift_start": "5:00pm", "shift_end": "10:00pm",
            "asked": [{"name": "Bo"}, {"name": "Cy"}, {"name": "Di"}]}
    _sql("INSERT INTO ops_issues (restaurant_id, kind, source_key, title, meta_json) VALUES (?,?,?,?,?)",
         rid, "coverage", f"coverage:{day}:ana", "Ana missing", json.dumps(meta))
    _punch(rid, day, "Bo", "17:00", "22:00", 5)          # Bo covered Ana's dinner
    _punch(rid, day, "Di", "10:00", "14:00", 4)          # Di worked her own lunch
    people.record_cover_signals(rid, days=7)
    rec = people.cover_record(rid)
    assert rec == {"bo": {"accepted": 1, "declined": 0}}


def test_cover_verdicts_read_the_gap_the_offer_and_the_persons_own_shift():
    import people
    gap = [{"employee": "Ana", "shift_start": "5:00pm", "shift_end": "10:00pm", "status": "missing"}]
    asked = [{"name": "Bo"}, {"name": "Cy"}, {"name": "Di", "offer_id": 7}]
    # nobody covered: the ones asked who did not come in declined; a declined offer is a decline
    v = people.cover_verdicts(gap, asked, {}, {}, {7: "declined"})
    assert v == {"bo": "declined", "cy": "declined", "di": "declined"}
    # an accepted offer is the cover; the others were not needed
    v = people.cover_verdicts(gap, asked, {}, {}, {7: "accepted"})
    assert v == {"bo": None, "cy": None, "di": "accepted"}
    # a punch inside the person's own scheduled dinner is their shift, not a cover
    v = people.cover_verdicts(gap, asked[:1], {"bo": [(17 * 60, 22 * 60)]}, {"bo": [(17 * 60, 22 * 60)]})
    assert v == {"bo": "declined"}
    # the missing person came in: nobody asked was needed
    v = people.cover_verdicts([dict(gap[0], status="arrived")], asked[:2], {}, {})
    assert v == {"bo": None, "cy": None}


# ══ LEARN-9: an admin's answer never locks the owner out ═════════════════

def test_the_owner_answers_over_an_admins_uncounted_answer():
    rid = _rid()
    mon = _monday(1)
    hid = _hist(rid, mon, [_row((mon + timedelta(days=4)).isoformat(), "Ana")])
    subj = {"kind": "moved_off", "employee": "Ana", "day": "Friday", "daypart": "night",
            "options": ["always", "this_week", "call_off"], "text": "Why?"}
    _sql("INSERT INTO schedule_edit_answers (restaurant_id, history_id, question_key, kind, subject_json, keys_json, "
         "phase) VALUES (?,?,?,?,?,?,?)", rid, hid, "q1", "moved_off", json.dumps(subj), "[]", "pre_publish")
    assert sl.answer_edit_question(rid, hid, "q1", "always", user=VIEW_AS)["counted"] is False
    out = sl.answer_edit_question(rid, hid, "q1", "this_week", user=OWNER)
    assert out["counted"] is True
    row_ = _q("SELECT answer, authority FROM schedule_edit_answers WHERE restaurant_id=?", rid)[0]
    assert (row_["answer"], row_["authority"]) == ("this_week", "principal")
    # a counted answer still locks it
    with pytest.raises(ValueError, match="already answered"):
        sl.answer_edit_question(rid, hid, "q1", "always", user=OWNER)


# ══ LEARN-10 / PROMPT-2: closers are the owner's chosen closers ══════════

def _closers(monkeypatch, chosen):
    monkeypatch.setattr(sr, "chosen_closers", lambda rid, restaurant=None, db_path=None: chosen)


def test_a_learned_closer_is_one_of_the_closers_the_owner_chose(monkeypatch):
    rid = _rid()
    for w in (3, 2, 1):
        d = _sat(w)
        _punch(rid, d, "Ana", "16:00", "22:00", 6)
        _punch(rid, d, "Bo", "17:00", "23:00", 6)        # Bo is last out — but Ana is the chosen closer
    _closers(monkeypatch, {"server": {"ana"}})
    sm.consolidate(rid)
    closers = {m["person"]: m for m in _mem(rid, kind="closer")}
    assert "Bo" not in closers and closers["Ana"]["hits"] == 3
    # a learned closer already in force who is not chosen binds nothing and reaches no prompt
    _sql("INSERT INTO schedule_memory (restaurant_id, memory_key, kind, fact_class, person, role, day, status, "
         "enforcement, confidence, value_json) VALUES (?,?,?,?,?,?,?,'active','soft',0.9,?)",
         rid, "closer|server|Friday|bo", "closer", "ownership", "Bo", "server", "Friday",
         json.dumps({"role": "Server", "end": "11:00pm"}))
    assert not [s for s in sm.enforced_signals(rid, [_fri()], ["Ana", "Bo"]) if s["person"] == "Bo"]
    assert "Bo closes" not in sm.prompt_lines(rid, [_fri()], roster_names=["Ana", "Bo"], budget_chars=6000)
    sm.consolidate(rid)
    bo = _mem(rid, key="closer|server|Friday|bo")[0]
    assert (bo["status"], bo["retired_reason"]) == ("retired", "not_closer")


def test_chosen_closers_reads_the_flags_as_the_closer_rule_does(monkeypatch):
    import models
    rid = _rid()
    monkeypatch.setattr(models, "get_leader_flags", lambda r, db_path=None: {"Ana": True, "Bo": False})
    monkeypatch.setattr(staff_settings, "roster", lambda r, db_path=None, include_inactive=False: [
        {"name": "Ana", "role": "Server PM", "active": True, "settings": {}},
        {"name": "Bo", "role": "Server", "active": True, "settings": {}}])
    assert sr.chosen_closers(rid) == {"server": {"ana"}}
    monkeypatch.setattr(models, "get_leader_flags", lambda r, db_path=None: {})
    assert sr.chosen_closers(rid) == {}


def test_the_rotation_plan_hands_closes_only_to_chosen_closers(monkeypatch):
    import models
    rid = _rid()
    for i in range(4):
        mon = _monday(4 - i)
        rows = []
        for k, (who, end) in enumerate((("Ana", "11:00pm"), ("Ben", "11:00pm"), ("Cara", "9:00pm"),
                                        ("Dan", "11:00pm"))):
            for d in (1, 3, 5):
                rows.append(_row((mon + timedelta(days=d)).isoformat(), who, "5:00pm", end))
        hid = _hist(rid, mon, rows)
        assert hid
    monkeypatch.setattr(models, "get_close_times", lambda r, db_path=None: {d: "11:00pm" for d in DAYS})
    plan_all = si.rotation_plan(rid)
    assert set(plan_all["roles"]["Server"]["next_close"]) - {"Ana"}
    _closers(monkeypatch, {"server": {"ana", "dan"}})
    plan = si.rotation_plan(rid)
    srv = plan["roles"]["Server"]
    assert srv["chosen_closers"] is True and set(srv["next_close"]) <= {"Ana", "Dan"}
    assert set(srv["rest_from_close"]) <= {"Ana", "Dan"} and "Ben" in srv["alongside"]
    block = si.rotation_block(plan)
    assert "hand the week's closes first to" not in block
    assert "of its chosen closers, close first with" in block and "Ben" not in block.split("close first with")[1]


# ══ LEARN-11: the floor keeps an opener alive under Cavnar AI drafts ═════

def test_an_opener_the_floor_keeps_showing_stays_in_force_under_cavnar_ai_drafts():
    rid = _rid()
    today = date.today()
    sats = sorted({today - timedelta(days=(today.weekday() - 5) % 7 + 7 * w) for w in range(1, 23)})
    pre, drafted = sats[:8], sats[8:]
    for d in pre:                                             # the restaurant's own scheduling
        _punch(rid, d, "Ana", "08:55", "16:00", 7, role="Kitchen")
        _punch(rid, d, "Bo", "10:00", "18:00", 8, role="Kitchen")
    for d in drafted:                                         # Cavnar AI drafts; the manager keeps it
        mon = d - timedelta(days=d.weekday())
        rows = [_row(d.isoformat(), "Ana", "9:00am", "4:00pm", role="Kitchen", hours=7),
                _row(d.isoformat(), "Bo", "10:00am", "6:00pm", role="Kitchen", hours=8)]
        _week(rid, mon, rows, rows)
        _punch(rid, d, "Ana", "08:55", "16:00", 7, role="Kitchen")
        _punch(rid, d, "Bo", "10:00", "18:00", 8, role="Kitchen")
    sm.consolidate(rid)
    ana = next(m for m in _mem(rid, kind="opener") if m["person"] == "Ana")
    assert ana["status"] == "active" and ana["confidence"] >= sm.ACTIVE_CONFIDENCE
    # what the floor shows renews a fact, never starts one: a drafted-only opener is no habit
    rid2 = _rid(name="Drafted Only", owner_email="d@x.test")
    for d in drafted[-4:]:
        mon = d - timedelta(days=d.weekday())
        rows = [_row(d.isoformat(), "Cy", "9:00am", "4:00pm", role="Kitchen", hours=7)]
        _week(rid2, mon, rows, rows)
        _punch(rid2, d, "Cy", "08:55", "16:00", 7, role="Kitchen")
    sm.consolidate(rid2)
    assert not _mem(rid2, kind="opener")


# ══ LEARN-12: approved drops nobody claimed ══════════════════════════════

def test_an_approved_drop_nobody_claimed_still_counts_as_a_drop():
    rid = _rid()
    for w in (3, 2, 1):
        sun = (_monday(w) + timedelta(days=6)).isoformat()
        hid = _hist(rid, _monday(w), [_row(sun, "Ana"), _row(sun, "Bo")])
        _sql("INSERT INTO shift_change_requests (restaurant_id, history_id, kind, employee_name, date, shift_start, "
             "shift_end, status, decided_at, decided_by) VALUES (?,?,?,?,?,?,?,'expired',datetime('now'),'erik')",
             rid, hid, "drop", "Ana", sun, "5:00pm", "10:00pm")
        _sql("INSERT INTO shift_change_requests (restaurant_id, history_id, kind, employee_name, date, shift_start, "
             "shift_end, status) VALUES (?,?,?,?,?,?,?,'expired')", rid, hid, "drop", "Bo", sun, "5:00pm", "10:00pm")
    prefs = si.behaviour_preferences(rid)
    assert prefs["Ana"]["avoids"] == ["Sunday night"] and prefs["Ana"]["drops"] == 3
    assert "Bo" not in prefs, "a drop nobody approved is not the manager letting them off"


def test_a_claim_counts_for_the_person_however_it_spelled_them(monkeypatch):
    import people
    rid = _rid()
    day = (date.today() - timedelta(days=3)).isoformat()
    hid = _hist(rid, _monday(1), [_row(day, "Ana")])
    _sql("INSERT INTO shift_change_requests (restaurant_id, history_id, kind, employee_name, replacement_name, date, "
         "shift_start, shift_end, status) VALUES (?,?,?,?,?,?,?,?,'covered')",
         rid, hid, "drop", "Ana", "Mike S.", day, "5:00pm", "10:00pm")
    monkeypatch.setattr(people, "canonical_names", lambda r, names, db_path=None: {
        n: ("Michael Smith" if n == "Mike S." else n) for n in names})
    assert people.cover_record(rid) == {"michael smith": {"accepted": 1, "declined": 0}}


def test_the_next_one_in_after_the_manager_removed_the_drafted_opener_was_chosen_by_nobody():
    rid = _rid()
    for w in (3, 2, 1):
        d = _sat(w).isoformat()
        draft = [_row(d, "Ana", "9:00am", "4:00pm", role="Kitchen", hours=7),
                 _row(d, "Bo", "10:00am", "6:00pm", role="Kitchen", hours=8)]
        final = [draft[1]]                            # Ana taken off; Bo's shift untouched
        _week(rid, _monday(w), draft, final)
    sm.consolidate(rid)
    assert not [m for m in _mem(rid, kind="opener") if m["person"] == "Bo"]
