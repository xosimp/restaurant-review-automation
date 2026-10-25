"""The default assignment engine, as a behaviour spec (blind re-audit 10/11/26
ARCH-19): the solver-first draft (schedule_shapes, then schedule_cpsat) and
the repair loop's CP-SAT stage, on random synthetic weeks — whatever the
roster, availability, time off, maximums and minors, a week from either
breaks no legal rule (schedule_rules.violations is the oracle), keeps every
locked and pinned row exactly, puts a manager on before it fills a floor,
says why it chose each person, and is the same week on the same inputs.
schedule_engine._solver_first_rows — the generation's "solver" source — is
held to the same.

Organised by behaviour, not by audit round: legality, determinism, locks,
tiers, reasons, the generation's entry point. TESTING.md maps the rest of
the schedule suite the same way.
"""
import random

import pytest

import schedule_cpsat as cs
import schedule_engine as se
import schedule_rules as sr
import shift_quality as sq
from scripts import schedule_solver_bench as bench

pytestmark = pytest.mark.skipif(not cs.available(), reason="OR-Tools not installed")

SECONDS = 4.0


def _legal(rows, c):
    return [v for v in sr.violations(rows, c) if v.get("hard") and sr.tier_of(v) == sr.TIER_LEGAL]


def _sig(rows):
    return sorted((r.get("date"), r.get("employee"), r.get("role"), r.get("shift_start"), r.get("shift_end"))
                  for r in rows)


def _draft(s, seconds=SECONDS, context=None):
    return cs.solver_draft(bench.WEEK, s["constraints"], s["requirements"], concurrent=s["concurrent"],
                           history_rows=s["last_week"], context_rows=s["plan"] if context is None else context,
                           last_week_rows=s["last_week"], signals=s["signals"], roster_roles=s["roster_roles"],
                           budget_dollars=s["budget_dollars"], max_seconds=seconds)


_CASES = [(20, 1), (20, 2), (30, 3), (36, 4)]


@pytest.fixture(scope="module")
def drafts():
    """Each random restaurant drafted twice (the determinism check reads the
    pair) — built once for the module."""
    out = {}
    for size, seed in _CASES:
        s = bench.synthetic(size, seed)
        out[(size, seed)] = (s, _draft(s), _draft(s))
    return out


# ── legality ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", _CASES)
def test_a_solver_first_week_breaks_no_legal_rule_on_random_restaurants(drafts, case):
    s, got, _again = drafts[case]
    c = s["constraints"]
    rows = list(s["plan"]) + got["rows"]
    assert got["rows"], got["stats"]
    assert not _legal(rows, c), _legal(rows, c)[:3]
    # nobody twice on one date, nobody off the roster, nobody on a blocked date
    seen = set()
    for r in got["rows"]:
        k = c.key(r["employee"])
        assert k in c.active and (k, r["date"]) not in seen
        assert r["date"] not in (c.blocked_dates.get(k) or {})
        seen.add((k, r["date"]))
    for r in got["rows"]:
        k = c.key(r["employee"])
        if k in c.minors:
            assert not [v for v in sr.violations([r], c, person_only=True) if v.get("hard")]


@pytest.mark.parametrize("case", _CASES)
def test_the_unfilled_shifts_are_said_never_filled_illegally(drafts, case):
    s, got, _again = drafts[case]
    for u in got["unassigned"]:
        assert u["date"] in bench.WEEK and u["text"] and u["role"]


