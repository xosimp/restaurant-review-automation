"""
schedule_learning.py — what the schedule engine learns from the manager's
edits and from how published weeks went.

schedule_versions keeps every state a week has been in; schedule_intel
records what each published week did. This module reads both and turns
them into facts and suggestions. Everything is per restaurant,
deterministic, and never calls a model.

  edit_patterns                retimes, headcount changes, role changes and
                               leader swaps the manager keeps making to the
                               draft (merged into
                               schedule_versions.learned_patterns)
  learned_headcount_adjustments  {(weekday, daypart): {role: delta}}
  addressed_recommendations    which stored recommendations an edit carried
                               out (the implicit 'accepted')
  calibrate_weights            how each Shift Quality dimension's score
                               tracked real outcomes; suggested weights only,
                               never applied
  attendance_by_weekday        no-show rate per person per weekday
  standby_days                 the dates in a week most likely to lose a
                               scheduled person
  overtime_forecast            who a draft pushes past the weekly ceiling,
                               and a same-role person with room to take a
                               shift
"""
import json
import logging
import math
import re
from datetime import datetime

import models as _models_mod
from models import DB_PATH

log = logging.getLogger(__name__)

EDIT_WEEKS = 8
NEW_PATTERN_CAP = 8
BUSY_NIGHTS = ("Friday", "Saturday")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_PRETTY = {"morning": "lunch/day", "night": "dinner/night"}

CALIBRATION_MIN_WEEKS = 8
CALIBRATION_MIN_SHIFTS = 40
CALIBRATION_MIN_PAIRS = 20        # per dimension per outcome, before a correlation is reported
CALIBRATION_MIN_EVIDENCE = 0.1    # |mean correlation| below this suggests no change
CALIBRATION_MAX_NUDGE = 0.30      # a suggestion never moves a weight more than 30% of its default

ATTENDANCE_MIN_WEEKDAY_SHIFTS = 4
ATTENDANCE_MIN_OVERALL_SHIFTS = 6     # staff_settings.reliability's floor


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md's bound-import hazard)."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()


# ── small helpers ─────────────────────────────────────────────────────────

def _weekday(date_str, fallback=""):
    try:
        return datetime.strptime((date_str or "")[:10], "%Y-%m-%d").strftime("%A")
    except (TypeError, ValueError):
        return fallback or ""


def _part(start):
    from shift_quality import daypart_of
    return daypart_of(start or "")


def _hours(r):
    try:
        return float(r.get("scheduled_hours") or 0)
    except (TypeError, ValueError):
        return 0.0


def _clock(value):
    """'16:30' and '4:30pm' both read '4:30pm'; unreadable stays as written."""
    from schedule_rules import parse_minutes, _fmt_minutes
    m = parse_minutes(value or "")
    return _fmt_minutes(m) if m is not None else (value or "").strip()


def _plural(role):
    role = (role or "").strip()
    return role if role.lower().endswith("s") else role + "s"


def _weeks_text(n):
    return f"{n} recent week{'s' if n != 1 else ''}"


def _display(counter: dict) -> str:
    """The most common spelling seen for a name or role."""
    return max(counter.items(), key=lambda kv: (kv[1], kv[0]))[0] if counter else ""


# ── the weeks the manager edited ──────────────────────────────────────────

def edited_weeks(restaurant_id, weeks=EDIT_WEEKS, db_path=DB_PATH) -> list:
    """One entry per calendar week the manager edited in the last `weeks`
    weeks: the generated draft, the newest version, and the diff between
    them. The net diff — not every intermediate save — is what the manager
    settled on, so a shift removed and put back in one sitting teaches
    nothing. Regenerated drafts of the same week count once (the newest)."""
    from schedule_versions import diff, rows_from_csv
    conn = get_conn(db_path)
    try:
        hist = conn.execute(
            "SELECT DISTINCT v.history_id, h.week_start FROM schedule_versions v JOIN schedule_history h ON h.id=v.history_id "
            "WHERE v.restaurant_id=? AND h.restaurant_id=? AND v.reason='edited' AND v.created_at >= datetime('now', ?) "
            "ORDER BY v.history_id DESC LIMIT 60",
            (restaurant_id, restaurant_id, f"-{int(weeks) * 7} days")).fetchall()
        newest = {}
        for h in hist:
            wk = h["week_start"] or f"#{h['history_id']}"
            if wk not in newest:
                newest[wk] = h["history_id"]
        ids = sorted(newest.values())
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        versions = conn.execute(
            f"SELECT history_id, version, reason, schedule_csv FROM schedule_versions WHERE restaurant_id=? "
            f"AND history_id IN ({marks}) ORDER BY history_id, version", (restaurant_id, *ids)).fetchall()
    finally:
        conn.close()
    by = {}
    for v in versions:
        by.setdefault(v["history_id"], []).append(v)
    out = []
    for hid in ids:
        vs = by.get(hid) or []
        if len(vs) < 2:
            continue
        base = next((v for v in vs if v["reason"] == "generated"), vs[0])
        final = vs[-1]
        if final["version"] <= base["version"]:
            continue
        b, f = rows_from_csv(base["schedule_csv"]), rows_from_csv(final["schedule_csv"])
        out.append({"history_id": hid, "base": b, "final": f, "diff": diff(b, f)})
    return out


