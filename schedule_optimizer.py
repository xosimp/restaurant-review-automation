"""
schedule_optimizer.py — the Shift Quality score as the schedule's objective.

The generator writes a week once; the rule sweep repairs hard breaches; and
until this module the score was only ever REPORTED. Replaying real weeks
showed what that cost: one capped peak-night shift (a missing leader, a
half-hour with no pizza cook) took 9-12 points off a week, and nothing went
back to fix it, because neither is a hard-rule breach. The one existing
search (shift_quality.compare_candidates) could only trade two people in the
same role, chose which trades to try by rating gap — all zero at a
restaurant nobody has rated — and its answer was thrown away.

This is a bounded local search over the moves a manager actually makes:

  add       someone legal takes a new shift where a role is short
  extend    a shift starts earlier or ends later to cover a thin half hour
  replace   someone else takes an existing shift (a leader for a busy night,
            a rested person for somebody on day seven)
  swap      two people in one role trade shifts on different dates
  trim      a shift is shortened, or removed, where a day is over its target

Candidates come from the weak dimensions' own facts, costliest first
(demand-weighted, capped shifts before everything). Every candidate is
scored with the same shift_quality.score_rows the owner's number comes
from; the best improvement is taken, re-checked against the full rule
sweep (it must not add a hard breach), and recorded with a sentence saying
what changed and what it bought. The search stops at the target, when
nothing improves, or at its time and evaluation budget.

Nothing here calls a model. Pure over its inputs apart from time.
"""
import time as _time

import shift_quality as sq

# Stop once the week reaches this, or when no candidate improves it.
DEFAULT_TARGET = 92
# Seconds and evaluations one run may spend. A score is ~0.1-0.2s on a
# 250-row week, so this is well under a minute on the background job.
DEFAULT_SECONDS = 25.0
DEFAULT_EVALUATIONS = 1500
# Moves tried per round, and the smallest week-level gain worth taking.
MOVES_PER_ROUND = 24
MIN_GAIN = 0.15
# A move this good is taken without trying the rest of the round.
TAKE_AT_ONCE = 1.0
# The weekly hours budget is a ceiling (the prompt, the review and the trim
# all treat it as one); a move may not carry the week past it by more than
# the same 2% the review tolerates.
BUDGET_TOLERANCE = 1.02
# How far a shift may be stretched in one move.
MAX_EXTEND_MINUTES = 150
STEP = 30

NOTE_TAG = "Cavnar:"


# ── time helpers ───────────────────────────────────────────────────────────

def _m(value):
    return sq._slot_minutes(value)


def _fmt(minutes: int) -> str:
    minutes %= 24 * 60
    h, mm = divmod(minutes, 60)
    return f"{h % 12 or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


def _span(row):
    s, e = _m(row.get("shift_start")), _m(row.get("shift_end"))
    if s is None or e is None:
        return None, None
    if e <= s:
        e += 24 * 60
    return s, e


def _hours_between(s, e) -> float:
    return round((e - s) / 60.0, 2)


def _day(date: str) -> str:
    return sq._day_name(date)


# ── the objective ──────────────────────────────────────────────────────────

def objective(quality: dict) -> float:
    """The week's score before rounding. Integer week scores hide a shift
    moving three points; the search needs to see it."""
    scored = [s for s in (quality or {}).get("shifts") or [] if s.get("scored")]
    if not scored:
        return 0.0
    num = sum(s["score"] * sq.DEMAND_WEIGHT.get(s["profile"]["demand"], 1.0) for s in scored)
    den = sum(sq.DEMAND_WEIGHT.get(s["profile"]["demand"], 1.0) for s in scored)
    return num / (den or 1.0)


