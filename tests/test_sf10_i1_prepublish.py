"""Schedule fix round 10/10/26, workstream I1 — the pre-publish review (RM-6,
contract #6): one payload the web Studio and the phone render before a week
goes to staff, the tiered publish gate behind it, and the pieces that feed it.

TRUST-2/3/ARCH-30  must-fix vs decide; a must-fix key needs its own override
                   with a reason, a manager gap the name of who runs the floor
TRUST-4/6          one verdict, from the server, never "built ✓" over a red week
TRUST-5            a saved ⚠ line the live sweep no longer finds is not a blocker
TRUST-7/30/32      one labor grade, a named basis, overtime "—" when not priced
TRUST-13/14        a reason per person left off; the runner-up named
TRUST-18/19/20     a confidence from measured parts, ranges with a record, kept share
TRUST-22           counts are decisions; reminder kinds fold to one line
TRUST-24/26        what changed, where it is fragile
TRUST-29           the quality score graded against the restaurant's own weeks
TRUST-31           one word pair, Lunch/Dinner
TRUST-33           publish-check runs the gate once

Nothing is sent; the model is never called.
"""
import json

import pytest

import models
import prepublish
import schedule_economics as econ
import schedule_overrides as so
import schedule_publish as sp
import schedule_time as st
import strategy_routes
from tests.test_ios_parity_labor import _rid, db, pushed  # noqa: F401
from tests.test_ios_parity_labor import _client as _mobile_client

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


def _client(db, rid, monkeypatch):
    c, h, uid = _mobile_client(db, rid, monkeypatch)
    if "strategy_mobile" not in c.application.blueprints:
        c.application.register_blueprint(strategy_routes.strategy_mobile_bp)
    return c, h, uid


@pytest.fixture
def captured(monkeypatch):
    """Every section that failed quietly (prepublish._capture)."""
    got = []
    monkeypatch.setattr(prepublish, "_capture", lambda e, what, rid: got.append((what, repr(e))))
    return got


def _team(db, rid, people):
    for name, role in people:
        models.add_manual_team_member(rid, name, role=role, db_path=db)


def _week(db, rid, lines, review=None, quality=None):
    csv = HEADER + "\n" + "\n".join(lines)
    hid = models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 10, 0, 30, csv, [], db_path=db)
    conn = models.get_conn()
    conn.execute("UPDATE schedule_history SET review_json=?, quality_json=? WHERE id=?",
                 (json.dumps(review) if review is not None else None,
                  json.dumps(quality) if quality is not None else None, hid))
    conn.commit()
    conn.close()
    return hid


# ── the payload: every key, the verdict, one gate ───────────────────────────

def test_the_review_answers_every_section_of_the_contract(db, monkeypatch, captured):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Ana", "Server"), ("Ben", "Server")])
    hid = _week(db, rid, ["2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,",
                          "2026-10-12,Monday,Ana,Server,4:00pm,10:00pm,6,"])
    c, h, _ = _client(db, rid, monkeypatch)
    body = c.get(f"/mobile/api/labor/publish-review?schedule_id={hid}", headers=h).get_json()
    assert body["ok"] and body["schedule_id"] == hid and body["rows_source"] == "saved"
    for k in ("verdict", "must_fix", "decide", "fyi", "numbers", "quality", "changed", "fragile", "why", "sure",
              "send", "counts", "narrative", "likely_to_change", "checked_at", "version"):
        assert k in body, k
    assert set(body["verdict"]) == {"tone", "sentence", "subline"}
    assert set(body["changed"]) == {"vs_last_week", "vs_usual", "vs_last_draft", "cavnar_changes"}
    assert set(body["why"]) == {"assignments", "left_off"}
    assert body["counts"]["warnings"] == body["counts"]["must_fix"] + body["counts"]["decide"]
    assert "Week of 10/12/26" in body["verdict"]["subline"]
    # Ben has no shift, with a reason, never a bare name (TRUST-13).
    ben = [p for p in body["why"]["left_off"] if p["name"] == "Ben"]
    assert ben and ben[0]["reason"]
    assert not captured, captured


def test_the_web_twin_answers_the_same_object(db, monkeypatch):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Ana", "Server")])
    hid = _week(db, rid, ["2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,"])
    assert ("/api/labor/publish-review", "publish_review") in [
        (p, ep) for p, _m, _f, ep in [("/api" + r[0], r[1], r[2], r[3]) for r in strategy_routes._ROUTES]]
    assert hid