# ── retimes ───────────────────────────────────────────────────────────────

def retime_patterns(week_edits: list, min_repeats=2) -> list:
    """Per role × weekday × daypart, the start or end the manager keeps
    moving shifts to — counted once per week, so three servers retimed on
    one Friday is one week of evidence, not three."""
    tally = {}
    for w in week_edits:
        for rt in (w["diff"].get("retimed") or []):
            role = (rt.get("role") or "").strip()
            if not role:
                continue
            day = _weekday(rt.get("date"), rt.get("day"))
            new_start, old_start = rt.get("new_start") or "", rt.get("old_start") or ""
            new_end, old_end = rt.get("new_end") or "", rt.get("old_end") or ""
            part = _part(new_start)
            if not day or part == "unknown":
                continue
            moves = []
            if new_start and _clock(new_start) != _clock(old_start):
                moves.append(("retime_start", _clock(new_start)))
            if new_end and _clock(new_end) != _clock(old_end):
                moves.append(("retime_end", _clock(new_end)))
            for kind, when in moves:
                e = tally.setdefault((kind, role.lower(), day, part, when), {"weeks": set(), "role": {}})
                e["weeks"].add(w["history_id"])
                e["role"][role] = e["role"].get(role, 0) + 1
    out = []
    for (kind, _rl, day, part, when), e in tally.items():
        n = len(e["weeks"])
        if n < min_repeats:
            continue
        role = _display(e["role"])
        verb = "starting" if kind == "retime_start" else "ending"
        out.append({"kind": kind, "employee": "", "role": role, "day": day, "daypart": part, "time": when, "times": n,
                    "text": f"The manager keeps {verb} {_plural(role)} on {day} {_PRETTY.get(part, part)} at {when} "
                            f"({_weeks_text(n)}) — {'start' if kind == 'retime_start' else 'end'} them then in the draft."})
    return out


# ── headcount ─────────────────────────────────────────────────────────────

def _slot_heads(rows: list) -> tuple:
    """({(date, daypart, role lower): {people}}, {role lower: {spelling: n}})."""
    out, names = {}, {}
    for r in rows:
        n = (r.get("employee") or "").strip().lower()
        role = (r.get("role") or "").strip()
        d = (r.get("date") or "")[:10]
        if not (n and role and d):
            continue
        part = _part(r.get("shift_start"))
        if part == "unknown":
            continue
        k = (d, part, role.lower())
        out.setdefault(k, set()).add(n)
        names.setdefault(role.lower(), {})
        names[role.lower()][role] = names[role.lower()].get(role, 0) + 1
    return out, names