def _problems(quality: dict) -> list:
    """(cost, shift, dimension) for every dimension under 100, costliest
    first. A capping dimension carries the whole shift's shortfall."""
    out = []
    for s in (quality or {}).get("shifts") or []:
        if not s.get("scored"):
            continue
        w = sq.DEMAND_WEIGHT.get(s["profile"]["demand"], 1.0)
        dims = s.get("dimensions") or []
        total = sum(d["weight"] for d in dims) or 1.0
        for d in dims:
            if d["score"] >= sq.SCORE_MAX:
                continue
            if s.get("capped_by") == d["key"]:
                cost = (sq.SCORE_MAX - s["score"]) * w + 100
            else:
                cost = (sq.SCORE_MAX - d["score"]) * d["weight"] / total * w
            out.append((cost, s, d))
    out.sort(key=lambda t: -t[0])
    return out


# ── the search ─────────────────────────────────────────────────────────────

class _State:
    """What move generation needs, recomputed after each accepted move."""

    def __init__(self, rows, signals, inputs):
        self.rows = rows
        self.signals = signals
        rules = signals.get("rules") or {}
        self.index = sq._SwapIndex(rows, signals.get("availability") or {},
                                   signals.get("constraints") or {}, rules)
        self.scores = signals.get("scores") or {}
        self.leader_flags = signals.get("leader_flags") or {}
        roster = [n for n in (signals.get("roster") or []) if n]
        roster_roles = (inputs or {}).get("roster_roles") or {}
        cross = signals.get("cross_trained") or {}
        pool = {}
        for n in roster:
            r = (roster_roles.get(n) or "").strip().lower()
            if r:
                pool.setdefault(r, set()).add(n)
        for r in rows:
            n = (r.get("employee") or "").strip()
            role = (r.get("role") or "").strip().lower()
            if n and role and (not roster or n in set(roster)):
                pool.setdefault(role, set()).add(n)
        for n, roles in cross.items():
            if roster and n not in set(roster):
                continue
            for role in roles or []:
                pool.setdefault(str(role).strip().lower(), set()).add(n)
        self.pool = pool
        self.pending = {str(k).strip().lower(): set(v or ()) for k, v in
                        ((inputs or {}).get("pending_time_off") or {}).items()}
        self.close_times = (signals.get("close_times") or {})
        self.open_times = (signals.get("open_times") or {})

    def role_rows(self, date, role, part=None):
        role = role.strip().lower()
        out = []
        for i, r in enumerate(self.rows):
            if r.get("date") == date and (r.get("role") or "").strip().lower() == role:
                if part is None or part in sq.present_dayparts(r):
                    out.append(i)
        return out

    def template(self, date, role, part):
        """Start and end for a new shift: the commonest among this role on
        this date and daypart, else this role's in that daypart any day."""
        def common(idxs):
            counts = {}
            for i in idxs:
                r = self.rows[i]
                if sq.daypart_of(r.get("shift_start", "")) != part:
                    continue
                key = (r.get("shift_start"), r.get("shift_end"))
                counts[key] = counts.get(key, 0) + 1
            return max(counts.items(), key=lambda kv: kv[1])[0] if counts else None
        found = common(self.role_rows(date, role))
        if not found:
            role_low = role.strip().lower()
            found = common([i for i, r in enumerate(self.rows)
                            if (r.get("role") or "").strip().lower() == role_low])
        return found

    def can_add(self, name, row) -> bool:
        low = (name or "").strip().lower()
        if not low or (low, row.get("date")) in self.index.working:
            return False
        # Somebody who has asked for the day off (not yet decided) is not
        # the person to add: the owner would be approving their own gap.
        if row.get("date") in self.pending.get(low, ()):
            return False
        if not self.index.person_fits(name, row):
            return False
        return self.index.total_hours(low) + sq._row_hours(row) <= self.index.cap(low) + 0.01


def _new_row(date, name, role, start, end, why):
    s, e = _m(start), _m(end)
    hours = _hours_between(s, e if e > s else e + 24 * 60) if s is not None and e is not None else 0
    return {"date": date, "day": _day(date), "employee": name, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": hours,
            "notes": f"{NOTE_TAG} {why}"}


