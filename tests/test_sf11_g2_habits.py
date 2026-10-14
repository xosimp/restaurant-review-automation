"""Schedule fix round 10/11/26, workstream G2 — habits the manager's hand
teaches, and only those (blind re-audit LEARNING-9, LEARNING-10,
LEARNING-11, LEARNING-12) and the nightly learners on the restaurant's own
lunch/dinner boundary (ARCH-11).

LEARNING-9   a week the draft merely carried a habit is never a hit: two
             one-off edits no longer bind by themselves five weeks later
LEARNING-10  a candidate needs three opportunities and a Wilson lower bound;
             the one-tap why is asked from the first occurrence
LEARNING-11  three hand corrections in a row bind (it took five); a
             candidate leans the draft for CANDIDATE_BIND_DAYS; a manager's
             explicit "Always" binds at once; the editor says how many more
LEARNING-12  a bound habit is its own tier in both solvers, a fixed weight
             in misses, and a soft habit_broken breach the review lists
ARCH-11      a non-3pm restaurant's learned keys are the generation's
"""
import random
from datetime import date, timedelta

import learning_stats
import models
import schedule_cpsat
import schedule_memory
import schedule_rules as sr
import schedule_solver as ss
import schedule_time
import schedule_versions as sv
from sched_d2_week import WEEK, DAYS, big_week, row
from tests.test_sf10_g1_habits import (  # noqa: F401  (_db: the autouse fixture)
    EVERYONE, WITHOUT_BOB, _db, _bob_off, _csv, _memory_status, _rid, _tuesday, _week)


def _standing_bob(rid):
    return next((s for s in sv.standing_patterns(rid) if s["employee"] == "Bob" and s["kind"] == "moved_off"), None)


# ── LEARNING-9: kept weeks are not evidence ─────────────────────────────────

def test_two_one_off_edits_then_an_indifferent_manager_never_bind():
    """sim_habit.py transient: two removals, then whatever the draft says is
    published. It used to read 3/3, 4/4, 5/5 from the carried weeks and bind
    at week 5 for ~14 weeks."""
    rid = _rid()
    _week(rid, _tuesday(8), EVERYONE, WITHOUT_BOB, age_days=56)
    _week(rid, _tuesday(7), EVERYONE, WITHOUT_BOB, age_days=49)
    for w in range(6, 0, -1):
        _week(rid, _tuesday(w), WITHOUT_BOB, WITHOUT_BOB, age_days=7 * w)   # carried, nobody touched it
    sv.refresh_standing_patterns(rid)
    row_ = _standing_bob(rid)
    assert row_ is None or (not row_["enforced_since"] and int(row_["hits"]) <= 2)
    assert _memory_status(rid) in (None, "candidate")


def test_a_carried_week_renews_but_never_adds_a_hit():
    rid = _rid()
    for w in (5, 4, 3):
        _week(rid, _tuesday(w), EVERYONE, WITHOUT_BOB, age_days=7 * w)
    sv.refresh_standing_patterns(rid)
    before = _standing_bob(rid)
    for w in (2, 1):
        _week(rid, _tuesday(w), WITHOUT_BOB, WITHOUT_BOB, age_days=7 * w)
    sv.refresh_standing_patterns(rid)
    after = _standing_bob(rid)
    assert (after["hits"], after["opportunities"]) == (before["hits"], before["opportunities"]) == (3, 3)
    assert after["times_applied"] >= before["times_applied"] + 2


