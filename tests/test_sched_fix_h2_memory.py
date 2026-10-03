"""Schedule fix round 10/3/26, workstream H2 — the scheduling memory, what
happened, and what binds the draft.

L-29  one memory with confidence, decay, a status and an enforcement level;
      the observation log it is built from; the standing patterns carried in
      whole; the guards (an admin's word, Cavnar AI's own changes, a draft
      choice merely kept never start a habit)
L-12  outcomes record what the punches say beside the plan
L-13  a daypart's labor % is its own (punches × rates over its measured
      sales), never the day's copied
L-16  overtime actually worked and closes that run late are learned; the
      draft's closes are padded where legal; a solver-readable signal
L-21  teams whose shared shifts ran well — a candidate, active only with
      confidence; keeping people apart is never applied on its own
L-22  openers and usual sections are memories
L-23 / D-27  measured server performance is a suggested rating the owner
      confirms — owner-only, never in a prompt
L-28  the learned prompt blocks share one budget, ranked by relevance
L-34  more advice is read as carried out, and each is measured on the week
      as it ran
L-14  a strong calibration waits in the action queue; the dead constants and
      the stale comments are gone
"""
import inspect
import json
import sys
from datetime import date, timedelta

import pytest

import client_api  # noqa: F401  (imported before the fixture redirects every bound get_conn)
import action_queue  # noqa: F401  (the same: these bind models.get_conn at import — CLAUDE.md)
import attendance  # noqa: F401
import people  # noqa: F401
import rec_ledger  # noqa: F401
import service_performance  # noqa: F401
import strategy_jobs  # noqa: F401
import strategy_routes  # noqa: F401
import models
import schedule_intel
import schedule_learning as sl
import schedule_memory as sm
import schedule_versions as sv
import staff_settings
from models import Restaurant, create_restaurant

HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
OWNER = {"id": 1, "username": "erik", "role": "owner", "restaurant_id": None}
VIEW_AS = {"id": 1, "username": "erik", "role": "owner", "acting_admin_id": 9, "acting_admin": "will"}


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
    monkeypatch.setattr(staff_settings, "get_conn", conn)
    monkeypatch.setattr(models, "get_leader_flags", lambda r, db_path=None: {})
    import auth
    monkeypatch.setattr(auth, "DB_PATH", db_path, raising=False)
    auth.init_auth(db_path=db_path)
    yield


def _rid(**kw):
    kw.setdefault("name", "Memory Co")
    kw.setdefault("owner_email", "m@x.test")
    kw.setdefault("module_labor", 1)
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(**kw))


def _monday(weeks_ago=0):
    t = date.today()
    return t - timedelta(days=t.weekday()) - timedelta(weeks=weeks_ago)


def _row(d, emp, start="5:00pm", end="10:00pm", role="Server", hours=5, notes=""):
    d = d if isinstance(d, str) else d.isoformat()
    return {"date": d, "day": date.fromisoformat(d).strftime("%A"), "employee": emp, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": str(hours), "notes": notes}


def _csv(rows):
    return HEAD + "".join(",".join(str(r[c]).replace(",", ";") for c in sv.COLS) + "\n" for r in rows)


def _sql(q, *args):
    conn = models.get_conn()
    try:
        cur = conn.execute(q, args)
        conn.commit()
        return cur
    finally:
        conn.close()


def _q(q, *args):
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute(q, args).fetchall()]
    finally:
        conn.close()


def _hist(rid, mon, rows, published=True, quality=None, labor_target=30):
    return _sql("INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, "
                "labor_target, schedule_csv, summary_json, published_at, quality_json) VALUES (?,?,?,0,100,?,?,'[]',?,?)",
                rid, mon.isoformat(), (mon + timedelta(days=6)).isoformat(), labor_target, _csv(rows),
                (mon - timedelta(days=3)).isoformat() + " 12:00:00" if published else None,
                json.dumps(quality) if quality is not None else None).lastrowid


def _v(rid, hid, reason, rows, by="erik", auth="principal", quality=None):
    sv.append(rid, hid, reason, _csv(rows), saved_by=by, saved_authority=auth, quality=quality)


def _week(rid, mon, draft, final, by="erik", auth="principal", published=True):
    hid = _hist(rid, mon, final, published=published)
    _v(rid, hid, "generated", draft, by="Cavnar AI", auth="system")
    if final != draft:
        _v(rid, hid, "edited", final, by=by, auth=auth)
    if published:
        _v(rid, hid, "published", final, by=by, auth=auth)
    return hid


def _punch(rid, d, emp, start, end, hours, role="Server", rate=15.0, source="rpower"):
    d = d if isinstance(d, str) else d.isoformat()
    _sql("INSERT INTO shift_facts (restaurant_id, business_date, employee_name, employee_key, role, shift_start, "
         "shift_end, scheduled_hours, actual_hours, pay_rate, source) VALUES (?,?,?,?,?,?,?,NULL,?,?,?)",
         rid, d, emp, staff_settings.name_key(emp), role, start, end, hours, rate, source)


def _mem(rid, key=None, kind=None):
    rows = _q("SELECT * FROM schedule_memory WHERE restaurant_id=?", rid)
    if key:
        rows = [r for r in rows if r["memory_key"] == key]
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    for r in rows:
        r["value"] = json.loads(r["value_json"] or "{}")
    return rows


# ══ L-29: the observation log ═════════════════════════════════════════════

def test_an_event_is_appended_and_a_measured_fact_is_replaced_in_place():
    rid = _rid()
    sm.observe(rid, "edit_move", person="Bob", value={"change": "removed"})
    sm.observe(rid, "edit_move", person="Bob", value={"change": "removed"})
    sm.observe(rid, "stayed_late", person="Ana", value={"minutes": 20}, fact_key="stayed_late|x")
    sm.observe(rid, "stayed_late", person="Ana", value={"minutes": 35}, fact_key="stayed_late|x")
    rows = sm.observations(rid)
    assert len([r for r in rows if r["kind"] == "edit_move"]) == 2
    late = [r for r in rows if r["kind"] == "stayed_late"]
    assert len(late) == 1 and late[0]["value"]["minutes"] == 35
    # Unknown vocabulary is stored as the contract's defaults, never invented.
    sm.observe(rid, "edit_move", origin="robot", phase="later", authority="boss")
    r = sm.observations(rid, kinds="edit_move")[0]
    assert (r["origin"], r["phase"], r["authority"]) == ("manager", "pre_publish", "principal")


def test_observe_never_raises_and_a_failed_write_reaches_the_digest(monkeypatch):
    import ops
    seen = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: seen.append(k))
    monkeypatch.setattr(sm, "get_conn", lambda db_path=None: (_ for _ in ()).throw(RuntimeError("disk full")))
    assert sm.observe(1, "edit_move", person="Bob") is None
    assert seen and seen[0]["job"] == "schedule_memory"


def test_counted_observations_leave_out_an_admins_word_and_cavnar_ais_changes():
    rid = _rid()
    sm.observe(rid, "redo_days", authority="admin", origin="manager")
    sm.observe(rid, "cavnar_change_saved", origin="cavnar")
    sm.observe(rid, "redo_days", authority="principal", origin="manager")
    counted = sm.observations(rid, counted_only=True)
    assert len(counted) == 1 and counted[0]["authority"] == "principal"