def _tag(row, why):
    note = (row.get("notes") or "").strip()
    row["notes"] = (note + (" — " if note else "") + f"{NOTE_TAG} {why}")[:240]


def _moves_for(problem, state: _State) -> list:
    """Candidate moves for one weak dimension on one shift, most likely first.
    Each move is (signature, description, apply(rows) -> rows)."""
    _cost, shift, dim = problem
    key, facts = dim["key"], dim.get("facts") or {}
    date, part, day = shift["date"], shift["daypart"], shift["day"]
    where = f"{day} {'lunch' if part == 'morning' else 'dinner' if part == 'night' else part}"
    moves = []

    def add_person(role, why, prefer=None):
        tpl = state.template(date, role, part)
        if not tpl:
            return
        start, end = tpl
        pool = sorted(state.pool.get(role.strip().lower(), ()),
                      key=lambda n: (-(prefer(n) if prefer else 0),
                                     state.index.total_hours(n.lower())))
        for name in pool:
            row = _new_row(date, name, role, start, end, why)
            if state.can_add(name, row):
                moves.append((("add", date, name, role, start),
                              f"Added {name} as {role} on {where} ({start}–{end}) — {why}.",
                              lambda rows, row=row: rows + [dict(row)]))
                if len([m for m in moves if m[0][0] == "add"]) >= 3:
                    return

    def replace_in(idx, predicate, why):
        row = state.rows[idx]
        role = (row.get("role") or "").strip().lower()
        cur = (row.get("employee") or "").strip()
        for name in sorted(state.pool.get(role, ()), key=lambda n: state.index.total_hours(n.lower())):
            if name == cur or not predicate(name):
                continue
            if row.get("date") in state.pending.get(name.lower(), ()):
                continue
            if not state.index.replacement_legal(idx, name):
                continue

            def apply(rows, idx=idx, name=name, why=why):
                out = [dict(r) for r in rows]
                out[idx]["employee"] = name
                _tag(out[idx], why + f" (was {cur})")
                return out
            moves.append((("replace", idx, name),
                          f"Put {name} on {row.get('role')} {where} instead of {cur} — {why}.", apply))
            if len([m for m in moves if m[0][0] == "replace"]) >= 4:
                return

    if key == "coverage":
        for role, n in sorted((facts.get("short") or {}).items(), key=lambda kv: -kv[1]):
            add_person(role, f"{role} was short on {where}")
            # Stretch a same-role shift from the other daypart into this one.
            for i in state.role_rows(date, role):
                r = state.rows[i]
                if part in sq.present_dayparts(r):
                    continue
                s, e = _span(r)
                if s is None:
                    continue
                lo, hi = sq.CORE_WINDOWS.get(part, (None, None))
                if lo is None:
                    continue
                new_s, new_e = (min(s, lo), e) if part == "morning" else (s, max(e, lo + sq.PRESENCE_MIN_OVERLAP))
                if (new_e - new_s) - (e - s) > MAX_EXTEND_MINUTES:
                    continue
                moves.append(_retime_move(state, i, new_s, new_e, f"to cover {role} on {where}"))

    elif key == "coverage_curve":
        for role, g in (facts.get("gaps") or {}).items():
            t = g.get("worst_minute")
            if t is None:
                continue
            # Extend the nearest same-role shift that day to reach the gap.
            best = []
            for i in state.role_rows(date, role):
                s, e = _span(state.rows[i])
                if s is None:
                    continue
                if t >= e:
                    best.append((t - e, i, s, t + STEP))
                elif t < s:
                    best.append((s - t, i, t, e))
            for dist, i, ns, ne in sorted(best)[:2]:
                if dist <= MAX_EXTEND_MINUTES:
                    moves.append(_retime_move(state, i, ns, ne, f"{role} was thin at {g.get('worst_at')} on {where}"))
            add_person(role, f"{role} was thin at {g.get('worst_at')} on {where}")

    elif key == "leadership":
        for miss in facts.get("misses") or []:
            role = (miss.get("role") or "").strip()
            if not role:
                continue
            if miss.get("attribute"):
                ok = lambda n: bool(state.leader_flags.get(n))
            elif miss.get("min_score") is not None:
                ok = lambda n, ms=float(miss["min_score"]): (state.scores.get(n) or 0) >= ms
            else:
                ok = lambda n: True
            label = miss.get("rule") or role
            weakest = sorted(state.role_rows(date, role, part),
                             key=lambda i: (bool(ok(state.rows[i].get("employee"))),
                                            state.scores.get(state.rows[i].get("employee")) or 0))
            for i in weakest[:2]:
                if ok(state.rows[i].get("employee")):
                    continue
                replace_in(i, ok, f"{where} needs {label}")
                _swap_moves(state, i, moves, f"{where} needs {label}", want=ok)
            add_person(role, f"{where} needs {label}", prefer=lambda n, ok=ok: 1 if ok(n) else -1)
        if facts.get("profile_leader_missing"):
            roles = [r for r in (facts.get("leader_roles") or [])] or \
                    sorted({(state.rows[i].get("role") or "") for i in range(len(state.rows))
                            if state.rows[i].get("date") == date})
            ms = float(facts.get("leader_min_score") or 4)
            ok = lambda n: bool(state.leader_flags.get(n)) or (state.scores.get(n) or 0) >= ms
            for role in roles:
                for i in state.role_rows(date, role, part)[:2]:
                    replace_in(i, ok, f"{where} needs somebody who can run it")
                    _swap_moves(state, i, moves, f"{where} needs somebody who can run it", want=ok)

    elif key == "operational_strength":
        for sf in facts.get("shortfalls") or []:
            role = sf.get("role") or ""
            idxs = sorted(state.role_rows(date, role, part),
                          key=lambda i: state.scores.get(state.rows[i].get("employee")) or 0)
            for i in idxs[:2]:
                cur = state.scores.get(state.rows[i].get("employee")) or 0
                stronger = lambda n, cur=cur: (state.scores.get(n) or 0) > cur
                replace_in(i, stronger, f"{role} strength was under target on {where}")
                _swap_moves(state, i, moves, f"{role} strength was under target on {where}", want=stronger)
            add_person(role, f"{role} strength was under target on {where}",
                       prefer=lambda n: state.scores.get(n) or 0)

    elif key == "training_balance":
        for text in facts.get("isolated_names") or []:
            name, _, role = text.partition(" on ")
            idxs = [i for i in state.role_rows(date, role, part)
                    if (state.rows[i].get("employee") or "") != name]
            mentor = lambda n: (state.scores.get(n) or 0) >= (state.scores.get(name) or 0) + 2
            for i in idxs[:1]:
                replace_in(i, mentor, f"{name} needed somebody stronger alongside on {where}")
                _swap_moves(state, i, moves, f"{name} needed somebody stronger alongside on {where}", want=mentor)

    elif key == "fatigue":
        strained = {o["name"] for o in facts.get("overloaded") or []} | \
                   {o["name"] for o in facts.get("long_runs") or []} | \
                   {o["name"] for o in facts.get("heavy_weeks") or []}
        for name in sorted(strained):
            for i, r in enumerate(state.rows):
                if (r.get("employee") or "").strip() == name and r.get("date") == date:
                    replace_in(i, lambda n: True, f"{name} was on too many days or hours")
        for ls in facts.get("long_shifts") or []:
            for i, r in enumerate(state.rows):
                if (r.get("employee") or "").strip() == ls["name"] and r.get("date") == date:
                    s, e = _span(r)
                    lim = state.signals.get("max_shift_hours")
                    if s is not None and lim:
                        moves.append(_retime_move(state, i, s, s + int(float(lim) * 60),
                                                  f"{ls['name']}'s shift was over {float(lim):g} hours"))

    elif key == "fairness":
        for kind, info in facts.items():
            if not isinstance(info, dict):
                continue
            for o in info.get("overloaded") or []:
                for i, r in enumerate(state.rows):
                    if (r.get("employee") or "").strip() == o["name"] and r.get("date") == date \
                            and part in sq.present_dayparts(r):
                        replace_in(i, lambda n: True, f"{o['name']} had more than their share of {kind} shifts")
                        _swap_moves(state, i, moves, f"{o['name']} had more than their share of {kind} shifts")

    elif key == "labor_efficiency":
        ratio = facts.get("ratio") or 1
        if ratio > 1.02:
            # The longest discretionary shifts that day, trimmed by an hour,
            # or the last-added person on the most-staffed role removed.
            idxs = sorted([i for i, r in enumerate(state.rows) if r.get("date") == date],
                          key=lambda i: -sq._row_hours(state.rows[i]))
            for i in idxs[:3]:
                s, e = _span(state.rows[i])
                if s is not None and e - s > 5 * 60:
                    moves.append(_retime_move(state, i, s, e - 60, f"{day} was over its hour target"))
            for i in idxs[:3]:
                moves.append(_remove_move(state, i, f"{day} was over its hour target"))

    elif key == "stability":
        for name in facts.get("changed") or []:
            for i, r in enumerate(state.rows):
                if (r.get("employee") or "").strip() == name and r.get("date") == date:
                    _swap_moves(state, i, moves, f"{name} is not usually on {where}")
    return moves


