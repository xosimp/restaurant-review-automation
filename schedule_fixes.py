"""
schedule_fixes.py — Shift Quality's two searches over a finished week: the
what-if (`compare_candidates`: the swaps and replacements that raise the
score without making any rule worse) and Apply fixes (`apply_fixes`: each
breach the code can fix, handed to the teammate who costs the week the
least score, with the days-off pass).

Both judge a candidate by the scorer (shift_quality) and keep it only when
the rule sweep says nothing got worse (schedule_rules), so they sit above
both — layer S4 of the schedule modules (ARCHITECTURE_MANIFEST.md §3a).
They lived in shift_quality, which made the pure scorer import the rules.
"""
from shift_quality import (
    MAX_CANDIDATE_EVALUATIONS, MIN_IMPROVEMENT, PERSON_FIXABLE, LocalScorer, _plural, _row_hours, _slot_minutes,
    _SwapIndex, role_family, score_rows)


def _people_by_family(rows: list, roster: list, signals: dict, families: dict = None) -> dict:
    """{family: names} who may take a shift of the family: anybody working
    it this week, the roster by role, held roles and the roles their
    history shows (cross_trained) — "Server AM" and "Server PM" are one
    family (D-13). Without a roster, only the people working the family."""
    out = {}
    roster_low = {n.strip().lower() for n in roster}
    for r in rows:
        n = (r.get("employee") or "").strip()
        if n and (not roster or n.lower() in roster_low):
            out.setdefault(role_family(r.get("role"), families), set()).add(n)
    roles = signals.get("roster_roles") or {}
    cross = signals.get("cross_trained") or {}
    held = {str(k).strip().lower(): v for k, v in (signals.get("held_roles") or {}).items()}
    for n in roster:
        for role in [roles.get(n)] + list(cross.get(n) or []) + list(held.get(n.strip().lower()) or ()):
            if str(role or "").strip():
                out.setdefault(role_family(role, families), set()).add(n)
    return out


