"""Schedule fix round 10/11/26, workstream G2 — standby, reliability and
"avoids this slot" (blind re-audit LEARNING-19, LEARNING-21, LEARNING-22)."""
import sys
import types
from datetime import date, timedelta

import attendance_risk as ar
import schedule_intel as si
import staff_settings


# ── LEARNING-19: the standby rule scales with the crew ──────────────────────

def _risk(n, p, need=None):
    miss = [p] * n
    need = n if need is None else need
    k = ar.short_by_k(need)
    return {"busy": True, "need": need, "scheduled": n, "p_short": ar.p_short(miss, need), "short_by": k,
            "p_short_by": ar.p_short_by(miss, need, k),
            "standby_saves": round(ar.expected_short(miss, need) - ar.expected_short(miss + [0.0], need), 4)}


def test_a_big_crew_is_not_given_a_standby_for_any_one_absence():
    """sim: STANDBY_RISK was reached at a 10% base with 4 people, 6.9% with
    6, 5.2% with 8 — every big busy shift got a paid extra person."""
    eight = _risk(8, 0.10)
    assert eight["p_short"] >= ar.STANDBY_RISK                     # P(any absence): what used to decide
    assert eight["short_by"] == 2 and not ar.standby_needed([eight])
    assert [ar.short_by_k(n) for n in (1, 4, 7, 8, 12, 13)] == [1, 1, 1, 2, 2, 3]
    # A small crew with a real risk still gets one.
    assert ar.standby_needed([_risk(4, 0.15)])


def test_a_standby_must_be_worth_its_cost_when_both_are_known():
    x = _risk(4, 0.15)
    assert ar.standby_needed([x], value_of=lambda r: 400.0, cost_of=lambda r: 60.0)
    assert not ar.standby_needed([x], value_of=lambda r: 50.0, cost_of=lambda r: 60.0)
    assert ar.standby_needed([x], value_of=lambda r: None, cost_of=lambda r: 60.0)   # unknown: threshold decides


def test_the_engine_prices_standbys_from_the_one_objective(monkeypatch):
    import schedule_engine as se
    import schedule_rules as sr
    fake = types.ModuleType("schedule_objective")

    class Ctx:
        @staticmethod
        def build(rid, dates, **k):
            return {"rid": rid}
    fake.ObjectiveContext = Ctx
    fake.slot_value = lambda ctx, d, m, role: 2.0
    monkeypatch.setitem(sys.modules, "schedule_objective", fake)
    c = sr.Constraints(restaurant_id=1, week_dates=["2026-10-16"], week_days=["Friday"])
    value_of, cost_of = se._standby_prices(1, ["2026-10-16"], c, {"overtime": {"rates": {"server": 12.0}}})
    x = {"date": "2026-10-16", "daypart": "night", "role": "server"}
    assert value_of(x) > 0 and cost_of(x) > 0
    monkeypatch.setitem(sys.modules, "schedule_objective", None)
    assert se._standby_prices(1, ["2026-10-16"], c, {}) == (None, None)


# ── LEARNING-21: the unreliable label can come off ─────────────────────────

def _events(today, misses_days_ago, clean_shifts_days_ago):
    ev = [("Kim", (today - timedelta(days=d)).isoformat(), "no_show") for d in misses_days_ago]
    ev += [("Kim", (today - timedelta(days=d)).isoformat(), "on_time") for d in clean_shifts_days_ago]
    ev += [("Lee", (today - timedelta(days=d)).isoformat(), "on_time") for d in range(1, 120, 3)]
    return ev


def test_clean_weeks_with_few_shifts_pull_a_rate_back_toward_the_base(monkeypatch):
    today = date(2026, 10, 12)
    # Two bad weeks in early summer, then only a handful of shifts since:
    # the decayed misses and the decayed shifts kept the ratio where it was.
    ev = _events(today, misses_days_ago=(70, 66, 63, 59), clean_shifts_days_ago=(72, 68, 65, 61, 57, 30))
    rel = staff_settings.weighted_attendance(ev, today=today)["Kim"]
    monkeypatch.setattr(staff_settings, "REDEEM_SHIFTS_PER_CLEAN_WEEK", 0.0)
    before = staff_settings.weighted_attendance(ev, today=today)["Kim"]
    assert before["unreliable"] and not rel["unreliable"]
    assert rel["redeemed_shifts"] > 0 and rel["no_show_rate"] < before["no_show_rate"]
    assert staff_settings.redemption_shifts((today - timedelta(days=10)).isoformat(), today) == 0.0


