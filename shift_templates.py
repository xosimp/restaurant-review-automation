"""
shift_templates.py — the shifts a restaurant really works, per role family,
weekday and daypart, read from its own weeks (scheduler audit 10/10/26
AI-20).

Learned history reached the model as up to 6,000 characters of ranked prose
(schedule_memory.prompt_lines), while what a SHAPE needs — the typical
starts, ends and lengths per role and weekday and daypart, and how often a
shift runs straight through — was never given as a table the model or code
could apply. Here it is, from the published weeks (and, before any, the
punches):

  templates(rows)        {(family, weekday, daypart): [{"start", "end",
                         "hours", "count", "share"}]} — the most common
                         start/end pairs, best first (starts and ends on the
                         quarter hour)
  straight_share(rows)   {(family, weekday): share of shifts that cover both
                         lunch and dinner}
  pick(templates, ...)   the template for a slot: the role's own for that
                         weekday and daypart, else the role's on any day at
                         that daypart, else None
  prompt_table(...)      the compact table the shape contract's prompt
                         carries in place of the learned prose that
                         duplicates the solver's signals

Pure: rows in, tables out. The deterministic draft (schedule_fallback's
"solver" source — requirement counts laid onto these templates, people
chosen by the solver) and the shape prompt (labor) read it.
"""
from collections import Counter, defaultdict

import shift_quality as _sq

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# A template is kept when it is at least this share of its slot's shifts, and
# at most this many per slot.
MIN_SHARE = 0.1
MAX_PER_SLOT = 3
QUARTER = 15


def _minutes(t):
    import schedule_rules as _r
    return _r.parse_minutes(str(t or ""))


def _fmt(m) -> str:
    m = int(m) % (24 * 60)
    h, mm = divmod(m, 60)
    return f"{(h % 12) or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


def _q(m):
    return int(round(float(m) / QUARTER) * QUARTER)


def _weekday(d):
    from datetime import date
    try:
        return date.fromisoformat(str(d)[:10]).strftime("%A")
    except (TypeError, ValueError):
        return None


def _span(r):
    s, e = _minutes(r.get("shift_start")), _minutes(r.get("shift_end"))
    if s is None or e is None:
        return None
    if e <= s:
        e += 24 * 60
    return s, e


def templates(rows, family=None) -> dict:
    """{(family, weekday, daypart): [template]} from `rows` (shift rows with
    date, role, shift_start, shift_end). A shift counts at the daypart it
    STARTS in (its first) — the slot it was written for. `family` maps a
    role to its family (default shift_quality.role_family)."""
    fam = family or _sq.role_family
    by = defaultdict(Counter)
    for r in rows or []:
        sp = _span(r)
        day = _weekday(r.get("date"))
        role = str(r.get("role") or "").strip()
        if sp is None or not day or not role:
            continue
        part = _sq.daypart_of(r.get("shift_start") or "")
        if part not in ("morning", "night"):
            continue
        by[(fam(role), day, part)][(_q(sp[0]), _q(sp[1]))] += 1
    out = {}
    for key, counts in by.items():
        total = float(sum(counts.values()))
        keep = []
        for (s, e), n in counts.most_common():
            share = n / total
            if share < MIN_SHARE and keep:
                break
            keep.append({"start": _fmt(s), "end": _fmt(e), "hours": round((e - s) / 60.0, 2), "count": n,
                         "share": round(share, 3)})
            if len(keep) >= MAX_PER_SLOT:
                break
        out[key] = keep
    return out


def straight_share(rows, family=None) -> dict:
    """{(family, weekday): share of its shifts present at lunch AND dinner}."""
    fam = family or _sq.role_family
    tot, both = Counter(), Counter()
    for r in rows or []:
        day = _weekday(r.get("date"))
        role = str(r.get("role") or "").strip()
        if not day or not role or _span(r) is None:
            continue
        key = (fam(role), day)
        tot[key] += 1
        parts = set(_sq.touched_dayparts(r) or [])
        if {"morning", "night"} <= parts:
            both[key] += 1
    return {k: round(both[k] / float(n), 3) for k, n in tot.items() if n}


def pick(tpl, role, day, part, family=None, i=0, n=1):
    """The template for the i-th of `n` slots of `role` on `day` at `part`:
    the slot's own templates spread over the n slots by their shares, else
    the role's at that part on any weekday, else None."""
    fam = (family or _sq.role_family)(role)
    own = tpl.get((fam, day, part)) or []
    if not own:
        pooled = Counter()
        for (f, _d, p), items in tpl.items():
            if f == fam and p == part:
                for t in items:
                    pooled[(t["start"], t["end"])] += t["count"]
        own = [{"start": s, "end": e, "count": n} for (s, e), n in pooled.most_common(MAX_PER_SLOT)]
    if not own:
        return None
    # Spread the n slots over the templates in proportion: slot i sits at
    # (i + 0.5) / n of the way through the templates' cumulative shares —
    # three slots on a 2:1 pair are two of the first and one of the second.
    weights = [max(1, int(t.get("count") or 1)) for t in own]
    total = float(sum(weights))
    at = (min(max(0, int(i)), max(1, int(n)) - 1) + 0.5) / max(1, int(n))
    cum = 0.0
    for t, w in zip(own, weights):
        cum += w / total
        if at < cum:
            return t
    return own[-1]


def prompt_table(tpl, days=None, max_lines=120) -> str:
    """The compact table the shape contract's prompt carries (AI-20): one
    line per role and daypart, the weekday's usual shifts with their share —
    "Server dinner: Fri 4:30pm-10:00pm 60%, 6:00pm-10:00pm 30% · Sat …"."""
    if not tpl:
        return ""
    want = list(days or DAYS)
    rows = defaultdict(list)
    for (fam, day, part), items in tpl.items():
        if day not in want or not items:
            continue
        cells = ", ".join(f"{t['start']}-{t['end']} {int(round(t['share'] * 100))}%" for t in items)
        rows[(fam, part)].append((want.index(day), f"{day[:3]} {cells}"))
    lines = []
    for (fam, part) in sorted(rows):
        bits = " · ".join(b for _i, b in sorted(rows[(fam, part)]))
        lines.append(f"  {fam.title()} {'lunch' if part == 'morning' else 'dinner'}: {bits}")
    if not lines:
        return ""
    head = ("\n\nTHIS RESTAURANT'S USUAL SHIFTS — from its own published weeks: per role and daypart, each weekday's "
            "usual start-end and how often it is worked. Shape each day from these unless SHIFT REQUIREMENTS need "
            "otherwise:\n")
    return head + "\n".join(lines[:max_lines])