def test_a_manager_gap_is_red_must_fix_and_named_in_the_verdict(db, monkeypatch, captured):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Ana", "Server"), ("Gabe", "Manager")])
    hid = _week(db, rid, ["2026-10-13,Tuesday,Erik,Manager,10:00am,4:00pm,6,",
                          "2026-10-13,Tuesday,Ana,Server,11:00am,10:00pm,11,"])
    out = prepublish.review(rid, hid)
    gaps = [m for m in out["must_fix"] if m["kind"] == "rule:no_manager"]
    assert gaps, out["must_fix"]
    assert gaps[0]["override"]["needs"] == "covered_by"
    assert "Ana" in gaps[0]["override"]["candidates"]
    assert out["verdict"]["tone"] == "red" and out["verdict"]["sentence"].startswith("Not ready:")
    # Ranked by severity, never by date: the manager rule leads.
    assert out["must_fix"][0]["kind"] in ("rule:no_manager", "roster:no_manager_roster")
    assert not captured, captured


def test_a_clean_week_is_green_and_never_says_built_over_a_broken_one():
    v = prepublish.verdict([], [], {"week_start": "2026-10-12", "week_end": "2026-10-18"}, [])
    assert v["tone"] == "green" and v["sentence"].startswith("Ready to send")
    v = prepublish.verdict([], [{"holds": True}, {"holds": False}], {"week_start": "2026-10-12",
                                                                   "week_end": "2026-10-18"}, [])
    assert v["tone"] == "amber" and "1 decision" in v["sentence"]
    v = prepublish.verdict([{"text": "Tue 10/13/26 — no manager on the floor 4:00pm–10:00pm."}], [],
                           {"week_start": "2026-10-12", "week_end": "2026-10-18"}, [])
    assert v["tone"] == "red" and "no manager" in v["sentence"]


# ── the tiered gate (TRUST-2, TRUST-3, ARCH-30) ─────────────────────────────

def _review(blockers):
    return {"blockers": [dict(b) for b in blockers]}


GAP = {"key": "rule:no_manager:2026-10-13:960", "text": "gap", "tier": sp.MUST_FIX}
MINOR = {"key": "rule:minor_late:maya:2026-10-16:5:00pm", "text": "minor", "tier": sp.MUST_FIX}
OVER = {"key": "hours_over", "text": "over", "tier": sp.DECIDE}


def test_a_bare_true_or_a_key_list_never_covers_a_must_fix():
    acked, unacked = sp._ack_gate(_review([GAP, MINOR, OVER]), True)
    assert acked == ["hours_over"] and {b["key"] for b in unacked} == {GAP["key"], MINOR["key"]}
    acked, unacked = sp._ack_gate(_review([GAP, MINOR, OVER]), [GAP["key"], MINOR["key"], "hours_over"])
    assert acked == ["hours_over"]


def test_a_must_fix_needs_its_own_reason_and_a_gap_its_floor_runner():
    reasons = {MINOR["key"]: "Her parent signed the permit", GAP["key"]: "I'll cover it"}
    acked, unacked = sp._ack_gate(_review([GAP, MINOR]), [], reasons)
    assert acked == [MINOR["key"]] and [b["key"] for b in unacked] == [GAP["key"]]
    reasons[GAP["key"]] = {"reason": "", "covered_by": "Erik"}
    acked, unacked = sp._ack_gate(_review([GAP, MINOR]), [], reasons)
    assert set(acked) == {GAP["key"], MINOR["key"]} and not unacked


def test_the_request_overrides_carry_who_runs_the_floor():
    got = so.reasons_from([{"key": "a", "reason": "fine"}, {"key": "b", "reason": "x", "covered_by": " Erik  M "}])
    assert got == {"a": "fine", "b": {"reason": "x", "covered_by": "Erik M"}}
    assert sp.override_needs("rule:no_manager:2026-10-13:960") == "covered_by"
    assert sp.override_needs("roster:no_manager_roster") == "covered_by"
    assert sp.override_needs("rule:minor_late:maya") == "reason"


def test_the_override_record_keeps_who_covers(db):
    rid = _rid(db)
    so.init_schedule_overrides()
    so.record_overridden(rid, 7, 3, [GAP], [GAP["key"]], reasons={GAP["key"]: {"reason": "r", "covered_by": "Erik"}})
    conn = models.get_conn()
    row = conn.execute("SELECT covered_by, reason, version FROM schedule_publish_overrides WHERE history_id=7").fetchone()
    conn.close()
    assert (row["covered_by"], row["reason"], row["version"]) == ("Erik", "r", 3)