def test_legacy_rows_lose_their_carried_weeks_once_at_boot():
    rid = _rid()
    for w in (3, 2, 1):
        _week(rid, _tuesday(w), EVERYONE, WITHOUT_BOB, age_days=7 * w)
    sv.refresh_standing_patterns(rid)
    conn = models.get_conn()
    try:
        # As the old arithmetic left it: 2 hand + 4 carried weeks, bound.
        conn.execute("UPDATE schedule_standing_patterns SET hits=6, opportunities=6, times_applied=4, "
                     "times_confirmed=0, enforced_since='2026-09-01 00:00:00', evidence_basis=NULL, source='learned' "
                     "WHERE restaurant_id=? AND kind='moved_off' AND employee='Bob'", (rid,))
        conn.commit()
        assert sv.migrate_hand_evidence(conn) == 1
        conn.commit()
        r = dict(conn.execute("SELECT * FROM schedule_standing_patterns WHERE restaurant_id=? AND kind='moved_off' "
                              "AND employee='Bob'", (rid,)).fetchone())
        assert sv.migrate_hand_evidence(conn) == 0              # idempotent
    finally:
        conn.close()
    assert (r["hits"], r["opportunities"], r["evidence_basis"]) == (2, 2, "hand")
    assert r["enforced_since"] is None                         # it bound on carried weeks alone


# ── LEARNING-10: noise is not a candidate ───────────────────────────────────

def test_candidacy_needs_opportunities_and_a_wilson_bound():
    assert not sv.candidate_ok(2, 2, 2.0, 2.0)                  # 2 of 2: could be two one-offs
    assert not sv.candidate_ok(2, 3, 2.0, 3.0)                  # 2 of 3
    assert sv.candidate_ok(3, 3, 3.0, 3.0)
    assert sv.candidate_ok(1, 1, 1.0, 1.0, min_repeats=1, min_rate=0)   # a count, not a candidacy


def test_noise_rarely_becomes_a_candidate():
    """sim_changepoint.py: a manager removing someone one week in ten for
    one-off reasons made a candidate within 24 weeks in 22% of person-slots."""
    rng = random.Random(1)
    n, ever = 2000, 0
    for _ in range(n):
        series = []
        for _w in range(24):
            series.append(rng.random() < 0.10)
            sc = learning_stats.since_change([(h, 1.0) for h in series])
            seg = series[sc["start"]:]
            if sv.candidate_ok(sum(seg), len(seg), sc["hits_w"], sc["opps_w"]):
                ever += 1
                break
    assert ever / n < 0.06


# ── LEARNING-11: faster binding for real corrections ────────────────────────

def test_three_hand_corrections_in_a_row_bind():
    assert not sv.habit_enforceable({"hits": 2, "opportunities": 2})
    assert sv.habit_enforceable({"hits": 3, "opportunities": 3})
    assert not sv.habit_enforceable({"hits": 3, "opportunities": 4})
    assert sv.hand_still_needed({"hits": 2, "opportunities": 2}) == 1
    rid = _rid()
    for w in (3, 2, 1):
        _week(rid, _tuesday(w), EVERYONE, WITHOUT_BOB, age_days=7 * w)
    sv.refresh_standing_patterns(rid)
    assert _standing_bob(rid)["enforced_since"]
    assert _memory_status(rid) == "active"


def test_a_learning_candidate_leans_the_draft_for_a_while_only():
    rid = _rid()
    for w in (4, 3, 2, 1):
        _week(rid, _tuesday(w), EVERYONE, WITHOUT_BOB if w != 3 else EVERYONE, age_days=7 * w)
    sv.refresh_standing_patterns(rid)
    schedule_memory.consolidate(rid)
    week = [(_tuesday(-1) + timedelta(days=i - 1)).isoformat() for i in range(7)]
    conn = models.get_conn()
    try:
        m = dict(conn.execute("SELECT * FROM schedule_memory WHERE restaurant_id=? AND kind='moved_off'",
                              (rid,)).fetchone())
    finally:
        conn.close()
    assert m["status"] == "candidate"
    sig = [s for s in schedule_memory.enforced_signals(rid, week, roster_names=EVERYONE) if s["kind"] == "moved_off"]
    assert sig and sig[0]["tier"] == "learning"
    assert sig[0]["confidence"] < sig[0]["learned_confidence"]
    view = schedule_memory.memory_view(rid)
    item = next(i for i in view["items"] if i["kind"] == "moved_off")
    assert item["needs_more"] and "more correction" in item["status_label"]
    # A month on with no hand edit: no longer leaning.
    conn = models.get_conn()
    try:
        conn.execute("UPDATE schedule_memory SET last_confirmed_by_hand=? WHERE restaurant_id=? AND kind='moved_off'",
                     ((date.today() - timedelta(days=schedule_memory.CANDIDATE_BIND_DAYS + 3)).isoformat(), rid))
        conn.commit()
    finally:
        conn.close()
    assert not [s for s in schedule_memory.enforced_signals(rid, week, roster_names=EVERYONE)
                if s["kind"] == "moved_off"]


