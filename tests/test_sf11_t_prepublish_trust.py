"""Schedule fix round 10/11/26, workstream T — pre-publish trust (the blind
re-audit's TRUST lens): what the owner sees and decides with before a week
goes to staff.

TRUST-1/2/12   the "why" never argues against the pick; the solver's own
               reasons are kept with the draft and read; chosen by risk, one
               per person
TRUST-3        no green "nothing breaks your rules" over overtime
TRUST-4/5/8    the law is fixed or not sent; a manager gap is covered by a
               real manager or owner; a reason is a few words; a posting-
               notice law is must-fix, the owner's own rule a decision
TRUST-6        "How sure" is the K1 engine over how published weeks ran
TRUST-9, BUGSLIFE-12  demotion by content and size; "shown" when rendered
TRUST-10/11/13/14/15/16/17/18  always-overridden with a rule change; people
               not shifts; "all filled" only when true; fixes say their side
               effect; groups; counts; kept answers; stays folded
TRUST-19/26/27/28/33/34  web fallback; core first + cache + time-bound
               searches; the budget's own trim; one figure per tile; trust
               text at body size; one gate run

Nothing is sent; the model is never called.
"""
import json
import re

import pytest

import models
import prepublish
import schedule_job
import schedule_overrides as so
import schedule_publish as sp
import schedule_rules as sr
from tests.test_ios_parity_labor import _rid, db, pushed  # noqa: F401

@pytest.fixture(autouse=True)
def _before_the_week(monkeypatch):
    """These weeks are 10/12-10/18/26: the gate reads them from before they
    start, whatever day the suite runs (a week under way steps its worked
    days aside - schedule_publish, 10/14/26)."""
    import datetime as _dt
    import schedule_publish as _sp_fx
    monkeypatch.setattr(_sp_fx, "_local_today", lambda rid: _dt.date(2026, 10, 9))
    import strategy_routes as _sr_fx
    monkeypatch.setattr(_sr_fx, "_local_today", lambda *a, **k: _dt.date(2026, 10, 9))


HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
SRC = open("templates/dashboard.html", encoding="utf-8").read()
MON = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]


@pytest.fixture(autouse=True)
def _fresh_cache():
    prepublish.invalidate()
    yield
    prepublish.invalidate()


def _team(db, rid, people):
    for name, role in people:
        models.add_manual_team_member(rid, name, role=role, db_path=db)


def _week(db, rid, lines, review=None, economics=None):
    csv = HEADER + "\n" + "\n".join(lines)
    hid = models.save_schedule_history(rid, MON[0], MON[-1], 0, 0, 30, csv, [])
    conn = models.get_conn()
    conn.execute("UPDATE schedule_history SET review_json=?, economics_json=? WHERE id=?",
                 (json.dumps(review) if review is not None else None,
                  json.dumps(economics) if economics is not None else None, hid))
    conn.commit()
    conn.close()
    return hid


def _c(rid):
    import datetime as dt
    days = [dt.date.fromisoformat(d).strftime("%A") for d in MON]
    return sr.build_constraints(rid, MON, days, restaurant=models.get_restaurant(rid))


# ── TRUST-1: never a reason against the pick ───────────────────────────────

