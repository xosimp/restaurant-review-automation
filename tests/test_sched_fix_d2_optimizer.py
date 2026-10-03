"""The Shift Quality repair loop after the 10/3/26 schedule fix round
(workstream D2): a move is held to the week as it stands by breach identity
(E-1, P-14), every add or hand-over asks the person-level rules (P-2), no
move creates overtime, the budget is hourly hours (P-6), each trial is
scored with its own breaches (P-28), labor dollars count (P-32), the
what-if's swaps are taken inside the search and checked with every rule
(P-33), the six dimensions that had no move have one (SQ-19), and what the
restaurant's scheduling memory holds is kept (L-3, D-35, L-16).
"""
import schedule_optimizer as so
import schedule_rules as sr
import shift_quality as sq
from sched_d2_week import WEEK, DAYS, big_week, row

MON, TUE, WED, THU, FRI, SAT, SUN = WEEK


def cons(names, **kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=list(names),
                       active={n.lower() for n in names})
    c.compliance = dict(sr.DEFAULTS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _sig(names, role="Server", **kw):
    out = {"roster": list(names), "roster_roles": {n: role for n in names}}
    out.update(kw)
    return out


def _on(rows, name, date):
    return [r for r in rows if r["employee"] == name and r["date"] == date]


# ── E-1 / P-14: breaches compared by identity, against the week as it stands ──

def _scripted(monkeypatch, moves_for, values):
    """The search over scripted moves, each worth what `values` says: what
    stops a move is then only the rule check."""
    monkeypatch.setattr(so, "_problems", lambda q: [(1.0, {"date": TUE, "daypart": "night", "day": "Tuesday"},
                                                     {"key": "scripted", "facts": {}})])
    monkeypatch.setattr(so, "_learned_problems", lambda q: [])
    monkeypatch.setattr(so, "_moves_for", lambda problem, state: moves_for(state))

    def value(q, rs, *a, **k):
        return float(sum(values.get(t, 0) for r in rs for t in values if t in (r.get("notes") or "")))
    monkeypatch.setattr(so, "week_value", value)


def test_a_move_may_not_trade_one_breach_for_another_nor_bring_back_one_it_fixed(monkeypatch):
    """The guard counted hard breaches against the draft's count, set once:
    a move could swap Ann's time off for Bob's, and once a breach was fixed
    a later move could bring it back "within the count" (P-14)."""
    names = ["Ann", "Bob", "Cat"]
    c = cons(names, blocked_dates={"ann": {TUE: "on approved time off"}, "bob": {WED: "on approved time off"}})
    rows = [row(TUE, "Ann", "4:00pm", "10:00pm"), row(WED, "Cat", "4:00pm", "10:00pm"),
            row(THU, "Bob", "4:00pm", "10:00pm")]

    def moves_for(state):
        rs = state.rows
        out = []
        tue = next((i for i, r in enumerate(rs) if r["date"] == TUE and r["employee"] == "Ann"), None)
        wed = next(i for i, r in enumerate(rs) if r["date"] == WED)
        if tue is not None:
            def trade(x, tue=tue, wed=wed):
                x = [dict(r) for r in x]
                x[tue].update(employee="Cat", notes="TRADE")
                x[wed].update(employee="Bob", notes="TRADE")
                return x

            def fix(x, tue=tue):
                x = [dict(r) for r in x]
                x[tue].update(employee="Cat", notes="FIX")
                return x
            out += [(("trade",), "trade", trade), (("fix",), "fix", fix)]
        else:
            def back(x):
                return [dict(r) for r in x] + [dict(row(TUE, "Ann", "5:00pm", "10:00pm"), notes="BACK")]
            out.append((("back",), "back", back))
        return out
    _scripted(monkeypatch, moves_for, {"TRADE": 5.0, "FIX": 1.0, "BACK": 3.0})
    res = so.optimize(rows, {}, signals=_sig(names, typical_headcount={("Tuesday", "night"): {"Server": 1}}),
                      constraints=c, target=101, what_if=False)
    assert [ch["kind"] for ch in res["changes"]] == ["fix"]
    assert not _on(res["rows"], "Ann", TUE) and not _on(res["rows"], "Bob", WED)
    assert not [v for v in sr.violations(res["rows"], c) if v.get("hard")]


# ── P-2: whoever gains a shift is legal for it and choosable by code ─────────

def _short_tuesday(names, extra=(), **kw):
    """Tuesday dinner needs three servers and has one; the others work on
    other days."""
    rows = [row(TUE, names[0], "4:00pm", "10:00pm")] + [row(MON, n, "4:00pm", "10:00pm") for n in names[1:]]
    rows += list(extra)
    sig = _sig(names, typical_headcount={("Tuesday", "night"): {"Server": 3}, ("Monday", "night"): {"Server": 3}},
               **kw)
    return rows, sig


def test_code_never_adds_or_hands_a_shift_to_somebody_it_may_not_choose():
    """A dormant person and somebody whose own note covers Tuesday are on
    the roster and free; the old add move asked only the swap index."""
    names = ["Ann", "Old", "Noted", "Kim"]
    c = cons(names, dormant={"old": "2026-08-01"},
             note_caution={"noted": {"days": {"Tuesday"}, "dates": set(), "text": "no Tuesdays this month"}})
    rows, sig = _short_tuesday(names)
    res = so.optimize(rows, {}, signals=sig, constraints=c)
    tue = {r["employee"] for r in res["rows"] if r["date"] == TUE}
    assert "Kim" in tue and not tue & {"Old", "Noted"}


def test_a_minor_is_never_given_a_shift_past_their_band():
    """The template for Tuesday dinner ends at 11pm; a 14-15 year old may
    not work past 7pm — the add move used to check availability only."""
    names = ["Ann", "Teen", "Kim"]
    c = cons(names, minors={"teen"}, minor_bands={"teen": "14-15"})
    rows = [row(TUE, "Ann", "5:00pm", "11:00pm"), row(MON, "Teen", "3:00pm", "6:00pm"), row(MON, "Kim", "5:00pm", "11:00pm")]
    sig = _sig(names, typical_headcount={("Tuesday", "night"): {"Server": 3}})
    res = so.optimize(rows, {}, signals=sig, constraints=c)
    assert not _on(res["rows"], "Teen", TUE)
    assert not [v for v in sr.violations(res["rows"], c) if v.get("hard") and v.get("employee") == "Teen"]


def test_no_move_creates_overtime_while_a_teammate_has_room():
    """Bob is at 36h; the Tuesday add would take him to 42h. Kim has room:
    she takes it, and nobody is newly past their line."""
    names = ["Ann", "Bob", "Kim"]
    c = cons(names)
    rows = [row(TUE, "Ann", "4:00pm", "10:00pm")]
    rows += [row(d, "Bob", "10:00am", "7:00pm") for d in (MON, WED, THU, FRI)]       # 4 × 9h = 36h
    rows += [row(MON, "Kim", "4:00pm", "10:00pm")]
    sig = _sig(names, typical_headcount={("Tuesday", "night"): {"Server": 2}})
    res = so.optimize(rows, {}, signals=sig, constraints=c)
    assert _on(res["rows"], "Kim", TUE) and not _on(res["rows"], "Bob", TUE)
    assert so.overtime_created(rows, res["rows"], c) == []
    # and with nobody else, nobody: Bob is not pushed past his line for it
    alone = so.optimize([r for r in rows if r["employee"] != "Kim"], {},
                        signals=_sig(["Ann", "Bob"], typical_headcount={("Tuesday", "night"): {"Server": 2}}),
                        constraints=cons(["Ann", "Bob"]))
    assert not _on(alone["rows"], "Bob", TUE)


# ── P-6: the budget ceiling is hourly hours ─────────────────────────────────

def test_the_budget_ceiling_counts_hourly_hours_never_salaried_ones():
    """Erik (salaried) is on the floor 50h; the hourly hours budget is 20h.
    Counting his hours put the week past the budget before anybody was
    added, and every add was refused."""
    names = ["Erik", "Ann", "Kim"]
    c = cons(names, salaried={"erik"})
    rows = [row(d, "Erik", "11:00am", "9:00pm", role="Owner") for d in (MON, TUE, WED, THU, FRI)]
    rows += [row(TUE, "Ann", "4:00pm", "10:00pm"), row(MON, "Kim", "4:00pm", "10:00pm")]
    sig = _sig(names, typical_headcount={("Tuesday", "night"): {"Server": 2}}, salaried={"erik"},
               roster_roles={"Erik": "Owner", "Ann": "Server", "Kim": "Server"})
    assert sr.hourly_hours(rows, c) == 12.0
    res = so.optimize(rows, {}, signals=sig, constraints=c, hours_budget=20)
    assert _on(res["rows"], "Kim", TUE)
    assert sr.hourly_hours(res["rows"], c) <= 20 * so.BUDGET_TOLERANCE + 0.01


# ── P-28: each trial scored with its own breaches ───────────────────────────

def test_a_move_that_fixes_a_breach_is_credited_for_it():
    """Ann is on Tuesday dinner on approved time off: the breach caps the
    shift. The breaches were read once before the search, so handing her
    shift to Kim was scored with the cap still on (P-28). (The budget leaves
    no room for an added shift: the hand-over is the only move.)"""
    names = ["Ann", "Kim"]
    c = cons(names, blocked_dates={"ann": {TUE: "on approved time off"}})
    rows = [row(TUE, "Ann", "4:00pm", "10:00pm"), row(MON, "Kim", "4:00pm", "10:00pm"),
            row(WED, "Ann", "4:00pm", "10:00pm")]
    sig = _sig(names, typical_headcount={(d, "night"): {"Server": 1} for d in ("Monday", "Tuesday", "Wednesday")})
    res = so.optimize(rows, {}, signals=sig, constraints=c, hours_budget=18)
    assert _on(res["rows"], "Kim", TUE) and not _on(res["rows"], "Ann", TUE)
    assert res["after_score"] > res["before_score"]
    tue = next(s for s in res["quality"]["shifts"] if s["date"] == TUE)
    assert not tue.get("hard_breaches")


# ── P-32: labor dollars ──────────────────────────────────────────────────────

def test_of_two_adds_that_buy_the_same_the_cheaper_is_taken():
    """Dear is paid $25/h, Kim $14/h, both unrated servers with the same
    week: the objective had no dollars, so the add went by name."""
    names = ["Ann", "Dear", "Kim"]
    c = cons(names)
    rows = [row(TUE, "Ann", "4:00pm", "10:00pm"), row(MON, "Dear", "4:00pm", "10:00pm"),
            row(MON, "Kim", "4:00pm", "10:00pm")]
    ot = {"line": 40.0, "rates": {"server": 15.0}, "default_rate": 15.0, "person_rates": {"dear": 25.0, "kim": 14.0},
          "bucket_of": {d: c.bucket(d) for d in WEEK}}
    sig = _sig(names, typical_headcount={("Tuesday", "night"): {"Server": 2}, ("Monday", "night"): {"Server": 2}},
               overtime=ot)
    res = so.optimize(rows, {}, signals=sig, constraints=c)
    assert _on(res["rows"], "Kim", TUE) and not _on(res["rows"], "Dear", TUE)
    add = next(ch for ch in res["changes"] if ch["kind"] == "add")
    assert add["dollars"] == 84          # 6h at $14
    s = so.summary(res, sig)
    assert s["dollars_after"] - s["dollars_before"] == 84


def test_labor_dollars_are_priced_per_person_with_overtime_at_its_premium():
    pricing = so.pricing_inputs({"overtime": {"line": 40.0, "rates": {"server": 10.0}, "default_rate": 10.0,
                                              "person_rates": {"ann": 20.0}}}, {}, None)
    rows = [row(d, "Ann", "9:00am", "6:00pm") for d in (MON, TUE, WED, THU, FRI)]        # 45h
    assert so.labor_dollars(rows, pricing) == 45 * 20.0 + 5 * 20.0 * 0.5
    assert so.labor_dollars([row(MON, "Bo", "9:00am", "1:00pm")], pricing) == 40.0
    # a saving earns nothing: the search finishes the draft's quality
    q = {"shifts": [{"scored": True, "score": 80, "profile": {"demand": "normal"}}], "raw_score": 80.0}
    assert so.week_value(q, rows[:1], pricing, so.labor_dollars(rows, pricing)) == 80.0


# ── P-33: the what-if's best swaps taken inside the search, every rule held ──

def test_the_what_if_swap_is_taken_inside_the_search_and_checked_with_every_rule(monkeypatch):
    """Saturday is busy and Bea (5) is on Tuesday, Cal (2) on Saturday —
    the comparison used to report the trade beside "Cavnar optimized"
    instead of the search making it. Here the weak dimensions offer
    nothing, Bea's six-hour maximum rules out her taking Saturday as well,
    and the what-if's swap is what the search takes."""
    monkeypatch.setattr(so, "_problems", lambda q: [])
    names = ["Bea", "Cal", "Max"]
    c = cons(names, managers={"max": "Manager"}, hours_limits={"bea": (None, 6.0)})
    rows = [row(TUE, "Bea", "4:00pm", "10:00pm"), row(SAT, "Cal", "4:00pm", "10:00pm"),
            row(TUE, "Max", "4:00pm", "10:00pm", role="Manager"), row(SAT, "Max", "4:00pm", "10:00pm", role="Manager")]
    sig = _sig(names, scores={"Bea": 5, "Cal": 2, "Max": 3}, demand_by_day={"Saturday": 40.0},
               roster_roles={"Bea": "Server", "Cal": "Server", "Max": "Manager"},
               typical_headcount={("Saturday", "night"): {"Server": 1}, ("Tuesday", "night"): {"Server": 1}})
    assert so.optimize(rows, {}, signals=sig, constraints=c, what_if=False)["changes"] == []
    res = so.optimize(rows, {}, signals=sig, constraints=c)
    assert _on(res["rows"], "Bea", SAT) and _on(res["rows"], "Cal", TUE)
    assert [ch["kind"] for ch in res["changes"]] == ["swap"] and "better arrangement" in res["changes"][0]["reason"]
    after = sq.compare_candidates(res["rows"], rule_constraints=c, **sig)
    assert after["checked_with"] == "every rule" and not after["swaps"]


def test_the_what_if_never_offers_a_swap_a_rule_refuses():
    """Only Dee closes the bar (closers per role). Swapping her Saturday
    close for Ann's Tuesday lunch would score — and leave Saturday with no
    closer, a rule the swap index never knew (P-33)."""
    names = ["Ann", "Dee", "Max"]
    c = cons(names, closers_by_role={"bartender": {"dee"}}, close_times={d: "11:00pm" for d in DAYS})
    rows = [row(SAT, "Dee", "5:00pm", "11:00pm", role="Bartender"), row(TUE, "Ann", "11:00am", "4:00pm", role="Bartender")]
    sig = _sig(names, role="Bartender", scores={"Ann": 5, "Dee": 1}, demand_by_day={"Saturday": 40.0},
               typical_headcount={("Saturday", "night"): {"Bartender": 1}, ("Tuesday", "morning"): {"Bartender": 1}})
    loose = sq.compare_candidates(rows, **sig)
    held = sq.compare_candidates(rows, rule_constraints=c, **sig)
    assert held["checked_with"] == "every rule"
    for r in held["rows"]:
        if r["date"] == SAT:
            assert r["employee"] == "Dee"
    assert loose["checked_with"] == "availability and hours"
    # what the swap index alone offered: Ann on Saturday, nobody closing the bar
    assert any(r["date"] == SAT and r["employee"] == "Ann" for r in loose["rows"])
    assert any(v["kind"] == "keyholder_until_close" and v.get("hard") for v in sr.violations(loose["rows"], c))


def test_the_what_if_never_offers_a_pinned_row():
    names = ["Bea", "Cal"]
    rows = [dict(row(SAT, "Cal", "4:00pm", "10:00pm"), _pinned="manager_plan"), row(TUE, "Bea", "4:00pm", "10:00pm")]
    sig = _sig(names, scores={"Bea": 5, "Cal": 2}, demand_by_day={"Saturday": 40.0},
               typical_headcount={("Saturday", "night"): {"Server": 1}, ("Tuesday", "night"): {"Server": 1}})
    out = sq.compare_candidates(rows, rule_constraints=cons(names), **sig)
    assert out["rows"][0]["employee"] == "Cal" and not out["swaps"]


# ── SQ-19: the six dimensions that had no move ───────────────────────────────

def _problem(dim_key, facts, shift_people, date=TUE, part="night", demand="busy"):
    shift = {"date": date, "daypart": part, "day": sq._day_name(date), "people": list(shift_people),
             "profile": {"demand": demand}, "scored": True, "score": 70, "dimensions": []}
    return (10.0, shift, {"key": dim_key, "score": 50, "weight": 10, "facts": facts})


def _state(rows, sig, c):
    return so._State(rows, sig, {}, constraints=c)


def test_each_of_the_six_dimensions_has_a_move_toward_a_better_person():
    names = ["Ann", "Bob", "Vet", "Rel", "Flex", "Kim"]
    c = cons(names)
    rows = [row(TUE, "Ann", "4:00pm", "10:00pm"), row(TUE, "Bob", "4:00pm", "10:00pm"),
            row(MON, "Vet", "4:00pm", "10:00pm"), row(MON, "Rel", "4:00pm", "10:00pm"),
            row(MON, "Flex", "4:00pm", "10:00pm"), row(MON, "Kim", "4:00pm", "10:00pm")]
    sig = _sig(names, scores={"Ann": 2, "Bob": 2, "Vet": 5, "Kim": 4}, tenure={"Vet": 200, "Ann": 2, "Bob": 3},
               reliability={"Ann": {"no_show_rate": 0.3, "shifts": 20}, "Rel": {"no_show_rate": 0.0, "shifts": 30},
                            "Bob": {"no_show_rate": 0.0, "shifts": 30, "late_risk": True}},
               cross_trained={"Flex": ["Server", "Host"]},
               preferences={"Ann": {"preferred_dayparts": ["morning"]}},
               pairs={"avoid": {frozenset({"ann", "bob"})}, "prefer": set()})
    st = _state(rows, sig, c)
    cases = {
        "demand_match": {"by_role": {"server": {"average": 2.0, "wanted": 4.0}}},
        "experience_balance": {"experienced_share": 0.0, "target_share": 0.5, "veterans": [], "rookies": ["Ann", "Bob"]},
        "reliability": {"exposed": [{"name": "Ann", "role": "Server"}]},
        "pairings": {"clashes": [["Ann", "Bob"]]},
        "preferences": {"strained": ["Ann"]},
        "cross_training": {"by_role": {"Server": {"score": 0}}},
    }
    for key, facts in cases.items():
        moves = so._moves_for(_problem(key, facts, ["Ann", "Bob"]), st)
        assert moves, key
        assert all(m[0][0] in ("replace", "swap", "trade") for m in moves), key


def test_a_rated_person_may_take_an_unrated_persons_shift():
    """Rated↔unrated replacements were refused: the score counted an
    unrated person as 0, so any trade looked like a gain. It judges them as
    unknown now, so the score decides (SQ-19)."""
    names = ["Una", "Kim"]
    c = cons(names)
    rows = [row(TUE, "Una", "4:00pm", "10:00pm"), row(MON, "Kim", "4:00pm", "10:00pm")]
    st = _state(rows, _sig(names, scores={"Kim": 5}), c)
    assert st.can_take("Kim", 0)
    idx = sq._SwapIndex(rows, {}, {}, {})
    assert idx.legal(0, 1, {"Kim": 5})


# ── the hand-offs D1b named: overtime, stations, salaried rows ───────────────

def test_overtime_a_teammate_could_take_is_handed_to_them():
    names = ["Ann", "Kim"]
    c = cons(names)
    rows = [row(d, "Ann", "10:00am", "7:00pm") for d in (MON, TUE, WED, THU, FRI)]           # 45h
    rows += [row(SAT, "Kim", "4:00pm", "10:00pm")]
    st = _state(rows, _sig(names), c)
    facts = {"people": [{"name": "Ann", "bucket": c.bucket(TUE), "overtime_hours": 5.0, "teammate": "Kim"}]}
    moves = so._moves_for(_problem("overtime", facts, ["Ann"], part="morning"), st)
    assert any(m[0][0] == "replace" and m[0][2] == "Kim" for m in moves)


def test_a_salaried_persons_hours_are_never_trimmed_for_the_day_target():
    names = ["Erik", "Ann"]
    c = cons(names, salaried={"erik"})
    rows = [row(TUE, "Erik", "10:00am", "10:00pm", role="Owner"), row(TUE, "Ann", "10:00am", "10:00pm"),
            row(MON, "Ann", "4:00pm", "10:00pm")]
    st = _state(rows, _sig(names, salaried={"erik"}), c)
    moves = so._moves_for(_problem("labor_efficiency", {"ratio": 1.5}, ["Erik", "Ann"]), st)
    assert moves and all(m[0][1] != 0 for m in moves if m[0][0] in ("retime", "remove"))


def test_no_move_touches_a_pinned_row():
    names = ["Max", "Ann", "Kim"]
    c = cons(names, managers={"max": "Manager"})
    rows = [dict(row(TUE, "Max", "4:00pm", "10:00pm", role="Manager"), _pinned="manager_plan"),
            row(TUE, "Ann", "4:00pm", "10:00pm"), row(MON, "Kim", "4:00pm", "10:00pm")]
    sig = _sig(names, scores={"Max": 1, "Ann": 2, "Kim": 5}, demand_by_day={"Tuesday": 40.0},
               roster_roles={"Max": "Server", "Ann": "Server", "Kim": "Server"},
               typical_headcount={("Tuesday", "night"): {"Server": 2}})
    res = so.optimize(rows, {}, signals=sig, constraints=c)
    assert res["rows"][0] == rows[0]


# ── L-3, D-35, L-16: what the scheduling memory holds ────────────────────────

def _memory(kind, person=None, day="Tuesday", daypart="night", role="server", value=None, conf=0.8):
    return {"kind": kind, "key": f"{kind}|{person}|{day}|{daypart}", "person": person, "day": day,
            "daypart": daypart, "role": role, "value": value or {}, "confidence": conf, "enforcement": "soft",
            "source": "by hand"}


def test_the_score_says_what_the_week_breaks_of_the_scheduling_memory():
    names = ["Bob", "Cy"]
    rows = [row(TUE, "Bob", "4:00pm", "10:00pm"), row(MON, "Cy", "4:00pm", "10:00pm")]
    sig = _sig(names, typical_headcount={("Tuesday", "night"): {"Server": 1}, ("Monday", "night"): {"Server": 1}})
    plain = sq.score_rows(rows, **sig)
    assert "learned" not in {d["key"] for d in plain["week_dimensions"]}
    q = sq.score_rows(rows, **dict(sig, learned=[_memory("moved_off", "Bob")]))
    week = {d["key"]: d for d in q["week_dimensions"]}
    assert week["learned"]["score"] == sq.learned_score(0.8) == 79
    assert sq.learned_score(0) == 100 and sq.learned_score(1) == 75
    # never a flat floor: one row put right of a habit missed on many still shows
    assert sq.learned_score(4.0) > sq.learned_score(4.8) > sq.learned_score(9.5) > 0
    assert week["learned"]["weaknesses"] == ["Bob is on Tuesday dinner; your managers keep taking them off it."]
    assert q["raw_score"] < plain["raw_score"]
    kept = sq.score_rows([row(TUE, "Cy", "4:00pm", "10:00pm"), row(MON, "Bob", "4:00pm", "10:00pm")],
                         **dict(sig, learned=[_memory("moved_off", "Bob")]))
    assert {d["key"]: d for d in kept["week_dimensions"]}["learned"]["score"] == 100
    # a memory the prompt holds only is never scored
    assert "learned" not in {d["key"] for d in sq.score_rows(
        rows, **dict(sig, learned=[dict(_memory("moved_off", "Bob"), enforcement="prompt")]))["week_dimensions"]}


def test_the_search_puts_right_what_the_managers_keep_undoing():
    """Bob on Tuesday dinner, which the managers take him off every week;
    Cy, who they keep putting on it, off it."""
    names = ["Bob", "Cy", "Dee"]
    c = cons(names)
    rows = [row(TUE, "Bob", "4:00pm", "10:00pm"), row(TUE, "Dee", "4:00pm", "10:00pm"),
            row(WED, "Cy", "4:00pm", "10:00pm"), row(MON, "Bob", "4:00pm", "10:00pm")]
    learned = [_memory("moved_off", "Bob"), _memory("moved_on", "Cy")]
    sig = _sig(names, learned=learned,
               typical_headcount={(d, "night"): {"Server": 2} for d in ("Monday", "Tuesday", "Wednesday")})
    res = so.optimize(rows, {}, signals=sig, constraints=c)
    assert not _on(res["rows"], "Bob", TUE) and _on(res["rows"], "Cy", TUE)
    assert so.memory_misses(res["quality"]) == []
    # the notes say only that this is how the shift is usually scheduled
    assert all("keep" not in (r.get("notes") or "") for r in res["rows"])


def test_the_usual_opener_trades_into_the_opening_shift():
    names = ["Ana", "Ben"]
    c = cons(names)
    rows = [row(SAT, "Ben", "9:00am", "3:00pm", role="Line Cook"), row(SAT, "Ana", "11:00am", "5:00pm", role="Line Cook")]
    sig = _sig(names, role="Line Cook", learned=[_memory("opener", "Ana", day="Saturday", daypart=None, role="line cook",
                                                         value={"start": "9:00am", "role": "Line Cook"})],
               typical_headcount={("Saturday", "morning"): {"Line Cook": 2}})
    res = so.optimize(rows, {}, signals=sig, constraints=c)
    assert next(r for r in res["rows"] if r["shift_start"] == "9:00am")["employee"] == "Ana"
    assert any(ch["kind"] == "trade" for ch in res["changes"])


def test_a_close_the_memory_says_runs_late_is_never_cut_back():
    """Friday server closes run 30 minutes past their end; the pad made the
    close end at 11pm. A trim for the day's hour target took it back to
    10:30pm — exactly the edit the manager kept undoing (L-16)."""
    names = ["Ann", "Bob", "Cat"]
    c = cons(names)
    rows = [row(FRI, "Ann", "4:00pm", "11:00pm"), row(FRI, "Bob", "4:00pm", "10:00pm"),
            row(FRI, "Cat", "4:00pm", "10:00pm")]
    memory = _memory("end_overrun", None, day="Friday", daypart="night", role="server",
                     value={"minutes": 30, "padded_end": "11:00pm", "role": "Server"})
    sig = _sig(names, learned=[memory], typical_headcount={("Friday", "night"): {"Server": 3}},
               daily_target_hours={FRI: 12.0})
    res = so.optimize(rows, {}, signals=sig, constraints=c)
    ends = sorted(r["shift_end"] for r in res["rows"] if r["date"] == FRI)
    assert "11:00pm" in ends
    assert so._overrun_weight(res["rows"], [memory]) == 0


def test_somebody_who_runs_past_their_shifts_is_kept_their_headroom():
    """Bob usually runs 4h past what he is scheduled: drafted to 36h he
    works 40. No move takes him into that headroom; Kim takes the add."""
    names = ["Ann", "Bob", "Kim"]
    c = cons(names)
    rows = [row(TUE, "Ann", "4:00pm", "10:00pm")]
    rows += [row(d, "Bob", "10:00am", "4:00pm") for d in (MON, WED, THU, FRI)]       # 24h
    rows += [row(MON, "Kim", "10:00am", "4:00pm")]
    memory = _memory("ot_risk", "Bob", day=None, daypart=None, role=None, value={"headroom_hours": 14.0})
    sig = _sig(names, learned=[memory], typical_headcount={("Tuesday", "night"): {"Server": 2}})
    assert so.ot_headroom(sig, c) == {"bob": 14.0}
    trial = rows + [row(TUE, "Bob", "4:00pm", "10:00pm")]          # 30h, past 40 - 14
    assert so.overtime_created(rows, trial, c, headroom=so.ot_headroom(sig, c))
    assert not so.overtime_created(rows, trial, c)
    res = so.optimize(rows, {}, signals=sig, constraints=c)
    assert not _on(res["rows"], "Bob", TUE)


def test_a_realistic_week_keeps_every_rule_and_the_memory():
    """On the realistic week the search improves the score without making
    any breach new or worse, and holds a memory the generic moves break."""
    rows, c, sig = big_week()
    before = sr.breach_profile(rows, c)
    res = so.optimize(rows, {}, signals=sig, constraints=c, max_seconds=8)
    assert res["after_score"] >= res["before_score"]
    assert sr.regressions(before, sr.breach_profile(res["rows"], c), upto=sr.TIER_BUDGET, hard_only=False) == []
    assert so.overtime_created(rows, res["rows"], c) == []
    assert all(any(r == x for x in res["rows"]) for r in rows if r.get("_pinned"))


def test_a_pair_from_an_owner_only_rule_is_kept_apart_and_never_named():
    """F2 holds a private pairing in the score without naming it (D-38);
    the search moves it as well, and its change and the row notes say only
    that the shift is better arranged — the team reads both."""
    names = ["Ann", "Bob", "Kim"]
    c = cons(names)
    rows = [row(TUE, "Ann", "4:00pm", "10:00pm"), row(TUE, "Bob", "4:00pm", "10:00pm"),
            row(WED, "Kim", "4:00pm", "10:00pm")]
    pair = frozenset({"ann", "bob"})
    sig = _sig(names, pairs={"avoid": {pair}, "prefer": set(), "private": {pair}},
               typical_headcount={("Tuesday", "night"): {"Server": 2}, ("Wednesday", "night"): {"Server": 1}})
    res = so.optimize(rows, {}, signals=sig, constraints=c, target=101)
    tue = {r["employee"] for r in res["rows"] if r["date"] == TUE}
    assert not {"Ann", "Bob"} <= tue
    text = " ".join([ch["reason"] for ch in res["changes"]] + [r.get("notes") or "" for r in res["rows"]])
    assert res["changes"] and "apart" not in text and "rule" not in text