# ══ L-29: the standing patterns, carried into the memory in whole ══════════

def _crew(mon, tue_people, wed=("Bob",)):
    tue, wd = mon + timedelta(days=1), mon + timedelta(days=2)
    return [_row(tue, p) for p in tue_people] + [_row(wd, p, "11:00am", "3:00pm", hours=4) for p in wed]


EVERYONE, WITHOUT_BOB = ["Ana", "Bob", "Cy"], ["Ana", "Cy"]


def _bob_off_weeks(rid, weeks=(3, 2, 1)):
    for w in weeks:
        mon = _monday(w)
        _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, WITHOUT_BOB))


def test_every_standing_pattern_is_in_the_memory_with_all_its_evidence():
    rid = _rid()
    _bob_off_weeks(rid)
    sv.refresh_standing_patterns(rid)
    standing = next(s for s in sv.standing_patterns(rid) if s["kind"] == "moved_off" and s["employee"] == "Bob")
    sm.consolidate(rid)
    m = _mem(rid, key="pattern:" + standing["key"])[0]
    assert m["kind"] == "moved_off" and m["fact_class"] == "habit"
    assert (m["opportunities"], m["hits"]) == (standing["opportunities"], standing["hits"])
    assert m["last_confirmed_by_hand"] == standing["last_hand_iso"]
    assert m["misses_by_hand"] == standing["times_overridden"]
    assert m["confidence"] == round(float(standing["confidence"]), 3)
    assert m["value"]["slot"] == "off" and m["text"] == standing["text"]


def test_a_retired_ruled_or_dormant_pattern_keeps_its_status_in_the_memory():
    rid = _rid()
    _bob_off_weeks(rid)
    sv.refresh_standing_patterns(rid)
    key = next(s["key"] for s in sv.standing_patterns(rid) if s["kind"] == "moved_off")
    for status, want in (("retired", "retired"), ("ruled", "rule"), ("dormant", "dormant")):
        _sql("UPDATE schedule_standing_patterns SET status=? WHERE restaurant_id=? AND pattern_key=?", status, rid, key)
        sm.consolidate(rid, only=("patterns",))
        assert _mem(rid, key="pattern:" + key)[0]["status"] == want, status


def test_a_pattern_is_enforced_only_once_its_confidence_clears_the_line():
    rid = _rid()
    _bob_off_weeks(rid)
    sv.refresh_standing_patterns(rid)
    key = next(s["key"] for s in sv.standing_patterns(rid) if s["kind"] == "moved_off")
    week = [(_monday(-1) + timedelta(days=i)).isoformat() for i in range(7)]
    sm.consolidate(rid)
    m = _mem(rid, key="pattern:" + key)[0]
    assert m["confidence"] < sm.ACTIVE_CONFIDENCE and m["status"] == "candidate" and m["enforcement"] == "prompt"
    assert not sm.enforced_signals(rid, week, roster_names=EVERYONE)
    # Kept by the manager's own hand week after week: sure enough to bind.
    _sql("UPDATE schedule_standing_patterns SET hits=9, opportunities=9, confidence=0.71, last_hand=? "
         "WHERE restaurant_id=? AND pattern_key=?", date.today().isoformat(), rid, key)
    sm.consolidate(rid, only=("patterns",))
    sig = sm.enforced_signals(rid, week, roster_names=EVERYONE)
    assert [s["kind"] for s in sig] == ["moved_off"]
    s = sig[0]
    assert set(s) == {"kind", "key", "person", "day", "daypart", "role", "value", "confidence", "enforcement",
                      "source"}
    assert (s["person"], s["day"], s["daypart"], s["enforcement"]) == ("Bob", "Tuesday", "night", "soft")
    # Bob off the roster, or a week without a Tuesday: nothing binds.
    assert not sm.enforced_signals(rid, week, roster_names=["Ana", "Cy"])
    assert not sm.enforced_signals(rid, [d for d in week if date.fromisoformat(d).weekday() != 1], EVERYONE)


def test_a_dismissed_pattern_is_retired_in_the_memory():
    rid = _rid()
    _bob_off_weeks(rid)
    sv.refresh_standing_patterns(rid)
    key = next(s["key"] for s in sv.standing_patterns(rid) if s["kind"] == "moved_off")
    schedule_intel.dismiss_pattern(rid, key, actor="erik", authority="principal")
    sm.consolidate(rid)
    m = _mem(rid, key="pattern:" + key)[0]
    assert (m["status"], m["retired_reason"]) == ("retired", "dismissed")


def test_what_is_bound_elsewhere_is_never_a_second_signal():
    rid = _rid()
    for key, kind, extra in (("pattern:headcount_add|x", "headcount_add", {}),
                             ("staff_avoid|ana|Sunday|night", "staff_avoid", {"person": "Ana"}),
                             ("reliability|ana", "reliability", {"person": "Ana"})):
        _sql("INSERT INTO schedule_memory (restaurant_id, memory_key, kind, fact_class, person, day, daypart, "
             "status, enforcement, confidence) VALUES (?,?,?,?,?,?,?,'active','soft',0.9)",
             rid, key, kind, "habit", extra.get("person"), "Sunday", "night")
    assert sm.enforced_signals(rid, [(_monday(-1) + timedelta(days=6)).isoformat()], ["Ana"]) == []
    assert "headcount_add" not in sm.SIGNAL_KINDS and "reliability" in sm.BOUND_ELSEWHERE


def test_a_demo_learns_nothing():
    rid = _rid(is_demo=1)
    _bob_off_weeks(rid)
    assert sm.consolidate(rid).get("skipped") == "not_eligible"
    assert sm.enforced_signals(rid, ["2026-10-06"]) == [] and sm.prompt_lines(rid, ["2026-10-06"]) == ""


# ══ L-22: openers, and the guard against learning the draft's own choice ═══

def _sat(weeks_ago):
    return _monday(weeks_ago) + timedelta(days=5)


def test_an_opener_from_the_restaurants_own_scheduling_is_learned_and_bound():
    rid = _rid()
    for w in range(1, 7):
        d = _sat(w)
        _punch(rid, d, "Ana", "08:55", "16:00", 7, role="Kitchen")
        _punch(rid, d, "Bo", "10:00", "18:00", 8, role="Kitchen")
    sm.consolidate(rid)
    m = _mem(rid, kind="opener")
    ana = [x for x in m if x["person"] == "Ana"][0]
    assert ana["day"] == "Saturday" and ana["role"] == "kitchen" and ana["hits"] == 6 and ana["opportunities"] == 6
    assert ana["value"]["start"] == "8:55am" and "Ana opens Kitchen on Saturdays" in ana["text"]
    assert ana["status"] == "active" and ana["enforcement"] == "soft"
    assert not [x for x in m if x["person"] == "Bo" and x["status"] in ("candidate", "active")]
    sig = [s for s in sm.enforced_signals(rid, [_sat(-1).isoformat()], ["Ana", "Bo"]) if s["kind"] == "opener"]
    assert sig and sig[0]["person"] == "Ana" and sig[0]["value"]["start"] == "8:55am"
    # The signal's meaning, for any pass: Bo opening instead is a miss.
    d = _sat(-1).isoformat()
    rows = [_row(d, "Bo", "9:00am", "5:00pm", role="Kitchen", hours=8), _row(d, "Ana", "11:00am", "5:00pm",
                                                                             role="Kitchen", hours=6)]
    assert [x["kind"] for x in sm.misses(rows, sig)] == ["opener"]
    rows[1]["shift_start"] = "9:00am"
    assert sm.misses(rows, sig) == []