def _retime_move(state, i, new_s, new_e, why):
    row = state.rows[i]
    start, end = _fmt(new_s), _fmt(new_e)

    def apply(rows, i=i, start=start, end=end, new_s=new_s, new_e=new_e, why=why):
        out = [dict(r) for r in rows]
        was = f"{out[i].get('shift_start')}–{out[i].get('shift_end')}"
        out[i]["shift_start"], out[i]["shift_end"] = start, end
        out[i]["scheduled_hours"] = _hours_between(new_s, new_e)
        _tag(out[i], f"{why} (was {was})")
        return out
    return (("retime", i, start, end),
            f"Moved {row.get('employee')}'s {row.get('day') or _day(row.get('date'))} {row.get('role')} shift to "
            f"{start}–{end} (was {row.get('shift_start')}–{row.get('shift_end')}) — {why}.", apply)


def _remove_move(state, i, why):
    row = state.rows[i]

    def apply(rows, i=i):
        return [dict(r) for j, r in enumerate(rows) if j != i]
    return (("remove", i),
            f"Took {row.get('employee')}'s {row.get('day') or _day(row.get('date'))} {row.get('role')} shift "
            f"({row.get('shift_start')}–{row.get('shift_end')}) off — {why}.", apply)


def _swap_moves(state, i, moves, why, want=None):
    """Trade row i's person with someone in the same role on another date.
    `want(name)` limits who may come in (a leader, somebody stronger)."""
    row = state.rows[i]
    role = (row.get("role") or "").strip().lower()
    for j in state.index.by_role.get(role, []):
        if j == i:
            continue
        if want is not None and not want((state.rows[j].get("employee") or "").strip()):
            continue
        if not state.index.legal(i, j, state.scores):
            continue
        other = state.rows[j]

        def apply(rows, i=i, j=j, why=why):
            out = [dict(r) for r in rows]
            a, b = out[i]["employee"], out[j]["employee"]
            out[i]["employee"], out[j]["employee"] = b, a
            _tag(out[i], f"{why} (swapped with {a})")
            _tag(out[j], f"{why} (swapped with {b})")
            return out
        moves.append((("swap", min(i, j), max(i, j)),
                      f"Swapped {row.get('employee')} ({row.get('day') or _day(row.get('date'))}) and "
                      f"{other.get('employee')} ({other.get('day') or _day(other.get('date'))}) on {row.get('role')} — {why}.",
                      apply))
        if len([m for m in moves if m[0][0] == "swap"]) >= 3:
            return


