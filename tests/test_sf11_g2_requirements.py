"""Schedule fix round 10/11/26, workstream G2 — the manager's learned
headcount is a level after the driver, and one ledger says why a shift asks
for what it asks (blind re-audit LEARNING-18, LEARNING-27)."""
import labor
import schedule_requirements as req
import staffing_model as sm

FRI = "2026-10-16"


def _model(total=300.0):
    fam = sm._family("Server")
    return {"families": {fam: {"lag": 0, "unit": "guests", "parts": {"night": {
        "std": 20.0, "fixed": 2, "source": "measured", "text": "20 guests per server-hour"}}}},
        "work": {("Friday", "night"): {"guests": {"total": total, "days": 8, "shares": {
            m: (0.2 if m in (1110, 1140) else 0.06) for m in range(1020, 1320, 30)}}}}}


def _server(rows):
    night = next(r for r in rows if r["daypart"] == "night")
    return next(x for x in night["roles"] if x["role"].lower() == "server")


def _patterns(usual, peak, delta, settled):
    """The baseline as labor.apply_learned_to_patterns leaves it: typical and
    the people on at once raised by what the learned headcount applied."""
    applied = max(0, min(delta, settled - usual))
    return ({("Friday", "night"): {"Server": usual + applied}},
            {("Friday", "night"): {"Server": {"peak": peak + applied}}},
            {("Friday", "night"): {"Server": {"delta": delta, "settled": settled, "base": usual,
                                              "applied": applied}}})


def _required(usual, peak, delta, settled, total=300.0):
    typical, conc, learned = _patterns(usual, peak, delta, settled)
    rows = req.shift_requirements([FRI], typical_headcount=typical, staffing_model=_model(total), concurrent=conc,
                                  learned=learned, open_times={"Friday": "4:00pm"}, close_times={"Friday": "11:00pm"})
    return _server(rows)


def test_a_settled_level_is_never_amplified_by_the_driver_stagger():
    """sim_headcount_override.py: usual 4 → required 6; the manager settled 5
    → required 8; 6 → 9. The level is the manager's: the driver's own figure
    (6 at the rush) already covers 5, so it stays 6."""
    assert _required(4, 4, 0, 4)["required"] == 6
    assert _required(4, 4, 1, 5)["required"] == 6
    assert _required(4, 4, 2, 6)["required"] == 6


def test_a_settled_level_above_the_driver_binds_as_a_level():
    low = _required(4, 4, 0, 4, total=120.0)["required"]
    got = _required(4, 4, 2, low + 2, total=120.0)
    assert got["required"] == low + 2
    learned = [e for e in got["ledger"] if e["lever"] == "learned"]
    assert learned and learned[0]["applied"] and learned[0]["delta"] == 2 and learned[0]["provenance"] == "manager"


def test_the_ledger_explains_the_number_and_the_arbiter_holds_stacking():
    typical, conc, learned = _patterns(4, 4, 1, 5)
    rows = req.shift_requirements([FRI], typical_headcount=typical, concurrent=conc, learned=learned,
                                  elasticity={("server", "night"): {"move": 1, "reason": "one more server sold more"}},
                                  role_floors={"Server": {"night": 2}})
    s = _server(rows)
    levers = [e["lever"] for e in s["ledger"]]
    assert levers[0] == "usual" and "learned" in levers and "elasticity" in levers
    el = next(e for e in s["ledger"] if e["lever"] == "elasticity")
    assert el["applied"] is False and "manager's own learned level" in el["held_by"]
    assert s["required"] == 5
    words = req.explain_role(s)
    assert words.startswith("5: usual 4") and "the manager's own level +1" in words


def test_an_elasticity_target_level_and_weekdays_go_through_the_ledger():
    """G1's level semantics (LEARNING-6/7) on the one ledger path: a +1
    measured to a target of 5 lifts a usual 4 to 5, a baseline already at 5
    is held (logged, not applied), and a weekday it was not measured on is
    left alone."""
    mv = {("server", "night"): {"move": 1, "target": 5, "weekdays": ["Friday"], "reason": "measured +1"}}
    up = _server(req.shift_requirements([FRI], typical_headcount={("Friday", "night"): {"Server": 4}},
                                        elasticity=mv))
    assert up["required"] == 5
    assert [e for e in up["ledger"] if e["lever"] == "elasticity"][0]["applied"]
    held = _server(req.shift_requirements([FRI], typical_headcount={("Friday", "night"): {"Server": 5}},
                                          elasticity=mv))
    e = [e for e in held["ledger"] if e["lever"] == "elasticity"][0]
    assert held["required"] == 5 and not e["applied"] and "already at the level" in e["held_by"]
    sat = "2026-10-17"
    rows = req.shift_requirements([sat], typical_headcount={("Saturday", "night"): {"Server": 4}}, elasticity=mv)
    s = next(x for x in next(r for r in rows if r["daypart"] == "night")["roles"] if x["role"] == "Server")
    assert s["required"] == 4 and not [e for e in s["ledger"] if e["lever"] == "elasticity"]


def test_the_baseline_keeps_people_on_at_once_in_step(monkeypatch):
    import schedule_learning
    monkeypatch.setattr(schedule_learning, "learned_headcount",
                        lambda rid, *a, **k: {("Friday", "night"): {"Server": {"delta": 1, "settled": 5}}})
    p = {"typical_headcount": {("Friday", "night"): {"Server": 4}},
         "concurrent": {("Friday", "night"): {"Server": {"peak": 3}}}}
    labor.apply_learned_to_patterns(7, p)
    assert p["typical_headcount"][("Friday", "night")]["Server"] == 5
    assert p["concurrent"][("Friday", "night")]["Server"]["peak"] == 4
    assert p["learned_headcount"][("Friday", "night")]["Server"]["applied"] == 1
