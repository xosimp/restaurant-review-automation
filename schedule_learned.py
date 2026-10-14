"""
schedule_learned.py — what a learned signal means, for any pass (pure).

The memory (schedule_memory) learns the manager's habits and hands the
active ones to a generation as signals (enforced_signals): moved off or on
a slot, a start or end retimed, a role change, a leader swap, the opener
and the closer, a pair, an overtime risk, a close that runs late. Which of
them a week's rows break, and what each miss weighs, is read here once —
`misses` (the scorer, the solver, the optimizer and the trim all charge it),
`learned_cost` (their sum) and `slot_cost` (a solver's per-assignment
term).

Layer S0 of the schedule modules (ARCHITECTURE_MANIFEST.md §3a): stdlib,
schedule_time and schedule_roles only, so the scorer and the rules read the
learned signals' meaning without importing the memory that learns them.
schedule_memory re-exports every name.
"""
from datetime import date, datetime, timedelta

import schedule_time as _stime
from schedule_roles import role_family


HABIT_MISS_WEIGHT = 1.0


_MEAL = {"morning": "lunch/day", "night": "dinner/night", "late": "late night"}


def _nk(name) -> str:
    """staff_settings.name_key: one person however the name was typed."""
    return " ".join(str(name or "").split()).casefold()


def _weekday(iso) -> str:
    try:
        return datetime.strptime(str(iso or "")[:10], "%Y-%m-%d").strftime("%A")
    except ValueError:
        return ""


def _minutes(value):
    """Minutes past midnight from "4:00pm", "4pm", "16:00", "noon"
    (schedule_time.parse_minutes — the one reading)."""
    return _stime.parse_minutes(value)


def _end_part(row) -> str:
    """The daypart a shift ENDS in — what a close is about: a double that
    started at 10am and closed at 11pm closed the night
    (schedule_time.end_daypart: the restaurant's boundary, read at the end
    of the shift on its business night)."""
    part = _stime.end_daypart(row)
    return _stime.start_daypart(row.get("shift_start") or "") if part == "unknown" else part


def _row_end(row):
    """A row's end on its business date's timeline (schedule_time.end_of)."""
    return _stime.end_of(row)


# Openers (L-22): everyone who clocked in within this of the role's first
# clock-in that day opened it.
OPENER_TIE_MINUTES = 15


def _week_of(iso, start_day=0) -> str:
    """The first date of the schedule week holding `iso` — the restaurant's
    own week (restaurants.week_start_day, 0 = Monday; ARCH-24): a Wednesday-
    to-Tuesday week read as two Monday weeks counted one published week of
    evidence twice, and a redo's week_start (a Wednesday) never matched a
    Monday key."""
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return ""
    return (d - timedelta(days=(d.weekday() - int(start_day or 0) % 7) % 7)).isoformat()


def _end_adj(m, end):
    """A row's end in minutes, read across midnight from its start."""
    if end is None:
        return None
    return end + (1440 if end <= m else 0)


def _edge_of(group, kind) -> set:
    """The people who open (first in, OPENER_TIE_MINUTES of the first) or
    close (last out, as close to the last) a role on one day."""
    if kind == "opener":
        first = min(m for m, *_r in group)
        return {_nk(n) for m, n, *_r in group if m - first <= OPENER_TIE_MINUTES}
    ends = [(_end_adj(m, e), n) for m, n, _r, _s, e in group if e is not None]
    if not ends:
        return set()
    last = max(x for x, _n in ends)
    return {_nk(n) for x, n in ends if last - x <= OPENER_TIE_MINUTES}


def _fam(role, families=None) -> str:
    return role_family(role, families)


def _row_part(r) -> str:
    return (_stime.present_dayparts(r) or ["unknown"])[0]