def _hard_count(rows, constraints) -> int:
    if constraints is None:
        return 0
    import schedule_rules as _rules
    try:
        return sum(1 for v in _rules.violations(rows, constraints) if v.get("hard"))
    except Exception:
        return 0


def optimize(rows: list, inputs: dict = None, signals: dict = None, weights: dict = None,
             constraints=None, target: int = DEFAULT_TARGET, max_seconds: float = DEFAULT_SECONDS,
             max_evaluations: int = DEFAULT_EVALUATIONS, hours_budget: float = None,
             max_server_overlap: int = None) -> dict:
    """Improve the week's Shift Quality with legal moves, and say what moved.

    Returns {rows, changes: [{kind, reason, gain}], before_score, after_score,
    quality, evaluations, seconds, stopped}. `rows` is a new list; the input
    is not modified. A week that is already at `target`, or that has nothing
    configured to score, comes back unchanged with the reason.
    """
    t0 = _time.monotonic()
    inputs = inputs or {}
    signals = dict(signals or {})
    profiles = inputs.get("shift_profiles") or None
    constraints = constraints if constraints is not None else inputs.get("constraints")

    def score(rs):
        return sq.score_rows(rs, profiles=profiles, weights=weights, **signals)

    current_rows = [dict(r) for r in rows or []]
    current = score(current_rows)
    before = current.get("score")
    out = {"rows": current_rows, "changes": [], "before_score": before, "after_score": before,
           "quality": current, "evaluations": 1, "seconds": 0.0, "stopped": ""}
    if not current.get("checked"):
        out["stopped"] = "nothing to score"
        return out
    base_hard = _hard_count(current_rows, constraints)
    budget = float(hours_budget if hours_budget is not None else (inputs.get("hours_budget") or 0) or 0)
    ceiling = budget * BUDGET_TOLERANCE if budget > 0 else None

    def _week_hours(rs):
        return sum(sq._row_hours(r) for r in rs)

    # One server per section (restaurants.section_count): the backstop that
    # enforces it runs before this search, so no move may undo it.
    try:
        cap = int(max_server_overlap if max_server_overlap is not None else (inputs.get("section_count") or 0))
    except (TypeError, ValueError):
        cap = 0

    def _server_peaks(rs):
        from schedule_engine import _peak_server_overlap
        by = {}
        for r in rs:
            if (r.get("role") or "").strip().lower() == "server":
                by.setdefault(r.get("date"), []).append(r)
        return {d: _peak_server_overlap(v)[0] for d, v in by.items()}
    peaks_before = _server_peaks(current_rows) if cap > 0 else {}

    def _servers_ok(rs):
        """No date's peak goes past the section cap — or, where the draft is
        already past it, any higher than it already is."""
        if cap <= 0:
            return True
        return all(p <= max(cap, peaks_before.get(d, 0)) for d, p in _server_peaks(rs).items())
    evaluations, tabu = 1, set()
    stopped = "no improving move"
    while True:
        if (current.get("score") or 0) >= target:
            stopped = "target reached"
            break
        if _time.monotonic() - t0 > max_seconds or evaluations >= max_evaluations:
            stopped = "budget spent"
            break
        state = _State(current_rows, signals, inputs)
        candidates, seen = [], set()
        for problem in _problems(current)[:8]:
            for mv in _moves_for(problem, state):
                if mv[0] in seen or mv[0] in tabu:
                    continue
                seen.add(mv[0])
                candidates.append(mv)
            if len(candidates) >= MOVES_PER_ROUND:
                break
        if not candidates:
            break
        base_obj = objective(current)
        ranked = []
        for sig, desc, apply in candidates[:MOVES_PER_ROUND]:
            if _time.monotonic() - t0 > max_seconds or evaluations >= max_evaluations:
                break
            try:
                trial_rows = apply(current_rows)
            except Exception:
                tabu.add(sig)
                continue
            if ceiling is not None and _week_hours(trial_rows) > max(ceiling, _week_hours(current_rows)) + 0.01:
                tabu.add(sig)
                continue
            if not _servers_ok(trial_rows):
                tabu.add(sig)
                continue
            try:
                trial = score(trial_rows)
            except Exception:
                tabu.add(sig)
                continue
            evaluations += 1
            if not trial.get("checked"):
                continue
            gain = objective(trial) - base_obj
            ranked.append((gain, sig, desc, trial_rows, trial))
            if gain >= TAKE_AT_ONCE:
                break
        ranked.sort(key=lambda t: -t[0])
        accepted = False
        for gain, sig, desc, trial_rows, trial in ranked[:3]:
            if gain < MIN_GAIN:
                break
            # The full rule sweep, once, for the move about to be taken: no
            # improvement is worth a breach the owner would have to undo.
            if _hard_count(trial_rows, constraints) > base_hard:
                tabu.add(sig)
                continue
            out["changes"].append({"kind": sig[0], "reason": desc, "gain": round(gain, 1),
                                   "score_after": trial.get("score")})
            current_rows, current = trial_rows, trial
            tabu.add(sig)
            accepted = True
            break
        if not accepted:
            break
    out.update(rows=current_rows, after_score=current.get("score"), quality=current,
               evaluations=evaluations, seconds=round(_time.monotonic() - t0, 1), stopped=stopped)
    return out


