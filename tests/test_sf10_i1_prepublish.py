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
    # Additive since the re-audit (10/11/26 contract #8): worth_a_look on the
    # verdict; whose reasons and which engine drafted, on why.
    assert set(body["verdict"]) == {"tone", "sentence", "subline", "worth_a_look"}
    assert set(body["changed"]) == {"vs_last_week", "vs_usual", "vs_last_draft", "cavnar_changes"}
    assert set(body["why"]) == {"assignments", "left_off", "engine", "draft"}
    for k in ("must_fix_groups", "decide_groups", "suggestions", "always_overridden"):
        assert k in body, k
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
    # Only a manager or owner on the roster who can work that day (re-audit
    # 10/11/26 TRUST-5) — never anybody who happens to be on the floor.
    assert set(gaps[0]["override"]["candidates"]) == {"Erik", "Gabe"}
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


GAP = {"key": "rule:no_manager:2026-10-13:960", "text": "gap", "tier": sp.MUST_FIX, "cls": "manager",
       "cover_candidates": ["Erik"]}
MINOR = {"key": "rule:minor_late:maya:2026-10-16:5:00pm", "text": "minor", "tier": sp.MUST_FIX, "cls": "law",
         "law": True, "fixable": True}
AWAY = {"key": "rule:unavailable_day:ana:2026-10-16:5:00pm", "text": "unavailable", "tier": sp.MUST_FIX,
        "cls": "person"}
OVER = {"key": "hours_over", "text": "over", "tier": sp.DECIDE}


def test_a_bare_true_or_a_key_list_never_covers_a_must_fix():
    acked, unacked = sp._ack_gate(_review([GAP, MINOR, OVER]), True)
    assert acked == ["hours_over"] and {b["key"] for b in unacked} == {GAP["key"], MINOR["key"]}
    acked, unacked = sp._ack_gate(_review([GAP, MINOR, OVER]), [GAP["key"], MINOR["key"], "hours_over"])
    assert acked == ["hours_over"]


def test_a_must_fix_needs_its_own_reason_and_a_gap_its_floor_runner():
    # Re-audit 10/11/26 TRUST-4/5: the law (a minor's hours) is never typed
    # past; a gap is covered only by a manager the gate offers (never
    # "Mickey Mouse"); a reason is a few words, never "x".
    reasons = {MINOR["key"]: "Her parent signed the permit", GAP["key"]: "I'll cover it", AWAY["key"]: "x"}
    acked, unacked = sp._ack_gate(_review([GAP, MINOR, AWAY]), [], reasons)
    assert acked == [] and {b["key"] for b in unacked} == {GAP["key"], MINOR["key"], AWAY["key"]}
    assert all(b.get("override_problem") for b in unacked)
    reasons[GAP["key"]] = {"reason": "", "covered_by": "Mickey Mouse"}
    reasons[AWAY["key"]] = "She swapped her day off with Ben"
    acked, unacked = sp._ack_gate(_review([GAP, MINOR, AWAY]), [], reasons)
    gap = [b for b in unacked if b["key"] == GAP["key"]][0]
    assert acked == [AWAY["key"]] and "isn't a manager" in gap["override_problem"]
    reasons[GAP["key"]] = {"reason": "", "covered_by": "Erik"}
    acked, unacked = sp._ack_gate(_review([GAP, MINOR, AWAY]), [], reasons)
    assert set(acked) == {GAP["key"], AWAY["key"]} and [b["key"] for b in unacked] == [MINOR["key"]]
    assert not sp.override_policy(MINOR)["allowed"] and sp.override_policy(GAP)["needs"] == "covered_by"


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


# ── TRUST-19: a range from the measured forecast error, with the record ─────

def _forecast_weeks(rid, errors):
    conn = models.get_conn()
    for i, e in enumerate(errors):
        end = (__import__("datetime").date(2026, 1, 4) + __import__("datetime").timedelta(days=7 * i)).isoformat()
        conn.execute("INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, actual, error_pct, "
                     "signed_error_pct, scored_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
                     (rid, "revenue_week", end, 10000.0, 10000.0 * (1 - e / 100.0), e, -e))
    conn.commit()
    conn.close()