def headcount_patterns(week_edits: list, min_repeats=2) -> list:
    """The net number of people per role × weekday × daypart the manager
    keeps adding to or taking off the draft. Needs `min_repeats` weeks
    moving the same way, and more of them than weeks moving the other way;
    the size is the median of those weeks."""
    tally, spellings = {}, {}
    for w in week_edits:
        before, sb = _slot_heads(w["base"])
        after, sa = _slot_heads(w["final"])
        for src in (sb, sa):
            for rl, c in src.items():
                for k, v in c.items():
                    spellings.setdefault(rl, {})[k] = spellings.get(rl, {}).get(k, 0) + v
        per = {}
        for k in set(before) | set(after):
            delta = len(after.get(k, ())) - len(before.get(k, ()))
            if delta:
                d, part, rl = k
                slot = (_weekday(d), part, rl)
                per[slot] = per.get(slot, 0) + delta
        for slot, delta in per.items():
            if delta:
                tally.setdefault(slot, []).append(delta)
    out = []
    for (day, part, rl), deltas in tally.items():
        ups = sorted(d for d in deltas if d > 0)
        downs = sorted(d for d in deltas if d < 0)
        side, other = (ups, downs) if len(ups) >= len(downs) else (downs, ups)
        if len(side) < min_repeats or len(side) <= len(other):
            continue
        delta = side[len(side) // 2]
        role = _display(spellings.get(rl, {})) or rl.title()
        n = len(side)
        if delta > 0:
            text = (f"The manager has added {delta} {role if delta == 1 else _plural(role)} to {day} "
                    f"{_PRETTY.get(part, part)} in {_weeks_text(n)} — draft {delta} more there.")
        else:
            text = (f"The manager has cut {-delta} {role if delta == -1 else _plural(role)} from {day} "
                    f"{_PRETTY.get(part, part)} in {_weeks_text(n)} — draft {-delta} fewer there.")
        out.append({"kind": "headcount_add" if delta > 0 else "headcount_cut", "employee": "", "role": role,
                    "day": day, "daypart": part, "delta": int(delta), "times": n, "text": text})
    return out


# ── role changes ──────────────────────────────────────────────────────────

def role_change_patterns(week_edits: list, min_repeats=2) -> list:
    """The same person, date and start kept, a different role: per person ×
    old role × new role × weekday × daypart, counted once per week."""
    tally = {}
    for w in week_edits:
        for rc in (w["diff"].get("role_changed") or []):
            name = (rc.get("employee") or "").strip()
            old, new = (rc.get("old_role") or "").strip(), (rc.get("new_role") or "").strip()
            day, part = _weekday(rc.get("date"), rc.get("day")), _part(rc.get("shift_start"))
            if not (name and new and day) or part == "unknown":
                continue
            e = tally.setdefault((name.lower(), old.lower(), new.lower(), day, part),
                                 {"weeks": set(), "name": {}, "old": {}, "new": {}})
            e["weeks"].add(w["history_id"])
            for fld, val in (("name", name), ("old", old), ("new", new)):
                e[fld][val] = e[fld].get(val, 0) + 1
    out = []
    for (_n, _o, _w, day, part), e in tally.items():
        n = len(e["weeks"])
        if n < min_repeats:
            continue
        name, old, new = _display(e["name"]), _display(e["old"]), _display(e["new"])
        out.append({"kind": "role_change", "employee": name, "role": new, "was_role": old, "day": day, "daypart": part,
                    "times": n,
                    "text": f"The manager keeps switching {name} from {old or 'no role'} to {new} on {day} "
                            f"{_PRETTY.get(part, part)} ({_weeks_text(n)}) — draft them as {new} there."})
    return out


# ── leader substitutions on busy nights ───────────────────────────────────

def _strength(restaurant_id, db_path=DB_PATH) -> tuple:
    """(closers lower, {name lower: Operational Score}) — empty when unrated."""
    leaders, scores = set(), {}
    try:
        from models import get_leader_flags, get_operational_scores
        leaders = {" ".join(str(n).split()).casefold() for n, v in (get_leader_flags(restaurant_id, db_path) or {}).items() if v}
        scores = {" ".join(str(n).split()).casefold(): float(s)
                  for n, s in (get_operational_scores(restaurant_id, db_path) or {}).items() if s is not None}
    except Exception as e:
        log.warning("schedule_learning: strength lookup failed for %s: %s", restaurant_id, e)
    return leaders, scores


def leader_swap_patterns(week_edits: list, leaders: set, scores: dict, min_repeats=2) -> list:
    """A 'moved' edit on a Friday or Saturday night where the person the
    manager put in is a closer and the one taken off is not, or has the
    higher Operational Score. Per weekday × role, once per week."""
    tally = {}
    for w in week_edits:
        for m in (w["diff"].get("moved") or []):
            day, part = _weekday(m.get("date"), m.get("day")), _part(m.get("shift_start"))
            if day not in BUSY_NIGHTS or part != "night":
                continue
            inc = " ".join(str(m.get("to") or "").split())
            out_ = " ".join(str(m.get("from") or "").split())
            il, ol = inc.casefold(), out_.casefold()
            if not il or not ol:
                continue
            stronger = (il in leaders and ol not in leaders) or \
                       (il in scores and ol in scores and scores[il] > scores[ol])
            if not stronger:
                continue
            role = (m.get("role") or "").strip()
            e = tally.setdefault((day, part, role.lower()), {"weeks": set(), "role": {}, "names": {}})
            e["weeks"].add(w["history_id"])
            e["role"][role] = e["role"].get(role, 0) + 1
            e["names"][inc] = e["names"].get(inc, 0) + 1
    out = []
    for (day, part, _rl), e in tally.items():
        n = len(e["weeks"])
        if n < min_repeats:
            continue
        role = _display(e["role"])
        names = [k for k, _v in sorted(e["names"].items(), key=lambda kv: (-kv[1], kv[0]))][:3]
        out.append({"kind": "leader_swap", "employee": "", "role": role, "day": day, "daypart": part, "times": n,
                    "names": names,
                    "text": f"On {day} {_PRETTY.get(part, part)} the manager keeps swapping a stronger hand onto "
                            f"{role or 'the floor'} — a closer or a higher Operational Score ({', '.join(names)}) — "
                            f"in {_weeks_text(n)}. Draft a leader there."})
    return out


def edit_patterns(restaurant_id, weeks=EDIT_WEEKS, min_repeats=2, db_path=DB_PATH, week_edits=None) -> list:
    """Every new pattern kind, strongest first, capped at NEW_PATTERN_CAP.
    Same shape as schedule_versions.learned_patterns' own entries (kind,
    employee, day, daypart, times, text) plus role / time / delta /
    was_role, which schedule_intel.pattern_key folds into the key."""
    week_edits = edited_weeks(restaurant_id, weeks, db_path) if week_edits is None else week_edits
    if not week_edits:
        return []
    out = retime_patterns(week_edits, min_repeats) + headcount_patterns(week_edits, min_repeats) + \
        role_change_patterns(week_edits, min_repeats)
    if any(w["diff"].get("moved") for w in week_edits):
        leaders, scores = _strength(restaurant_id, db_path)
        if leaders or scores:
            out += leader_swap_patterns(week_edits, leaders, scores, min_repeats)
    out.sort(key=lambda p: (-p["times"], p["kind"], p.get("day") or "", p.get("role") or "", p.get("employee") or ""))
    return out[:NEW_PATTERN_CAP]


def learned_headcount_adjustments(restaurant_id, weeks=EDIT_WEEKS, min_weeks=2, db_path=DB_PATH) -> dict:
    """{(weekday, daypart): {role: delta}} — the net headcount the manager
    keeps adding (+) or cutting (−) per role, for a requirements table to
    apply on top of its own figure. Dismissed patterns are left out; the
    role is spelled as the schedule spells it."""
    from schedule_intel import dismissed_patterns, pattern_key
    pats = headcount_patterns(edited_weeks(restaurant_id, weeks, db_path), min_weeks)
    if not pats:
        return {}
    dismissed = dismissed_patterns(restaurant_id, db_path)
    out = {}
    for p in pats:
        if pattern_key(p) in dismissed:
            continue
        out.setdefault((p["day"], p["daypart"]), {})[p["role"]] = p["delta"]
    return out


# ── implicit recommendation acceptance ───────────────────────────────────

_FILL = re.compile(r"^Fill the gap on (?P<where>.+?): (?P<role>.+?) short \d+ of \d+")
_LEAD = re.compile(r'^Move somebody who clears ".*" onto (?P<where>.+?)\.?$')
_PAIR = re.compile(r"^Pair (?P<name>.+?) on (?P<where>.+?) with a stronger hand")
_TRIM = re.compile(r"^Trim about (?P<h>\d+(?:\.\d+)?)h from (?P<where>.+?) to get back")
_REST = re.compile(r"^Give (?P<name>.+?) a day off — \d+ in a row")


def _slot_test(where: str):
    """'Saturday night' or '2026-10-10' → a row predicate, or None."""
    where = (where or "").strip().rstrip(".")
    parts = where.split()
    if len(parts) == 2 and parts[0] in WEEKDAYS and parts[1] in ("morning", "night"):
        day, part = parts
        return lambda r: _weekday(r.get("date"), r.get("day")) == day and _part(r.get("shift_start")) == part
    if re.match(r"^\d{4}-\d{2}-\d{2}$", where):
        return lambda r: (r.get("date") or "")[:10] == where
    return None


def _people(rows, test, role=None):
    return {(r.get("employee") or "").strip().lower() for r in rows if test(r) and (r.get("employee") or "").strip()
            and (role is None or (r.get("role") or "").strip().lower() == role)}


def _longest_run(rows, name_low):
    from datetime import date as _d
    days = sorted({(r.get("date") or "")[:10] for r in rows if (r.get("employee") or "").strip().lower() == name_low})
    best = run = 0
    prev = None
    for ds in days:
        try:
            cur = _d.fromisoformat(ds)
        except ValueError:
            continue
        run = run + 1 if prev is not None and (cur - prev).days == 1 else 1
        best = max(best, run)
        prev = cur
    return best


def addressed_recommendations(recommendations: list, before_rows: list, after_rows: list) -> list:
    """The recommendations (shift_quality.recommendations sentences) that
    an edit from before_rows to after_rows carried out. Conservative — a
    sentence it cannot read, or an edit that only partly moves toward it,
    is not counted."""
    out = []
    for rec in recommendations or []:
        rec = str(rec or "")
        hit = False
        m = _FILL.match(rec)
        if m:
            test = _slot_test(m.group("where"))
            role = m.group("role").strip().lower()
            hit = bool(test) and len(_people(after_rows, test, role)) > len(_people(before_rows, test, role))
        elif _LEAD.match(rec):
            test = _slot_test(_LEAD.match(rec).group("where"))
            hit = bool(test) and bool(_people(after_rows, test) - _people(before_rows, test))
        elif _PAIR.match(rec):
            m = _PAIR.match(rec)
            test = _slot_test(m.group("where"))
            if test:
                name = m.group("name").strip().lower()
                b, a = _people(before_rows, test), _people(after_rows, test)
                hit = (name in b and name not in a) or bool(a - b)
        elif _TRIM.match(rec):
            m = _TRIM.match(rec)
            test = _slot_test(m.group("where"))
            if test:
                cut = sum(_hours(r) for r in before_rows if test(r)) - sum(_hours(r) for r in after_rows if test(r))
                hit = cut >= max(1.0, 0.5 * float(m.group("h")))
        elif _REST.match(rec):
            name = _REST.match(rec).group("name").strip().lower()
            hit = _longest_run(after_rows, name) < _longest_run(before_rows, name)
        if hit:
            out.append(rec)
    return out


# ── outcome calibration ───────────────────────────────────────────────────

def _pearson(pairs):
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    if sxx <= 1e-9 or syy <= 1e-9:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    return sxy / math.sqrt(sxx * syy)


def calibrate_weights(restaurant_id, db_path=DB_PATH) -> dict:
    """How each Shift Quality dimension's score lined up with what the shift
    actually did, over the published weeks schedule_intel.record_outcomes
    has recorded — and a suggested weight for each, at most 30% either side
    of the default. Nothing is applied: the owner (or an engineer) decides.

    Outcomes per shift: coverage/no-show issues (fewer is better), the
    day's review rating (higher is better), labor % against the week's
    target (lower is better). A dimension whose higher scores went with
    better outcomes is suggested up; one whose scores told nothing, or
    pointed the wrong way, is suggested down. Deterministic."""
    from shift_quality import DEFAULT_WEIGHTS
    conn = get_conn(db_path)
    try:
        outs = conn.execute("SELECT history_id, date, daypart, issues, review_rating, labor_pct FROM schedule_outcomes "
                            "WHERE restaurant_id=?", (restaurant_id,)).fetchall()
        ids = sorted({o["history_id"] for o in outs})
        hist = {}
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            marks = ",".join("?" for _ in chunk)
            for h in conn.execute(f"SELECT id, quality_json, labor_target FROM schedule_history WHERE restaurant_id=? "
                                  f"AND id IN ({marks})", (restaurant_id, *chunk)).fetchall():
                hist[h["id"]] = h
    finally:
        conn.close()
    dims_by = {}
    for hid, h in hist.items():
        try:
            q = json.loads(h["quality_json"] or "null") or {}
        except (TypeError, ValueError):
            continue
        for s in q.get("shifts") or []:
            if not s.get("scored"):
                continue
            dims_by[(hid, s.get("date"), s.get("daypart"))] = {
                d["key"]: float(d["score"]) for d in s.get("dimensions") or [] if d.get("key") and d.get("score") is not None}
    samples = []
    for o in outs:
        dims = dims_by.get((o["history_id"], o["date"], o["daypart"]))
        if not dims:
            continue
        target = hist[o["history_id"]]["labor_target"] if o["history_id"] in hist else None
        labor = None
        if o["labor_pct"] is not None and target:
            labor = float(o["labor_pct"]) - float(target)
        samples.append((dims, {"issues": float(o["issues"] or 0),
                               "review_rating": float(o["review_rating"]) if o["review_rating"] is not None else None,
                               "labor_vs_target": labor}, o["history_id"]))
    n_weeks = len({s[2] for s in samples})
    base = {"weeks": n_weeks, "shifts": len(samples),
            "needs": {"weeks": CALIBRATION_MIN_WEEKS, "shifts": CALIBRATION_MIN_SHIFTS}}
    if n_weeks < CALIBRATION_MIN_WEEKS or len(samples) < CALIBRATION_MIN_SHIFTS:
        return {"ready": False, **base,
                "reason": (f"{n_weeks} published week{'s' if n_weeks != 1 else ''} and {len(samples)} shift outcome"
                           f"{'s' if len(samples) != 1 else ''} with a stored score — calibration needs at least "
                           f"{CALIBRATION_MIN_WEEKS} weeks and {CALIBRATION_MIN_SHIFTS} shifts.")}
    # +1 = a higher score should mean a higher outcome value is GOOD; −1 = lower is good.
    direction = {"issues": -1, "review_rating": 1, "labor_vs_target": -1}
    report, suggested = {}, {}
    for key, default in DEFAULT_WEIGHTS.items():
        corr, pairs_n, evidence = {}, {}, []
        for outcome, sign in direction.items():
            pairs = [(dims[key], oc[outcome]) for dims, oc, _h in samples if key in dims and oc[outcome] is not None]
            pairs_n[outcome] = len(pairs)
            r = _pearson(pairs) if len(pairs) >= CALIBRATION_MIN_PAIRS else None
            corr[outcome] = round(r, 3) if r is not None else None
            if r is not None:
                evidence.append(sign * r)
        ev = sum(evidence) / len(evidence) if evidence else None
        nudge = 0.0
        if ev is not None and abs(ev) >= CALIBRATION_MIN_EVIDENCE:
            nudge = max(-CALIBRATION_MAX_NUDGE, min(CALIBRATION_MAX_NUDGE, ev))
        weight = round(default * (1 + nudge), 1)
        suggested[key] = weight
        report[key] = {"default": default, "suggested": weight, "nudge_pct": int(round(nudge * 100)),
                       "evidence": round(ev, 3) if ev is not None else None,
                       "correlation": corr, "pairs": pairs_n,
                       "reading": ("tracked better outcomes" if nudge > 0 else
                                   "did not track outcomes here" if nudge < 0 else
                                   "no clear signal yet")}
    return {"ready": True, **base, "applied": False, "dimensions": report, "suggested_weights": suggested,
            "note": "Suggestions only — the engine keeps its current weights until someone changes them."}


# ── attendance by weekday ─────────────────────────────────────────────────

def _attendance_tally(restaurant_id) -> dict:
    """{name: {"all": [shifts, no_shows], weekday: [shifts, no_shows]}} from
    clocked shifts — the same rows and rule staff_settings.reliability uses
    (a scheduled shift with actual_hours of zero is a no-show)."""
    from models import _cached_shifts
    from labor import _has_actual_hours
    tally = {}
    for s in _cached_shifts(restaurant_id) or []:
        n = (s.get("employee") or "").strip()
        if not n or not _has_actual_hours(s):
            continue
        try:
            sched = float(s.get("scheduled_hours") or s.get("hours") or 0)
            actual = float(s.get("actual_hours") or 0)
        except (TypeError, ValueError):
            continue
        if sched <= 0:
            continue
        wd = _weekday(s.get("date"))
        if not wd:
            continue
        t = tally.setdefault(n, {"all": [0, 0]})
        miss = 1 if actual == 0 else 0
        for k in ("all", wd):
            e = t.setdefault(k, [0, 0])
            e[0] += 1
            e[1] += miss
    return tally


def attendance_by_weekday(restaurant_id, min_shifts=ATTENDANCE_MIN_WEEKDAY_SHIFTS, db_path=DB_PATH) -> dict:
    """{name: {weekday: {"shifts": n, "no_shows": k, "no_show_rate": r}}} —
    only weekdays with at least `min_shifts` clocked shifts for that person."""
    out = {}
    for n, t in _attendance_tally(restaurant_id).items():
        for wd in WEEKDAYS:
            e = t.get(wd)
            if e and e[0] >= min_shifts:
                out.setdefault(n, {})[wd] = {"shifts": e[0], "no_shows": e[1], "no_show_rate": round(e[1] / e[0], 2)}
    return out


def _week_rows(restaurant_id, week_dates, db_path=DB_PATH) -> list:
    from schedule_versions import rows_from_csv
    if not week_dates:
        return []
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT schedule_csv FROM schedule_history WHERE restaurant_id=? AND week_start=? "
                           "ORDER BY (published_at IS NOT NULL) DESC, id DESC LIMIT 1",
                           (restaurant_id, sorted(week_dates)[0])).fetchone()
    finally:
        conn.close()
    return rows_from_csv(row["schedule_csv"]) if row else []