def _miss_weight(m) -> float:
    """What breaking one learned signal weighs (re-audit 10/11/26
    LEARNING-12): a bound habit HABIT_MISS_WEIGHT whatever its confidence —
    the manager taught it by hand, and a habit at 0.6 used to be half as
    expensive to break as one at 0.9 — anything else its confidence (a
    learning habit's lean is already scaled in enforced_signals)."""
    if m.get("tier") == "bound":
        return HABIT_MISS_WEIGHT
    try:
        return float(m.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0.0


def misses(rows, learned, families=None, line=None, bucket=None) -> list:
    """Which of the `learned` signals (enforced_signals) a week's rows break:
    [{key, kind, text, weight, indexes}] — `weight` is the signal's
    confidence (scaled for overtime by how far past the headroom it goes):
    the soft cost a solver, an optimizer, the trim or the scorer charges.
    Pure; the semantics of every kind live here, once."""
    line = float(line or _stime.OVERTIME_THRESHOLD_HOURS)
    out = []
    by_slot = {}
    for i, r in enumerate(rows or []):
        d = str(r.get("date") or "")[:10]
        by_slot.setdefault((_weekday(d), _row_part(r)), []).append(i)

    def name_of(i):
        return _nk((rows[i] or {}).get("employee"))

    for m in learned or []:
        kind, conf = m.get("kind"), _miss_weight(m)
        v = m.get("value") or {}
        day, part, who = m.get("day"), m.get("daypart"), _nk(m.get("person"))
        slot = by_slot.get((day, part), [])
        hit = []
        if kind == "moved_off":
            hit = [i for i in slot if name_of(i) == who]
        elif kind == "moved_on":
            works = any(name_of(i) == who for i in range(len(rows or [])))
            if works and slot and not any(name_of(i) == who for i in slot):
                hit = [-1]
        elif kind in ("retime_start", "retime_end"):
            want = _minutes(v.get("time"))
            col = "shift_start" if kind == "retime_start" else "shift_end"
            hit = [i for i in slot if _fam(rows[i].get("role"), families) == _fam(m.get("role"), families)
                   and want is not None and _minutes(rows[i].get(col)) != want]
        elif kind == "role_change":
            hit = [i for i in slot if name_of(i) == who and _nk(rows[i].get("role")) != _nk(v.get("role"))]
        elif kind == "leader_swap":
            names = {_nk(n) for n in v.get("names") or []}
            mine = [i for i in slot if not m.get("role")
                    or _fam(rows[i].get("role"), families) == _fam(m.get("role"), families)]
            if mine and not any(name_of(i) in names for i in mine):
                hit = [-1]
        elif kind in ("opener", "closer"):
            by_date = {}
            for i, r in enumerate(rows or []):
                if _weekday(r.get("date")) == day and _fam(r.get("role"), families) == _fam(m.get("role"), families):
                    s_, e_ = _minutes(r.get("shift_start")), _minutes(r.get("shift_end"))
                    if s_ is not None:
                        by_date.setdefault(r.get("date"), []).append((s_, r.get("employee"), r.get("role"),
                                                                       r.get("shift_start"), e_, i))
            for _d, g in by_date.items():
                mine = [x for x in g if _nk(x[1]) == who]
                if mine and who not in _edge_of([x[:5] for x in g], kind):
                    hit.append(mine[0][5])
        elif kind == "pair":
            # Its polarity is its meaning (schedule re-audit 10/4/26 LEARN-1:
            # every pair was read as "together", so the owner's Keep on a
            # learned keep-apart charged the week for keeping them apart).
            polarity = v.get("kind")
            if polarity not in ("prefer", "avoid"):
                continue
            group = {who} | {_nk(n) for n in v.get("with") or []}
            dates, idx = {}, {}
            for i, r in enumerate(rows or []):
                if name_of(i) in group:
                    d_, p_ = str(r.get("date"))[:10], _row_part(r)
                    dates.setdefault(d_, {}).setdefault(p_, set()).add(name_of(i))
                    idx.setdefault((d_, p_), []).append(i)
            for _d, parts in dates.items():
                if polarity == "avoid":
                    # Two of them on the same shift (a row's slot: its first
                    # daypart) — one miss, naming the rows on it.
                    clash = [i for p_, ps in parts.items() if len(ps) > 1 for i in idx[(_d, p_)]]
                    if clash:
                        hit.append(-1)
                        hit += clash
                    continue
                there = set().union(*parts.values())
                if len(there) > 1 and any(len(ps & there) < len(there) for ps in parts.values()):
                    hit.append(-1)
        elif kind == "ot_risk":
            head = float(v.get("headroom_hours") or 0)
            weeks = {}
            for i, r in enumerate(rows or []):
                if name_of(i) == who:
                    b = bucket(r.get("date")) if bucket else _week_of(r.get("date"))
                    try:
                        weeks[b] = weeks.get(b, 0.0) + float(r.get("scheduled_hours") or 0)
                    except (TypeError, ValueError):
                        continue
            for b, h in weeks.items():
                if head and h > line - head:
                    out.append({"key": m.get("key"), "kind": kind, "indexes": [],
                                "weight": round(conf * min(1.0, (h - (line - head)) / head), 3),
                                "text": f"{m.get('person')} is drafted {h:g}h — they usually run about {head:g}h over "
                                        f"what they are scheduled, past {line:g}h."})
            continue
        elif kind == "end_overrun":
            pad = int(v.get("minutes") or 0)
            want = _minutes(v.get("padded_end"))
            if pad and want is not None:
                closes = {}
                for i, r in enumerate(rows or []):
                    if _weekday(r.get("date")) == day and _end_part(r) == part \
                            and _fam(r.get("role"), families) == _fam(m.get("role"), families):
                        closes.setdefault(r.get("date"), []).append(i)
                for _d, idx in closes.items():
                    def _end(i):
                        if _minutes(rows[i].get("shift_start")) is None:
                            return None
                        return _row_end(rows[i])
                    ends = [(_end(i), i) for i in idx if _end(i) is not None]
                    if ends:
                        last = max(e for e, _i in ends)
                        want_adj = want + (1440 if want < 12 * 60 and last >= 1440 else 0)
                        hit += [i for e, i in ends if e == last and e < want_adj]
        if hit:
            out.append({"key": m.get("key"), "kind": kind, "tier": m.get("tier") or "soft",
                        "indexes": [i for i in hit if i >= 0],
                        "weight": round(conf * (len(hit) if kind in ("retime_start", "retime_end", "end_overrun")
                                                else 1), 3),
                        "text": _short(m)})
    return out


def learned_cost(rows, learned, families=None, line=None, bucket=None) -> float:
    """The total soft cost the learned signals put on a week (misses' weights)."""
    return round(sum(x["weight"] for x in misses(rows, learned, families, line, bucket)), 3)


def slot_cost(person, date_str, daypart, role, learned, families=None) -> float:
    """What putting `person` on `date_str`'s `daypart` in `role` costs (+)
    or earns (−) against the learned signals that are about one person on
    one slot (moved_off, moved_on, role_change): a solver's per-assignment
    term. Pure."""
    wd, who, cost = _weekday(date_str), _nk(person), 0.0
    for m in learned or []:
        if _nk(m.get("person")) != who or m.get("day") != wd or m.get("daypart") != daypart:
            continue
        conf = _miss_weight(m)
        if m.get("kind") == "moved_off":
            cost += conf
        elif m.get("kind") == "moved_on":
            cost -= conf
        elif m.get("kind") == "role_change":
            cost += 0.0 if _nk(role) == _nk((m.get("value") or {}).get("role")) else conf
    return round(cost, 3)


def _short(m) -> str:
    """One short line for a fact the passes also hold (L-28: anything
    enforced in code needs no paragraph)."""
    kind, v = m.get("kind"), m.get("value") or {}
    who, day, meal = m.get("person") or "", m.get("day") or "", _MEAL.get(m.get("daypart"), m.get("daypart") or "")
    role = v.get("role") if kind in ("opener", "closer") else m.get("role")
    if kind == "moved_off":
        return f"{who} off {day} {meal}"
    if kind == "moved_on":
        return f"{who} on {day} {meal}"
    if kind in ("retime_start", "retime_end"):
        return f"{role} {'start' if kind == 'retime_start' else 'end'} at {v.get('time')} on {day} {meal}"
    if kind == "role_change":
        return f"{who} as {v.get('role')} on {day} {meal}"
    if kind == "leader_swap":
        return f"one of {', '.join(v.get('names') or [])} on {day} {meal}" + (f" ({role})" if role else "")
    if kind == "opener":
        return f"{who} opens {role} on {day}s" + (f", from about {v.get('start')}" if v.get("start") else "")
    if kind == "closer":
        return f"{who} closes {v.get('role') or role} on {day}s" + (
            f", until about {v.get('end')}" if v.get("end") else "")
    if kind == "pair" and v.get("kind") == "avoid":
        # The owner's own keep-apart: never named where it is read (LEARN-6).
        return "two people the owner keeps apart, on the same shift"
    if kind == "pair":
        return f"{who} with {' and '.join(v.get('with') or [])} on the same shifts"
    if kind == "ot_risk":
        return f"{who}: keep about {v.get('headroom_hours')}h of room under the overtime line"
    if kind == "end_overrun":
        return f"{v.get('role') or role} closes on {day} {meal} end {v.get('minutes')} minutes later"
    return str(m.get("text") or "")
