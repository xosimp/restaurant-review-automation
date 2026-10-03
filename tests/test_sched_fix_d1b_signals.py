"""Schedule audit 10/3/26, workstream D1b — the signals the scorer, the
solver and the optimizer read (schedule_engine._quality_signals and its
helpers), the profiles the engine always builds, and calibration's floors
and bars. Each test names the finding or the contract line it pins."""
import json

import pytest

import models
import schedule_engine as se
import schedule_learning as sl
import schedule_rules as sr
import schedule_versions as sv
import shift_quality as sq
from models import Restaurant, create_restaurant, get_conn

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


def _row(date, emp, role="Server", start="5:00pm", end="11:00pm", hours=6):
    return {"date": date, "day": sq._day_name(date), "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(hours), "notes": ""}


def _csv(rows):
    return HEADER + "".join(",".join(str(r[c]) for c in sv.COLS) + "\n" for r in rows)


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, sl):
        monkeypatch.setattr(mod, "get_conn", (lambda *a, **k: real(db_path)) if mod is models else
                            (lambda db_path_=None: real(db_path)), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    monkeypatch.setattr(se, "_hourly_profile", lambda r: {})
    return create_restaurant(Restaurant(name="Signals Co", owner_email="s@x.test", module_labor=1), db_path=db_path)


def _constraints(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    c.compliance = dict(sr.DEFAULTS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _history(db_path, rid, week_start, rows, published=True, quality=None):
    import datetime as dt
    week_end = (dt.date.fromisoformat(week_start) + dt.timedelta(days=6)).isoformat()
    conn = get_conn(db_path)
    models._ensure_history_columns(conn)
    cur = conn.execute(
        "INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, labor_target, "
        "schedule_csv, summary_json, quality_json, published_at) VALUES (?,?,?,0,0,30,?,'[]',?,?)",
        (rid, week_start, week_end, _csv(rows), json.dumps(quality) if quality else None,
         "2026-09-01 10:00:00" if published else None))
    conn.commit()
    conn.close()
    return cur.lastrowid


# ── the D1b signals contract (SHARED §6) ──────────────────────────────────

def test_the_signals_carry_the_people_facts_the_passes_read(rid):
    c = _constraints(salaried={"erik baylis"}, managers={"ann": "Manager", "erik baylis": "Owner"},
                     acting_managers={"cy": {WEEK[2]}}, role_families={"server am": "server"},
                     held_roles={"bo": {"bartender"}}, closers_by_role={"server": {"bo"}},
                     stations={"roles": ["Kitchen"], "stations": ["Grill"], "needs": [], "skills": {}},
                     hours_limits={"bo": (None, 30)}, base_hours={"ann": {WEEK[0]: 6.0}})
    result = {"constraints": c, "roster": ["Erik Baylis", "Ann", "Bo", "Cy"],
              "roster_roles": {"Erik Baylis": "Owner", "Ann": "Manager", "Bo": "Server", "Cy": "Cook"},
              "learned_preferences": {"Bo": {"avoids": ["Sunday night"], "prefers": ["Monday day"], "drops": 2,
                                             "claims": 2}}}
    sig, _w = se._quality_signals(rid, result)
    assert sig["salaried"] == {"erik baylis"} and sig["salaried_cap"] == sr.SALARIED_HOURS_CAP
    assert sig["managers"] == {"ann": "Manager", "erik baylis": "Owner"}
    assert sig["acting_managers"] == {"cy": {WEEK[2]}}
    assert sig["experienced_default"] == {"ann", "erik baylis", "cy"}
    assert sig["role_families"] == {"server am": "server"} and sig["held_roles"] == {"bo": {"bartender"}}
    assert sig["closers_by_role"] == {"server": {"bo"}} and sig["stations"]["stations"] == ["Grill"]
    # each person's ceiling as the rules hold it: Bo's own 30h, the owner's salaried cap
    assert sig["hours_ceilings"]["bo"] == 30.0 and sig["hours_ceilings"]["erik baylis"] == sr.SALARIED_HOURS_CAP
    ot = sig["overtime"]
    assert ot["line"] == 40.0 and ot["bucket_of"][WEEK[3]] == WEEK[0] and ot["published"] == {"ann": {WEEK[0]: 6.0}}
    # L-19: learned slots on their own, and merged into the preferences
    learned = {"avoid": [["Sunday", "night"]], "prefer": [["Monday", "morning"]], "weight": sq.LEARNED_PREFERENCE_WEIGHT,
               "source": "what they drop and pick up", "drops": 2, "claims": 2}
    assert sig["learned_preferences"] == {"Bo": learned}
    assert sig["preferences"]["Bo"]["learned"] == learned
    # L-3 (H2 fills the memory): an empty list until it has any; hard
    # breaches are only ever said for rows the caller holds
    assert sig["learned"] == [] and sig["hard_breaches"] == {}
    assert sig["load_ledger"] == {}


def test_the_learned_memory_is_read_lazily_and_a_failure_costs_only_itself(rid, monkeypatch):
    import schedule_memory
    mem = [{"kind": "slot", "key": "slot|bob|Tuesday|night|off", "person": "Bob", "day": "Tuesday",
            "daypart": "night", "role": "Server", "value": "off", "confidence": 0.8, "enforcement": "soft",
            "source": "manager"}]
    seen = {}

    def _enforced(r, week_dates, roster_names=None, db_path=None):
        seen.update(rid=r, week=list(week_dates), roster=roster_names)
        return mem
    monkeypatch.setattr(schedule_memory, "enforced_signals", _enforced)
    sig, _w = se._quality_signals(rid, {"week_dates": WEEK, "roster": ["Bob"]})
    assert sig["learned"] == mem and seen == {"rid": rid, "week": WEEK, "roster": ["Bob"]}

    def _broken(*a, **k):
        raise RuntimeError("memory unreadable")
    monkeypatch.setattr(schedule_memory, "enforced_signals", _broken)
    sig, _w = se._quality_signals(rid, {"week_dates": WEEK})
    assert sig["learned"] == [] and "scores" in sig


def test_learned_preferences_keep_only_confident_slots():
    raw = {"Ana": {"avoids": ["Sunday night", "Friday day"], "prefers": ["Sunday night", "Tuesday day"],
                   "drops": 4, "claims": 2},
           "Bo": {"avoids": ["Someday night"], "prefers": [], "drops": 2, "claims": 0}}
    out = se._learned_preferences_signal(raw)
    # a slot both dropped and claimed says nothing; an unreadable one is dropped
    assert out == {"Ana": {"avoid": [["Friday", "morning"]], "prefer": [["Tuesday", "morning"]],
                           "weight": sq.LEARNED_PREFERENCE_WEIGHT, "source": "what they drop and pick up",
                           "drops": 4, "claims": 2}}


def test_l19_the_passes_score_a_week_against_what_staff_keep_dropping(rid):
    """Somebody who drops Sunday nights every time kept being handed them by
    every pass that chose by the score; the score now sees it."""
    c = _constraints()
    result = {"constraints": c, "roster": ["Bo", "Al"], "roster_roles": {"Bo": "Server", "Al": "Server"},
              "learned_preferences": {"Bo": {"avoids": ["Sunday night"], "prefers": [], "drops": 3, "claims": 0}},
              "typical_headcount": {("Sunday", "night"): {"Server": 1}}}
    sig, w = se._quality_signals(rid, result)
    on_bo = sq.score_rows([_row(WEEK[6], "Bo")], weights=w, **sig)
    on_al = sq.score_rows([_row(WEEK[6], "Al")], weights=w, **sig)
    assert on_bo["raw_score"] < on_al["raw_score"]
    assert any("Bo keeps asking to drop Sunday night" in x for x in on_bo["weaknesses"])


# ── SQ-17: the count capped at the people able, in the engine ─────────────

def test_sq17_a_rule_only_one_bartender_can_meet_is_kept_at_one(rid, monkeypatch):
    monkeypatch.setattr(models, "get_leader_flags", lambda r, db_path=None: {})
    rule = {"role": "Bartender", "days": ["Saturday"], "daypart": "night", "count": 2, "min_score": 5}
    nobody = {"role": "Host", "count": 1, "min_score": 5}
    result = {"leader_rules": [rule, nobody], "operational_scores": {"Pat": 5, "Al": 4, "Hal": 3},
              "roster": ["Pat", "Al", "Hal"],
              "roster_roles": {"Pat": "Bartender PM", "Al": "Bartender", "Hal": "Host"}}
    sig, _w = se._quality_signals(rid, result)
    assert sig["leader_rules"][0]["count"] == 1 and sig["leader_rules"][0]["asked_count"] == 2
    assert sig["leader_rules"][1] is nobody and sig["unmeetable_leader_rules"] == [nobody]
    assert sig["unsatisfiable"] == 1
    assert sig["capped_leader_rules"] == [{"label": "2 bartenders scoring 5 or above", "asked": 2, "able": 1}]
    out = sq.score_rows([_row(WEEK[5], "Al", "Bartender"), _row(WEEK[4], "Pat", "Bartender")], **sig)
    sat = next(s for s in out["shifts"] if s["date"] == WEEK[5])
    lead = next(d for d in sat["dimensions"] if d["key"] == "leadership")
    assert lead["facts"]["misses"][0]["count"] == 1
    reasons = out["confidence"]["reasons"]
    assert any("Only 1 on the roster can meet the rule" in r for r in reasons)
    assert any("Nobody on the roster can meet the rule" in r and "1 host scoring 5" in r for r in reasons)


# ── SQ-14 data for D1a: the hard breaches of the rows being scored ────────

def test_the_hard_breaches_name_the_date_daypart_and_freshness():
    rows = [_row(WEEK[0], "Ana", start="11:00am", end="11:00pm", hours=12), _row(WEEK[1], "Bo")]
    viols = [{"kind": "no_manager", "index": 0, "employee": "Ana", "date": WEEK[0], "shift_start": "11:00am",
              "hard": True, "no_show": False, "label": "no manager", "detail": "no manager 11am-11pm",
              "gap_start": 660, "gap_end": 1380, "minutes": 720, "day_level": True},
             {"kind": "rest_gap", "index": 1, "employee": "Bo", "date": WEEK[1], "shift_start": "5:00pm",
              "hard": True, "no_show": False, "label": "rest", "detail": "8h rest"},
             {"kind": "meal_break", "index": 0, "employee": "Ana", "date": WEEK[0], "hard": False},
             {"kind": "no_manager_roster", "index": 0, "employee": "Ana", "date": WEEK[0], "hard": True}]
    out = se.hard_breach_map(viols, rows)
    mon = out["by_date"][WEEK[0]]
    assert len(mon) == 1 and mon[0]["kind"] == "no_manager" and mon[0]["dayparts"] == ["morning", "night"]
    assert mon[0]["employee"] is None and mon[0]["minutes"] == 720 and mon[0]["tier"] == sr.TIER_MANAGER
    tue = out["by_date"][WEEK[1]][0]
    assert tue["employee"] == "Bo" and tue["dayparts"] == ["night"] and tue["id"] == ["rest_gap", WEEK[1], "bo"]
    assert [b["kind"] for b in out["week"]] == ["no_manager_roster"]
    assert out["rows_sig"][WEEK[0]] == sq.LocalScorer._signature([rows[0]])


def test_the_final_score_gets_the_breaches_of_exactly_its_rows(rid, monkeypatch):
    rows = [_row(WEEK[0], "Ana")]
    seen = {}

    def _capture(restaurant_id, result, **extra):
        seen.update(extra)
        return {}, None
    monkeypatch.setattr(se, "_quality_signals", _capture)
    monkeypatch.setattr(sq, "score_rows", lambda *a, **k: {"checked": False})
    viols = [{"kind": "rest_gap", "index": 0, "employee": "Ana", "date": WEEK[0], "shift_start": "5:00pm",
              "hard": True}]
    se._score_schedule_quality(rid, rows, {"rule_violations": viols})
    assert seen["hard_breaches"]["by_date"][WEEK[0]][0]["kind"] == "rest_gap"
    # a sweep of other rows is never reused: swept afresh with the week's rules
    c = _constraints(active={"bo"}, roster_names=["Bo"])
    se._score_schedule_quality(rid, [_row(WEEK[0], "Bo")], {"rule_violations": viols, "constraints": c})
    assert all(b["employee"] != "Ana" for bs in seen["hard_breaches"]["by_date"].values() for b in bs)


# ── SQ-15: the engine always builds the profiles ──────────────────────────

def test_sq15_a_restaurant_with_no_ratings_and_no_profiles_is_judged_on_its_own_sales(rid, monkeypatch):
    import labor
    monkeypatch.setattr(labor, "build_demand_forecast", lambda r: {"ok": True, "days": [
        {"day": "Saturday", "vs_average_pct": -30, "samples": 6}, {"day": "Thursday", "vs_average_pct": 30, "samples": 6}]})
    inputs = se.quality_inputs_from_db(rid, week_rows=[_row(WEEK[5], "Ana")])
    profiles = inputs["shift_profiles"]
    assert profiles, "built whether or not anybody is rated"
    sat = sq.profile_for_shift("Saturday", "night", profiles, inputs["demand_by_day"])
    assert (sat.demand, sat.min_quality, sat.requires_leader) == ("low", 65, False)
    thu = sq.profile_for_shift("Thursday", "night", profiles, inputs["demand_by_day"])
    assert (thu.demand, thu.min_quality) == ("peak", 82)


def test_sq15_the_generation_builds_them_always_and_keeps_the_prompt_as_it_was():
    import inspect
    src = inspect.getsource(se._build_schedule_result)
    assert "_profiles = _sq.profiles_from_config(" in src and "if _stored_profiles or _op_scores:\n" not in src
    assert 'result["shift_profiles"] = _profiles' in src
    assert "shift_profiles=_prompt_profiles" in src


# ── SQ-27: the published weeks before this one ────────────────────────────

def test_sq27_the_load_ledger_reads_the_published_weeks_before_this_one(rid, db_path):
    old = ["2026-09-21", "2026-09-22", "2026-09-26"]
    _history(db_path, rid, "2026-09-21", [_row(old[0], "Fav", hours=8), _row(old[2], "Fav", start="11:00am",
                                                                            end="3:00pm", hours=4),
                                          _row(old[1], "Al", "Cook")])
    _history(db_path, rid, "2026-09-28", [_row("2026-10-03", "Fav")], published=False)       # never sent
    _history(db_path, rid, "2026-07-06", [_row("2026-07-06", "Fav")])                      # past the window
    _history(db_path, rid, WEEK[0], [_row(WEEK[0], "Fav")])                                 # this week itself
    ledger = se.load_ledger(rid, WEEK[0])
    assert set(ledger) == {"Fav", "Al"}
    fav = ledger["Fav"]
    assert len(fav) == 1 and fav[0]["week"] == "2026-09-21" and fav[0]["hours"] == 12.0
    assert fav[0]["slots"] == [["Monday", "night"], ["Saturday", "morning"]] and fav[0]["role"] == "Server"
    assert ledger["Al"][0]["role"] == "Cook"
    # the signals carry it for the week being judged, reconciled to the roster
    sig, _w = se._quality_signals(rid, {"week_dates": WEEK, "roster": ["Fav"]})
    assert set(sig["load_ledger"]) == {"Fav"}


def test_sq27_the_tail_no_longer_says_every_day_was_normal(rid, db_path):
    _history(db_path, rid, "2026-09-28", [_row("2026-10-03", "Fav")])
    tail = se._prior_week_assignments(rid, before=WEEK[0])
    assert tail["Fav"][0] == {"date": "2026-10-03", "daypart": "night", "day": "Saturday"}


# ── D-6: the prompt's experienced list ────────────────────────────────────

def test_d6_the_prompt_lists_managers_and_salaried_people_as_experienced():
    import schedule_requirements as req
    c = _constraints(managers={"ann": "Manager"}, salaried={"erik baylis"})
    roster = [("Erik Baylis", "Owner"), ("Ann", "Manager"), ("New", "Server")]
    tenure = {"Erik Baylis": 1, "Ann": 2, "New": 2, "Vet": 40}
    names = se._prompt_experienced([], tenure, c, roster)
    assert names == ["Ann", "Erik Baylis"]
    block = req.experience_block(tenure, [n for n, _r in roster], {}, names)
    assert "Still developing — under 6 shifts here: New." in block
    # a history too short to judge anybody stays unclaimed, as the scorer leaves it
    assert se._prompt_experienced([], {"Ann": 2}, c, roster) == []


# ── SQ-22: calibration reads floors and bars per profile ──────────────────

def _calibration_world(db_path, rid, weeks=9, per_week=8, profile="weekday_dinner", bar=70):
    """Coverage under 60 goes wrong; from 60 up it goes fine — whatever the
    floor (70) says."""
    import random
    import datetime as dt
    rng = random.Random(11)
    conn = get_conn(db_path)
    for i in range(weeks):
        start = dt.date(2026, 6, 1) + dt.timedelta(weeks=i)
        days = [(start + dt.timedelta(days=k)).isoformat() for k in range(7)]
        shifts, outs = [], []
        for k in range(per_week):
            d, part = days[k % 7], ("night" if k % 2 else "morning")
            cov = rng.choice([30, 40, 50, 60, 70, 80, 90, 100])
            score = cov if cov < 70 else 90
            shifts.append({"date": d, "daypart": part, "scored": True, "score": score,
                           "profile": {"key": profile, "label": "Weekday dinner", "min_quality": bar},
                           "dimensions": [{"key": "coverage", "score": cov, "floor": 70},
                                          {"key": "fairness", "score": rng.choice([60, 80, 100]), "floor": None}]})
            bad = cov < 60
            outs.append((d, part, 2 if bad else 0, 3.6 if bad else 4.7, 34 if bad else 30))
        cur = conn.execute(
            "INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, labor_target, "
            "schedule_csv, summary_json, quality_json, published_at) VALUES (?,?,?,0,0,30,?,'[]',?,'2026-10-01')",
            (rid, days[0], days[6], HEADER, json.dumps({"score": 80, "shifts": shifts})))
        for d, part, issues, rating, labor in outs:
            conn.execute("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, people, issues, "
                         "review_rating, review_rating_attributed, labor_pct) VALUES (?,?,?,?,10,2,?,?,?,?)",
                         (rid, cur.lastrowid, d, part, issues, rating, rating, labor))
    conn.commit()
    conn.close()


def _watch_all(monkeypatch):
    import schedule_intel as si
    import datetime as dt
    monkeypatch.setattr(si, "watched_dates", lambda r, a, b, db_path=None: {
        (dt.date.fromisoformat(a) + dt.timedelta(days=i)).isoformat()
        for i in range((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days + 1)})


def test_sq22_calibration_reads_a_profiles_floor_against_what_its_shifts_did(rid, db_path, monkeypatch):
    _watch_all(monkeypatch)
    _calibration_world(db_path, rid)
    cal = sl.calibrate_weights(rid, db_path=db_path)
    entry = cal["profiles"]["weekday_dinner"]
    assert entry["ready"] and entry["shifts"] == 72
    floor = entry["floors"]["coverage"]
    assert floor["current"] == 70 and floor["fitted"] == 60 and floor["suggested"] == 65
    assert floor["evidence"] >= sl.CALIBRATION_THRESHOLD_EVIDENCE
    assert "Weekday dinner's coverage floor down to 65" in floor["explanation"]
    assert cal["suggested_profiles"]["weekday_dinner"]["floors"] == {"coverage": 65}
    assert "fairness" not in entry["floors"]                 # no floor was ever held there
    assert cal["moving_profiles"] == ["weekday_dinner"]


def test_sq22_a_profile_with_few_shifts_of_its_own_is_left_alone(rid, db_path, monkeypatch):
    _watch_all(monkeypatch)
    _calibration_world(db_path, rid, weeks=8, per_week=3)
    _calibration_world(db_path, rid, weeks=8, per_week=3, profile="saturday_dinner")
    cal = sl.calibrate_weights(rid, db_path=db_path)
    assert cal["ready"]
    assert cal["profiles"]["saturday_dinner"]["ready"] is False
    assert "24 of its shifts on record" in cal["profiles"]["saturday_dinner"]["explanation"]
    assert cal["suggested_profiles"] == {}


def test_sq22_the_fit_carries_one_dimension_per_ten_shifts(rid, db_path, monkeypatch):
    rows = []
    for k in range(45):
        dims = {f"d{j}": float((k * (j + 3)) % 17) for j in range(8)}
        rows.append((dims, {"issues": float(k % 5), "review_rating": None, "labor_vs_target": None}, 1, {}))
    coef, seen, used, crowded = sl._ridge_fit(rows, [f"d{j}" for j in range(8)], "issues", 30)
    assert used == 45 and len(coef) == 4 and len(crowded) == 4
    assert sl.CALIBRATION_MIN_PAIRS == 30


def test_sq22_apply_writes_a_built_in_into_the_tuning_and_an_owner_profile_into_itself(rid, db_path):
    models.save_shift_profile(rid, {"key": "fri_mine", "label": "Friday (mine)", "days": ["Friday"],
                                    "daypart": "night", "min_quality": 80, "floors": {"leadership": 55}})
    cal = {"ready": True, "suggested_profiles": {"weekday_dinner": {"floors": {"coverage": 65}, "min_quality": 68},
                                                 "fri_mine": {"min_quality": 75, "floors": {"coverage": 66}}}}
    out = sl.apply_profile_calibration(rid, cal, updated_by="Owner")
    assert set(out["profiles"]) == {"weekday_dinner", "fri_mine"} and out["before"] == {}
    assert models.get_quality_tuning(rid) == {"weekday_dinner": {"floors": {"coverage": 65}, "min_quality": 68}}
    mine = next(p for p in models.get_shift_profiles(rid) if p["key"] == "fri_mine")
    assert mine["min_quality"] == 75 and mine["floors"] == {"leadership": 55, "coverage": 66}
    # the engine judges the built-in with it, and the rest of the built-ins stay
    profiles = sq.profiles_from_config(None, tuning=models.get_quality_tuning(rid))
    wd = next(p for p in profiles if p.key == "weekday_dinner")
    assert wd.min_quality == 68 and wd.floors == {"coverage": 65}
    assert {p.key for p in profiles} == {b.key for b in sq.BUILTIN_PROFILES}
    # and the stamp clients cache a score against moves with it
    stamp = models.capability_version(rid)
    models.update_restaurant(rid, {"quality_tuning_json": json.dumps({"brunch": {"min_quality": 72}})})
    assert models.capability_version(rid) != stamp


def test_sq22_the_owners_apply_writes_the_floors_and_bars_too(rid, db_path, monkeypatch):
    import strategy_routes
    _watch_all(monkeypatch)
    _calibration_world(db_path, rid)
    owner = {"id": 1, "restaurant_id": rid, "role": "client", "is_admin": False, "username": "o"}
    monkeypatch.setattr(strategy_routes, "_principal", lambda u: True)
    payload, status = strategy_routes._do_calibration_apply(owner)
    assert status == 200 and payload["profiles"]["weekday_dinner"]["floors"] == {"coverage": 65}
    assert models.get_quality_tuning(rid)["weekday_dinner"]["floors"] == {"coverage": 65}
    # the next reading starts from where the floor now stands
    again = sl.calibrate_weights(rid, db_path=db_path)
    assert again["profiles"]["weekday_dinner"]["floors"]["coverage"]["current"] == 65
    assert again["suggested_profiles"]["weekday_dinner"]["floors"] == {"coverage": 60}


# ── the reconciliation reaches the weeks before this one ──────────────────

def test_people_who_have_left_are_no_longer_colleagues_a_share_is_measured_against():
    sig = {"roster": ["Ana"], "scores": {"Ana": 4, "Gone": 5}, "leader_flags": {},
           "ledger": {"Ana": {"shifts": 9}, "Gone": {"shifts": 30}},
           "load_ledger": {"Ana": [{"week": "x"}], "Gone": [{"week": "x"}]}}
    se._reconcile_to_roster(sig)
    assert set(sig["ledger"]) == {"Ana"} and set(sig["load_ledger"]) == {"Ana"} and set(sig["scores"]) == {"Ana"}