def test_a_draft_choice_the_manager_merely_kept_never_starts_a_habit():
    rid = _rid()
    for w in (4, 3, 2, 1):
        sat = _sat(w)
        draft = [_row(sat, "Ana", "9:00am", "3:00pm", role="Kitchen", hours=6),
                 _row(sat, "Bo", "11:00am", "5:00pm", role="Kitchen", hours=6)]
        _week(rid, _monday(w), draft, draft)                 # sent as Cavnar AI drafted it
    sm.consolidate(rid)
    assert not [m for m in _mem(rid, kind="opener") if m["status"] in ("candidate", "active")], \
        "the draft's own opener choice became the manager's habit"
    # The manager choosing Bo to open, by hand, twice: that is a habit.
    rid2 = _rid(name="Hand Co", owner_email="h@x.test")
    for w in (2, 1):
        sat = _sat(w)
        draft = [_row(sat, "Ana", "9:00am", "3:00pm", role="Kitchen", hours=6),
                 _row(sat, "Bo", "11:00am", "5:00pm", role="Kitchen", hours=6)]
        final = [_row(sat, "Ana", "11:00am", "5:00pm", role="Kitchen", hours=6),
                 _row(sat, "Bo", "9:00am", "3:00pm", role="Kitchen", hours=6)]
        _week(rid2, _monday(w), draft, final)
    sm.consolidate(rid2)
    bo = [m for m in _mem(rid2, kind="opener") if m["person"] == "Bo"]
    assert bo and bo[0]["status"] == "candidate" and bo[0]["hits"] == 2


def test_an_opener_handed_to_someone_else_twice_by_hand_is_retired():
    rid = _rid()
    for w in range(3, 13):
        d = _sat(w)
        _punch(rid, d, "Ana", "08:55", "16:00", 7, role="Kitchen")
        _punch(rid, d, "Bo", "10:00", "18:00", 8, role="Kitchen")
    sm.consolidate(rid)
    assert [m for m in _mem(rid, kind="opener") if m["person"] == "Ana"][0]["status"] == "active"
    for w in (2, 1):
        sat = _sat(w)
        draft = [_row(sat, "Ana", "9:00am", "3:00pm", role="Kitchen", hours=6),
                 _row(sat, "Bo", "11:00am", "5:00pm", role="Kitchen", hours=6)]
        final = [_row(sat, "Ana", "11:00am", "5:00pm", role="Kitchen", hours=6),
                 _row(sat, "Bo", "9:00am", "3:00pm", role="Kitchen", hours=6)]
        _week(rid, _monday(w), draft, final)
    sm.consolidate(rid)
    ana = [m for m in _mem(rid, kind="opener") if m["person"] == "Ana"][0]
    assert (ana["status"], ana["retired_reason"]) == ("retired", "reversed")
    assert ana["misses_by_hand"] == 2


def test_a_usual_section_is_learned_from_the_restaurants_own_hand_never_an_admins():
    rid = _rid()
    models.update_restaurant(rid, {"foh_sections_json": json.dumps(["Patio", "Bar"])})
    fri = [_monday(w) + timedelta(days=4) for w in range(1, 6)]
    for d in fri[:3]:
        models.set_shift_section(rid, d.isoformat(), "Ana", "5:00pm", "Patio", updated_by="erik", user=OWNER)
    for d in fri[3:]:
        models.set_shift_section(rid, d.isoformat(), "Ana", "5:00pm", "Bar", updated_by="support", user=VIEW_AS)
    assert _q("SELECT authority FROM shift_sections WHERE section='Bar'")[0]["authority"] == "admin"
    sm.consolidate(rid)
    secs = _mem(rid, kind="section")
    assert [(m["value"]["section"], m["hits"], m["opportunities"]) for m in secs] == [("Patio", 3, 3)]
    assert "Ana usually takes Patio on Friday dinner/night" in secs[0]["text"]
    assert secs[0]["enforcement"] == "prompt"


# ══ L-12, L-13: what happened, beside the plan ════════════════════════════

def test_outcomes_record_what_the_punches_say_beside_the_plan(monkeypatch):
    rid = _rid()
    models.update_restaurant(rid, {"salaried_staff_json": json.dumps([{"name": "Erik", "annual": 150000}])})
    mon = _monday(2)
    fri = (mon + timedelta(days=4)).isoformat()
    plan = [_row(fri, "Ana", "5:00pm", "10:00pm"), _row(fri, "Bo", "5:00pm", "10:00pm"),
            _row(fri, "Cy", "5:00pm", "10:00pm"), _row(fri, "Erik", "4:00pm", "11:00pm", role="Manager", hours=7)]
    _hist(rid, mon, plan)
    _punch(rid, fri, "Ana", "16:58", "22:40", 5.7)            # stayed 40 minutes past her end
    _punch(rid, fri, "Bo", "17:05", "21:00", 3.9)
    import attendance
    attendance.record(rid, "Cy", fri, "no_show", "schedule_vs_punch_join", shift_start="5:00pm")
    monkeypatch.setattr(schedule_intel, "_morning_share", lambda conn, rid_: {})
    schedule_intel.record_outcomes(rid)
    row = _q("SELECT * FROM schedule_outcomes WHERE restaurant_id=? AND date=?", rid, fri)[0]
    assert (row["hours"], row["people"]) == (22.0, 4)                       # the plan, kept
    assert row["actual_basis"] == "punches"
    assert (row["actual_hours"], row["actual_people"]) == (16.6, 3)        # Ana + Bo + Erik (salaried, planned)
    assert (row["missed"], row["stayed_late"]) == (1, 1)
    # A date nobody punched is unknown — never 0.
    mon2 = _monday(4)
    sat = (mon2 + timedelta(days=5)).isoformat()
    _hist(rid, mon2, [_row(sat, "Ana")])
    schedule_intel.record_outcomes(rid)
    blank = _q("SELECT actual_hours, actual_people, actual_basis FROM schedule_outcomes WHERE date=?", sat)[0]
    assert blank == {"actual_hours": None, "actual_people": None, "actual_basis": None}
    obs = sm.observations(rid, kinds=("actual_hours", "coverage_gap_actual"))
    assert {o["kind"] for o in obs} == {"actual_hours", "coverage_gap_actual"}
    gap = next(o for o in obs if o["kind"] == "coverage_gap_actual")
    assert gap["role"] == "server" and gap["value"] == {"planned": 3, "worked": 2}