def test_the_forecast_record_says_how_often_it_landed_and_no_range_below_the_floor(db):
    rid = _rid(db)
    assert prepublish.forecast_record(rid) is None
    _forecast_weeks(rid, [2, 3, 4, 5, 6, 7, 8, 9, 20, 30])
    rec = prepublish.forecast_record(rid)
    # The newest forecast_log.ACCURACY_WINDOW weeks: 4..9, 20, 30.
    assert rec["weeks"] == 8 and rec["within_pct"] == 20 and rec["weeks_within"] == 7
    assert rec["mean_error_pct"] == 11.1


def test_labor_is_a_range_with_its_track_record(db, monkeypatch):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Ana", "Server")])
    _forecast_weeks(rid, [5, 5, 5, 5, 10])
    hid = _week(db, rid, ["2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,",
                          "2026-10-12,Monday,Ana,Server,4:00pm,10:00pm,6,"])
    conn = models.get_conn()
    conn.execute("UPDATE schedule_history SET economics_json=? WHERE id=?",
                 (json.dumps({"projected_revenue": 2000, "labor_budget_dollars": 400, "blended_rate": 20}), hid))
    conn.commit()
    conn.close()
    monkeypatch.setattr(prepublish, "_labor_target", lambda r: 30)
    lp = prepublish.review(rid, hid)["numbers"]["labor_pct"]
    assert lp["value"] is not None and lp["low"] < lp["value"] < lp["high"]
    assert "within 5% on 4 of the last 5 weeks" in lp["track_record"]
    assert lp["grade"] in ("over", "under") and lp["label"]


def test_overtime_is_a_dash_when_the_week_was_not_priced(db, monkeypatch):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager")])
    hid = _week(db, rid, ["2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,"])
    monkeypatch.setattr(prepublish, "price_rows", lambda *a, **k: ({}, {"total": 100}))
    ot = prepublish.review(rid, hid)["numbers"]
    assert ot["overtime_hours"] is None and ot["overtime"]["priced"] is False
    assert ot["overtime"]["text"] == "Not priced for this week"


# ── TRUST-29: graded against the restaurant's own weeks ────────────────────

def test_the_quality_score_is_graded_against_the_restaurants_own_weeks(db):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager")])
    line = ["2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,"]
    for i, s in enumerate((60, 62, 64, 66, 68, 70)):
        # Six earlier weeks in force (a second draft of one week supersedes it).
        h = models.save_schedule_history(rid, f"2026-09-0{i + 1}", f"2026-09-0{i + 1}", 1, 0, 30,
                                         HEADER + "\n" + line[0], [], db_path=db)
        conn = models.get_conn()
        conn.execute("UPDATE schedule_history SET quality_score=? WHERE id=?", (s, h))
        conn.commit()
        conn.close()
    q = {"checked": True, "score": 69, "band": "fair",
         "below_profile": [{"date": "2026-10-13", "day": "Tuesday", "daypart": "night", "score": 40,
                            "reason": "Coverage is holding this shift at 40."}]}
    hid = _week(db, rid, line, quality=q)
    got = prepublish.review(rid, hid)["quality"]
    assert got["grade"] == "good" and got["percentile"] == 83 and got["weeks_compared"] == 6
    assert got["lead"].startswith("Capped by Tue 10/13/26 dinner")
    q["score"] = 59
    hid = _week(db, rid, line, quality=q)
    assert prepublish.review(rid, hid)["quality"]["grade"] == "weak"


def test_a_week_with_too_few_scored_weeks_is_ungraded(db):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager")])
    hid = _week(db, rid, ["2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,"],
                quality={"checked": True, "score": 75, "band": "good"})
    q = prepublish.review(rid, hid)["quality"]
    assert q["grade"] == "ungraded" and "graded once 5 of your weeks" in q["text"]


# ── TRUST-13: a reason per person left off ─────────────────────────────────

def test_who_is_left_off_says_why_first(db):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Ana", "Server"), ("Rosa", "Server"), ("Kim", "Cook")])
    conn = models.get_conn()
    conn.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
                 "VALUES (?,?,?,?,?)", (rid, "Rosa", "2026-10-12", "2026-10-18", "approved"))
    conn.commit()
    conn.close()
    hid = _week(db, rid, ["2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,",
                          "2026-10-12,Monday,Ana,Server,4:00pm,10:00pm,6,"])
    left = {p["name"]: p for p in prepublish.review(rid, hid)["why"]["left_off"]}
    assert left["Rosa"]["cause"] == "time_off" and left["Rosa"]["reason"].startswith("time off 10/12/26")
    assert left["Kim"]["cause"] == "role_full" and "Cook" in left["Kim"]["reason"]


