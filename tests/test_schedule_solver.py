"""The assignment solver (schedule_solver, audit #47).

What it must never do matters most: every hard rule the sweep knows holds
in what it returns, an impossible shift is named rather than filled
illegally, and the clock is respected. What it must do: on instances small
enough to enumerate, find the true optimum of its own cost and say it proved
it; and in generation keep its week only when Shift Quality scores it higher.
No model is called anywhere here.
"""
import itertools
import random
import time

import pytest

import schedule_rules as sr
import schedule_solver as ss
import shift_quality as sq

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = [sq._day_name(d) for d in WEEK]


def cons(names, **kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=list(names),
                       active={n.lower() for n in names})
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def row(date, name, role="Server", start="5:00pm", end="10:00pm"):
    h = ((sr.parse_minutes(end) - sr.parse_minutes(start)) % (24 * 60)) / 60.0
    return {"date": date, "day": sq._day_name(date), "employee": name, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": str(h), "notes": ""}


def _hard(rows, c):
    return [v for v in sr.violations(rows, c) if v["hard"]]


# ── the optimum, proved, on instances small enough to enumerate ───────────

def _small_instance(seed):
    rng = random.Random(seed)
    names = ["Ann", "Bob", "Cat", "Dee"][:rng.choice((3, 4))]
    rows = []
    for d in rng.sample(WEEK, 4):
        rows.append(row(d, rng.choice(names), "Server", *rng.choice((("11:00am", "3:00pm"), ("5:00pm", "10:00pm")))))
    rows.append(row(rng.choice(WEEK), rng.choice(names), "Server", "4:00pm", "11:00pm"))
    blocked = {names[0].lower(): {rows[0]["date"]: "on approved time off"}} if rng.random() < 0.5 else {}
    c = cons(names, blocked_dates=blocked,
             hours_limits={names[1].lower(): (None, 12.0)} if rng.random() < 0.5 else {})
    c.compliance = dict(sr.DEFAULTS, max_consecutive_days=2)
    sig = {"roster": names, "roster_roles": {n: "Server" for n in names},
           "scores": {n: rng.randint(1, 5) for n in names if rng.random() < 0.8},
           "prior_pattern": {names[-1]: {"days": [DAYS[rng.randrange(7)]], "dayparts": ["night"]}},
           "preferences": {names[0]: {"preferred_dayparts": ["morning"]}},
           "pairs": {"avoid": {frozenset({names[0].lower(), names[1].lower()})}, "prefer": set()},
           "leader_flags": {names[1]: True},
           "leader_rules": [{"role": "Server", "count": 1, "attribute": "can_close", "closing": True}]}
    return rows, c, sig


@pytest.mark.parametrize("seed", range(12))
def test_small_instances_reach_the_brute_force_optimum_and_say_it_is_proved(seed):
    rows, c, sig = _small_instance(seed)
    res = ss.solve(rows, c, signals=sig, max_seconds=5)
    prob = res["problem"]
    live = [u for u in prob.units if not u.fixed]
    best = None
    for combo in itertools.product(range(len(prob.names)), repeat=len(live)):
        a = [u.draft for u in prob.units]
        for u, p in zip(live, combo):
            a[u.id] = p
        if not prob.feasible(a):
            continue
        # the model of the rules agrees with the rule sweep, both ways
        written = ss._rows_for(prob, a)
        assert not _hard(written, c), (seed, combo)
        cost = prob.evaluate(a)
        best = cost if best is None else min(best, cost)
    if best is None:
        assert res["status"] == "infeasible" and res["infeasible"]
        return
    assert res["status"] == "optimal" and res["proved_optimal"]
    assert res["cost"] == pytest.approx(best, abs=1e-6)
    # the incremental bookkeeping and the from-scratch cost agree
    assert prob.evaluate(res["assignment"]) == pytest.approx(res["cost"], abs=1e-6)


def test_every_legal_arrangement_the_sweep_accepts_is_in_the_search_space():
    rows, c, sig = _small_instance(3)
    prob = ss.Problem(rows, c, signals=sig)
    live = [u for u in prob.units if not u.fixed]
    for combo in itertools.product(range(len(prob.names)), repeat=len(live)):
        a = [u.draft for u in prob.units]
        for u, p in zip(live, combo):
            a[u.id] = p
        written = ss._rows_for(prob, a)
        per_date = {}
        for r in written:
            per_date[(r["employee"], r["date"])] = per_date.get((r["employee"], r["date"]), 0) + 1
        if not _hard(written, c) and max(per_date.values()) == 1:
            assert prob.feasible(a), combo


# ── hard rules hold on randomised weeks ─────────────────────────────────────

def _random_week(seed, people=16, per_day=6):
    rng = random.Random(1000 + seed)
    names = [f"P{i}" for i in range(people)]
    roles = {n: rng.choice(("Server", "Server", "Cook", "Bartender")) for n in names}
    rows = []
    for d in WEEK:
        for k in range(per_day):
            role = rng.choice(("Server", "Server", "Cook", "Bartender"))
            pool = [n for n in names if roles[n] == role] or names
            start, end = rng.choice((("10:00am", "3:00pm"), ("4:00pm", "10:00pm"), ("5:00pm", "11:30pm"),
                                     ("11:00am", "7:00pm")))
            rows.append(row(d, rng.choice(pool), role, start, end))
    blocked, unavailable, daypart, windows, certs = {}, {}, {}, {}, {}
    for n in names:
        low = n.lower()
        if rng.random() < 0.3:
            blocked[low] = {rng.choice(WEEK): "on approved time off"}
        if rng.random() < 0.3:
            unavailable[low] = {rng.choice(DAYS)}
        if rng.random() < 0.2:
            daypart[low] = {rng.choice(DAYS): rng.choice(("morning", "night", "off"))}
        if rng.random() < 0.2:
            windows[low] = {rng.choice(DAYS): (sr.parse_minutes("9:00am"), sr.parse_minutes("9:00pm"))}
        if rng.random() < 0.6:
            certs[low] = {"alcohol"}
    minors = {names[0].lower()} if rng.random() < 0.5 else set()
    inactive = {names[1].lower()} if rng.random() < 0.5 else set()
    limits = {names[2].lower(): (None, 20.0)}
    base_hours = {names[3].lower(): {"": 0.0}}
    base_rows = {names[4].lower(): [row("2026-10-04", names[4], "Server", "6:00pm", "11:59pm"),
                                    row("2026-10-03", names[4], "Server", "6:00pm", "11:00pm"),
                                    row("2026-10-02", names[4], "Server", "6:00pm", "11:00pm")]}
    active = {n.lower() for n in names} - inactive
    c = cons(names, blocked_dates=blocked, unavailable_days=unavailable, daypart_avail=daypart,
             time_windows=windows, certifications=certs, role_requirements={"bartender": {"alcohol"}},
             minors=minors, inactive=inactive, active=active, hours_limits=limits, base_hours=base_hours,
             base_rows=base_rows)
    c.compliance = dict(sr.DEFAULTS, max_consecutive_days=5, weekly_hours_ceiling=40)
    sig = {"roster": sorted(set(names) - {names[1]}), "roster_roles": roles,
           "cross_trained": {names[5]: ["Server", "Cook"]},
           "scores": {n: rng.randint(1, 5) for n in names if rng.random() < 0.6}}
    return rows, c, sig


def _breaches(rows, c):
    return {(v["index"], v["kind"]) for v in _hard(rows, c)}


_SOLVED = {}


def _solved(seed):
    if seed not in _SOLVED:
        rows, c, sig = _random_week(seed)
        _SOLVED[seed] = (rows, c, ss.solve(rows, c, signals=sig, max_seconds=0.8))
    return _SOLVED[seed]


@pytest.mark.parametrize("seed", range(10))
def test_no_hard_rule_is_ever_broken_on_randomised_weeks(seed):
    rows, c, res = _solved(seed)
    # the solver never adds a breach of its own, anywhere
    assert _breaches(res["rows"], c) <= _breaches(rows, c)
    kept = set(res["kept_rows"])
    if res["status"] in ("optimal", "time_limit") and not kept:
        assert not _hard(res["rows"], c), _hard(res["rows"], c)[:3]
    # what no legal week can staff is kept as drafted, and says why
    for e in res["infeasible"]:
        assert e["text"] and all(res["rows"][i]["employee"] == rows[i]["employee"] for i in e["rows"])
    # every breach left is on a shift kept as drafted, or on its person
    people = {rows[i]["employee"].lower() for i in kept}
    for v in _hard(res["rows"], c):
        assert v["index"] in kept or (v["employee"] or "").lower() in people, v
    # nothing but who works each row ever changes
    for a, b in zip(rows, res["rows"]):
        assert {k: v for k, v in a.items() if k != "employee"} == {k: v for k, v in b.items() if k != "employee"}


def test_an_impossible_shift_does_not_stop_the_rest_of_the_week_being_solved():
    # these weeks are deliberately short of certified bartenders and cooks
    solved = 0
    for seed in range(10):
        rows, c, res = _solved(seed)
        if res["status"] in ("optimal", "time_limit"):
            solved += 1
            short = {e["role"] for e in res["infeasible"]}
            assert all(rows[i]["role"] in short for i in res["kept_rows"]), seed
            assert len(res["kept_rows"]) < len(rows), seed
    assert solved >= 8, solved


def test_a_role_nobody_is_certified_for_is_reported_with_the_reason():
    names = ["Ann", "Bob"]
    rows = [row(WEEK[5], "Ann", "Bartender"), row(WEEK[4], "Bob", "Server")]
    c = cons(names, role_requirements={"bartender": {"alcohol"}})
    sig = {"roster": names, "roster_roles": {"Ann": "Bartender", "Bob": "Server"}}
    res = ss.solve(rows, c, signals=sig, max_seconds=1)
    [e] = res["infeasible"]
    assert res["kept_rows"] == [0] and res["status"] == "optimal"     # the rest is still solved
    assert e["date"] == WEEK[5] and e["role"] == "Bartender" and e["kept"] == "Ann"
    assert "certification" in e["text"] and "certification" in e["reasons"]["Ann"]
    assert res["rows"][0]["employee"] == "Ann"          # left as drafted, never filled illegally


def test_two_shifts_one_legal_person_is_proved_infeasible():
    names = ["Ann", "Bob"]
    rows = [row(WEEK[5], "Ann", "Bartender", "11:00am", "3:00pm"), row(WEEK[5], "Ann", "Bartender", "3:00pm", "11:00pm")]
    # Bob could, but is off that day: only Ann can work either leg, and a
    # new double is not the solver's to make — the draft's own double is
    # kept whole as one unit, so it is the split legs that cannot both go.
    c = cons(names, blocked_dates={"bob": {WEEK[5]: "on approved time off"}})
    rows[1]["employee"] = "Bob"
    sig = {"roster": names, "roster_roles": {"Ann": "Bartender", "Bob": "Bartender"}}
    res = ss.solve(rows, c, signals=sig, max_seconds=2)
    assert res["infeasible"] and res["kept_rows"]
    assert {e["date"] for e in res["infeasible"]} == {WEEK[5]}
    assert all(e["text"] for e in res["infeasible"])


def test_the_time_limit_is_respected_on_a_big_week():
    rng = random.Random(5)
    names = [f"P{i}" for i in range(45)]
    roles = {n: ("Server" if i < 18 else "Cook" if i < 32 else "Bartender" if i < 38 else "Host")
             for i, n in enumerate(names)}
    rows = []
    for d in WEEK:
        for role, k in (("Server", 12), ("Cook", 10), ("Bartender", 4), ("Host", 4)):
            pool = [n for n in names if roles[n] == role]
            for j in range(k):
                rows.append(row(d, pool[(j + WEEK.index(d)) % len(pool)], role,
                                *(("10:30am", "3:00pm") if j % 2 else ("4:30pm", "10:30pm"))))
    sig = {"roster": names, "roster_roles": roles, "scores": {n: rng.randint(1, 5) for n in names}}
    t0 = time.monotonic()
    res = ss.solve(rows, cons(names), signals=sig, max_seconds=1.0)
    took = time.monotonic() - t0
    assert len(rows) == 210 and took < 1.0 + 0.75, took
    assert res["status"] in ("optimal", "time_limit") and not _hard(res["rows"], cons(names))


def test_interchangeable_shifts_do_not_read_as_changes():
    names = ["Ann", "Bob"]
    rows = [row(WEEK[5], "Ann"), row(WEEK[5], "Bob")]
    sig = {"roster": names, "roster_roles": {n: "Server" for n in names}}
    res = ss.solve(rows, cons(names), signals=sig)
    assert [r["employee"] for r in res["rows"]] == ["Ann", "Bob"]


def test_someone_with_a_written_note_is_left_where_the_draft_put_them():
    names = ["Ann", "Bob", "Cat"]
    rows = [row(WEEK[5], "Ann"), row(WEEK[1], "Cat")]
    sig = {"roster": names, "roster_roles": {n: "Server" for n in names}, "scores": {"Ann": 1, "Bob": 5, "Cat": 1},
           "constraints": {"Ann": "only Saturdays, never before 5"}}
    res = ss.solve(rows, cons(names), signals=sig)
    assert res["rows"][0]["employee"] == "Ann"
    assert res["rows"][1]["employee"] == "Bob"


# ── the judge: keep the solver's week only when it scores higher ───────────

def _judge_case():
    names = ["Ann", "Bob", "Cat", "Dee"]
    rows = [row(WEEK[5], "Ann"), row(WEEK[5], "Bob"), row(WEEK[1], "Cat"), row(WEEK[1], "Dee")]
    sig = {"roster": names, "roster_roles": {n: "Server" for n in names},
           "scores": {"Ann": 2, "Bob": 2, "Cat": 5, "Dee": 4},
           "typical_headcount": {("Saturday", "night"): {"Server": 2}, ("Tuesday", "night"): {"Server": 2}}}
    return rows, names, sig


def test_the_better_week_is_kept_and_every_change_says_why():
    rows, names, sig = _judge_case()
    before = [dict(r) for r in rows]
    out = ss.improve(rows, {}, signals=sig, constraints=cons(names))
    assert rows == before                                   # the input is never modified
    assert out["applied"] and out["after_score"] > out["before_score"]
    head, *lines = out["changes"]
    assert head["kind"] == "solve" and head["gain"] > 0
    assert lines and all(c["kind"] == "reassign" and " — " in c["reason"] and c["reason"].endswith(".") for c in lines)
    sat = sorted(r["employee"] for r in out["rows"] if r["date"] == WEEK[5])
    assert sat == ["Cat", "Dee"]
    assert all(r["notes"].startswith(ss.NOTE_TAG) for r in out["rows"] if r["date"] == WEEK[5])


def test_a_draft_that_is_already_best_is_kept_untouched():
    rows, names, sig = _judge_case()
    first = ss.improve(rows, {}, signals=sig, constraints=cons(names))
    best = [dict(r, notes="") for r in first["rows"]]
    again = ss.improve(best, {}, signals=sig, constraints=cons(names))
    assert not again["applied"] and again["rows"] == best and not again["changes"]
    assert again["stats"]["kept"] == "draft"


def test_a_higher_score_that_breaks_a_hard_rule_is_refused(monkeypatch):
    rows, names, sig = _judge_case()
    c = cons(names, blocked_dates={"cat": {WEEK[5]: "on approved time off"}})
    illegal = [dict(rows[0], employee="Cat"), dict(rows[1], employee="Dee"), rows[2], rows[3]]
    real = ss.solve

    def rigged(*a, **k):
        out = real(*a, **k)
        out["candidates"] = [illegal]
        return out
    monkeypatch.setattr(ss, "solve", rigged)
    out = ss.improve(rows, {}, signals=sig, constraints=c)
    assert not out["applied"] and out["rows"] == rows


def test_the_solver_leads_what_cavnar_changed_and_carries_its_stats():
    rows, names, sig = _judge_case()
    s = ss.summary(ss.improve(rows, {}, signals=sig, constraints=cons(names)))
    opt = {"ran": True, "applied": False, "before_score": 100, "after_score": 100, "changes": [], "unresolved": []}
    merged = ss.merge_into_optimizer(opt, s)
    assert merged["applied"] and merged["changes"][0]["kind"] == "solve"
    assert merged["before_score"] == s["before_score"] and merged["after_score"] == 100
    assert merged["solver"]["status"] in ("optimal", "time_limit") and "proved_optimal" in merged["solver"]
    # a solver that changed nothing leaves the optimizer's own summary alone
    kept = ss.merge_into_optimizer(opt, dict(s, applied=False, changes=[]))
    assert kept["changes"] == [] and kept["before_score"] == 100 and kept["solver"]["applied"] is False