def test_the_outcome_block_says_what_was_worked_beside_what_was_scheduled(monkeypatch):
    rid = _rid()
    for w in (3, 2):
        mon = _monday(w)
        fri = (mon + timedelta(days=4)).isoformat()
        _hist(rid, mon, [_row(fri, "Ana"), _row(fri, "Bo")])
        _punch(rid, fri, "Ana", "17:00", "22:00", 5)
    monkeypatch.setattr(schedule_intel, "_morning_share", lambda conn, rid_: {})
    schedule_intel.record_outcomes(rid)
    o = schedule_intel.outcomes_by_daypart(rid)["Friday"]["night"]
    assert (o["avg_hours"], o["avg_actual_hours"], o["actual_weeks"]) == (10.0, 5.0, 2)
    block = schedule_intel.outcome_block({"Friday": {"night": o}}, ["Friday"])
    assert "about 10h scheduled, 5h worked" in block


def test_a_daypart_carries_its_own_labor_never_the_days_copied(monkeypatch):
    rid = _rid(hourly_rate=15)
    mon = _monday(2)
    fri = (mon + timedelta(days=4)).isoformat()
    _hist(rid, mon, [_row(fri, "Ana", "10:00am", "2:00pm", hours=4), _row(fri, "Bo", "5:00pm", "10:00pm"),
                     _row(fri, "Cy", "11:00am", "7:00pm", hours=8)])
    _punch(rid, fri, "Ana", "10:00", "14:00", 4, rate=15)
    _punch(rid, fri, "Bo", "17:00", "22:00", 5, rate=20)
    _punch(rid, fri, "Cy", "11:00", "19:00", 8, rate=10)       # a double: half before 3pm, half after
    _sql("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, sales) VALUES (?,?,?,?,?)",
         rid, fri, "Friday", 30.0, 1000.0)
    monkeypatch.setattr(schedule_intel, "_morning_share", lambda conn, rid_: {"Friday": 0.4})
    schedule_intel.record_outcomes(rid)
    rows = {r["daypart"]: r for r in _q("SELECT * FROM schedule_outcomes WHERE date=?", fri)}
    assert rows["morning"]["labor_pct"] == rows["night"]["labor_pct"] == 30.0     # the day's figure, labelled so
    # lunch: Ana 4h × $15 + Cy 4h × $10 = $100 over $400; dinner: Bo 5h × $20 + Cy 4h × $10 = $140 over $600
    assert rows["morning"]["labor_cost_daypart"] == 100.0 and rows["morning"]["labor_pct_daypart"] == 25.0
    assert rows["night"]["labor_cost_daypart"] == 140.0 and rows["night"]["labor_pct_daypart"] == 23.3
    # Calibration reads the daypart's own.
    samples, _w = sl._calibration_samples(rid, None)
    assert samples == [] or all(s[1]["labor_vs_target"] in (None, -5.0, -6.7) for s in samples)


def test_an_unmeasured_split_leaves_daypart_labor_unknown(monkeypatch):
    rid = _rid()
    mon = _monday(2)
    fri = (mon + timedelta(days=4)).isoformat()
    _hist(rid, mon, [_row(fri, "Bo")])
    _punch(rid, fri, "Bo", "17:00", "22:00", 5)
    _sql("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, sales) VALUES (?,?,?,?,?)",
         rid, fri, "Friday", 30.0, 1000.0)
    monkeypatch.setattr(schedule_intel, "_morning_share", lambda conn, rid_: {})
    schedule_intel.record_outcomes(rid)
    r = _q("SELECT labor_pct_daypart, split_basis FROM schedule_outcomes WHERE date=?", fri)[0]
    assert r == {"labor_pct_daypart": None, "split_basis": "unmeasured"}


def test_chemistry_reads_who_actually_worked(monkeypatch):
    rid = _rid()
    monkeypatch.setattr(schedule_intel, "watched_dates", lambda r, a, b, db_path=None: {
        (_monday(w) + timedelta(days=4)).isoformat() for w in range(1, 9)})
    for w in range(1, 8):
        mon = _monday(w)
        fri = (mon + timedelta(days=4)).isoformat()
        hid = _hist(rid, mon, [_row(fri, "Ana"), _row(fri, "Bo")])
        _sql("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, people, issues) "
             "VALUES (?,?,?,?,10,2,0)", rid, hid, fri, "night")
        _punch(rid, fri, "Ana", "17:00", "22:00", 5)              # Bo never came
    assert schedule_intel.chemistry_suggestions(rid) == [], "a pair the schedule planned but never worked together"


# ══ L-16: overtime actually worked, and closes that run late ══════════════

def test_overtime_actually_worked_is_observed_learned_and_read_by_the_passes():
    rid = _rid()
    for w in range(1, 7):
        mon = _monday(w)
        _hist(rid, mon, [_row(mon + timedelta(days=i), "Ana", "9:00am", "5:00pm", hours=7.6) for i in range(5)])
        for i in range(5):
            _punch(rid, mon + timedelta(days=i), "Ana", "08:55", "17:40", 8.6)       # 43h a week
    sm.consolidate(rid)
    ot = sm.observations(rid, kinds="ot_actual")
    assert len(ot) == 6 and all(round(o["value"]["over"], 1) == 3.0 for o in ot)
    m = _mem(rid, kind="ot_risk")[0]
    assert (m["person"], m["hits"], m["opportunities"]) == ("Ana", 6, 6)
    assert m["value"]["headroom_hours"] == 5.0 and m["status"] == "active"     # 38h planned, 43 worked
    week = [(_monday(-1) + timedelta(days=i)).isoformat() for i in range(7)]
    sig = [s for s in sm.enforced_signals(rid, week, ["Ana"]) if s["kind"] == "ot_risk"]
    assert sig and sig[0]["value"]["headroom_hours"] == 5.0
    draft = [_row(d, "Ana", "9:00am", "5:00pm", hours=7.6) for d in week[:5]]
    miss = sm.misses(draft, sig)
    assert miss and miss[0]["kind"] == "ot_risk" and 0 < miss[0]["weight"] <= sig[0]["confidence"]
    assert sm.misses(draft[:4], sig) == []                                      # 30.4h leaves the room


def _close_week(rid, w, out="22:40"):
    mon = _monday(w)
    fri = mon + timedelta(days=4)
    _hist(rid, mon, [_row(fri, "Ana", "4:00pm", "10:00pm", hours=6), _row(fri, "Bo", "5:00pm", "9:00pm", hours=4)])
    _punch(rid, fri, "Ana", "15:58", out, 6.7)
    _punch(rid, fri, "Bo", "17:00", "21:00", 4)


def test_closes_that_run_late_are_learned_and_the_draft_ends_them_when_they_really_end():
    rid = _rid()
    for w in range(1, 8):
        _close_week(rid, w)
    sm.consolidate(rid)
    m = _mem(rid, kind="end_overrun")[0]
    assert (m["role"], m["day"], m["daypart"]) == ("server", "Friday", "night")
    assert (m["hits"], m["opportunities"], m["value"]["minutes"]) == (7, 7, 45) and m["status"] == "active"
    assert m["value"]["padded_end"] == "10:45pm"
    assert len(sm.observations(rid, kinds="stayed_late")) == 7
    fri = (_monday(-1) + timedelta(days=4)).isoformat()
    sig = [s for s in sm.enforced_signals(rid, [fri], ["Ana", "Bo"]) if s["kind"] == "end_overrun"]
    draft = [_row(fri, "Ana", "4:00pm", "10:00pm", hours=6), _row(fri, "Bo", "5:00pm", "9:00pm", hours=4)]
    assert [x["kind"] for x in sm.misses(draft, sig)] == ["end_overrun"]
    out = sm.pad_overruns(draft, sig)
    assert out["rows"][0]["shift_end"] == "10:45pm" and float(out["rows"][0]["scheduled_hours"]) == 6.75
    assert out["rows"][1]["shift_end"] == "9:00pm" and not out["rows"][0]["notes"]       # the close only, no note
    assert sm.misses(out["rows"], sig) == []
    # A fixed row, or a day not being drafted, is never padded.
    pinned = [dict(draft[0], _pinned="manager_plan"), draft[1]]
    assert sm.pad_overruns(pinned, sig)["rows"][0]["shift_end"] == "10:00pm"
    assert sm.pad_overruns(draft, sig, editable=set())["padded"] == []