def compare_candidates(rows: list, profiles: list = None, weights: dict = None,
                       max_evaluations: int = MAX_CANDIDATE_EVALUATIONS, rule_constraints=None,
                       max_seconds: float = None, **signals) -> dict:
    """Hill-climb same-role swaps and replacements, and report what the
    alternatives cost.

    Returns the winning rows, the baseline and final scores, and one line
    per accepted swap saying which dimensions moved. A run that finds
    nothing is a real result and says so — but only ever about the
    candidates it actually tried, never about the whole space.

    With `rule_constraints` (schedule_rules.Constraints) every candidate is
    held to every rule the week is: the person-level rules for whoever
    gains a shift (Constraints.can_add, the overtime line for the side whose
    hours go up, fillable), then the whole week swept and compared by
    breach identity (schedule_rules.regressions to the budget tier — days
    in a row, the manager every minute, the closer, overtime, minimum
    hours). The swap index alone knew none of the day's rules, so the panel
    could offer an arrangement that broke one (schedule audit 10/3/26 P-33).
    A row the manager plan or the owner pinned is never offered. Rated and
    unrated people may trade: the score judges an unrated person as unknown,
    not as nothing, so it decides (SQ-19); rated pairs, where a gap can be
    seen, are tried first. Candidates are ranked with the local scorer and
    the one taken is confirmed by the full score (P-25: sixty whole-week
    scores per drag). `max_seconds` bounds the search in time as well as in
    evaluations (a generation's deadline); the verdict speaks only of what
    was tried."""
    import time as _time
    t0 = _time.monotonic()
    timed = []

    def out_of_time() -> bool:
        if max_seconds is not None and _time.monotonic() - t0 > max_seconds:
            timed.append(True)
            return True
        return False
    baseline = score_rows(rows, profiles=profiles, weights=weights, **signals)
    if not baseline.get("checked"):
        return {"ran": False, "reason": baseline.get("reason"), "baseline": baseline,
                "best": baseline, "rows": rows, "swaps": [], "evaluated": 0,
                "legal_swaps": 0}

    availability = signals.get("availability") or {}
    constraints = signals.get("constraints") or {}
    rules = signals.get("rules") or {}
    roster = [n for n in (signals.get("roster") or []) if n]
    scores = signals.get("scores") or {}
    families = signals.get("role_families") or getattr(rule_constraints, "role_families", None) or None
    c = rule_constraints
    current_rows = [dict(r) for r in rows]
    current = baseline
    swaps, evaluated = [], 0
    legal_total = 0
    sweep = prof = None
    if c is not None:
        import schedule_rules as _rules
        sweep = _rules.IncrementalSweep(c)
        prof = _rules.breach_profile(current_rows, c, viols=sweep.violations(current_rows))
    scorer = LocalScorer(current_rows, profiles=profiles, weights=weights, **signals)
    pool_of = _people_by_family(current_rows, roster, signals, families)

    def _sc(n):
        return scores.get((n or "").strip())

    def legal(trial, gainers):
        """(flagged, hard breaches, profile) of a trial every rule allows;
        False when a rule refuses it; None with no rule set to ask."""
        if c is None:
            return None
        for name, i, up in gainers:
            row = trial[i]
            if not c.fillable(name, row.get("date") or "")[0] or not c.holds(name, row.get("role") or "", row.get("date")):
                return False
            if not c.can_add(row, trial, overtime=up)[0]:
                return False
        viols = sweep.violations(trial)
        after = _rules.breach_profile(trial, c, viols=viols)
        if _rules.regressions(prof, after, upto=_rules.TIER_BUDGET, hard_only=False):
            return False
        return ({((v.get("employee") or "").strip().lower(), v.get("date") or "", v.get("shift_start") or "")
                 for v in viols if v.get("no_show")}, [v for v in viols if v.get("hard")], after)

    improved = True
    while improved and evaluated < max_evaluations and not out_of_time():
        improved = False
        index = _SwapIndex(current_rows, availability, constraints, rules)
        # Rated pairs first, the widest rating gap first: that is where an
        # improvement can be seen. Unrated pairs after, in order.
        pinned = {i for i, r in enumerate(current_rows) if r.get("_pinned")}
        by_family = {}
        for i, r in enumerate(current_rows):
            if (r.get("employee") or "").strip() and i not in pinned:
                by_family.setdefault(role_family(r.get("role"), families), []).append(i)
        ranked = []
        legal_here = 0
        for idxs in by_family.values():
            for a in range(len(idxs)):
                for b in range(a + 1, len(idxs)):
                    i, j = idxs[a], idxs[b]
                    if not index.legal(i, j, None):
                        continue
                    legal_here += 1
                    si, sj = _sc(current_rows[i].get("employee")), _sc(current_rows[j].get("employee"))
                    ranked.append(((0, -abs(float(si) - float(sj))) if si is not None and sj is not None else (1, 0.0),
                                   i, j))
        # A second kind of move: somebody who works the role's family and is
        # not on that date takes the row outright.
        for i, row in enumerate(current_rows) if roster else []:
            if i in pinned:
                continue
            cur = (row.get("employee") or "").strip()
            for name in sorted(pool_of.get(role_family(row.get("role"), families)) or ()):
                if name == cur or not index.replacement_legal(i, name):
                    continue
                legal_here += 1
                sc, sn = _sc(cur), _sc(name)
                ranked.append(((0, -abs(float(sc) - float(sn))) if sc is not None and sn is not None else (1, 0.0),
                               i, ("replace", name)))
        legal_total = max(legal_total, legal_here)
        ranked.sort(key=lambda t: t[0])

        for _prio, i, j in ranked:
            if evaluated >= max_evaluations or out_of_time():
                break
            trial = list(current_rows)
            if isinstance(j, tuple):
                trial[i] = dict(current_rows[i], employee=j[1])
                gainers = [(j[1], i, True)]
            else:
                a, b = current_rows[i], current_rows[j]
                trial[i], trial[j] = dict(a, employee=b["employee"]), dict(b, employee=a["employee"])
                up_b = _row_hours(a) > _row_hours(b)
                gainers = [((b["employee"] or "").strip(), i, up_b), ((a["employee"] or "").strip(), j, not up_b)]
            judged = legal(trial, gainers)
            if judged is False:
                continue
            fl, hb = (judged[0], judged[1]) if judged is not None else (None, None)
            evaluated += 1
            try:
                quick = scorer.score(trial, flagged=fl, hard_breaches=hb)
            except Exception:
                continue
            if quick - float(current.get("raw_score") or current["score"]) < MIN_IMPROVEMENT - 0.5:
                continue
            sig = dict(signals, flagged=fl, hard_breaches=hb) if judged is not None else signals
            candidate = score_rows(trial, profiles=profiles, weights=weights, **sig)
            if not candidate.get("checked"):
                continue
            gain = candidate["score"] - current["score"]
            if gain >= MIN_IMPROVEMENT:
                if isinstance(j, tuple):
                    swaps.append(_describe_replacement(current_rows[i], j[1], current, candidate, gain))
                else:
                    swaps.append(_describe_swap(current_rows[i], current_rows[j],
                                                current, candidate, gain))
                current_rows, current = trial, candidate
                if judged is not None:
                    prof = judged[2]
                improved = True
                break

    return {
        "ran": True,
        "evaluated": evaluated,
        "legal_swaps": legal_total,
        "baseline_score": baseline["score"],
        "best_score": current["score"],
        "improvement": current["score"] - baseline["score"],
        "swaps": swaps,
        "rows": current_rows,
        "baseline": baseline,
        "best": current,
        # Whether each swap was held to every rule the week is, or only to the
        # swap index's person checks (no rule set given).
        "checked_with": "every rule" if c is not None else "availability and hours",
        # The clock (max_seconds — a request thread's bound, blind re-audit
        # 10/11/26 SCALE-21), not the evaluations, ended the search: the
        # verdict speaks only of what was tried.
        "cut_short": bool(timed),
        "seconds": round(_time.monotonic() - t0, 2),
        "verdict": _candidate_verdict(baseline, current, swaps, evaluated, legal_total)
        + (" The search stopped at its time limit." if timed else ""),
    }


