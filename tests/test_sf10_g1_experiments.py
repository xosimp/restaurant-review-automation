"""Schedule fix round 10/10/26, workstream G1 — the experiment arms are
clean (LOGIC-27) and judged honestly (LEARNING-25)."""
import shift_quality as sq
import schedule_engine as se
import schedule_experiments as sx
import schedule_optimizer as so

SAT, MON = "2026-09-12", "2026-09-07"


def _row(date, name, role):
    return {"date": date, "day": sq._day_name(date), "employee": name, "role": role, "shift_start": "5:00pm",
            "shift_end": "10:00pm", "scheduled_hours": 5, "notes": ""}


def _closer_week():
    rows = [_row(SAT, "Ann", "Bartender"), _row(MON, "Dee", "Bartender")]
    rule = {"role": "Bartender", "count": 1, "attribute": "can_close", "days": ["Saturday"]}
    sig = {"roster": ["Ann", "Dee"], "typical_headcount": {("Saturday", "night"): {"Bartender": 1}},
           "availability": {}, "constraints": {}, "leader_rules": [rule], "leader_flags": {"Dee": True},
           "scores": {"Ann": 3, "Dee": 3}}
    return rows, sig


def test_the_model_arm_has_nobody_reassigned_by_the_optimizer():
    rows, sig = _closer_week()
    on = so.optimize(rows, {}, signals=dict(sig))
    assert any(c["kind"] in so.REASSIGN_KINDS for c in on["changes"]), "the reference week needs a reassignment"
    off = so.optimize(rows, {}, signals=dict(sig), reassign=False)
    assert not [c for c in off["changes"] if c["kind"] in so.REASSIGN_KINDS]
    assert sorted((r["date"], r["employee"]) for r in off["rows"] if r["employee"]) == \
        sorted((r["date"], r["employee"]) for r in rows) or off["changes"], off


def test_the_arms_toggle_reassignment_with_the_solver():
    exp = sx.experiment("assign_v1")
    flags = {a["key"]: a["flags"] for a in exp["arms"]}
    assert flags["model"] == {"solver": False, "reassign": False}
    assert flags["solver"] == {"solver": True, "reassign": True}
    assert sx.flag([], "reassign") is True
    assert sx.flag([{"flags": flags["model"]}], "reassign") is False


def test_the_learning_switchback_turns_the_memory_off_one_week_in_four_per_restaurant():
    import datetime as dt
    exp = sx.experiment("learning_v1")
    assert exp["design"] == "switchback" and exp["control"] == "memory_on"
    weeks = [(dt.date(2026, 1, 5) + dt.timedelta(weeks=k)).isoformat() for k in range(40)]
    for rid in (1, 2, 3):
        arms = [sx.hashed_arm(exp, rid, w) for w in weeks]
        assert arms.count("memory_off") == 10 and arms.count("memory_on") == 30
        # never two off weeks in a row: each restaurant is its own control
        assert not any(a == b == "memory_off" for a, b in zip(arms, arms[1:]))
        assert arms == [sx.hashed_arm(exp, rid, w) for w in weeks]          # a regeneration keeps it
    assert sx.flag([{"flags": {"learned": False}}], "learned") is False and sx.flag([], "learned") is True


def _arm_row(key, label, drr, issues=None, acc=None, restaurants=5, n=20):
    return {"arm": key, "label": label, "restaurants": restaurants, "drr": {"n": n, "mean": drr},
            "acceptance": {"n": n, "mean": acc}}


def _diff(lo, hi, se=0.5):
    return {"diff": (lo + hi) / 2, "ci90": (lo, hi), "se": se, "clusters": 5}


def test_acceptance_alone_never_calls_a_winner_and_the_primaries_do():
    exp = sx.experiment("assign_v1")
    control = _arm_row("model", "Model", 30.0)
    treat = _arm_row("solver", "Solver", 30.0)
    zero = _diff(-1, 1)
    treat["vs_control"] = {"acceptance": _diff(0.1, 0.3), **{k: zero for k, _l in sx.PRIMARIES}}
    v = sx.verdict(exp, [control, treat])
    assert v["state"] == "no_difference" and v["call"] is None, "acceptance alone called a winner"
    treat["vs_control"]["drr"] = _diff(-12, -4)
    v = sx.verdict(exp, [control, treat])
    assert v["state"] == "winner" and v["call"] == "solver" and "edits per 100" in v["text"]
    treat["vs_control"]["unmanaged_minutes"] = _diff(10, 50)
    v = sx.verdict(exp, [control, treat])
    assert v["state"] == "conflicting" and "minutes with no manager" in v["text"]


def test_each_arm_says_what_its_sample_can_detect():
    treat = {"vs_control": {"drr": _diff(-3, 3, se=2.0)}}
    pr = sx.power_read(treat, 20)
    assert pr["drr"]["mde"] == round((sx.Z90 + sx.Z80) * 2.0, 3)
    assert pr["drr"]["weeks_needed"] >= 20 and pr["issues"]["mde"] is None


def test_the_optimizer_stage_hears_the_arm(monkeypatch):
    seen = {}

    def fake(rows, inputs, **kw):
        seen.update(kw)
        return {"rows": rows, "changes": [], "before_score": 1, "after_score": 1, "seconds": 0}
    monkeypatch.setattr(so, "optimize", fake)
    monkeypatch.setattr(se._rules, "violations", lambda rows, c: [])

    class X:
        searches = True
        search_spent = {}
        signals = {}
        weights = {}
        c = None
        budget = 0
        trim_on = False
        section_count = None
        editable = None
        result = {}
        reassign_on = False

        def search_seconds(self):
            return 30.0
    se._stage_optimizer([_row(SAT, "Ann", "Server")], X())
    assert seen.get("reassign") is False