def test_a_pad_is_never_taken_past_the_law_or_over_the_owners_end_time():
    class _C:
        role_families, role_times = {}, {}

        def __init__(self, ok):
            self.ok = ok

        def can_add(self, row, rows, overtime=True):
            return (True, "") if self.ok else (False, "would take Ana past 40h")
    fri = (_monday(-1) + timedelta(days=4)).isoformat()
    sig = [{"kind": "end_overrun", "role": "server", "day": "Friday", "daypart": "night", "confidence": 0.8,
            "value": {"minutes": 30, "padded_end": "10:30pm", "typical_over": 35, "over": 6, "closes": 7}}]
    draft = [_row(fri, "Ana", "4:00pm", "10:00pm", hours=6)]
    refused = sm.pad_overruns(draft, sig, c=_C(False))
    assert refused["padded"] == [] and refused["left"][0]["reason"] == "would take Ana past 40h"
    c = _C(True)
    c.role_times = {("server", "Friday", "night"): {"end": 22 * 60}}
    assert sm.pad_overruns(draft, sig, c=c)["padded"] == []


def test_the_manager_setting_the_closing_time_by_hand_wins_over_the_punches():
    rid = _rid()
    for w in range(1, 8):
        _close_week(rid, w)
    sm.consolidate(rid)
    _sql("INSERT INTO schedule_memory (restaurant_id, memory_key, kind, fact_class, role, day, daypart, status, "
         "enforcement, confidence) VALUES (?,?,?,?,?,?,?,'candidate','prompt',0.5)",
         rid, "pattern:retime_end|x", "retime_end", "habit", "Server", "Friday", "night")
    fri = (_monday(-1) + timedelta(days=4)).isoformat()
    assert not [s for s in sm.enforced_signals(rid, [fri], ["Ana", "Bo"]) if s["kind"] == "end_overrun"]


def test_the_nightly_join_observes_a_shift_that_ran_late(monkeypatch):
    import attendance
    rid = _rid()
    mon = _monday(1)
    fri = (mon + timedelta(days=4)).isoformat()
    _hist(rid, mon, [_row(fri, "Ana", "4:00pm", "10:00pm", hours=6)])
    _punch(rid, fri, "Ana", "15:58", "22:40", 6.7)
    monkeypatch.setattr(attendance, "pos_day_final", lambda *a, **k: True)
    monkeypatch.setattr(attendance, "_published_rows", lambda rid_, day, db_path=None: (
        [_row(fri, "Ana", "4:00pm", "10:00pm", hours=6)], None))
    attendance.join_published(rid, fri)
    late = sm.observations(rid, kinds="stayed_late")
    assert len(late) == 1 and late[0]["value"]["minutes"] == 40 and late[0]["phase"] == "as_run"
    # The consolidation reads the same fact under the same key: still one.
    sm.consolidate(rid)
    assert len(sm.observations(rid, kinds="stayed_late")) == 1


def test_the_job_pads_the_draft_from_the_memory_before_the_solver_and_the_pricing():
    import schedule_engine
    src = inspect.getsource(schedule_engine._run_schedule_job)
    assert "_smem_pad.pad_overruns(" in src
    assert src.index("pad_overruns(") < src.index("_price_week(preview_rows)") < src.index("_opt.optimize(")
    assert src.index("apply_role_times(preview_rows") < src.index("pad_overruns(")


# ══ L-21: teams ═══════════════════════════════════════════════════════════

def _team_world(rid, together, apart, monkeypatch, good_together=True):
    """`together` Fridays with Ana and Bo on dinner (each ran well), `apart`
    Fridays with Cy and Di (each ran under the median)."""
    monkeypatch.setattr(schedule_intel, "watched_dates", lambda r, a, b, db_path=None: set())
    w = 1
    for i in range(together + apart):
        mon = _monday(w)
        fri = (mon + timedelta(days=4)).isoformat()
        team = ("Ana", "Bo") if i < together else ("Cy", "Di")
        hid = _hist(rid, mon, [_row(fri, p) for p in team])
        good = good_together if i < together else not good_together
        _sql("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, people, sales, issues, "
             "split_basis, actual_hours, actual_people, actual_basis) VALUES (?,?,?,?,10,2,?,0,'measured',10,2,"
             "'punches')", rid, hid, fri, "night", 900.0 if good else 400.0)
        for p in team:
            _punch(rid, fri, p, "17:00", "22:00", 5)
        w += 1


def test_a_team_whose_shared_shifts_ran_well_is_learned_and_bound_only_with_confidence(monkeypatch):
    rid = _rid()
    _team_world(rid, 8, 6, monkeypatch)
    sm.consolidate(rid)
    pairs = _mem(rid, kind="pair")
    ab = [p for p in pairs if p["value"].get("kind") == "prefer" and {p["person"], *p["value"]["with"]} == {"Ana", "Bo"}]
    assert ab and ab[0]["hits"] == 8 and ab[0]["opportunities"] == 8
    assert ab[0]["status"] == "active" and ab[0]["confidence"] >= sm.ACTIVE_CONFIDENCE
    assert "ran well" in ab[0]["text"] and ab[0]["value"]["baseline"] < 0.6
    fri = (_monday(-1) + timedelta(days=4)).isoformat()
    sig = [s for s in sm.enforced_signals(rid, [fri], ["Ana", "Bo", "Cy", "Di"]) if s["kind"] == "pair"]
    assert sig and sig[0]["value"]["with"]
    # Split across lunch and dinner the same day: a miss.
    rows = [_row(fri, "Ana", "10:00am", "3:00pm"), _row(fri, "Bo", "5:00pm", "10:00pm")]
    assert [m["kind"] for m in sm.misses(rows, sig)] == ["pair"]


def test_a_few_shared_shifts_are_a_candidate_and_keeping_people_apart_is_never_applied(monkeypatch):
    rid = _rid()
    _team_world(rid, 6, 8, monkeypatch, good_together=False)
    sm.consolidate(rid)
    pairs = _mem(rid, kind="pair")
    apart = [p for p in pairs if p["value"].get("kind") == "avoid"]
    assert apart and all(p["status"] == "candidate" and p["enforcement"] == "prompt" for p in apart)
    fri = (_monday(-1) + timedelta(days=4)).isoformat()
    assert not [s for s in sm.enforced_signals(rid, [fri], ["Ana", "Bo", "Cy", "Di"])
                if s["kind"] == "pair" and s["value"]["kind"] == "avoid"]