def _describe_swap(row_a: dict, row_b: dict, before: dict, after: dict, gain: int) -> dict:
    """Which two people traded shifts, and which dimensions it moved."""
    moved = []
    before_dims = {d["key"]: d["score"] for d in before.get("dimensions") or []}
    for d in after.get("dimensions") or []:
        delta = d["score"] - before_dims.get(d["key"], d["score"])
        if delta:
            moved.append(f"{d['label']} {delta:+d}")
    return {
        "from": {"employee": row_a.get("employee"), "date": row_a.get("date"),
                 "day": row_a.get("day"), "role": row_a.get("role")},
        "to": {"employee": row_b.get("employee"), "date": row_b.get("date"),
               "day": row_b.get("day"), "role": row_b.get("role")},
        "gain": gain,
        "moved": moved,
        "reason": (f"Swapped {row_a.get('employee')} and {row_b.get('employee')} between "
                   f"{row_a.get('day') or row_a.get('date')} and "
                   f"{row_b.get('day') or row_b.get('date')} "
                   f"({row_a.get('role')}) — overall {gain:+d}"
                   + (", " + ", ".join(moved) if moved else "") + "."),
    }


def _describe_replacement(row: dict, name: str, before: dict, after: dict, gain: int) -> dict:
    moved = []
    before_dims = {d["key"]: d["score"] for d in before.get("dimensions") or []}
    for d in after.get("dimensions") or []:
        delta = d["score"] - before_dims.get(d["key"], d["score"])
        if delta:
            moved.append(f"{d['label']} {delta:+d}")
    return {
        "from": {"employee": row.get("employee"), "date": row.get("date"), "day": row.get("day"), "role": row.get("role")},
        "to": {"employee": name, "date": row.get("date"), "day": row.get("day"), "role": row.get("role")},
        "gain": gain, "moved": moved, "kind": "replace",
        "reason": (f"Put {name} on {row.get('day') or row.get('date')} {row.get('role')} instead of "
                   f"{row.get('employee')} — overall {gain:+d}" + (", " + ", ".join(moved) if moved else "") + "."),
    }