# ── LEARNING-12: a bound habit is its own tier ──────────────────────────────

def _bound(conf=0.62, tier="bound"):
    return [{"kind": "moved_off", "key": "pattern:moved_off|p14|Monday|night", "person": "P14", "day": "Monday",
             "daypart": "night", "role": "server", "value": {"slot": "off"}, "confidence": conf,
             "enforcement": "soft", "source": "manager_edits", "tier": tier, "text": "P14 off Monday dinner"}]


def test_a_bound_miss_weighs_the_same_whatever_its_confidence():
    rows = [row(WEEK[0], "P14", "5:00pm", "10:00pm")]
    low = schedule_memory.misses(rows, _bound(0.61))
    high = schedule_memory.misses(rows, _bound(0.95))
    assert low[0]["weight"] == high[0]["weight"] == schedule_memory.HABIT_MISS_WEIGHT
    assert low[0]["tier"] == "bound"
    assert schedule_memory.misses(rows, _bound(0.61, tier="soft"))[0]["weight"] == 0.61


def test_both_solvers_price_a_bound_habit_above_any_shift_of_quality():
    rows, c, sig = big_week()
    mon = WEEK[0]
    prob = ss.Problem(rows, c, signals=dict(sig, learned=_bound()))
    u = next(u for u in prob.units if u.date == mon and u.primary == ["night"] and "server pm" in u.roles)
    comp = prob.components_of(u, prob.pidx["p14"])
    assert comp.get("habit") == ss.K_HABIT and "learned" not in comp
    assert ss.K_HABIT > 2 * 100                              # a whole peak shift's score
    soft = ss.Problem(rows, c, signals=dict(sig, learned=_bound(tier="soft")))
    assert soft.components_of(u, soft.pidx["p14"]).get("learned", 0) < ss.K_HABIT
    assert schedule_cpsat.W_HABIT < schedule_cpsat.W_REQ_HOUR
    if schedule_cpsat.available():
        out = schedule_cpsat.assign(rows, c, signals=dict(sig, learned=_bound()), only_dates=[mon], max_seconds=5,
                                    mode="improve", reasons=False, explain=False)
        assert not any(r.get("employee") == "P14" and r.get("date") == mon
                       and r.get("shift_start", "").endswith("pm") and int(r["shift_start"].split(":")[0]) >= 4
                       for r in out["rows"])


def test_a_broken_habit_is_a_soft_breach_the_review_lists():
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=["P14", "Ana"],
                       active={"p14", "ana"})
    c.compliance = dict(sr.DEFAULTS)
    c.habits = _bound()
    rows = [row(WEEK[0], "P14", "5:00pm", "10:00pm"), row(WEEK[0], "Ana", "5:00pm", "10:00pm")]
    v = [x for x in sr.violations(rows, c) if x["kind"] == "habit_broken"]
    assert len(v) == 1 and not v[0]["hard"] and "P14 off Monday" in v[0]["detail"]
    assert sr.BREACH_TIER["habit_broken"] == sr.TIER_QUALITY
    assert not [x for x in sr.violations([rows[1]], c) if x["kind"] == "habit_broken"]


def test_bound_habits_enter_the_typed_rule_path_with_source_habit():
    rules = schedule_memory.habit_rules(_bound() + _bound(tier="learning"))
    assert len(rules) == 1
    r = rules[0]
    assert (r["kind"], r["person"], r["days"], r["daypart"], r["source"], r["confirmed"]) == \
        ("person_off", "P14", ["Monday"], "night", "habit", False)