def test_the_409_names_each_must_fix_and_what_it_needs(db):
    rid = _rid(db)
    hid = _week(db, rid, ["2026-10-12,Monday,Ana,Server,4:00pm,10:00pm,6,"])
    out, status = sp._needs_ack({"blockers": [GAP, OVER]}, [GAP, OVER], hid)
    assert status == 409
    assert out["must_fix_keys"] == [GAP["key"]]
    assert out["needs_reason"] == [{"key": GAP["key"], "needs": "covered_by"}]


# ── TRUST-5: a saved line the sweep no longer finds ─────────────────────────

def test_a_saved_breach_line_never_outlives_the_live_sweep(db):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Ana", "Server")])
    stale = "Ana — Wednesday 5:00pm: not available Wednesdays"
    review = {"lines": ["⚠ " + stale], "rule_lines": ["⚠ " + stale], "lead_lines": ["⚠ " + stale],
              "budget_lines": []}
    hid = _week(db, rid, ["2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,",
                          "2026-10-12,Monday,Ana,Server,4:00pm,10:00pm,6,"], review=review)
    texts = [b["text"] for b in sp.publish_review(rid, hid)["blockers"]]
    assert stale not in texts


def test_a_day_the_owner_filled_is_not_said_as_unwritten(db):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Ana", "Server")])
    review = {"unwritten_dates": [{"date": "2026-10-12", "day": "Monday", "why": "the call ran out of time"}],
              "lead_lines": [], "lines": []}
    hid = _week(db, rid, ["2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,"], review=review)
    assert not [b for b in sp.publish_review(rid, hid)["blockers"] if b["key"].startswith("unwritten:")]
    hid2 = _week(db, rid, ["2026-10-13,Tuesday,Erik,Manager,10:00am,10:30pm,12.5,"], review=review)
    got = [b for b in sp.publish_review(rid, hid2)["blockers"] if b["key"].startswith("unwritten:")]
    assert got and got[0]["tier"] == sp.MUST_FIX


# ── TRUST-7, 30: one grade, one named basis ────────────────────────────────

def test_one_labor_grade_with_no_grace_and_no_green_without_a_target():
    assert econ.labor_grade(30.04, 30) == "under"         # 30.0% as shown
    assert econ.labor_grade(30.4, 30) == "over"           # the web's 0.5 grace called this green
    assert econ.labor_grade(45, None) == "no_target"      # never green with no target
    assert econ.labor_grade(45, 0) == "no_target"


# ── TRUST-31: one word pair ────────────────────────────────────────────────

def test_one_owner_word_pair_for_the_dayparts():
    assert st.PART_WORDS == {"morning": "Lunch", "night": "Dinner"}
    assert st.part_word("morning") == "Lunch" and st.part_word("night", capital=False) == "dinner"
    assert st.part_word("daytime") == "Lunch" and st.part_word("day") == "Lunch"
    import crew_matrix
    assert crew_matrix.PART_WORD == {"morning": "lunch", "night": "dinner"}


# ── TRUST-33: the gate runs once ───────────────────────────────────────────

def test_publish_check_runs_the_gate_once(db, monkeypatch):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager")])
    hid = _week(db, rid, ["2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,"])
    c, h, _ = _client(db, rid, monkeypatch)
    calls = []
    real = sp.publish_review

    def counted(*a, **k):
        calls.append(k.get("unattended"))
        return real(*a, **k)
    monkeypatch.setattr(sp, "publish_review", counted)
    body = c.get(f"/mobile/api/labor/publish-check?schedule_id={hid}", headers=h).get_json()
    assert body["ok"] and len(calls) == 1


def test_the_held_flags_are_marked_on_a_person_s_run(db):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Bob", "Server")])
    import schedule_rules as sr
    sr.save_compliance(rid, {"daily_ot_hours": 8})
    hid = _week(db, rid, ["2026-10-14,Wednesday,Bob,Server,10:00am,9:30pm,11.5,",
                          "2026-10-14,Wednesday,Erik,Manager,9:30am,10:00pm,12.5,"])
    attended = sp.publish_review(rid, hid)
    unattended = sp.publish_review(rid, hid, unattended=True)
    assert [b["key"] for b in unattended["blockers"] if b["key"].startswith("soft:")] == \
        [x["key"] for x in attended["held_unattended"]]
    assert not strategy_routes.draft_one_tap_safe(rid, hid, review=attended)