def standby_days(restaurant_id, week_dates, rows=None, limit=2, min_risk=0.1, db_path=DB_PATH) -> list:
    """The dates in `week_dates` where the people scheduled are most likely
    to lose one to a no-show: each person's no-show rate on that weekday
    (their overall rate when the weekday is too thin; nothing when they have
    no clocked record), combined as the chance at least one misses. `rows`
    is the draft; without it, the stored week starting week_dates[0]."""
    rows = _week_rows(restaurant_id, week_dates, db_path) if rows is None else rows
    if not rows:
        return []
    tally = {k.strip().lower(): v for k, v in _attendance_tally(restaurant_id).items()}
    wanted = set(week_dates or [])
    by_date = {}
    for r in rows:
        d = (r.get("date") or "")[:10]
        n = (r.get("employee") or "").strip()
        if n and d and (not wanted or d in wanted):
            by_date.setdefault(d, {})[n.lower()] = n
    out = []
    for d, people in by_date.items():
        wd = _weekday(d)
        risks, unknown = [], 0
        for low, name in people.items():
            t = tally.get(low) or {}
            day_e, all_e = t.get(wd), t.get("all")
            if day_e and day_e[0] >= ATTENDANCE_MIN_WEEKDAY_SHIFTS:
                risks.append((name, day_e[1] / day_e[0], f"{wd}s"))
            elif all_e and all_e[0] >= ATTENDANCE_MIN_OVERALL_SHIFTS:
                risks.append((name, all_e[1] / all_e[0], "overall"))
            else:
                unknown += 1
        expected = sum(p for _n, p, _b in risks)
        stay = 1.0
        for _n, p, _b in risks:
            stay *= (1 - p)
        chance = 1 - stay
        if chance < min_risk:
            continue
        top = sorted((x for x in risks if x[1] > 0), key=lambda x: (-x[1], x[0]))[:3]
        out.append({"date": d, "day": wd, "scheduled": len(people), "no_record": unknown,
                    "expected_no_shows": round(expected, 2), "chance_of_a_no_show": round(chance, 2),
                    "people": [{"employee": n, "no_show_rate": round(p, 2), "basis": b} for n, p, b in top]})
    out.sort(key=lambda x: (-x["chance_of_a_no_show"], x["date"]))
    return out[:limit]