def test_a_stronger_runner_up_is_said_as_one_with_a_swap(db):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Ben", "Server"), ("Cara", "Server")])
    rows = [{"date": MON[0], "day": "Monday", "employee": "Erik", "role": "Manager", "shift_start": "10:00am",
             "shift_end": "11:00pm", "scheduled_hours": 13},
            {"date": MON[0], "day": "Monday", "employee": "Ben", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": 6}]
    got = prepublish.weigh_one(rid, rows, 1, _c(rid), {"Ben": 2, "Cara": 5}, {})
    assert "Ben over Cara" not in got["text"] and "Ben is rated 2/5 against" not in got["text"]
    assert "Cara" in got["text"] and "rated higher" in got["text"]
    assert got["favours_pick"] is False and got["basis"] == "after"
    assert got["swap"]["payload"]["change"][0]["set"] == {"employee": "Cara"}
    # The pick rated higher still says so, for the pick.
    got = prepublish.weigh_one(rid, rows, 1, _c(rid), {"Ben": 5, "Cara": 2}, {})
    assert got["text"].startswith("Ben over Cara: Ben is rated 5/5") and got["favours_pick"]


# ── TRUST-2: the solver's reasons, kept and read ───────────────────────────

def test_the_final_solve_reasons_are_kept_only_where_the_row_still_carries_the_pick():
    result = {"draft_source": {"solver": {"reasons": [
        {"date": MON[0], "shift_start": "4:00pm", "employee": "Kim", "instead": "Lee",
         "text": "Kim, not Lee: Lee would hit overtime (+$27)."},
        {"date": MON[1], "shift_start": "4:00pm", "employee": "Lee", "instead": "Kim", "text": "Lee, not Kim: x."}]}}}
    rows = [{"date": MON[0], "shift_start": "4:00pm", "employee": "Kim"},
            {"date": MON[1], "shift_start": "4:00pm", "employee": "Max"}]      # a repair moved it
    got = schedule_job.final_assignment_reasons(result, rows)
    assert [r["employee"] for r in got["reasons"]] == ["Kim"] and got["changed_after"] == 1
    assert got["reasons"][0]["runner_up"] == "Lee"


def test_the_why_reads_the_engine_reason_and_labels_a_comparison(db):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Kim", "Server"), ("Lee", "Server")])
    review = {"assignment_reasons": {"source": "solver", "reasons": [
        {"date": MON[4], "shift_start": "4:00pm", "employee": "Kim", "runner_up": "Lee",
         "text": "Kim, not Lee: Lee would hit overtime (+$27)."}]},
        "draft_source": {"source": "solver", "chosen": "solver",
                         "solver": {"critic": {"called": True, "kept": ["a"], "refused": ["b", "c"]}}}}
    hid = _week(db, rid, [f"{MON[4]},Friday,Erik,Manager,10:00am,11:00pm,13,",
                          f"{MON[4]},Friday,Kim,Server,4:00pm,10:00pm,6,",
                          f"{MON[5]},Saturday,Erik,Manager,10:00am,11:00pm,13,",
                          f"{MON[5]},Saturday,Lee,Server,4:00pm,10:00pm,6,"], review=review,
                economics={"daily_target_hours": {MON[4]: 30, MON[5]: 20}})
    out = prepublish.review(rid, hid)
    why = out["why"]
    kim = [a for a in why["assignments"] if a["employee"] == "Kim"]
    assert kim and kim[0]["basis"] == "engine" and "Lee would hit overtime" in kim[0]["text"]
    assert all(a["basis"] in ("engine", "after") and a.get("basis_label") for a in why["assignments"])
    assert why["engine"]["recorded"] and "solver's own" in why["engine"]["text"]
    assert "2 refused" in why["draft"]["text"] and why["draft"]["source"] == "solver"
    # One line per person (TRUST-12).
    people = [a["employee"] for a in why["assignments"]]
    assert len(people) == len(set(people))


def test_no_recorded_reasons_is_said_not_dressed_up():
    st = prepublish.engine_status({}, [{"employee": "A", "date": MON[0], "shift_start": "4:00pm"}])
    assert st["recorded"] is False and "didn't record" in st["text"]


# ── TRUST-3: the verdict says what is true ─────────────────────────────────

def test_green_never_says_nothing_breaks_your_rules_over_overtime():
    row = {"week_start": MON[0], "week_end": MON[-1]}
    fyi = [{"key": "soft:over_hours_ceiling", "kind": "over_hours_ceiling", "count": 2, "text": "x"}]
    nums = {"overtime": {"hours": 25.5, "premium": 332.0, "priced": True}}
    v = prepublish.verdict([], [], row, [], fyi=fyi, numbers=nums)
    assert v["tone"] == "green" and "Nothing breaks your rules" not in v["sentence"]
    assert "25.5h of overtime" in v["sentence"] and v["worth_a_look"]
    v = prepublish.verdict([], [{"holds": True}], row, [], fyi=fyi, numbers=nums)
    assert v["tone"] == "amber" and "Everything else checks out" not in v["sentence"]
    v = prepublish.verdict([], [], row, [])
    assert v["sentence"] == "Ready to send. Nothing breaks your rules, and nothing needs a decision."


# ── TRUST-4/5/8: the gate ──────────────────────────────────────────────────

def _no_mgr_week(db, rid):
    return _week(db, rid, [f"{MON[1]},Tuesday,Erik,Manager,10:00am,4:00pm,6,",
                           f"{MON[1]},Tuesday,Ana,Server,11:00am,10:00pm,11,"])


def test_a_manager_gap_is_not_covered_by_mickey_mouse(db, monkeypatch):
    monkeypatch.setattr(sp, "deliver_notices", lambda **k: {"sent": [], "pending": [], "failed": [],
                                                           "unreachable": [], "sending": []})
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Gabe", "Manager"), ("Ana", "Server")])
    hid = _no_mgr_week(db, rid)
    rev = sp.publish_review(rid, hid)
    gap = [b for b in rev["blockers"] if b["key"].startswith("rule:no_manager")][0]
    assert gap["cls"] == "manager" and "Gabe" in gap["cover_candidates"] and "Ana" not in gap["cover_candidates"]
    actor = {"username": "mgr", "role": "manager"}
    out, status = sp.publish(rid, hid, actor=actor, acknowledge=[gap["key"]],
                             override_reasons={gap["key"]: {"reason": "", "covered_by": "Mickey Mouse"}})
    assert status == 409 and out["override_refused"] and "isn't a manager" in out["override_refused"][0]["why"]
    out, status = sp.publish(rid, hid, actor=actor, acknowledge=[gap["key"]],
                             override_reasons={gap["key"]: {"reason": "", "covered_by": "Gabe"}})
    assert status == 200 and out["ok"]


def test_a_one_letter_reason_is_refused():
    b = {"key": "rule:unavailable_day:ana:x", "tier": sp.MUST_FIX, "cls": "person"}
    assert sp.override_problem(b, {b["key"]: "x"}) and sp.override_problem(b, {b["key"]: "ok, fine by me"}) is None
    assert so.reason_problem("x") and so.reason_problem("aaaaaaaaaa") and so.reason_problem("Ana asked for it") is None


def test_the_law_is_fixed_or_not_sent(db):
    rid = _rid(db)
    b = {"key": "rule:minor_late:maya:x", "tier": sp.MUST_FIX, "cls": "law", "law": True, "fixable": True}
    pol = sp.override_policy(b)
    assert pol["allowed"] is False and "law" in pol["why"]
    acked, unacked = sp._ack_gate({"blockers": [dict(b)]}, [b["key"]], {b["key"]: "Her parent signed the permit"},
                                  actor={"username": "o"})
    assert acked == [] and unacked[0]["override_problem"]
    # A hard breach that is law is classed so by the gate.
    assert sp.breach_class({"kind": "minor_late"}) == "law"
    assert sp.breach_class({"kind": "rest_gap", "law": True}) == "law"
    assert sp.breach_class({"kind": "long_run"}) == "policy"
    assert sp.breach_class({"kind": "approved_time_off"}) == "person"


def test_a_policy_limit_is_a_decision_and_a_jurisdiction_notice_is_the_law(db, monkeypatch):
    import datetime as dt
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Ana", "Server")])
    lines = []
    for d in MON:
        wd = dt.date.fromisoformat(d).strftime("%A")
        lines += [f"{d},{wd},Erik,Manager,10:00am,11:00pm,13,", f"{d},{wd},Ana,Server,4:00pm,10:00pm,6,"]
    hid = _week(db, rid, lines)
    rev = sp.publish_review(rid, hid)
    runs = [b for b in rev["blockers"] if b["key"].startswith("rule:long_run")]
    assert runs and all(b["tier"] == sp.DECIDE and b["cls"] == "policy" for b in runs)
    # The posting notice where a jurisdiction makes it law: must-fix, said as
    # the law, sent past only by the owner acknowledging it.
    sr.save_compliance(rid, {"notice_days": 14})
    real = sr.law_backed
    monkeypatch.setattr(sr, "law_backed", lambda c, s: True if s == "notice_days" else real(c, s))
    rev = sp.publish_review(rid, hid, today=dt.date(2026, 10, 9))
    [n] = [b for b in rev["blockers"] if b["key"] == "notice_short"]
    assert n["tier"] == sp.MUST_FIX and n["law"] and n["fixable"] is False and "law requires" in n["text"]
    assert n["legal"]["text"].startswith("I understand")
    why = sp.override_problem(n, {n["key"]: {"reason": "We opened late", "legal_ack": True}},
                              actor={"username": "m", "role": "manager"})
    assert why == "Only the account owner can send a week past the law."
    assert sp.override_problem(n, {n["key"]: {"reason": "We opened late"}}, actor={"username": "o"})
    assert sp.override_problem(n, {n["key"]: {"reason": "We opened late this week", "legal_ack": True}},
                               actor={"username": "o"}) is None


# ── TRUST-9, BUGSLIFE-12: learning ─────────────────────────────────────────

def test_demotion_holds_a_bigger_overage_than_any_sent_past(db):
    rid = _rid(db)
    so.init_schedule_overrides()
    small = {"key": "hours_over", "text": "$40 over", "tier": sp.DECIDE, "over": {"dollars": 40, "hours": 3}}
    for hid in range(101, 107):
        so.record_overridden(rid, hid, 1, [small], ["hours_over"], reasons={"hours_over": "Game night here"})
    st = so.stats(rid)
    assert st["hours_over"]["demoted"] and st["hours_over"]["accepted"] == {"$": 40.0}
    out = so.annotate(rid, {"blockers": [dict(small, over={"dollars": 42})]})
    assert out["blockers"] == []                                  # within the band: a note
    big = so.annotate(rid, {"blockers": [dict(small, text="$4,000 over", over={"dollars": 4000})]})
    assert [b["key"] for b in big["blockers"]] == ["hours_over"] and big["blockers"][0]["beyond_accepted"]


def test_a_saved_line_quiets_only_its_own_kind(db):
    rid = _rid(db)
    so.init_schedule_overrides()
    trivial = {"key": "saved:Built 3 days in code", "text": "x", "tier": sp.DECIDE}
    for hid in range(201, 207):
        so.record_overridden(rid, hid, 1, [trivial], [trivial["key"]], reasons={})
    other = {"key": "saved:Tuesday has no closer", "text": "Tuesday has no closer", "tier": sp.DECIDE}
    out = so.annotate(rid, {"blockers": [dict(other), dict(trivial, key="saved:Built 5 days in code")]})
    assert [b["key"] for b in out["blockers"]] == [other["key"]]


def test_the_review_records_what_it_shows(db):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Gabe", "Manager"), ("Ana", "Server")])
    hid = _no_mgr_week(db, rid)
    prepublish.review(rid, hid)
    conn = models.get_conn()
    n = conn.execute("SELECT COUNT(*) FROM schedule_publish_overrides WHERE history_id=? AND overridden_at IS NULL",
                     (hid,)).fetchone()[0]
    conn.close()
    assert n >= 1


# ── TRUST-10/11/15/16/18 ───────────────────────────────────────────────────

def test_always_overridden_offers_a_rule_change(db):
    rid = _rid(db)
    c = _c(rid)
    got = prepublish._always_overridden(rid, {"always_overridden": [
        {"kind": "rule:long_run", "label": "Rule: long run", "overridden": 9, "shown": 9, "hard": True,
         "last_overridden": "10/5/26"}]}, c)
    assert got[0]["text"].startswith("You've sent past “too many days in a row” in 9 of 9 weeks")
    post = [a for a in got[0]["actions"] if a["action"] == "post"][0]
    assert post["payload"] == {"path": "labor/rules", "body": {"rules": {"max_consecutive_days": 7}}}


def test_fyi_counts_people_for_a_weekly_limit_and_days_in_week_order():
    viols = [{"kind": "over_hours_ceiling", "employee": "Erik", "bucket": "w1", "date": MON[4], "day": "Friday",
              "hard": False, "detail": "52h"},
             {"kind": "over_hours_ceiling", "employee": "Erik", "bucket": "w1", "date": MON[0], "day": "Monday",
              "hard": False, "detail": "52h"},
             {"kind": "over_hours_ceiling", "employee": "Ivy", "bucket": "w1", "date": MON[5], "day": "Saturday",
              "hard": False, "detail": "51h"}]
    [f] = prepublish._fyi({"notes": []}, viols)
    assert f["text"].startswith("2 people past") and "Fri, Mon" not in f["text"] and f["count"] == 2
    viols = [{"kind": "daily_ot", "employee": n, "date": d, "day": wd, "hard": False, "detail": "x"}
             for n, d, wd in (("A", MON[6], "Sunday"), ("B", MON[2], "Wednesday"), ("C", MON[4], "Friday"))]
    [f] = prepublish._fyi({"notes": []}, viols)
    assert "(Wed, Fri, Sun)" in f["text"]


def test_must_fix_items_are_grouped_by_rule_with_one_fix():
    items = [{"key": f"rule:long_run:{n}:x:y", "kind": "rule:long_run", "text": f"{n} 7 days", "override_allowed": True}
             for n in ("ana", "ben", "cara")]
    [g] = prepublish.group_items(items, "decide")
    assert g["count"] == 3 and g["text"] == "3 people — too many days in a row"
    assert [f["action"] for f in g["fixes"]] == ["apply_fixes", "acknowledge_all"]
    assert g["fixes"][1]["payload"]["keys"] == [i["key"] for i in items]


def test_counts_split_what_holds_from_suggestions(db):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Gabe", "Manager"), ("Ana", "Server")])
    out = prepublish.review(rid, _no_mgr_week(db, rid))
    cnt = out["counts"]
    assert cnt["to_fix_or_decide"] == cnt["must_fix"] + cnt["decide_holding"]
    assert cnt["suggestions"] == len(out["suggestions"]) == cnt["decide"] - cnt["decide_holding"]


def test_assumed_stays_fold_into_one_line(db, monkeypatch):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager")])
    rows = [{"date": d, "employee": "Erik", "role": "Manager", "shift_start": "10:00am", "shift_end": "11:00pm"}
            for d in MON]
    monkeypatch.setattr(sr, "assumed_stays", lambda rows, c, tz=None: [
        {"date": d, "employee": "Erik", "shift_start": "10:00am", "until": "11:30pm", "minutes": 30, "hours": 0.5,
         "hourly": True, "text": "t", "action": {"payload": {"new_end": "11:30pm", "shift_end": "11:00pm"}}}
        for d in MON])
    got = [f for f in prepublish._fragile(rid, rows, _c(rid), MON, {}) if f["kind"] == "assumed_stay"]
    assert len(got) == 1 and "7 nights" in got[0]["text"]
    assert {c["set"]["shift_end"] for c in got[0]["action"]["payload"]["change"]} == {"11:30pm"}


# ── TRUST-13/14 ─────────────────────────────────────────────────────────────

def test_left_off_is_never_all_filled_while_the_role_is_short(db):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Ana", "Server"), ("Uma", "Server")])
    rows = [{"date": MON[1], "day": "Tuesday", "employee": "Ana", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm"}]
    viols = [{"kind": "coverage_floor", "date": MON[1], "floor_role": "Server", "daypart": "night"}]
    got = [p for p in prepublish.left_off_reasons(rid, rows, _c(rid), {}, MON, viols=viols) if p["name"] == "Uma"]
    assert got and got[0]["cause"] == "short_unused" and "all filled" not in got[0]["reason"]
    assert "short of Server" in got[0]["reason"]


def test_a_replacement_fix_says_its_overtime_and_prefers_none(db, monkeypatch):
    import schedule_engine as se
    rid = _rid(db)
    _team(db, rid, [("Ana", "Server"), ("Dan", "Server"), ("Eve", "Server")])
    rows = [{"date": MON[0], "employee": "Ana", "role": "Server", "shift_start": "11:00am", "shift_end": "3:00pm",
             "scheduled_hours": 4}]
    rows += [{"date": d, "employee": "Dan", "role": "Server", "shift_start": "9:00am", "shift_end": "4:00pm",
              "scheduled_hours": 7} for d in MON[1:]]                 # 42h already
    monkeypatch.setattr(se, "replacement_is_legal", lambda *a, **k: (True, ""))
    got = prepublish._replacement_fixes(rid, rows, _c(rid), 0, None, {"Dan": 5, "Eve": 1})
    assert got[0]["employee"] == "Eve"                                # no overtime first, despite the rating
    dan = [f for f in got if f["employee"] == "Dan"]
    assert dan and "overtime" in dan[0]["label"] and dan[0]["overtime_hours"] > 0


# ── TRUST-17: answers kept ──────────────────────────────────────────────────

def test_a_dismissal_is_kept_and_a_standing_one_follows_its_evidence(db):
    rid = _rid(db)
    so.init_schedule_overrides()
    assert so.dismiss(rid, "left_off:uma", scope="week", until=MON[-1])
    assert so.dismiss(rid, "floor:server:Monday:night", scope="every", evidence="floor=4|suggest=3")
    import datetime as dt
    held = so.dismissals(rid, today=dt.date.fromisoformat(MON[2]))
    assert so.dismissed_now(held, "left_off:uma")
    assert so.dismissed_now(held, "floor:server:Monday:night", "floor=4|suggest=3")
    assert not so.dismissed_now(held, "floor:server:Monday:night", "floor=4|suggest=2")
    after = so.dismissals(rid, today=dt.date(2026, 10, 19))
    assert not so.dismissed_now(after, "left_off:uma")                # the week ended


# ── TRUST-26/27/34: speed, the budget's own trim, one gate run ─────────────

def test_core_first_then_explain_and_one_gate_run(db, monkeypatch):
    rid = _rid(db)
    _team(db, rid, [("Erik", "Manager"), ("Gabe", "Manager"), ("Ana", "Server")])
    hid = _no_mgr_week(db, rid)
    calls = []
    real = sp.publish_review

    def counted(*a, **k):
        calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(sp, "publish_review", counted)
    core = prepublish.review(rid, hid, sections="core")
    assert core["explain_pending"] and core["must_fix"] and core["why"]["assignments"] == []
    assert len(calls) == 1
    full = prepublish.review(rid, hid)
    again = prepublish.review(rid, hid)
    assert len(calls) == 2, "the saved week's second read is served from the cache"
    assert again["verdict"] == full["verdict"]


def test_legality_searches_stop_at_the_review_deadline(db, monkeypatch):
    import schedule_engine as se
    rid = _rid(db)
    _team(db, rid, [("Ana", "Server"), ("Dan", "Server"), ("Eve", "Server")])
    rows = [{"date": MON[0], "employee": "Ana", "role": "Server", "shift_start": "11:00am", "shift_end": "3:00pm"}]
    seen = []
    monkeypatch.setattr(se, "replacement_is_legal", lambda *a, **k: (seen.append(1), (False, ""))[1])
    got = prepublish._replacement_fixes(rid, rows, _c(rid), 0, None, {}, memo={"deadline": 0})
    assert got == [] and seen == []


def test_hours_over_gets_the_budget_trim_priced_before_it_is_applied(db, monkeypatch):
    import schedule_economics as econ
    rid = _rid(db)
    keep = {"row": {"hours_budget": 10, "economics_json": json.dumps({"labor_budget_dollars": 100})},
            "rows": [{"date": MON[0], "employee": "Ana", "role": "Server", "shift_start": "11:00am",
                      "shift_end": "11:00pm", "scheduled_hours": 12}],
            "constraints": _c(rid)}
    monkeypatch.setattr(econ, "trim_to_budget", lambda rows, *a, **k: (
        [dict(rows[0], shift_end="5:00pm", scheduled_hours=6)],
        [{"date": MON[0], "employee": "Ana", "shift_start": "11:00am", "shift_end": "11:00pm", "kind": "cut",
          "to": "5:00pm", "hours": 6}], 6))
    opt = prepublish.trim_preview(rid, keep)
    assert opt["action"] == "edit_rows" and opt["payload"]["change"][0]["set"] == {"shift_end": "5:00pm"}
    assert opt["label"].startswith("Trim $") and opt["dollars"] > 0
    assert "optimize" not in json.dumps(prepublish._DECIDE_OPTIONS["hours_over"])


# ── TRUST-6: how sure, over how weeks ran ───────────────────────────────────

def test_how_sure_is_the_k1_engine_over_weeks_as_run(monkeypatch):
    import schedule_scorecard as ssc
    rows = [{"week_start": f"2026-0{m}-01", "week_end": f"2026-0{m}-07", "unmanaged_minutes": 0,
             "uncovered_no_shows": 0, "shortfall_hours": 1.0, "labor_vs_target": 1.0, "ot_hours": 2,
             "ot_hours_planned": 2} for m in range(1, 9)]
    rows[0]["unmanaged_minutes"] = 45
    monkeypatch.setattr(ssc, "_rows", lambda where, args, db_path=None, limit=5000: rows[:limit])
    import datetime as dt
    got = prepublish.week_confidence(1, today=dt.date(2026, 8, 10))
    assert got["weeks_measured"] == 8 and got["weeks_ran_ok"] == 7
    assert got["k1"]["pct"] == got["pct"] and got["pct"] is not None and got["pct"] < 100
    assert "7 of your last 8 published weeks ran as planned" in got["line"]
    monkeypatch.setattr(ssc, "_rows", lambda where, args, db_path=None, limit=5000: rows[:1])
    assert prepublish.week_confidence(1)["pct"] is None              # "—" below the sample floor


# ── the web (TRUST-14/16/19/26/28/33) ──────────────────────────────────────

def _pr():
    return re.search(r'<script id="cav-pr">(.*?)</script>', SRC, re.S).group(1)


def test_the_web_falls_back_to_the_gate_and_asks_core_first():
    js = _pr()
    assert "/api/labor/publish-check?schedule_id=" in js and "fromCheck" in js       # TRUST-19
    assert "sections: 'core'" in js and "sections: 'explain'" in js                  # TRUST-26
    assert "S.stale = true" in js and "Re-checking" in js                            # TRUST-14
    assert "Decide before sending" in js and "Suggestions" in js                     # TRUST-16
    assert "/api/labor/publish-review/dismiss" in js                                 # TRUST-17
    assert "Someone else…" not in js and "__other" not in js                    # TRUST-5


def test_summary_tiles_read_the_review_numbers_and_trust_text_is_body_size():
    i = SRC.index("function ssRenderSummary()")
    body = SRC[i:SRC.index("function ssOptimize", i)]
    assert "pnum.labor_pct" in body and "pnum.overtime" in body                      # TRUST-28
    assert "to_fix_or_decide" in body
    assert re.search(r"\.pr-meta\{font-size:14px", SRC) and re.search(r"\.pr-sub\{font-size:14px", SRC)
    assert re.search(r"\.pr-it\{[^}]*font-size:15\.5px", SRC)                         # TRUST-33
