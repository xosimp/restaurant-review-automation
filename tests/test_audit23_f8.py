"""Audit #23 fix round (10/21/26), group F8 — the residual of A3-02.

Historical costing prices a $0 punch by the corrected chain
(schedule_objective._shift_rate_source); forward pricing of a DRAFTED week
still read `person_rates.get(name) or role_rate`, where the person map's
value is the person's latest pay in ANY job — so Amy, $11 as a Barback AM
and $0 as a Bartender PM (EJ's $0-tipped PM jobs), was priced at $11 on a
drafted Bartender PM shift ahead of the owner's $9 role rate. Every forward
site now reads one chain (schedule_objective.forward_rate_source): the
owner's rate for the person → the owner's rate for the role → the person's
latest pay in the same job → in any job → the role's typical → the blended.
"""
import json

import pytest

import schedule_economics as econ
import schedule_objective as so
import schedule_optimizer as sopt
import schedule_rules as sr
import schedule_solver as ss
from sched_d2_week import WEEK, DAYS, row

MON, TUE, WED, THU, FRI, SAT, SUN = WEEK


def _punch(d, emp, role, pay):
    return {"date": d, "employee": emp, "role": role, "pay_rate": pay, "shift_start": "9:00am",
            "shift_end": "3:00pm", "actual_hours": 6}


# Amy: paid as a Barback AM ($11), $0 as a Bartender PM. Bo: paid in both,
# $10 in Bartender PM before a later $12 Barback AM.
PUNCHES = [_punch("2026-09-01", "Amy", "Barback AM", 11.0), _punch("2026-09-02", "Amy", "Bartender PM", 0),
           _punch("2026-09-01", "Bo", "Bartender PM", 10.0), _punch("2026-09-03", "Bo", "Barback AM", 12.0)]
OWNER_ROLES = {"Bartender PM": 9.0, "_default": 15.0}


def _book(owner=None):
    return so.rate_book(PUNCHES, owner or {})


def _fwd(name, role, owner_people=None, role_rates=OWNER_ROLES):
    people, typical = _book(owner_people)
    return so.forward_rate_of(name, role, role_rates, 15.0, people, typical)


# ── the chain ────────────────────────────────────────────────────────────────

def test_a_zero_dollar_pm_job_is_priced_at_the_owner_role_rate_and_the_am_job_at_its_pay():
    assert _fwd("Amy", "Bartender PM") == 9.0          # not her $11 Barback AM pay
    assert _fwd("Amy", "Barback AM") == 11.0           # her pay in the same job
    assert _fwd(" amy ", "bartender  pm") == 9.0       # keys lowered, whitespace collapsed


def test_the_owner_rate_for_the_person_still_wins():
    assert _fwd("Amy", "Bartender PM", {"amy": 14.0}) == 14.0
    assert _fwd("Amy", "Barback AM", {"Amy": 14.0}) == 14.0


def test_without_an_owner_role_rate_same_job_then_any_job_pay():
    roles = {"_default": 15.0}
    assert _fwd("Bo", "Bartender PM", role_rates=roles) == 10.0   # his PM pay, not his later $12 AM pay
    assert _fwd("Amy", "Bartender PM", role_rates=roles) == 11.0  # no PM pay: her latest in any job
    assert _fwd("Cy", "Bartender PM", role_rates=roles) == 10.5   # nobody's rate: the role's typical (rate_book)
    assert _fwd("Cy", "Host", role_rates=roles) == 15.0           # nothing: the fallback


def test_sources_are_named():
    people, typical = _book()
    pp, owner, jobs = so.forward_maps(people)
    kw = dict(role_rates={"bartender pm": 9.0}, people=pp, owner=owner, jobs=jobs, role_typical=typical, fallback=15)
    assert so.forward_rate_source("Amy", "Bartender PM", **kw) == (9.0, "role_rate")
    assert so.forward_rate_source("Amy", "Barback AM", **kw) == (11.0, "person")
    assert so.forward_rate_source(None, "Host", **kw) == (15.0, "fallback")


def test_rate_maps_are_plain_json_and_an_old_payload_keeps_the_old_order():
    people, _ = _book({"Zed": 20.0})
    maps = so.rate_maps(people)
    assert json.loads(json.dumps(maps)) == maps
    assert maps["owner_person_rates"] == {"zed": 20.0}
    assert maps["person_job_rates"]["amy"] == {"barback am": 11.0}
    assert maps["person_job_rates"]["bo"] == {"bartender pm": 10.0, "barback am": 12.0}
    assert so.rate_maps({"amy": 11.0}) == {}
    # A plain person map with no maps beside it (an old checkpoint): the
    # person first, as before.
    pp, owner, jobs = so.forward_maps({"amy": 11.0})
    assert owner is None and jobs is None
    assert so.forward_rate("Amy", "bartender pm", role_rates={"bartender pm": 9.0}, people=pp, owner=owner,
                           jobs=jobs) == 11.0


# ── every forward site agrees ────────────────────────────────────────────────

def _overtime_payload():
    """signals["overtime"] as schedule_engine builds it — the RateBook
    stripped to a dict, its maps carried beside it — round-tripped through
    JSON as a checkpoint would."""
    people, typical = _book()
    ot = {"line": 40.0, "rates": dict(OWNER_ROLES), "default_rate": 15.0,
          "person_rates": dict(people), "role_typical": dict(typical)}
    ot.update(so.rate_maps(people))
    return json.loads(json.dumps(ot))