# ── TRUST-22: counts are decisions; reminders fold ─────────────────────────

def test_the_soft_rules_fold_to_one_line_per_kind():
    viols = [{"kind": "meal_break", "hard": False, "employee": f"P{i}", "date": "2026-10-12", "day": "Monday",
              "shift_start": "10:00am", "detail": "x", "label": "y"} for i in range(11)]
    viols += [{"kind": "daily_ot", "hard": False, "employee": "Bob", "date": "2026-10-13", "day": "Tuesday",
               "shift_start": "9:00am", "detail": "11.5h in a day", "label": "daily overtime"}]
    fyi = prepublish._fyi({"notes": []}, viols)
    meal = [f for f in fyi if f["kind"] == "meal_break"]
    assert len(meal) == 1 and meal[0]["count"] == 11 and meal[0]["text"].startswith("11 shifts")
    assert fyi[-1]["kind"] == "meal_break"          # a reminder ranks last


# ── TRUST-4: the rows on screen ───────────────────────────────────────────

def test_the_rows_on_screen_are_checked_without_saving(db, monkeypatch):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Ana", "Server")])
    hid = _week(db, rid, ["2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,",
                          "2026-10-12,Monday,Ana,Server,4:00pm,10:00pm,6,"])
    c, h, _ = _client(db, rid, monkeypatch)
    screen = [{"date": "2026-10-12", "day": "Monday", "employee": "Ana", "role": "Server",
               "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}]
    body = c.post("/mobile/api/labor/publish-review", headers=h, json={"schedule_id": hid, "rows": screen}).get_json()
    assert body["rows_source"] == "screen" and body["verdict"]["tone"] == "red"
    assert any(m["kind"] == "rule:no_manager" for m in body["must_fix"])
    assert "unsaved edits included" in body["verdict"]["subline"]
    saved = c.get(f"/mobile/api/labor/publish-review?schedule_id={hid}", headers=h).get_json()
    assert not any(m["kind"] == "rule:no_manager" for m in saved["must_fix"])
    bad = c.post("/mobile/api/labor/publish-review", headers=h,
                 json={"schedule_id": hid, "rows": [{"date": "Monday"}]})
    assert bad.status_code == 400


# ── TRUST-26: fragile, with its one-tap ───────────────────────────────────

def test_the_closing_stay_the_verdict_assumes_is_fragile_with_its_one_tap(db, monkeypatch):
    import schedule_rules as sr
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Ana", "Server")])
    hid = _week(db, rid, ["2026-10-12,Monday,Erik,Manager,10:00am,10:00pm,12,"])
    monkeypatch.setattr(sr, "assumed_stays", lambda rows, c, tz=None: [{
        "date": "2026-10-12", "employee": "Erik", "until": "10:45pm", "text": "Mon 10/12/26 close: Erik is assumed "
        "to stay until 10:45pm", "action": {"label": "Schedule Erik to 10:45pm", "action": "extend_shift",
                                            "payload": {"date": "2026-10-12", "employee": "Erik",
                                                        "shift_start": "10:00am", "shift_end": "10:45pm"}}}])
    stay = [f for f in prepublish.review(rid, hid)["fragile"] if f["kind"] == "assumed_stay"]
    assert stay and stay[0]["action"]["action"] == "edit_rows"
    ch = stay[0]["action"]["payload"]["change"][0]
    assert ch["set"] == {"shift_end": "10:45pm"} and ch["match"]["employee"] == "Erik"


# ── TRUST-18, 20: how sure, from measured parts only ───────────────────────

def test_no_confidence_is_stated_before_anything_was_measured(db):
    out = prepublish._sure(_rid(db), {}, None, {})
    assert out["confidence"]["pct"] is None and out["kept_share"] is None


def test_drafts_kept_unchanged_and_the_forecast_are_context_never_the_confidence(db, monkeypatch):
    # Re-audit 10/11/26 TRUST-6: the old figure averaged the share of drafts
    # sent unchanged (it rose when owners stopped reading), the forecast's
    # error and the scorer's completeness — one unedited week read 100%.
    # Those stay as lines; the % is the K1 engine over how published weeks
    # RAN, "—" with none measured.
    import schedule_versions as sv
    rid = _rid(db)
    monkeypatch.setattr(sv, "acceptance", lambda r, weeks=8: {
        "weeks": [{"unchanged_share": 1.0}], "mean_unchanged_share": 1.0, "trend": None})
    monkeypatch.setattr(prepublish, "forecast_record", lambda r: {"weeks": 10, "within_pct": 8, "weeks_within": 8,
                                                                  "mean_error_pct": 6.0, "last": None})
    out = prepublish._sure(rid, {"score": 80, "confidence": {"score": 96}}, None, {})
    assert out["kept_share"] == 1.0
    assert out["confidence"]["pct"] is None and out["confidence"]["weeks_measured"] == 0
    assert any(l.startswith("Your last 1 draft went out with 100% of shifts unchanged") for l in out["lines"])


# ── TRUST-16: the stages the job reached ───────────────────────────────────

def test_the_generation_reports_only_the_stages_it_reached():
    import schedule_engine as se
    clock = se.GenerationClock(job_id=None)
    assert clock.progress()["stage"] is None and not any(s["done"] for s in clock.progress()["stages"])
    clock.reach("rules")
    clock.reach("inputs")                    # never backwards
    p = clock.progress()
    assert p["stage"] == "rules"
    assert [s["key"] for s in p["stages"] if s["done"]] == ["inputs", "draft"]
    assert [s["key"] for s in p["stages"] if s["current"]] == ["rules"]


# ── TRUST-25: a reopened draft keeps its aids ──────────────────────────────

def test_a_reopened_draft_carries_its_aids_again(db, monkeypatch):
    import schedule_engine as se
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Ana", "Server")])
    hid = _week(db, rid, ["2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,"])
    monkeypatch.setattr(se, "generation_aids", lambda r, rows, dates, constraints=None, roster_roles=None: {
        "overtime_forecast": [{"employee": "Ana"}], "standby_days": [{"date": "2026-10-12"}],
        "likely_edits": [{"employee": "Erik"}]})
    c, h, _ = _client(db, rid, monkeypatch)
    d = c.get(f"/mobile/api/labor/schedule-history/{hid}", headers=h).get_json()
    assert d["standby_days"] == [{"date": "2026-10-12"}] and d["likely_edits"] and d["overtime_forecast"]


def test_the_aids_are_the_generations_own_functions(db, monkeypatch):
    import schedule_engine as se
    import schedule_learning as sl
    rid = _rid(db)
    monkeypatch.setattr(sl, "overtime_forecast", lambda rows, constraints=None, roster_roles=None: [{"text": "ot"}])
    monkeypatch.setattr(sl, "price_overtime_moves", lambda f, *a, **k: f)
    asked = {}
    monkeypatch.setattr(sl, "standby_days", lambda r, dates, rows=None, limit=2, requirements=None, busy=None:
                        asked.update(req=requirements) or [{"date": dates[0]}])
    # The generation's own requirement inputs, re-derived (G2's standby_shift).
    monkeypatch.setattr(se, "quality_inputs_from_db", lambda r, week_rows=None: {"requirements": [{"date": "x"}]})
    monkeypatch.setattr(se, "likely_edits", lambda r, rows: [{"text": "likely"}])
    got = se.generation_aids(rid, [], ["2026-10-12"])
    assert asked["req"] == [{"date": "x"}]
    assert got == {"overtime_forecast": [{"text": "ot"}], "standby_days": [{"date": "2026-10-12"}],
                   "likely_edits": [{"text": "likely"}]}


# ── TRUST-14: what Cavnar AI weighed names the runner-up ───────────────────

def test_the_why_names_the_runner_up_and_never_dresses_up_a_coin_flip():
    class C:
        def max_hours(self, n):
            return 40.0
    rows = [{"date": "2026-10-16", "employee": "Ana", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "11:00pm", "scheduled_hours": "7"}] + \
        [{"date": d, "employee": "Ben", "role": "Server", "shift_start": "9:00am", "shift_end": "9:00pm",
          "scheduled_hours": "12"} for d in ("2026-10-13", "2026-10-14", "2026-10-15")]
    usual = {"Ana": {"days": ["Friday"]}, "Ben": {"days": ["Monday"]}}
    got = prepublish.weigh_one(1, rows, 0, C(), {"Ana": 4, "Ben": 3}, usual, runner="Ben")
    assert got["runner_up"] == "Ben" and got["text"].startswith("Ana over Ben:")
    assert "rated 4/5 against Ben's 3/5" in got["text"] and "usually works Fridays" in got["text"]
    assert "33h of room left against Ben's 4h" in got["text"]
    even = prepublish.weigh_one(1, rows[:1] + [], 0, C(), {}, {}, runner="Cy")
    assert "nothing on file" in even["text"] and "could legally have taken it too" in even["text"]
    alone = prepublish.weigh_one(1, rows, 0, C(), {}, {}, runner=None)
    assert alone["runner_up"] is None and "nobody else" in alone["text"]


# ── G2: a standby the requirement asks for is fragile; a floor the work never
#    needed is a decision written to the owner's floors ─────────────────────

def test_a_standby_shift_is_fragile_with_its_ask(db, monkeypatch):
    import schedule_engine as se
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Ana", "Server")])
    hid = _week(db, rid, ["2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,"])
    monkeypatch.setattr(se, "standby_for_rows", lambda r, rows, dates, inputs=None, limit=2: [{
        "date": "2026-10-16", "day": "Friday", "chance_of_a_no_show": 0.1, "base_rate": 0.04,
        "standby_shift": {"text": "Fri 10/16/26 dinner (servers: 4 needed, 3.4 expected)", "p_short": 0.42},
        "standby": {"employee": "Lee", "role": "Server", "shift_start": "4:00pm", "shift_end": "10:00pm"}}])
    fr = prepublish.review(rid, hid)["fragile"]
    sb = [f for f in fr if f["kind"] == "standby"]
    assert sb and sb[0]["text"].startswith("A standby is needed on Fri 10/16/26 dinner")
    assert sb[0]["action"]["payload"]["path"] == "labor/standby/ask"
    assert sb[0]["action"]["payload"]["body"]["employee"] == "Lee"
    assert fr[0]["kind"] == "standby"                 # ranked first


def test_a_floor_the_work_never_needed_is_a_scoped_decision(db, monkeypatch):
    import staffing_model
    import schedule_rules as sr
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Ana", "Server")])
    models.update_restaurant(rid, {"role_floors_json": json.dumps({"Server": {"morning": 2, "night": 3, "days": {}}})})
    hid = _week(db, rid, ["2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,"])
    monkeypatch.setattr(staffing_model, "floor_suggestions", lambda r, **k: [{
        "day": "Monday", "daypart": "night", "role": "Server", "floor": 3, "suggest": 2,
        "text": "Monday dinner: your floor holds 3 Server on at once, but the work never needed more than 2."}])
    dec = [d for d in prepublish.review(rid, hid)["decide"] if d["kind"] == "floor"]
    assert dec and dec[0]["holds"] is False
    lower = dec[0]["options"][0]
    assert lower["scope"] == "every" and lower["action"] == "post" and lower["payload"]["path"] == "labor/rules"
    spec = lower["payload"]["body"]["role_floors"]["Server"]
    assert sr.floor_for({"Server": spec}, "Server", "Monday", "night") == 2
    assert sr.floor_for({"Server": spec}, "Server", "Tuesday", "night") == 3   # every other floor as it was
    assert dec[0]["options"][1]["action"] == "dismiss"


# ── J2: a POS write that failed is said after Send ─────────────────────────

def test_a_failed_pos_push_is_said_on_the_sent_week(db, monkeypatch):
    import pos_schedule_push as psp
    rid = _rid(db)
    row = {"published_at": "2026-10-10 12:00:00", "week_start": "2026-10-12"}
    monkeypatch.setattr(psp, "last_push", lambda r, ws, db_path=None: {
        "status": "failed", "error": "RPOWER refused the job code", "shifts": [], "sent_on": None})
    got = prepublish.pos_push_status(rid, row)
    assert got["status"] == "failed" and got["text"] == "The week didn't reach your POS: RPOWER refused the job code."
    monkeypatch.setattr(psp, "last_push", lambda r, ws, db_path=None: None)
    assert prepublish.pos_push_status(rid, row) is None
    assert prepublish.pos_push_status(rid, {"published_at": None, "week_start": "2026-10-12"}) is None


def test_the_held_week_push_never_offers_send_now_past_a_must_fix_item():
    import delayed
    items = [{"key": "notice_short", "text": "late", "tier": sp.DECIDE},
             {"key": "input:availability:ana", "text": "Ana's settings", "tier": sp.MUST_FIX}]
    assert delayed._send_now_keys({}, {"blocker_items": items, "new_blockers": ["late"]}) is None
    assert delayed._send_now_keys({}, {"blocker_items": items[:1], "new_blockers": ["late"]}) == ["notice_short"]