# ── determinism ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", _CASES)
def test_same_inputs_same_week_from_the_solver_first_draft(drafts, case):
    _s, got, again = drafts[case]
    # The promise holds when the deterministic work budget ended both solves.
    # When the wall clock (the emergency cap) cut one short, as on a loaded
    # CI runner, the draft says so in its stats; that is the contract, not a
    # difference to fail on (CI 10/22-10/24/26 failed a different case each run).
    cut = [k for k in ("wall_capped", "memory_capped")
           if (got.get("stats") or {}).get(k) or (again.get("stats") or {}).get(k)]
    if cut:
        pytest.skip(f"a solve was cut by the {cut[0].split('_')[0]} cap, and says so in its stats")
    a, b = _sig(got["rows"]), _sig(again["rows"])
    if a != b:
        # What differed, compactly, so a CI failure names its cause: the rows
        # each draft alone has, and every stage's status, budget and how it ran.
        def _stages(st):
            st = st or {}
            return {k: (v.get("status"), v.get("budget"), (v.get("memory") or {}).get("isolated"),
                        (v.get("memory") or {}).get("fallback"), v.get("wall_capped"))
                    for k, v in st.items() if isinstance(v, dict) and "status" in v} | \
                {"top": (st.get("status"), st.get("workers"), st.get("budget"))}
        only_a, only_b = sorted(set(a) - set(b))[:4], sorted(set(b) - set(a))[:4]
        pytest.fail(f"{len(set(a) ^ set(b))} rows differ; first only {only_a}; second only {only_b}; "
                    f"stages {_stages(got.get('stats'))} vs {_stages(again.get('stats'))}")
    assert [(u["date"], u["role"], u["shift_start"]) for u in got["unassigned"]] == \
        [(u["date"], u["role"], u["shift_start"]) for u in again["unassigned"]]


def _perturbed(s, rows, seed):
    """The draft with a quarter of its shifts handed to somebody else of the
    role — what the repair loop's solver stage is asked to improve."""
    rng = random.Random(seed)
    out = [dict(r) for r in rows]
    by_role = {}
    for n, role in s["roster_roles"].items():
        by_role.setdefault(role, []).append(n)
    for r in out:
        if not r.get("_pinned") and rng.random() < 0.25:
            r["employee"] = rng.choice(by_role.get(r["role"]) or [r["employee"]])
    return out


@pytest.mark.parametrize("case", _CASES[:2])
def test_improve_never_adds_a_legal_breach_and_is_deterministic(drafts, case):
    s, got, _again = drafts[case]
    c = s["constraints"]
    plan = [dict(r, _pinned=True) for r in s["plan"]]
    week = plan + _perturbed(s, got["rows"], case[1])
    before = {(v["kind"], v.get("employee"), v.get("date")) for v in _legal(week, c)}
    inputs = {"roster_roles": s["roster_roles"], "labor_budget_dollars": s["budget_dollars"]}
    a = cs.improve(week, inputs, signals=s["signals"], constraints=c, max_seconds=SECONDS)
    b = cs.improve(week, inputs, signals=s["signals"], constraints=c, max_seconds=SECONDS)
    after = {(v["kind"], v.get("employee"), v.get("date")) for v in _legal(a["rows"], c)}
    assert after <= before, after - before
    assert _sig(a["rows"]) == _sig(b["rows"]) and a["applied"] == b["applied"]
    # the plan's rows are kept exactly, whatever improve did
    for p, r in zip(plan, a["rows"][:len(plan)]):
        assert (r["employee"], r["shift_start"], r["shift_end"]) == (p["employee"], p["shift_start"], p["shift_end"])


# ── locks ─────────────────────────────────────────────────────────────────

def test_the_manager_plan_and_a_locked_row_are_held_exactly_by_the_draft(drafts):
    s, _got, _again = drafts[_CASES[0]]
    server = next(n for n, r in s["roster_roles"].items() if r == "Server")
    lock = bench._row(bench.WEEK[2], server, "Server", 11 * 60, 15 * 60, locked=True)
    got = _draft(s, context=list(s["plan"]) + [lock])
    rows = list(s["plan"]) + [lock] + got["rows"]
    assert not [r for r in got["rows"] if r is lock]           # context rows are not returned as new shifts
    # nobody (the locked person included) is double-booked against the lock
    same = [r for r in got["rows"] if r["date"] == lock["date"] and r["employee"] == server]
    assert not same
    assert not _legal(rows, s["constraints"])