def test_a_pair_the_owner_already_set_is_their_rule_not_a_memory(monkeypatch):
    rid = _rid()
    staff_settings.set_pair(rid, "Ana", "Bo", "prefer")
    _team_world(rid, 8, 6, monkeypatch)
    sm.consolidate(rid)
    assert not [p for p in _mem(rid, kind="pair") if {p["person"], *p["value"]["with"]} == {"Ana", "Bo"}]


# ══ the owner's say over the memory ═══════════════════════════════════════

def test_the_owner_keeps_lets_go_or_makes_a_team_a_rule_and_view_as_changes_nothing(monkeypatch):
    rid = _rid()
    _team_world(rid, 8, 6, monkeypatch)
    sm.consolidate(rid)
    key = next(p["memory_key"] for p in _mem(rid, kind="pair") if p["value"].get("kind") == "prefer"
               and len(p["value"]["with"]) == 1)
    with pytest.raises(ValueError, match="view-as"):
        sm.owner_answer(rid, key, "let_go", user=VIEW_AS)
    sm.owner_answer(rid, key, "let_go", user=OWNER)
    m = _mem(rid, key=key)[0]
    assert (m["status"], m["retired_reason"], m["owner_said"]) == ("retired", "owner", "let_go")
    sm.consolidate(rid)
    assert _mem(rid, key=key)[0]["status"] == "retired", "the owner's no was undone by the next night's read"
    sm.owner_answer(rid, key, "rule", user=OWNER)
    assert _mem(rid, key=key)[0]["status"] == "rule"
    assert staff_settings.pair_sets(rid)["prefer"]
    view = sm.memory_view(rid)
    item = next(i for i in view["items"] if i["key"] == key)
    assert item["status_label"] == "A rule" and item["held_in_code"] and item["confidence_pct"].endswith("%")


def test_a_fact_that_cannot_be_a_rule_is_refused_in_words():
    rid = _rid()
    _sql("INSERT INTO schedule_memory (restaurant_id, memory_key, kind, fact_class, person, status, enforcement, "
         "value_json) VALUES (?,?,?,?,?,'active','soft','{}')", rid, "ot_risk|ana", "ot_risk", "overtime", "Ana")
    with pytest.raises(ValueError, match="hours limit"):
        sm.owner_answer(rid, "ot_risk|ana", "rule", user=OWNER)
    _sql("INSERT INTO schedule_memory (restaurant_id, memory_key, kind, fact_class, person, status, enforcement) "
         "VALUES (?,?,?,?,?,'active','soft')", rid, "reliability|ana", "reliability", "attendance", "Ana")
    with pytest.raises(ValueError, match="own record"):
        sm.owner_answer(rid, "reliability|ana", "keep", user=OWNER)


def test_the_memory_routes_exist_for_web_and_phone_and_answer_with_the_owners_word(monkeypatch):
    import strategy_routes as sr
    rules = {(r[0], tuple(r[1])) for r in sr._ROUTES}
    for path, method in (("/labor/schedule-memory", "GET"), ("/labor/schedule-memory", "POST"),
                         ("/labor/ratings/suggested", "GET"), ("/labor/ratings/suggested", "POST")):
        assert (path, (method,)) in rules
    rid = _rid()
    _sql("INSERT INTO schedule_memory (restaurant_id, memory_key, kind, fact_class, person, status, enforcement, "
         "confidence, opportunities, hits) VALUES (?,?,?,?,?,'candidate','prompt',0.4,1,1)",
         rid, "section|ana|Friday|night|patio", "section", "ownership", "Ana")
    manager = {"id": 2, "username": "mgr", "role": "manager", "restaurant_id": rid}
    monkeypatch.setattr(sr, "_rid", lambda u: rid)
    monkeypatch.setattr(sr, "_sees_labor", lambda u: True)
    from flask import Flask
    with Flask(__name__).test_request_context("/api/labor/schedule-memory"):
        body, code = sr._do_schedule_memory(manager)
    assert code == 200 and body["items"][0]["confidence_pct"] == "—"       # one opportunity: below the floor
    monkeypatch.setattr(sr, "_may_draft", lambda u: True)
    monkeypatch.setattr(sr, "_principal", lambda u: False)
    monkeypatch.setattr(sr, "_body", lambda: {"key": "section|ana|Friday|night|patio", "action": "rule"})
    body, code = sr._do_schedule_memory_answer(manager)
    assert code == 403


# ══ L-28: one budget for everything learned ═══════════════════════════════

def test_the_learned_blocks_share_one_budget_and_the_least_relevant_go_first():
    rid = _rid()
    lines = "\n".join(f"  Person{i}: missed 3 of 9 watched shifts" for i in range(40))
    block = "\n\nATTENDANCE (from shifts somebody watched):\n" + lines + "\n  Ana: missed 4 of 9 watched shifts"
    rot = "\n\nROTATION PLAN (over the last 8 published weeks):\n  Server: hand the closes first to Bo."
    out = sm.prompt_lines(rid, [(_monday(-1) + timedelta(days=i)).isoformat() for i in range(7)],
                          roster_names=["Ana", "Bo"], budget_chars=420,
                          sections=[("reliability", block), ("rotation", rot)])
    assert len(out) <= 420 + 120
    assert "Ana: missed 4 of 9" in out and "Bo." in out, "the roster's own lines were cut for strangers'"
    assert "more learned lines here not shown" in out
    unbudgeted = sm.prompt_lines(rid, [], budget_chars=100000, sections=[("reliability", block)])
    assert unbudgeted.count("watched shifts") == 41


def test_a_fact_the_passes_hold_is_one_short_line_and_a_candidate_says_its_evidence():
    rid = _rid()
    _sql("INSERT INTO schedule_memory (restaurant_id, memory_key, kind, fact_class, person, role, day, daypart, "
         "status, enforcement, confidence, value_json, text, opportunities, hits) VALUES "
         "(?,?,?,?,?,?,?,?,'active','soft',0.81,?,?,6,6)", rid, "opener|kitchen|Saturday|ana", "opener",
         "ownership", "Ana", "kitchen", "Saturday", "morning",
         json.dumps({"start": "8:55am", "role": "Kitchen"}), "Ana opens Kitchen on Saturdays — a long sentence.")
    _sql("INSERT INTO schedule_memory (restaurant_id, memory_key, kind, fact_class, person, day, daypart, "
         "status, enforcement, confidence, value_json, text, opportunities, hits) VALUES "
         "(?,?,?,?,?,?,?,'candidate','prompt',0.42,?,?,3,2)", rid, "section|bo|Saturday|night|patio", "section",
         "ownership", "Bo", "Saturday", "night", json.dumps({"section": "Patio"}),
         "Bo usually takes Patio on Saturday dinner/night — 2 of 3 shifts there with a section.")
    out = sm.prompt_lines(rid, [_sat(-1).isoformat()], roster_names=["Ana", "Bo"])
    assert "[held] Ana opens Kitchen on Saturdays, from about 8:55am (81% sure)." in out
    assert "a long sentence" not in out
    assert "Bo usually takes Patio on Saturday dinner/night — 2 of 3 shifts there with a section (42% sure)." in out