def apply_fixes(rows: list, violations: list, profiles: list = None, weights: dict = None,
                max_evaluations: int = MAX_CANDIDATE_EVALUATIONS, rule_constraints=None,
                only_dates=None, max_seconds: float = None, **signals) -> dict:
    """Repair the rows that break a hard rule by putting somebody legal on
    them, choosing the replacement that scores best. Rows nobody legal can
    take are left as they are and named, never dropped: a coverage gap the
    owner can see beats a silently thinner week.

    Only breaches a different person can clear are attempted (PERSON_FIXABLE).
    A breach about a DAY (no manager on, a floor short — `day_level`) is the
    day's, shown on the day by the review, never an "unfixed" line on whoever
    the sweep pinned it to (E-13). A row the manager plan or the owner pinned
    ("_pinned") is never handed to somebody else.

    With `rule_constraints` (schedule_rules.Constraints) each candidate is
    somebody code may choose that day (Constraints.fillable) for whom the row
    is legal with their week swept (Constraints.can_add — first only people
    who stay under their overtime line, and only when nobody can, someone
    past it but never past their maximum: a person's legality outranks
    overtime, the owner's rule is that overtime goes to nobody while a
    teammate has room; schedule audit 10/3/26 P-2). The fix is kept only when
    the whole week, swept (incrementally — P-37) and compared by breach
    identity, shows the row's breach gone or smaller and nothing else new or
    worse: nothing about anybody, no minute without a manager, no floor or
    closer, and — taking somebody under their line — no overtime or minimum
    hours either (schedule_rules.regressions; it used to compare (row index,
    kind) sets, blind to a new breach pinned to the same row — E-1). Rows the
    evaluation budget (or `max_seconds`, a generation's deadline) did not
    reach are reported as not tried, never as impossible.

    A missed day off (schedule_rules "days_off": fewer consecutive days off
    than the rule) is fixable too, after the hard breaches: one of the
    person's shifts goes to a legal teammate, the one that costs the week
    the least score (_fix_days_off). It needs `rule_constraints` to know the
    rule and to prove no new hard breach; `only_dates` limits which rows
    may be handed over (a partial redo keeps the owner's days).

    Returns {rows, fixes: [{index, from, to, kind, reason}], unfixed: [...]}.
    """
    rows = [dict(r) for r in rows]
    c = rule_constraints
    days_off = [v for v in (violations or []) if v.get("kind") == "days_off"]
    availability = signals.get("availability") or {}
    cons = signals.get("constraints") or {}
    rules = signals.get("rules") or {}
    roster = [n for n in (signals.get("roster") or []) if n]
    families = signals.get("role_families") or getattr(c, "role_families", None) or None
    fixes, unfixed, evaluated = [], [], 0
    hard = [v for v in (violations or []) if v.get("hard") and not v.get("day_level")]
    for v in hard:
        if v.get("kind") not in PERSON_FIXABLE:
            unfixed.append({"index": v.get("index"), "employee": v.get("employee"), "kind": v.get("kind"),
                            "reason": f"{v.get('label') or v.get('kind')} — not something a different person on "
                                      "the shift would change."})
    hard = [v for v in hard if v.get("kind") in PERSON_FIXABLE]
    # over_max_hours lands on EVERY row of the person's payroll week. Fixing
    # each one would strip them of the whole week (48h → 0h); only the
    # excess of THAT payroll week should move. Keep its latest rows until
    # the week fits, and drop the rest of its entries from the work list.
    over = {}
    for v in hard:
        if v.get("kind") == "over_max_hours":
            over.setdefault(((v.get("employee") or "").strip().lower(), v.get("bucket")), []).append(v)
    if over:
        keep = set()
        for (_low, _b), vs in over.items():
            excess = float(vs[0].get("over_by") or 0)
            if excess <= 0:
                probe = _SwapIndex(rows, availability, cons, rules)
                excess = probe.total_hours(_low) - probe.cap(_low)
            vs_sorted = sorted(vs, key=lambda x: (rows[x["index"]].get("date") or "",
                                                  _slot_minutes(rows[x["index"]].get("shift_start") or "") or 0),
                               reverse=True)
            removed = 0.0
            for v in vs_sorted:
                if removed >= excess - 0.05:
                    break
                keep.add(id(v))
                removed += _row_hours(rows[v["index"]])
        hard = [v for v in hard if v.get("kind") != "over_max_hours" or id(v) in keep]

    sweep = before = None
    if c is not None:
        import schedule_rules as _rules
        sweep = _rules.IncrementalSweep(c)
        before = _rules.breach_profile(rows, c, viols=sweep.violations(rows))
    scorer = None
    pools = None
    seen_idx = set()
    budget_out = False
    import time as _time
    t0 = _time.monotonic()

    def spent() -> bool:
        return evaluated >= max_evaluations or (max_seconds is not None and _time.monotonic() - t0 > max_seconds)
    for v in sorted(hard, key=lambda x: (0 if x.get("no_show") else 1, x.get("index", 0))):
        i = v.get("index")
        if i is None or i in seen_idx or i >= len(rows):
            continue
        seen_idx.add(i)
        row = rows[i]
        cur = (row.get("employee") or "").strip()
        if row.get("_pinned"):
            unfixed.append({"index": i, "employee": cur, "kind": v.get("kind"),
                            "reason": f"{cur}'s {row.get('day') or row.get('date')} {row.get('role')} shift is fixed "
                                      "(the manager plan, or a day you kept) — change it by hand."})
            continue
        if only_dates is not None and row.get("date") not in only_dates:
            continue
        if budget_out or spent():
            budget_out = True
            unfixed.append({"index": i, "employee": cur, "kind": v.get("kind"), "not_tried": True,
                            "reason": f"{cur}'s {row.get('day') or row.get('date')} {row.get('role')} shift wasn't "
                                      "checked yet — press Apply fixes to check the rest."})
            continue
        if scorer is None:
            scorer = LocalScorer(rows, profiles=profiles, weights=weights, **signals)
            pools = _people_by_family(rows, roster, signals, families)
        index = _SwapIndex(rows, availability, cons, rules)
        pool = set(pools.get(role_family(row.get("role"), families)) or ())
        pool |= {(r.get("employee") or "").strip() for r in rows
                 if role_family(r.get("role"), families) == role_family(row.get("role"), families)}
        pool.discard("")
        if c is not None:
            # The week's rules decide the hours (can_add below: the overtime
            # line, then the maximum) — the swap index's cap is only the
            # caller's copy of them, the ceiling when none was passed.
            candidates = [n for n in sorted(pool) if n != cur and (n.lower(), row.get("date")) not in index.working
                          and index.person_fits(n, row)]
        else:
            candidates = [n for n in sorted(pool) if n != cur and index.replacement_legal(i, n)]
        target = None
        if c is not None:
            target = _rules.breach_id(v)
        legal_opts, tried = [], 0
        for under_line in (True, False) if c is not None else (True,):
            for name in candidates:
                if spent():
                    budget_out = True
                    break
                trial = list(rows)
                trial[i] = dict(row, employee=name)
                fl = hb = after = None
                if c is not None:
                    if not c.fillable(name, row.get("date") or "")[0]:
                        continue
                    if not c.can_add(trial[i], trial, overtime=under_line)[0]:
                        continue
                    viols = sweep.violations(trial)
                    after = _rules.breach_profile(trial, c, viols=viols)
                    if after["by_id"].get(target, 0.0) >= before["by_id"].get(target, 0.0) - 1e-9:
                        continue            # the breach this fix is for is still there
                    if _rules.regressions(before, after, upto=_rules.TIER_MIN_HOURS if under_line else _rules.TIER_POLICY,
                                          hard_only=not under_line):
                        continue            # a fix that trades one breach for another is not a fix
                    fl = {((x.get("employee") or "").strip().lower(), x.get("date") or "", x.get("shift_start") or "")
                          for x in viols if x.get("no_show")}
                    hb = [x for x in viols if x.get("hard")]
                tried += 1
                evaluated += 1
                try:
                    val = scorer.score(trial, flagged=fl, hard_breaches=hb)
                except Exception:
                    continue
                legal_opts.append((val, name, trial, after))
            if legal_opts or budget_out:
                break
        if legal_opts:
            val, name, trial, after = sorted(legal_opts, key=lambda t: (-t[0], t[1]))[0]
            rows = trial
            rows[i] = dict(rows[i])
            if after is not None:
                before = after
            note = (rows[i].get("notes") or "").strip()
            rows[i]["notes"] = (note + f" (was {cur} — {v.get('label') or v.get('kind')})").strip()
            fixes.append({"index": i, "from": cur, "to": name, "kind": v.get("kind"),
                          "reason": f"{cur} — {v.get('detail') or v.get('label')}; {name} can take it."})
        elif budget_out and not tried:
            unfixed.append({"index": i, "employee": cur, "kind": v.get("kind"), "not_tried": True,
                            "reason": f"{cur}'s {row.get('day') or row.get('date')} {row.get('role')} shift wasn't "
                                      "checked yet — press Apply fixes to check the rest."})
        else:
            unfixed.append({"index": i, "employee": cur, "kind": v.get("kind"),
                            "reason": f"Nobody on the roster can legally take {cur}'s {row.get('day') or row.get('date')} {row.get('role')} shift."})
    if days_off:
        # Its own budget: each option is scored locally (LocalScorer), a
        # fraction of what a whole-week re-score costs.
        rows, more, missed, used = _fix_days_off(rows, days_off, profiles, weights, rule_constraints,
                                                 max(MAX_CANDIDATE_EVALUATIONS, max_evaluations - evaluated),
                                                 only_dates, signals)
        fixes.extend(more)
        unfixed.extend(missed)
        evaluated += used
    return {"rows": rows, "fixes": fixes, "unfixed": unfixed, "evaluated": evaluated}