def test_a_managers_always_binds_at_once():
    import schedule_learning as sl
    import json as _json
    rid = _rid()
    tue = _tuesday(1)
    conn = models.get_conn()
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv) "
                       "VALUES (?,?,?,?)", (rid, (tue - timedelta(days=1)).isoformat(),
                                            (tue + timedelta(days=5)).isoformat(), _csv(tue, EVERYONE))).lastrowid
    subj = {"kind": "moved_off", "employee": "Bob", "day": "Tuesday", "daypart": "night",
            "options": ["always", "this_week"], "text": "Why?"}
    conn.execute("INSERT INTO schedule_edit_answers (restaurant_id, history_id, question_key, kind, subject_json, "
                 "keys_json, phase) VALUES (?,?,?,?,?,?,?)",
                 (rid, hid, "q1", "moved_off", _json.dumps(subj), "[]", "pre_publish"))
    conn.commit()
    conn.close()
    sl.answer_edit_question(rid, hid, "q1", "always",
                            user={"id": 7, "username": "gm", "role": "manager", "restaurant_id": rid})
    row_ = _standing_bob(rid)
    assert row_["enforced_since"] and row_["source"] == "learned"


# ── ARCH-11: the learners read the restaurant's own boundary ────────────────

def test_a_non_3pm_restaurants_learned_keys_match_the_generations():
    rid = _rid()
    models.update_restaurant(rid, {"daypart_boundary": "2:00pm"})
    tues = [_tuesday(w) for w in (3, 2, 1)]

    def csv(tue, people):
        return "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + "".join(
            f"{tue.isoformat()},Tuesday,{p},Server,2:30pm,4:30pm,2,\n" for p in people) + \
            f"{(tue + timedelta(days=1)).isoformat()},Wednesday,Bob,Server,11:00am,3:00pm,4,\n"
    for i, tue in enumerate(tues):
        conn = models.get_conn()
        ws = tue - timedelta(days=1)
        hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, "
                           "published_at, generated_at) VALUES (?,?,?,?,datetime('now'),datetime('now'))",
                           (rid, ws.isoformat(), (ws + timedelta(days=6)).isoformat(), csv(tue, ["Ana"]))).lastrowid
        conn.commit()
        conn.close()
        sv.append(rid, hid, "generated", csv(tue, ["Ana", "Bob"]), saved_by="cavnar", saved_authority="system")
        sv.append(rid, hid, "edited", csv(tue, ["Ana"]), saved_by="dana", saved_authority="principal")
        sv.append(rid, hid, "published", csv(tue, ["Ana"]), saved_by="dana", saved_authority="principal")
    # Outside any scope the default 3pm reads a 2:30pm start as lunch...
    assert schedule_time.daypart_split() == schedule_time.DAYPART_SPLIT
    assert sr.daypart_of("2:30pm") == "morning"
    # ...but the learner, called with no scope at all, reads the restaurant's.
    pats = [p for p in sv.learned_patterns(rid) if p["kind"] == "moved_off" and p["employee"] == "Bob"]
    assert pats and pats[0]["daypart"] == "night"
    # The generation's own scope asks for the same key.
    with schedule_time.restaurant_scope(rid):
        from shift_quality import present_dayparts
        assert present_dayparts({"shift_start": "2:30pm", "shift_end": "4:30pm"})[0] == "night"
    sv.refresh_standing_patterns(rid)
    schedule_memory.consolidate(rid)
    conn = models.get_conn()
    try:
        m = conn.execute("SELECT daypart FROM schedule_memory WHERE restaurant_id=? AND kind='moved_off'",
                         (rid,)).fetchone()
    finally:
        conn.close()
    assert m and m["daypart"] == "night"


def test_restaurant_scope_nests_and_restores():
    rid = _rid()
    models.update_restaurant(rid, {"daypart_boundary": "4:00pm"})
    with schedule_time.restaurant_scope(rid) as cut:
        assert cut == 16 * 60
        with schedule_time.restaurant_scope(rid):
            assert schedule_time.daypart_split() == 16 * 60
    assert schedule_time.daypart_split() == schedule_time.DAYPART_SPLIT