def test_every_mode_keeps_a_locked_row_and_a_pinned_row():
    names = ["Kim", "Lee", "Ana"]
    roles = {n: "Server" for n in names}
    c = sr.Constraints(restaurant_id=1, week_dates=list(bench.WEEK), week_days=[sq._day_name(d) for d in bench.WEEK],
                       roster_names=names, active={n.lower() for n in names})
    c.compliance = dict(sr.DEFAULTS)
    c.known_roles = {n.lower(): {"server"} for n in names}
    rows = [bench._row(bench.WEEK[0], "Kim", "Server", 17 * 60, 22 * 60, locked=True),
            bench._row(bench.WEEK[0], "Lee", "Server", 11 * 60, 16 * 60, _pinned=True),
            bench._row(bench.WEEK[1], "Ana", "Server", 17 * 60, 22 * 60)]
    sig = {"roster": names, "roster_roles": roles, "overtime": {"rates": {"server": 10.0}, "line": 40.0}}
    for mode in ("draft", "improve", "resolve"):
        res = cs.assign([dict(r) for r in rows], c, signals=sig, max_seconds=2, mode=mode)
        assert res["rows"][0]["employee"] == "Kim" and res["rows"][0].get("locked") is True, mode
        assert res["rows"][1]["employee"] == "Lee", mode


# ── the tiers ─────────────────────────────────────────────────────────────

def test_a_manager_on_the_floor_outranks_a_floor_shift():
    """Max is the only manager and may also serve; Ann serves dinner. Max
    can work one shift that day: the lunch server shift a floor needs, or
    the dinner manager shift. On lunch, Ann's dinner has no manager — the
    manager rule outranks the floor, so Max manages dinner and the floor
    shift is left unfilled and said."""
    names = ["Max", "Ann"]
    roles = {"Max": "Manager", "Ann": "Server"}
    d = bench.WEEK[0]
    c = sr.Constraints(restaurant_id=1, week_dates=list(bench.WEEK), week_days=[sq._day_name(x) for x in bench.WEEK],
                       roster_names=names, active={"max", "ann"})
    c.compliance = dict(sr.DEFAULTS)
    c.known_roles = {"max": {"manager", "server"}, "ann": {"server"}}
    c.managers = {"max": "Manager"}
    rows = [bench._row(d, "Ann", "Server", 16 * 60, 22 * 60, _pinned=True),
            bench._row(d, "", "Manager", 16 * 60, 22 * 60),
            dict(bench._row(d, "", "Server", 11 * 60, 15 * 60), _floor=True)]
    sig = {"roster": names, "roster_roles": roles, "overtime": {"rates": {"manager": 25.0, "server": 10.0},
                                                                "line": 40.0}}
    res = cs.assign(rows, c, signals=sig, max_seconds=3)
    on = {(r["role"], r["employee"]) for r in res["rows"]}
    assert ("Manager", "Max") in on, on
    assert not sr.manager_gaps(res["rows"], c)
    assert res["unfilled"] and res["unfilled"][0]["floor"] is True


def test_no_tier_buys_a_legal_breach():
    """A manager gap (the highest soft tier) is left — and said — rather
    than filled by the only manager on their approved time off."""
    names = ["Max", "Ann"]
    d = bench.WEEK[0]
    c = sr.Constraints(restaurant_id=1, week_dates=list(bench.WEEK), week_days=[sq._day_name(x) for x in bench.WEEK],
                       roster_names=names, active={"max", "ann"})
    c.compliance = dict(sr.DEFAULTS)
    c.known_roles = {"max": {"manager"}, "ann": {"server"}}
    c.managers = {"max": "Manager"}
    c.blocked_dates = {"max": {d: "on approved time off"}}
    rows = [bench._row(d, "Ann", "Server", 16 * 60, 22 * 60, _pinned=True), bench._row(d, "", "Manager", 16 * 60,
                                                                                       22 * 60)]
    res = cs.assign(rows, c, signals={"roster": names, "roster_roles": {"Max": "Manager", "Ann": "Server"}},
                    max_seconds=3)
    assert not [r for r in res["rows"] if r["employee"] == "Max"]
    assert not _legal(res["rows"], c)