def test_a_risky_person_short_of_recent_shifts_is_explored():
    today = date(2026, 10, 12)
    ev = _events(today, misses_days_ago=(40, 33, 26), clean_shifts_days_ago=(45, 38, 31, 24, 20))
    rel = staff_settings.weighted_attendance(ev, today=today)["Kim"]
    assert rel["explore"] is True
    lee = staff_settings.weighted_attendance(ev, today=today)["Lee"]
    assert lee["explore"] is False


def test_the_solver_keeps_offering_an_explored_person_shared_busy_shifts():
    import schedule_solver as ss
    from sched_d2_week import big_week, WEEK
    rows, c, sig = big_week()
    busy_unit = None
    for explore in (False, True):
        rel = {"P14": {"no_show_rate": 0.3, "weekday_rates": {}, "explore": explore}}
        prob = ss.Problem(rows, c, signals=dict(sig, reliability=rel))
        if busy_unit is None:
            busy_unit = next((u for u in prob.units if u.hard and len(
                [i for i in (prob.groups.get(("S", u.date, u.primary[0])) or {}).get("units") or ()
                 if prob.units[i].role_at.get(u.primary[0]) == u.role_at.get(u.primary[0])]) > 1), None)
        if busy_unit is None:
            return
        cost = prob.components_of(busy_unit, prob.pidx["p14"]).get("reliability", 0)
        assert (cost > 0) is (not explore)


# ── LEARNING-22: an avoid takes three drops ─────────────────────────────────

def test_avoid_needs_three_drops_and_decays_faster():
    assert si.AVOID_MIN_EVENTS == 3 and si.AVOID_HALF_LIFE_DAYS < 120


def test_the_person_is_told_what_the_draft_learned_they_avoid(monkeypatch):
    import staff_routes
    monkeypatch.setattr(si, "behaviour_preferences", lambda rid, **k: {"Kim Lo": {
        "avoids": ["Sunday night"], "evidence": {"Sunday night": {
            "said": "dropped 3 of the 4 Sunday dinner shifts they were scheduled on in the last 12 weeks"}}}})
    got = staff_routes._learned_avoids(1, "kim  lo")
    assert got and got[0]["slot"] == "Sunday night" and "dropped 3 of the 4" in got[0]["text"]
    assert staff_routes._learned_avoids(1, "Someone Else") == []


def test_with_a_standby_policy_a_standby_is_on_call_not_an_extra_shift(monkeypatch):
    import schedule_engine as se
    import schedule_requirements as sreq
    x = dict(_risk(4, 0.15), date="2026-10-16", day="Friday", daypart="night", role="server", expected=3.4,
             people=[])
    monkeypatch.setattr(sreq, "busy_shifts", lambda *a, **k: {("2026-10-16", "night")})
    monkeypatch.setattr(ar, "rates", lambda rid, **k: {})
    monkeypatch.setattr(ar, "shift_risks", lambda *a, **k: [x])
    monkeypatch.setattr(se, "_standby_prices", lambda *a, **k: (None, None))
    monkeypatch.setattr(se, "_standby_policy", lambda rid: {"stated": True, "pay_hours": 2.0})
    got = {"rows": [{"date": "2026-10-16", "employee": "Ana", "role": "Server"}]}
    se._solver_standby(1, ["2026-10-16"], got, [], {}, None, {}, None)
    st = got["stats"]["standby"]
    assert st["staffed"] == 0 and len(st["on_call"]) == 1 and len(got["rows"]) == 1
    assert st["ledger"][0]["lever"] == "standby" and st["ledger"][0]["provenance"] == "attendance"