def _days_off_people(rows: list, rule_constraints, viols=None) -> set:
    from schedule_rules import violations as _viol
    viols = _viol(rows, rule_constraints) if viols is None else viols
    return {(v.get("employee") or "").strip().lower() for v in viols if v.get("kind") == "days_off"}


def _fix_days_off(rows: list, violations: list, profiles, weights, rule_constraints,
                  max_evaluations: int, only_dates, signals: dict) -> tuple:
    """Give each person the rule's run of days off together by handing the
    fewest of their shifts to legal teammates.

    For each window of consecutive days as long as the rule, the shifts the
    person works inside it are what would have to go; the windows needing
    the fewest are tried, and for each shift the teammate taken is the one
    that costs the week the least score (LocalScorer). A window is kept only
    when the rule sweep then shows the person's days off met, nothing new or
    worse by breach identity (schedule_rules.regressions — the manager every
    minute, the floors, the closer; it compared (row, kind) sets — E-1), and
    nobody newly short of their own days off; of the windows that pass, the
    best-scoring one wins. Each teammate is somebody code may choose that
    day for whom the row is legal (Constraints.fillable, can_add — P-2); a
    pinned row never moves.

    Returns (rows, fixes, unfixed, evaluations)."""
    fixes, unfixed, used = [], [], 0
    if rule_constraints is None:
        for v in violations:
            unfixed.append({"index": v.get("index"), "employee": v.get("employee"), "kind": "days_off",
                            "reason": f"{v.get('employee')} — {v.get('detail') or v.get('label')}; "
                                      "the rules were not loaded, so nothing was moved."})
        return rows, fixes, unfixed, used
    import schedule_rules as _rules
    c = rule_constraints
    sweep = _rules.IncrementalSweep(c)
    week = list(getattr(c, "week_dates", None) or [])
    availability = signals.get("availability") or {}
    cons = signals.get("constraints") or {}
    rules = signals.get("rules") or {}
    roster = [n for n in (signals.get("roster") or []) if n]
    families = signals.get("role_families") or getattr(c, "role_families", None) or None
    pools = _people_by_family(rows, roster, signals, families)
    done = set()
    for v in violations:
        name = (v.get("employee") or "").strip()
        low = name.lower()
        if not low or low in done:
            continue
        done.add(low)
        now = sweep.violations(rows)
        if low not in _days_off_people(rows, c, now):
            continue                      # an earlier fix already gave them the run
        part = (getattr(c, "employment", {}) or {}).get(low) == "part"
        req = c.compliance.get("part_time_days_off") if part else c.compliance.get("min_consecutive_days_off")
        try:
            req = int(req or 0)
        except (TypeError, ValueError):
            req = 0
        mine = [i for i, r in enumerate(rows) if (r.get("employee") or "").strip().lower() == low
                and not r.get("_pinned")]
        windows = []
        for k in range(0, max(0, len(week) - req + 1)):
            span = week[k:k + req]
            hand = [i for i in mine if rows[i].get("date") in set(span)]
            if not hand or (only_dates and any(rows[i].get("date") not in only_dates for i in hand)):
                continue
            windows.append((len({rows[i].get("date") for i in hand}), k, hand))
        if not req or not windows:
            unfixed.append({"index": v.get("index"), "employee": name, "kind": "days_off",
                            "reason": f"{name} — {v.get('detail') or v.get('label')}; no shift of theirs "
                                      "could be handed over here."})
            continue
        fewest = min(w[0] for w in windows)
        before = _rules.breach_profile(rows, c, viols=now)
        before_short = _days_off_people(rows, c, now)
        scorer = LocalScorer(rows, profiles=profiles, weights=weights, **signals)
        best = None
        out_of_budget = False
        for _n, _k, hand in [w for w in windows if w[0] == fewest]:
            trial = [dict(r) for r in rows]
            moved = []
            ok = True
            for i in hand:
                if used >= max_evaluations:
                    out_of_budget = True
                    ok = False
                    break
                index = _SwapIndex(trial, availability, cons, rules)
                pool = set(pools.get(role_family(trial[i].get("role"), families)) or ())
                pool.discard("")
                cands = [n for n in sorted(pool) if n.lower() != low and index.replacement_legal(i, n)
                         and c.fillable(n, trial[i].get("date") or "")[0]
                         and c.can_add(dict(trial[i], employee=n), trial)[0]]
                options = []
                for n in cands:
                    t2 = list(trial)
                    t2[i] = dict(trial[i], employee=n)
                    options.append((n, t2))
                used += len(options)
                pick, _val = scorer.best(options)
                if pick is None:
                    ok = False
                    break
                trial[i] = dict(trial[i], employee=pick)
                moved.append((i, pick))
            if not ok or not moved:
                continue
            after_v = sweep.violations(trial)
            after = _rules.breach_profile(trial, c, viols=after_v)
            after_short = _days_off_people(trial, c, after_v)
            if _rules.regressions(before, after, upto=_rules.TIER_COVERAGE) or low in after_short \
                    or (after_short - before_short):
                continue
            val = scorer.score(trial)
            if best is None or val > best[0] + 1e-9:
                best = (val, trial, moved)
        if best is None:
            unfixed.append({"index": v.get("index"), "employee": name, "kind": "days_off",
                            "not_tried": out_of_budget or None,
                            "reason": (f"{name}'s days off weren't checked yet — press Apply fixes to check the rest.")
                                      if out_of_budget else
                                      (f"{name} — {v.get('detail') or v.get('label')}; nobody could legally "
                                       "take one of their shifts without breaking another rule.")})
            continue
        _val, trial, moved = best
        for i, pick in moved:
            note = (trial[i].get("notes") or "").strip()
            trial[i]["notes"] = (note + f" (was {name} — days off)").strip()
            fixes.append({"index": i, "from": name, "to": pick, "kind": "days_off",
                          "reason": f"{name} had no {req} days off together; {pick} takes their "
                                    f"{trial[i].get('day') or trial[i].get('date')} {trial[i].get('role')} shift."})
        rows = trial
    return rows, fixes, unfixed, used