# ── reasons ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", _CASES[:2])
def test_every_staffed_shift_says_why_its_person(drafts, case):
    _s, got, _again = drafts[case]
    reasons = {(x["date"], x["shift_start"], x["employee"]) for x in got["reasons"]}
    for r in got["rows"]:
        assert (r["date"], r["shift_start"], r["employee"]) in reasons, r
    for x in got["reasons"]:
        assert x["employee"] and str(x["text"]).startswith(x["employee"])


# ── the generation's entry point (schedule_engine._solver_first_rows) ─────

def _entry(s, monkeypatch, **kw):
    monkeypatch.setattr(se, "_staffing_value", lambda rid: {})
    facts = {"concurrent": s["concurrent"], "requirements": s["requirements"],
             "labor_budget_dollars": s["budget_dollars"]}
    base = {"source": "published", "rows": s["last_week"]}
    return se._solver_first_rows(None, list(bench.WEEK), facts, s["constraints"], base, s["last_week"], s["plan"],
                                 s["signals"], None, s["roster_roles"], SECONDS, [], {"week_dates": bench.WEEK},
                                 **kw)


def test_the_generations_solver_source_drafts_legally_around_the_plan(drafts, monkeypatch):
    s, got, _again = drafts[_CASES[0]]
    out = _entry(s, monkeypatch)
    assert out["stats"]["engine"] == "shapes+cp-sat"
    rows = list(s["plan"]) + out["rows"]
    assert not _legal(rows, s["constraints"])
    plan_keys = {(r["date"], r["employee"]) for r in s["plan"]}
    assert not plan_keys & {(r["date"], r["employee"]) for r in out["rows"] if r["role"] == "Manager"
                            and (r["date"], r["employee"]) in plan_keys and r not in s["plan"]}
    assert _sig(out["rows"]) == _sig(_entry(s, monkeypatch)["rows"])


def test_the_generations_solver_source_steps_aside_without_or_tools_or_on_an_error(drafts, monkeypatch):
    s, _got, _again = drafts[_CASES[0]]
    monkeypatch.setattr(cs, "available", lambda: False)
    assert _entry(s, monkeypatch) is None                        # the v0 code draft runs instead
    monkeypatch.setattr(cs, "available", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("solver blew up")
    monkeypatch.setattr(cs, "solver_draft", boom)
    assert _entry(s, monkeypatch) is None                        # never costs the generation


# ── Stage 1 is deterministic too ──────────────────────────────────────────

def test_which_of_two_equal_shifts_a_floor_marks_is_the_earlier_one_never_a_memory_address():
    """schedule_shapes._mark_floor broke ties by id(row): the floor flag
    landed on a different one of two identical shifts run to run, so Stage
    2 priced a different shift at the coverage tier and the same inputs
    drafted different weeks (found by the determinism test above)."""
    from collections import defaultdict
    import schedule_shapes as shp
    d = bench.WEEK[0]
    day = sq._day_name(d)
    for perm in range(12):
        rows = [{"date": d, "role": "Server", "shift_start": "5:00pm", "shift_end": "10:00pm", "k": i}
                for i in range(4)]
        random.Random(perm).shuffle(rows)
        rows = [dict(r) for r in rows]                 # fresh objects, addresses in no particular order
        order = [r["k"] for r in rows]
        shp._mark_floor(rows, {(d, "server", "night"): (2, "Server")}, lambda role: str(role).lower(),
                        {day: "11:00am"}, {day: "10:00pm"}, defaultdict(lambda: defaultdict(int)))
        assert [r["k"] for r in rows if r.get("_floor")] == order[:2], perm