def test_the_generation_routes_its_learned_blocks_through_the_one_budget():
    import schedule_engine
    src = inspect.getsource(schedule_engine._build_schedule_result)
    assert "_smem.prompt_lines(" in src and "budget_chars=_smem.LEARNED_PROMPT_BUDGET_CHARS" in src
    assembled = src[src.index("extra_blocks = ("):src.index("_gen_kwargs = dict(")]
    for raw in ("_versions.prompt_block(", "_reliability_block(", "_intel.outcome_block(", "_intel.rotation_block(",
                "_could_hold_block(", "_intel.preferences_block("):
        assert raw not in assembled, f"{raw} still concatenated outside the budget"
    assert "_smem.consolidate(restaurant_id, patterns=(learned, pattern_conflicts), only=(\"patterns\",))" in src


# ══ L-34: more advice read as carried out, each measured as it ran ═════════

def test_an_edit_that_carries_out_more_kinds_of_advice_is_read_as_acceptance():
    d = _sat(-1).isoformat()
    before = [_row(d, "Ana", "5:00pm", "8:00pm"), _row(d, "Bo", "5:00pm", "10:00pm", role="Bartender")]
    cover = "Cover the gap in service on Saturday night: Server is down to 0 at 9:00pm, under 1."
    stronger = "Put a stronger server on Saturday night: servers average 2 against a bar of 3."
    spread = "Spread the busy shifts — Bo is carrying too many."
    after = [_row(d, "Ana", "5:00pm", "10:00pm"), _row(d, "Cy", "5:00pm", "10:00pm", role="Server PM")]
    got = sl.addressed_recommendations([cover, stronger, spread], before, after)
    assert set(got) == {cover, stronger, spread}
    assert sl.addressed_recommendations([cover, stronger, spread], before, before) == []


def test_a_carried_out_recommendation_is_measured_on_the_week_as_it_ran():
    import rec_ledger
    rid = _rid()
    mon = _monday(2)
    sat = (mon + timedelta(days=5)).isoformat()
    rec = "Pair Ana on Saturday night with a stronger hand, or move one there."
    quality = {"recommendations": [rec], "shifts": [{"date": sat, "day": "Saturday", "daypart": "night",
                                                     "scored": True, "dimensions": [{"key": "training_balance",
                                                                                     "score": 40}]}]}
    hid = _hist(rid, mon, [_row(sat, "Ana")])
    _v(rid, hid, "generated", [_row(sat, "Ana")], by="Cavnar AI", auth="system", quality=quality)
    _sql("UPDATE schedule_versions SET created_at=datetime('now', '-15 days') WHERE history_id=?", hid)
    schedule_intel.record_recommendation(rid, "strength", rec, "accepted", actor="erik", authority="principal")
    rec_ledger.present(rid, schedule_intel.schedule_rec_key("strength", rec), "schedule", "schedule_review",
                       kind="schedule_strength", title=rec)
    rec_ledger.record(rid, schedule_intel.schedule_rec_key("strength", rec), "accepted", authority="principal")
    sm.observe(rid, "sq_as_run", date=sat, daypart="night", history_id=hid, phase="as_run", origin="system",
               authority="system", value={"score": 88, "dims": {"training_balance": 92}, "hard_breaches": 0},
               fact_key=f"sq_as_run|{hid}|{sat}|night")
    assert schedule_intel.measure_recommendations_as_run(rid) == 1
    ev = _q("SELECT meta FROM rec_events WHERE restaurant_id=? AND event='outcome'", rid)
    meta = json.loads(ev[0]["meta"])
    assert (meta["verdict"], meta["measure"], meta["dimension"], meta["before"], meta["as_run"]) == \
        ("improved", "as_run", "training_balance", 40.0, 92.0)
    assert schedule_intel.measure_recommendations_as_run(rid) == 0                 # once


def test_the_week_is_scored_as_it_ran_beside_its_planned_score(monkeypatch):
    rid = _rid()
    mon = _monday(2)
    fri = (mon + timedelta(days=4)).isoformat()
    plan = [_row(fri, "Ana"), _row(fri, "Bo")]
    hid = _hist(rid, mon, plan, quality={"score": 80, "shifts": [{"date": fri, "daypart": "night", "scored": True,
                                                                  "score": 80, "dimensions": [{"key": "coverage",
                                                                                               "score": 100}]}]})
    _punch(rid, fri, "Ana", "17:00", "22:00", 5)
    scored = {}

    def fake_score(rid_, hid_, rows):
        scored["rows"] = rows
        return {"checked": True, "score": 60, "shifts": [{"date": fri, "daypart": "night", "scored": True, "score": 60,
                                                         "dimensions": [{"key": "coverage", "score": 50}]}]}
    monkeypatch.setattr(schedule_intel, "score_as_run", fake_score)
    assert schedule_intel.record_as_run_quality(rid) == 1
    assert [r["employee"] for r in scored["rows"]] == ["Ana"], "the plan was scored, not the punches"
    obs = {o["kind"]: o for o in sm.observations(rid, kinds=("sq_as_run", "sq_planned")) if o.get("date")}
    assert obs["sq_as_run"]["value"]["dims"]["coverage"] == 50 and obs["sq_planned"]["value"]["dims"]["coverage"] == 100
    assert obs["sq_as_run"]["history_id"] == hid


# ══ L-23, D-27: measured server performance, the owner's to confirm ═══════

def _tickets(rid, server, n, sales_per_guest, mealtime="Dinner", day=None):
    day = day or (date.today() - timedelta(days=3)).isoformat()
    for i in range(n):
        _sql("INSERT INTO pos_tickets (restaurant_id, provider, ticket_id, business_date, server_id, server_name, "
             "guest_count, net_sales, mealtime) VALUES (?,?,?,?,?,?,?,?,?)",
             rid, "rpower", f"{server}-{mealtime}-{i}", day, server, server, 2, 2 * sales_per_guest, mealtime)


def test_measured_performance_is_a_suggestion_only_the_owner_confirms(monkeypatch):
    rid = _rid()
    monkeypatch.setattr(staff_settings, "roster", lambda r, **k: [{"name": "Ana"}, {"name": "Bo"}, {"name": "Cy"}])
    _tickets(rid, "Ana", 30, 40)
    _tickets(rid, "Bo", 30, 25)
    _tickets(rid, "Cy", 5, 60)                                        # under the sample floor
    sug = sm.suggested_ratings(rid)
    by = {s["name"]: s for s in sug["servers"]}
    # The house sells $34.62 a guest at dinner (Cy's few tickets included):
    # Ana 16% above it, Bo 28% below.
    assert set(by) == {"Ana", "Bo"} and by["Ana"]["suggested"] == 4 and by["Bo"]["suggested"] == 1
    assert by["Ana"]["ratio"] == 1.156 and by["Ana"]["house_sales_per_cover"] == 34.62
    assert by["Ana"]["current"] is None and by["Ana"]["differs"]
    with pytest.raises(ValueError, match="account holder"):
        sm.confirm_suggested_rating(rid, "Ana", user={"id": 3, "username": "mgr", "role": "manager"})
    with pytest.raises(ValueError, match="account holder"):
        sm.confirm_suggested_rating(rid, "Ana", user=VIEW_AS)
    out = sm.confirm_suggested_rating(rid, "Ana", user=dict(OWNER, restaurant_id=rid))
    assert out["score"] == 4
    assert models.get_operational_scores(rid)["Ana"] == 4
    cap = models.get_capabilities(rid, attribute="overall")["Ana"]["overall"]
    assert cap["authority"] == "principal"
    # Never in the prompt, never in the team-visible memory.
    assert "40" not in sm.prompt_lines(rid, ["2026-10-09"], ["Ana", "Bo"])
    assert not [i for i in sm.memory_view(rid)["items"] if "sold" in str(i.get("text"))]