# ── overtime forecast ─────────────────────────────────────────────────────

def overtime_forecast(rows: list, constraints=None, base_hours=None, bucket=None, ceiling=None,
                      max_hours=None, roster_roles=None) -> list:
    """Who this draft puts past the weekly ceiling in a payroll week,
    counting hours already published in that payroll week, with a same-role
    person who has room to take one of their shifts. Pure.

    constraints (schedule_rules.Constraints) supplies base_hours, bucket and
    each person's cap; otherwise pass base_hours ({lower: {bucket: h}} or
    {lower: h}), bucket (date → payroll-week key), ceiling (default 40) and
    max_hours (name → cap). roster_roles ({name: role}) widens the pool of
    candidates beyond the people already in the draft."""
    from shift_quality import _SwapIndex, WEEKLY_HOURS_CEILING
    rows = [r for r in (rows or []) if (r.get("employee") or "").strip()]
    if constraints is not None:
        base_hours = constraints.base_hours if base_hours is None else base_hours
        bucket = constraints.bucket if bucket is None else bucket
        max_hours = constraints.max_hours if max_hours is None else max_hours
    ceil = float(ceiling or WEEKLY_HOURS_CEILING)

    def _cap(name):
        if max_hours is not None:
            try:
                v = max_hours(name)
                if v:
                    return float(v)
            except (TypeError, ValueError):
                pass
        return ceil

    def _bucket(d):
        if bucket is None or not d:
            return ""
        try:
            return bucket(d) or ""
        except (TypeError, ValueError):
            return ""

    def _base(low, b):
        v = (base_hours or {}).get(low, 0) or 0
        if isinstance(v, dict):
            return float(v.get(b, 0) or 0) if bucket is not None else float(sum(float(x or 0) for x in v.values()))
        return float(v)

    rules = {}
    if constraints is not None:
        c = constraints
        rules = {"blocked_dates": c.blocked_dates, "daypart_avail": c.daypart_avail, "inactive": sorted(c.inactive),
                 "minors": sorted(c.minors), "certifications": c.certifications,
                 "role_requirements": c.role_requirements, "time_windows": c.time_windows,
                 "min_rest_hours": (c.compliance or {}).get("min_rest_hours") or 0, "base_rows": c.base_rows}
    idx = _SwapIndex(rows, {}, {}, rules)

    draft = {}           # (low, bucket) -> hours
    names, roles = {}, {}
    for r in rows:
        n = r["employee"].strip()
        low = n.lower()
        names[low] = n
        k = (low, _bucket(r.get("date")))
        draft[k] = draft.get(k, 0.0) + _hours(r)
        role = (r.get("role") or "").strip().lower()
        if role:
            roles.setdefault(role, set()).add(low)
    for n, role in (roster_roles or {}).items():
        if n and role:
            low = n.strip().lower()
            names.setdefault(low, n.strip())
            roles.setdefault(str(role).strip().lower(), set()).add(low)

    def _total(low, b):
        return draft.get((low, b), 0.0) + _base(low, b)

    out = []
    for (low, b), h in sorted(draft.items()):
        name = names[low]
        total, cap = _total(low, b), _cap(name)
        if total <= cap + 0.05:
            continue
        over = round(total - cap, 1)
        mine = [(i, r) for i, r in enumerate(rows) if r["employee"].strip().lower() == low and _bucket(r.get("date")) == b]
        # The shift that clears the overtime with the least disruption: the
        # smallest one at least as long as the overage, else the longest.
        enough = sorted((x for x in mine if _hours(x[1]) >= over), key=lambda x: _hours(x[1]))
        order = enough + sorted((x for x in mine if _hours(x[1]) < over), key=lambda x: -_hours(x[1]))
        pick = None
        for i, r in order:
            role = (r.get("role") or "").strip().lower()
            best = None
            for cand in sorted(roles.get(role, set()) - {low}):
                cname = names.get(cand, cand)
                room = _cap(cname) - _total(cand, b)
                if room + 0.05 < _hours(r):
                    continue
                if (cand, r.get("date")) in idx.working or not idx.person_fits(cname, r):
                    continue
                if best is None or room > best[1]:
                    best = (cname, room)
            if best:
                pick = {"employee": best[0], "role": r.get("role") or "", "headroom": round(best[1], 1),
                        "date": r.get("date"), "shift_start": r.get("shift_start"), "shift_end": r.get("shift_end"),
                        "hours": round(_hours(r), 1)}
                break
        from time_utils import mdy
        published = round(_base(low, b), 1)
        text = (f"{name} is scheduled for {round(total, 1):g}h in the payroll week{f' of {mdy(b)}' if b else ''} — "
                f"{over:g}h past {cap:g}h")
        text += f" ({published:g}h already published)." if published else "."
        if pick:
            text += (f" {pick['employee']} ({pick['role'] or 'same role'}, {pick['headroom']:g}h of room) could take "
                     f"the {mdy(pick['date'])} {pick['shift_start']}–{pick['shift_end']} shift.")
        out.append({"employee": name, "bucket": b, "hours": round(total, 1), "draft_hours": round(h, 1),
                    "published_hours": published, "ceiling": cap, "over": over, "candidate": pick, "text": text})
    out.sort(key=lambda x: (-x["over"], x["employee"]))
    return out