def test_priced_cost_prices_the_drafted_pm_shift_at_the_role_rate():
    people, typical = _book()
    pm = [row(MON, "Amy", "4:00pm", "10:00pm", "Bartender PM")]      # 6h
    am = [row(MON, "Amy", "9:00am", "3:00pm", "Barback AM")]
    # A RateBook carries its own maps (dsr/tomorrow passes one).
    assert econ.priced_cost(pm, OWNER_ROLES, 15.0, person_rates=people, role_typical=typical)["total"] == 54.0
    assert econ.priced_cost(am, OWNER_ROLES, 15.0, person_rates=people, role_typical=typical)["total"] == 66.0
    # The plain payload with the maps beside it (price_week / week_pricing).
    ot = _overtime_payload()
    pricing = {"role_rates": ot["rates"], "blended_rate": 15.0, "person_rates": ot["person_rates"],
               "role_typical": ot["role_typical"], "owner_person_rates": ot["owner_person_rates"],
               "person_job_rates": ot["person_job_rates"]}
    assert econ.price_week(pm, pricing)["total"] == 54.0
    # Without the maps (an old payload): the old order, $11.
    old = {k: v for k, v in pricing.items() if k not in ("owner_person_rates", "person_job_rates")}
    assert econ.price_week(pm, old)["total"] == 66.0


def test_week_pricing_carries_the_maps(monkeypatch):
    import labor
    import models
    monkeypatch.setattr(models, "get_role_rates", lambda rid: dict(OWNER_ROLES))
    monkeypatch.setattr(labor, "person_rate_book", lambda rid: _book())
    p = econ.week_pricing(1)
    assert p["person_job_rates"]["amy"] == {"barback am": 11.0} and p["owner_person_rates"] == {}
    assert econ.price_week([row(MON, "Amy", "4:00pm", "10:00pm", "Bartender PM")], p)["total"] == 54.0


def _cons(names):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=list(names),
                       active={n.lower() for n in names})
    c.compliance = dict(sr.DEFAULTS)
    return c


def test_the_solver_optimizer_objective_and_priced_cost_agree():
    ot = _overtime_payload()
    names = ["Amy", "Bo"]
    rows = [row(MON, "Amy", "4:00pm", "10:00pm", "Bartender PM"), row(TUE, "Amy", "9:00am", "3:00pm", "Barback AM")]
    sig = {"roster": names, "roster_roles": {n: "Bartender PM" for n in names}, "overtime": ot}
    c = _cons(names)
    want_pm, want_am = 9.0, 11.0
    # schedule_solver (and CP-SAT, whose _rate_cents reads Problem.rate)
    prob = ss.Problem(rows, c, signals=sig)
    amy = prob.pidx["amy"]
    assert prob.rate(amy, "bartender pm") == want_pm and prob.rate(amy, "barback am") == want_am
    import schedule_cpsat as cp
    pm_unit = next(u for u in prob.units if u.role_at.get(u.primary[0]) == "bartender pm")
    assert cp._rate_cents(prob, amy, pm_unit) == 900
    # Her overtime hour: the role she is drafted most hours in (a tie: the
    # first by name, barback am) — the forward chain, not her any-job pay.
    assert prob.ot_rate(amy) in (want_pm, want_am)
    # schedule_optimizer
    st = sopt._State(rows, sig, {}, c)
    assert st.rate("Amy", "Bartender PM") == want_pm and st.rate("Amy", "Barback AM") == want_am
    pricing = sopt.pricing_inputs(sig, {}, c)
    assert sopt.labor_dollars(rows, pricing) == pytest.approx(6 * want_pm + 6 * want_am)
    # the objective's context (CP-SAT / the engine build it so)
    ctx = so.ObjectiveContext.build(1, list(WEEK), rates={k: v for k, v in ot["rates"].items() if k != "_default"},
                                    role_typical=ot["role_typical"], person_rates=ot["person_rates"],
                                    owner_person_rates=ot["owner_person_rates"],
                                    person_job_rates=ot["person_job_rates"], blended_rate=15.0)
    assert ctx.rate("Amy", "Bartender PM") == want_pm and ctx.rate("Amy", "Barback AM") == want_am
    # and priced_cost, the week's one price
    assert econ.priced_cost(rows, ot["rates"], 15.0, person_rates=ot["person_rates"],
                            role_typical=ot["role_typical"], owner_person_rates=ot["owner_person_rates"],
                            person_job_rates=ot["person_job_rates"])["total"] == 6 * want_pm + 6 * want_am


def test_an_old_payload_keeps_the_solver_and_objective_on_the_old_order():
    people, typical = _book()
    ot = {"line": 40.0, "rates": dict(OWNER_ROLES), "default_rate": 15.0, "person_rates": dict(people),
          "role_typical": dict(typical)}
    names = ["Amy"]
    rows = [row(MON, "Amy", "4:00pm", "10:00pm", "Bartender PM")]
    prob = ss.Problem(rows, _cons(names), signals={"roster": names, "roster_roles": {"Amy": "Bartender PM"},
                                                   "overtime": ot})
    assert prob.rate(prob.pidx["amy"], "bartender pm") == 11.0
    ctx = so.ObjectiveContext.build(1, list(WEEK), rates={"Bartender PM": 9.0}, person_rates=ot["person_rates"],
                                    blended_rate=15.0)
    assert ctx.rate("Amy", "Bartender PM") == 11.0


def test_one_off_forward_prices_read_the_chain(monkeypatch):
    import labor
    import models
    import event_reactive
    import standby_insurance
    monkeypatch.setattr(models, "get_role_rates", lambda rid: dict(OWNER_ROLES))
    monkeypatch.setattr(labor, "person_rate_book", lambda rid: _book())
    assert event_reactive._rate(1, "Amy", "Bartender PM") == 9.0
    assert standby_insurance._rate(1, "Amy", "Bartender PM") == 9.0
    assert standby_insurance._rate(1, "Amy", "Barback AM") == 11.0