def unresolved(result: dict, signals: dict = None, limit: int = 4) -> list:
    """What is still wrong after the search, each with why no legal change
    fixed it — the owner's to decide, not the draft's to hide."""
    signals = signals or {}
    flags = signals.get("leader_flags") or {}
    roster_rows = result.get("rows") or []
    out, seen = [], set()
    for cost, s, d in _problems(result.get("quality") or {}):
        if cost < 5 or len(out) >= limit:
            continue
        where = f"{s['day']} {'lunch' if s['daypart'] == 'morning' else 'dinner'}"
        text = (d.get("weaknesses") or [""])[0]
        if d["key"] == "leadership":
            for miss in (d.get("facts") or {}).get("misses") or []:
                if miss.get("attribute"):
                    role = (miss.get("role") or "").strip()
                    able = sorted({(r.get("employee") or "").strip() for r in roster_rows
                                   if (r.get("role") or "").strip().lower() == role.lower()
                                   and flags.get((r.get("employee") or "").strip())})
                    if len(able) <= 1:
                        text = (f"{where} has no {role.lower()} authorised to close. "
                                + (f"Only {able[0]} is, and they are already on the other nights they can legally work. "
                                   if able else "Nobody in that role is. ")
                                + f"Authorising another {role.lower()} to close fixes this.")
        key = (s["date"], s["daypart"], d["key"])
        if key in seen or not text:
            continue
        seen.add(key)
        out.append({"date": s["date"], "day": s["day"], "daypart": s["daypart"], "dimension": d["key"],
                    "text": text, "fixable_by_draft": False})
    return out


def summary(result: dict, signals: dict = None) -> dict:
    """What the owner is shown: before, after, and each change with why."""
    changes = result.get("changes") or []
    before, after = result.get("before_score"), result.get("after_score")
    if not changes:
        verdict = ("The draft already met the quality target." if result.get("stopped") == "target reached"
                   else "No legal change improved the draft.")
    else:
        verdict = (f"Cavnar made {len(changes)} change{'s' if len(changes) != 1 else ''} to the draft, "
                   f"raising Shift Quality from {before} to {after}. Each is listed with why.")
    return {"ran": True, "applied": bool(changes), "before_score": before, "after_score": after,
            "improvement": (after or 0) - (before or 0) if before is not None and after is not None else 0,
            "changes": [{"kind": c["kind"], "reason": c["reason"], "gain": c["gain"]} for c in changes],
            "evaluations": result.get("evaluations"), "seconds": result.get("seconds"),
            "stopped": result.get("stopped"), "verdict": verdict,
            "unresolved": unresolved(result, signals)}