def _candidate_verdict(baseline: dict, best: dict, swaps: list,
                       evaluated: int, legal: int = 0) -> str:
    """What the comparison actually established, and nothing more.

    The first version said "This is the strongest team available from this
    roster" after sixty evaluations. On a two-hundred-person week that is
    sixty of roughly sixteen thousand legal swaps, and the sentence was the
    most confident thing on the panel and the least supported. It now only
    makes that claim when the run genuinely exhausted the candidates.
    """
    if not evaluated:
        return ("No alternative arrangement was possible — every swap would have "
                "double-booked somebody, broken an availability or a staff note, "
                "or pushed a week past forty hours.")
    if not swaps:
        if legal and evaluated >= legal:
            return (f"Tried every one of the {legal} possible swaps and none scored "
                    "better. This is the strongest team available from this roster.")
        scope = (f" out of {legal:,} possible" if legal > evaluated else "")
        return (f"Tried the {evaluated} most promising swaps{scope} and none scored "
                "better. A wider search might still find something.")
    names = ", ".join(f"{s['from']['employee']}/{s['to']['employee']}" for s in swaps[:3])
    tail = (f", out of {legal:,} possible" if legal > evaluated else "")
    return (f"{len(swaps)} " + _plural(len(swaps), "swap") +
            f" raised overall quality from {baseline['score']} to {best['score']} "
            f"({names}), from {evaluated} tried{tail}.")