def test_measured_ratings_are_the_owners_alone_on_both_twins(monkeypatch):
    import strategy_routes as sr
    rid = _rid()
    monkeypatch.setattr(sr, "_rid", lambda u: rid)
    manager = {"id": 2, "username": "mgr", "role": "manager", "restaurant_id": rid}
    body, code = sr._do_ratings_suggested(manager)
    assert code == 403
    monkeypatch.setattr(sr, "_body", lambda: {"name": "Ana"})
    body, code = sr._do_ratings_suggested_confirm(manager)
    assert code == 403


def test_unconfirmed_measured_ratings_wait_in_the_owners_queue_only(monkeypatch):
    import action_queue
    rid = _rid()
    monkeypatch.setattr(staff_settings, "roster", lambda r, **k: [{"name": "Ana"}, {"name": "Bo"}])
    _tickets(rid, "Ana", 30, 40)
    _tickets(rid, "Bo", 30, 25)
    sm.consolidate(rid)
    assert sm.ratings_waiting(rid) == ["Ana", "Bo"]
    items = action_queue.items(rid, viewer=None, present=False)["items"]
    assert any(i["key"] == "ratings:measured" for i in items)
    manager = {"id": 2, "username": "mgr", "role": "manager", "restaurant_id": rid,
               "permissions": ["labor_view"]}
    monkeypatch.setattr(action_queue, "_sees", lambda v, m: True)
    items = action_queue.items(rid, viewer=manager, present=False)["items"]
    assert not any(i["key"] == "ratings:measured" for i in items)


# ══ L-14: the calibration loop ════════════════════════════════════════════

def test_a_strong_calibration_waits_in_the_action_queue_until_it_is_applied():
    import action_queue
    rid = _rid()
    assert not any(i["key"] == "calibration:weights" for i in action_queue.items(rid, present=False)["items"])
    sm.observe(rid, "calibration_suggested", value={"evidence": 0.31, "moving": ["coverage", "leadership"],
                                                    "profiles": [], "strong": True},
               origin="system", phase="as_run", authority="system", fact_key="calibration_suggested|w")
    items = action_queue.items(rid, present=False)["items"]
    cal = [i for i in items if i["key"] == "calibration:weights"]
    assert cal and "strong suggestion" in cal[0]["title"] and cal[0]["action"]["nav"] == "labor/intel"
    models.record_capability_change(rid, "quality_weights_applied", subject="weights", before={}, after={})
    _sql("UPDATE capability_changes SET changed_at=datetime('now', '+1 minute') WHERE restaurant_id=?", rid)
    assert not any(i["key"] == "calibration:weights" for i in action_queue.items(rid, present=False)["items"])


def test_a_weak_calibration_does_not_reach_the_queue():
    import action_queue
    rid = _rid()
    sm.observe(rid, "calibration_suggested", value={"evidence": 0.12, "moving": ["coverage"], "profiles": [],
                                                    "strong": False},
               origin="system", phase="as_run", authority="system", fact_key="calibration_suggested|w")
    assert not any(i["key"] == "calibration:weights" for i in action_queue.items(rid, present=False)["items"])


def test_the_weekly_job_records_the_profiles_and_how_strong_the_suggestion_is(monkeypatch):
    import ops
    import strategy_jobs
    rid = _rid()
    mon = _monday(1)
    _hist(rid, mon, [_row(mon, "Ana")])
    monkeypatch.setattr(ops, "claim_marker", lambda *a, **k: True)
    monkeypatch.setattr(sl, "calibrate_weights", lambda r, db_path=None: {
        "ready": True, "suggested_weights": {"coverage": 27.5}, "moving": ["coverage"],
        "dimensions": {"coverage": {"evidence": -0.27}},
        "profiles": {"peak": {"bar": {"current": 80}, "floors": {"coverage": {"current": 60}}}},
        "suggested_profiles": {"peak": {"min_quality": 75, "floors": {"coverage": 65}}}})
    strategy_jobs.run_quality_calibration()
    kinds = {c["kind"]: c for c in models.get_capability_changes(rid)}
    assert {"quality_weights_suggested", "quality_profiles_suggested"} <= set(kinds)
    # Recorded as the applied side is (strategy_routes._do_calibration_apply): a JSON text.
    assert json.loads(kinds["quality_profiles_suggested"]["before"]) == {
        "peak": {"min_quality": 80, "floors": {"coverage": 60}}}
    got = sm.latest(rid, "calibration_suggested")["value"]
    assert got["evidence"] == 0.27 and got["strong"] is True


def test_the_dead_calibration_constants_and_the_stale_comments_are_gone():
    import scheduler
    import strategy_jobs
    for name in ("CALIBRATION_MIN_WEEKS", "CALIBRATION_STEP", "CALIBRATION_MAX_WEIGHT"):
        assert not hasattr(strategy_jobs, name), name
    src = inspect.getsource(strategy_jobs)
    assert "Read by nothing" not in src
    assert "nudge its quality weights" not in inspect.getsource(scheduler.scheduler_loop)


# ══ the job ═══════════════════════════════════════════════════════════════

def test_the_nightly_memory_job_is_registered_bounded_resumable_and_counts():
    import jobs_registry
    import scheduler
    import strategy_jobs
    spec = jobs_registry.JOBS["schedule_memory"]
    assert spec["target"] == ("strategy_jobs", "run_schedule_memory") and spec["sends"] is False
    assert 'run_job("schedule_memory", run_schedule_memory)' in inspect.getsource(scheduler.scheduler_loop)
    src = inspect.getsource(strategy_jobs.run_schedule_memory)
    assert "resumable_sweep(MEMORY_CURSOR_KEY" in src
    _rid()
    out = strategy_jobs.run_schedule_memory()
    assert {"attempted", "ok", "failed", "skipped", "hit_bound"} <= set(out) and out["attempted"] == 1


def test_a_learner_that_fails_leaves_its_facts_as_they_were(monkeypatch):
    rid = _rid()
    for w in range(1, 7):
        d = _sat(w)
        _punch(rid, d, "Ana", "08:55", "16:00", 7, role="Kitchen")
    sm.consolidate(rid)
    assert _mem(rid, kind="opener")[0]["status"] == "active"
    monkeypatch.setattr(sm, "_learn_openers", lambda ctx: 1 / 0)
    monkeypatch.setattr(sm, "_LEARNERS", tuple((n, k, (sm._learn_openers if n == "openers" else f))
                                              for n, k, f in sm._LEARNERS))
    stats = sm.consolidate(rid)
    assert "openers" in stats["failed"] and _mem(rid, kind="opener")[0]["status"] == "active"


def test_both_memory_stores_follow_the_person():
    import people
    tables = {s["table"] for s in people.NAME_STORES}
    assert {"schedule_observations", "schedule_memory"} <= tables
