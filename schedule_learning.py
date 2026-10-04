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
  predict_row_edits            which rows of a draft the manager is likely
                               to change, from smoothed edit rates over their
                               own finished drafts (edit_prediction_backtest
                               measures it on held-out weeks); once the
                               backtest has earned it, likely_edit_signals /
                               likely_to_change put it to use (L-15)
  learning_weeks / edited_weeks  every week a person finished with, read by
                               schedule_versions.learning_weeks (the original
                               draft, the manager's own pre-publish changes)
  capture_save                 what a save teaches as it is made: Cavnar AI's
                               kept changes credit that move, a change to a
                               sent week is a reaction, the first weeks ask a
                               one-tap why (answer_edit_question)
  calibrate_weights            the Shift Quality weights fitted to what
                               published shifts did (a joint ridge fit per
                               outcome, watched nights only for issues,
                               bounded steps, the outcome that drove each
                               change named); suggested only, the owner applies
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

# How far back the edit learners read (schedule audit 10/3/26 L-27): 24
# weeks, every week weighted by its age's half-life
# (schedule_versions.HALF_LIFE_DAYS — a person on a slot 120 days, a role's
# headcount or start 180) instead of a flat 8-week cliff. Within the
# intermediate versions' retention floor (ops "schedule_versions", 168 days).
EDIT_WEEKS = 24
NEW_PATTERN_CAP = 8
BUSY_NIGHTS = ("Friday", "Saturday")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_PRETTY = {"morning": "lunch/day", "night": "dinner/night"}

CALIBRATION_MIN_WEEKS = 8
CALIBRATION_MIN_SHIFTS = 40
# More shifts per dimension (schedule audit 10/3/26 SQ-22): sixteen
# dimensions were fitted together on as few as forty shifts. A dimension now
# needs CALIBRATION_MIN_PAIRS shifts with the outcome before it is read at
# all, and the joint fit carries at most one dimension per
# CALIBRATION_SAMPLES_PER_DIMENSION shifts — the best-observed first; the
# rest wait, and say so.
CALIBRATION_MIN_PAIRS = 30        # per dimension per outcome, before a correlation is reported
CALIBRATION_SAMPLES_PER_DIMENSION = 10
CALIBRATION_MIN_EVIDENCE = 0.1    # |mean fitted effect| below this suggests no change
CALIBRATION_MAX_NUDGE = 0.30      # the fitted weight never strays more than 30% from its default
CALIBRATION_MAX_STEP = 0.10       # one Apply moves a weight at most 10% of its default
CALIBRATION_RIDGE = 0.1           # ridge penalty, as a share of the shifts fitted (standardized)
# Floors and bars, per shift profile (SQ-22): weights cannot lift a shift a
# floor caps, so calibration also reads, for each profile with enough of
# its own shifts on record, where its critical floors and its quality bar
# sit against what those shifts did — the line under which shifts measurably
# went worse — and suggests a bounded step toward it.
CALIBRATION_MIN_PROFILE_SHIFTS = 30   # a profile's own shifts with an outcome, before its floors and bar are read
CALIBRATION_MIN_SIDE = 10             # shifts on each side of a candidate line
CALIBRATION_THRESHOLD_EVIDENCE = 0.5  # the outcome gap (in standard deviations) a line must show
CALIBRATION_THRESHOLD_STEP = 5        # one Apply moves a floor or a bar at most this many points
CALIBRATION_THRESHOLD_RANGE = 20      # lines are looked for within this many points of where it is now

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


def _weeks_text(n, of=None):
    from schedule_versions import weeks_phrase
    return weeks_phrase(n, of)


def _display(counter: dict) -> str:
    """The most common spelling seen for a name or role."""
    return max(counter.items(), key=lambda kv: (kv[1], kv[0]))[0] if counter else ""


# ── the weeks the manager edited ──────────────────────────────────────────

def learning_weeks(restaurant_id, weeks=EDIT_WEEKS, db_path=DB_PATH) -> list:
    """Every week a person of the restaurant finished with in the last
    `weeks` weeks — schedule_versions.learning_weeks: the ORIGINAL draft
    (before any redo the owner asked for), the manager's own pre-publish
    changes only (no reaction after the week went out, no change Cavnar AI
    made, no admin's hand, no change the owner called a one-off), unedited
    weeks included as the evidence a draft was kept. Each: {history_id,
    week_start, base, final, diff, editor, edited, age_days, ...}."""
    import schedule_versions as _sv
    return _sv.learning_weeks(restaurant_id, weeks, db_path)


def edited_weeks(restaurant_id, weeks=EDIT_WEEKS, db_path=DB_PATH) -> list:
    """The weeks of learning_weeks in which the manager changed something:
    the generated draft, the week as they settled it before it went out, and
    the diff between them. The net diff — not every intermediate save — is
    what the manager settled on, so a shift removed and put back in one
    sitting teaches nothing.

    What it no longer counts (schedule audit 10/3/26): saves after the first
    publish (L-4 — a replacement for a call-out is a reaction, not a habit),
    Cavnar AI's changes the owner saved (L-5), an admin's changes through
    view-as unless adopted — the rest of that week still counts (L-8) — and
    the days an owner's redo replaced: the draft is the original one, so the
    edits made before the redo are kept (L-26)."""
    return [w for w in learning_weeks(restaurant_id, weeks, db_path) if w["edited"]]


def _weight(w, kind) -> float:
    from schedule_versions import recency_weight, half_life
    return recency_weight(w.get("age_days") or 0, half_life(kind))


def _strength(hits_w, opps_w) -> tuple:
    from schedule_versions import _strength as _s
    return _s(hits_w, opps_w)


# ── retimes ───────────────────────────────────────────────────────────────

def _min_rate():
    from schedule_versions import PATTERN_MIN_RATE
    return PATTERN_MIN_RATE


def retime_patterns(week_edits: list, min_repeats=2, min_rate=None) -> list:
    """Per role × weekday × daypart, the start or end the manager keeps
    moving shifts to — counted once per week, so three servers retimed on
    one Friday is one week of evidence, not three — in at least `min_rate`
    of the weeks the draft had that role there at another time (L-6),
    recent weeks weighing more (L-27)."""
    min_rate = _min_rate() if min_rate is None else min_rate
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
                e = tally.setdefault((kind, role.lower(), day, part, when), {"weeks": {}, "role": {}})
                e["weeks"][w["history_id"]] = (_weight(w, kind), str(w.get("week_start") or ""))
                e["role"][role] = e["role"].get(role, 0) + 1
    # Where each week's draft had each role, and at what times.
    drafted = []
    for w in week_edits:
        at = {}
        for r in w.get("base") or []:
            role = (r.get("role") or "").strip().lower()
            day, part = _weekday(r.get("date"), r.get("day")), _part(r.get("shift_start"))
            if role and day and part != "unknown":
                e = at.setdefault((role, day, part), {"starts": set(), "ends": set()})
                e["starts"].add(_clock(r.get("shift_start")))
                e["ends"].add(_clock(r.get("shift_end")))
        drafted.append((w, at))
    out = []
    for (kind, rl, day, part, when), e in tally.items():
        n = len(e["weeks"])
        if n < min_repeats:
            continue
        opp_w, of = 0.0, 0
        for w, at in drafted:
            slot = at.get((rl, day, part))
            hit = w["history_id"] in e["weeks"]
            times = (slot or {}).get("starts" if kind == "retime_start" else "ends") or set()
            if hit or (slot and times != {when}):
                opp_w += _weight(w, kind)
                of += 1
        rate, conf = _strength(sum(x[0] for x in e["weeks"].values()), opp_w)
        if rate < min_rate:
            continue
        role = _display(e["role"])
        verb = "starting" if kind == "retime_start" else "ending"
        out.append({"kind": kind, "employee": "", "role": role, "day": day, "daypart": part, "time": when, "times": n,
                    "last_week": max(e["weeks"]), "last_week_start": max(x[1] for x in e["weeks"].values()),
                    "opportunities": of, "rate": rate, "confidence": conf,
                    "text": f"The manager keeps {verb} {_plural(role)} on {day} {_PRETTY.get(part, part)} at {when} "
                            f"({_weeks_text(n, of)}) — {'start' if kind == 'retime_start' else 'end'} them then in the draft."})
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
        # Every daypart the shift is on the floor for — the rule the scorer
        # and the requirements table count by — so a 2-10pm server the
        # manager adds teaches "+1 dinner", not "+1 lunch".
        from shift_quality import present_dayparts
        for part in present_dayparts(r):
            if part == "unknown":
                continue
            out.setdefault((d, part, role.lower()), set()).add(n)
        names.setdefault(role.lower(), {})
        names[role.lower()][role] = names[role.lower()].get(role, 0) + 1
    return out, names


def headcount_patterns(week_edits: list, min_repeats=2, min_rate=None) -> list:
    """The net number of people per role × weekday × daypart the manager
    keeps adding to or taking off the draft. Needs `min_repeats` weeks
    moving the same way, more of them than weeks moving the other way, and
    at least `min_rate` of the weeks that daypart traded (L-6: two adds in
    eight Fridays is an occasion, not a habit — an unedited Friday is a
    Friday the draft's count was kept), recent weeks weighing more (L-27);
    the size is the median of those weeks."""
    min_rate = _min_rate() if min_rate is None else min_rate
    tally, spellings, traded = {}, {}, []
    for w in week_edits:
        before, sb = _slot_heads(w["base"])
        after, sa = _slot_heads(w["final"])
        for src in (sb, sa):
            for rl, c in src.items():
                for k, v in c.items():
                    spellings.setdefault(rl, {})[k] = spellings.get(rl, {}).get(k, 0) + v
        # The weekday dayparts that traded this week (any shift on them).
        traded.append((w, {(_weekday(d), part) for (d, part, _rl) in set(before) | set(after)}))
        per = {}
        for k in set(before) | set(after):
            delta = len(after.get(k, ())) - len(before.get(k, ()))
            if delta:
                d, part, rl = k
                slot = (_weekday(d), part, rl)
                per[slot] = per.get(slot, 0) + delta
        for slot, delta in per.items():
            if delta:
                tally.setdefault(slot, []).append((delta, int(w.get("history_id") or 0), _weight(w, "headcount_add"),
                                                   str(w.get("week_start") or "")))
    out = []
    for (day, part, rl), pairs in tally.items():
        deltas = [d for d, _h, _w, _s in pairs]
        ups = sorted(d for d in deltas if d > 0)
        downs = sorted(d for d in deltas if d < 0)
        side, other = (ups, downs) if len(ups) >= len(downs) else (downs, ups)
        if len(side) < min_repeats or len(side) <= len(other):
            continue
        delta = side[len(side) // 2]
        hits = [p for p in pairs if (p[0] > 0) == (delta > 0)]
        opp_w, of = 0.0, 0
        for w, slots in traded:
            if (day, part) in slots:
                opp_w += _weight(w, "headcount_add")
                of += 1
        rate, conf = _strength(sum(p[2] for p in hits), opp_w)
        if rate < min_rate:
            continue
        role = _display(spellings.get(rl, {})) or rl.title()
        n = len(side)
        if delta > 0:
            text = (f"The manager has added {delta} {role if delta == 1 else _plural(role)} to {day} "
                    f"{_PRETTY.get(part, part)} in {_weeks_text(n, of)} — draft {delta} more there.")
        else:
            text = (f"The manager has cut {-delta} {role if delta == -1 else _plural(role)} from {day} "
                    f"{_PRETTY.get(part, part)} in {_weeks_text(n, of)} — draft {-delta} fewer there.")
        out.append({"kind": "headcount_add" if delta > 0 else "headcount_cut", "employee": "", "role": role,
                    "day": day, "daypart": part, "delta": int(delta), "times": n, "text": text,
                    "opportunities": of, "rate": rate, "confidence": conf,
                    "last_week": max((h for d, h, _w, _s in hits), default=0),
                    "last_week_start": max((s_ for _d, _h, _w, s_ in hits), default="")})
    return out


# ── role changes ──────────────────────────────────────────────────────────

def role_change_patterns(week_edits: list, min_repeats=2, min_rate=None) -> list:
    """The same person, date and start kept, a different role: per person ×
    old role × new role × weekday × daypart, counted once per week, in at
    least `min_rate` of the weeks the draft had them there in the old role."""
    min_rate = _min_rate() if min_rate is None else min_rate
    tally = {}
    for w in week_edits:
        for rc in (w["diff"].get("role_changed") or []):
            name = (rc.get("employee") or "").strip()
            old, new = (rc.get("old_role") or "").strip(), (rc.get("new_role") or "").strip()
            day, part = _weekday(rc.get("date"), rc.get("day")), _part(rc.get("shift_start"))
            if not (name and new and day) or part == "unknown":
                continue
            e = tally.setdefault((name.lower(), old.lower(), new.lower(), day, part),
                                 {"weeks": {}, "name": {}, "old": {}, "new": {}})
            e["weeks"][w["history_id"]] = (_weight(w, "role_change"), str(w.get("week_start") or ""))
            for fld, val in (("name", name), ("old", old), ("new", new)):
                e[fld][val] = e[fld].get(val, 0) + 1
    out = []
    for (nl, ol, _w, day, part), e in tally.items():
        n = len(e["weeks"])
        if n < min_repeats:
            continue
        opp_w, of = 0.0, 0
        for w in week_edits:
            there = any((r.get("employee") or "").strip().lower() == nl and (r.get("role") or "").strip().lower() == ol
                        and _weekday(r.get("date"), r.get("day")) == day and _part(r.get("shift_start")) == part
                        for r in w.get("base") or [])
            if there or w["history_id"] in e["weeks"]:
                opp_w += _weight(w, "role_change")
                of += 1
        rate, conf = _strength(sum(x[0] for x in e["weeks"].values()), opp_w)
        if rate < min_rate:
            continue
        name, old, new = _display(e["name"]), _display(e["old"]), _display(e["new"])
        out.append({"kind": "role_change", "employee": name, "role": new, "was_role": old, "day": day, "daypart": part,
                    "times": n, "last_week": max(e["weeks"]), "last_week_start": max(x[1] for x in e["weeks"].values()),
                    "opportunities": of, "rate": rate, "confidence": conf,
                    "text": f"The manager keeps switching {name} from {old or 'no role'} to {new} on {day} "
                            f"{_PRETTY.get(part, part)} ({_weeks_text(n, of)}) — draft them as {new} there."})
    return out


# ── leader substitutions on busy nights ───────────────────────────────────

def _strength_of_people(restaurant_id, db_path=DB_PATH) -> tuple:
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



def leader_swap_patterns(week_edits: list, leaders: set, scores: dict, min_repeats=2, min_rate=None) -> list:
    """A 'moved' edit on a Friday or Saturday night where the person the
    manager put in is a closer and the one taken off is not, or has the
    higher Operational Score. Per weekday × role, once per week, in at least
    `min_rate` of the weeks the draft had that role on that night."""
    min_rate = _min_rate() if min_rate is None else min_rate
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
            e = tally.setdefault((day, part, role.lower()), {"weeks": {}, "role": {}, "names": {}})
            e["weeks"][w["history_id"]] = (_weight(w, "leader_swap"), str(w.get("week_start") or ""))
            e["role"][role] = e["role"].get(role, 0) + 1
            e["names"][inc] = e["names"].get(inc, 0) + 1
    out = []
    for (day, part, rl), e in tally.items():
        n = len(e["weeks"])
        if n < min_repeats:
            continue
        opp_w, of = 0.0, 0
        for w in week_edits:
            there = any((r.get("role") or "").strip().lower() == rl and _weekday(r.get("date"), r.get("day")) == day
                        and _part(r.get("shift_start")) == part for r in w.get("base") or [])
            if there or w["history_id"] in e["weeks"]:
                opp_w += _weight(w, "leader_swap")
                of += 1
        rate, conf = _strength(sum(x[0] for x in e["weeks"].values()), opp_w)
        if rate < min_rate:
            continue
        role = _display(e["role"])
        names = [k for k, _v in sorted(e["names"].items(), key=lambda kv: (-kv[1], kv[0]))][:3]
        out.append({"kind": "leader_swap", "employee": "", "role": role, "day": day, "daypart": part, "times": n,
                    "names": names, "last_week": max(e["weeks"]), "last_week_start": max(x[1] for x in e["weeks"].values()),
                    "opportunities": of, "rate": rate, "confidence": conf,
                    "text": f"On {day} {_PRETTY.get(part, part)} the manager keeps swapping a stronger hand onto "
                            f"{role or 'the floor'} — a closer or a higher Operational Score ({', '.join(names)}) — "
                            f"in {_weeks_text(n, of)}. Draft a leader there."})
    return out


def edit_patterns(restaurant_id, weeks=EDIT_WEEKS, min_repeats=2, db_path=DB_PATH, week_edits=None,
                  min_rate=None) -> list:
    """Every new pattern kind, strongest first, capped at NEW_PATTERN_CAP.
    Same shape as schedule_versions.learned_patterns' own entries (kind,
    employee, day, daypart, times, opportunities, rate, confidence, text)
    plus role / time / delta / was_role, which schedule_intel.pattern_key
    folds into the key. `week_edits` is every week the manager finished
    with (learning_weeks), unedited ones included — they are the
    denominator."""
    week_edits = learning_weeks(restaurant_id, weeks, db_path) if week_edits is None else week_edits
    if not week_edits:
        return []
    out = retime_patterns(week_edits, min_repeats, min_rate) + headcount_patterns(week_edits, min_repeats, min_rate) + \
        role_change_patterns(week_edits, min_repeats, min_rate)
    if any(w["diff"].get("moved") for w in week_edits):
        leaders, scores = _strength_of_people(restaurant_id, db_path)
        if leaders or scores:
            out += leader_swap_patterns(week_edits, leaders, scores, min_repeats, min_rate)
    out.sort(key=lambda p: (-p["times"], -(p.get("confidence") or 0), p["kind"], p.get("day") or "",
                            p.get("role") or "", p.get("employee") or ""))
    return out[:NEW_PATTERN_CAP]


def learned_headcount_adjustments(restaurant_id, weeks=EDIT_WEEKS, min_weeks=2, db_path=DB_PATH) -> dict:
    """{(weekday, daypart): {role: delta}} — the net headcount the manager
    keeps adding (+) or cutting (−) per role, for a requirements table to
    apply on top of its own figure. Dismissed patterns are left out; the
    role is spelled as the schedule spells it.

    What worked stays learned (memory re-audit 9/29/26, QUALITY-3): once
    the draft carried the 4th server the manager stopped adding one, the
    edits aged out of the eight-week window and the draft dropped the
    server again. An ACTIVE standing headcount row (schedule_versions.
    refresh_standing_patterns) now applies whenever the live window no
    longer shows it, and a live pattern its standing row has retired
    (without newer evidence) no longer applies. One being re-tested
    (status 'retest', L-30) is left out of the draft once on purpose."""
    from schedule_intel import dismissed_patterns, pattern_key
    import schedule_versions as _sv
    pats = headcount_patterns(learning_weeks(restaurant_id, weeks, db_path), min_weeks)
    dismissed = dismissed_patterns(restaurant_id, db_path)
    try:
        standing = {s_["key"]: s_ for s_ in _sv.standing_patterns(restaurant_id, db_path=db_path)
                    if s_["kind"] in ("headcount_add", "headcount_cut")}
    except Exception as e:                 # a read; the live window still applies
        log.warning("standing headcount unavailable for restaurant %s: %s", restaurant_id, e)
        standing = {}
    out, live_slots = {}, set()
    for p in pats:
        k = pattern_key(p)
        if k in dismissed or _sv.suppressed(p, standing.get(k), standing):
            continue
        out.setdefault((p["day"], p["daypart"]), {})[p["role"]] = p["delta"]
        live_slots.add((p["day"], p["daypart"], (p["role"] or "").strip().lower()))
    for k, s_ in standing.items():
        if s_["status"] != "active" or k in dismissed or s_.get("delta") in (None, ""):
            continue
        slot = (s_["day"], s_["daypart"], (s_["role"] or "").strip().lower())
        if slot in live_slots:
            continue                       # the live window's own figure is the newer word
        try:
            delta = int(s_["delta"])
        except (TypeError, ValueError):
            continue
        if delta:
            out.setdefault((s_["day"], s_["daypart"]), {})[s_["role"]] = delta
            live_slots.add(slot)
    return out


# ── implicit recommendation acceptance ───────────────────────────────────

_FILL = re.compile(r"^Fill the gap on (?P<where>.+?): (?P<role>.+?) short \d+ of \d+")
_LEAD = re.compile(r'^Move somebody who clears ".*" onto (?P<where>.+?)\.?$')
_PAIR = re.compile(r"^Pair (?P<name>.+?) on (?P<where>.+?) with a stronger hand")
_TRIM = re.compile(r"^Trim about (?P<h>\d+(?:\.\d+)?)h from (?P<where>.+?) to get back")
_REST = re.compile(r"^Give (?P<name>.+?) a day off — \d+ in a row")
# The shapes the five above missed (schedule audit 10/3/26 L-34: most kinds
# never got an outcome because an edit that carried them out was never read
# as acceptance). A rule breach is not here: whether an edit fixed it needs
# the rule sweep, and Apply fixes — Cavnar AI's own change — answers it.
_COVER = re.compile(r"^Cover the gap in service on (?P<where>.+?): (?P<role>.+?) is down to \d+ at (?P<at>\S+), under")
_STRONGER = re.compile(r"^Put a stronger (?P<role>.+?) on (?P<where>.+?): ")
_SPREAD = re.compile(r"^Spread the busy shifts — (?P<names>.+?) (?:is|are) carrying too many")


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
        elif _COVER.match(rec):
            m = _COVER.match(rec)
            test = _slot_test(m.group("where"))
            at = _covering_minute(m.group("at"))
            if test and at is not None:
                role = m.group("role")
                b = {n for n in _family_people(before_rows, test, role) if _covers(before_rows, n, test, at)}
                a = {n for n in _family_people(after_rows, test, role) if _covers(after_rows, n, test, at)}
                hit = len(a) > len(b)
        elif _STRONGER.match(rec):
            m = _STRONGER.match(rec)
            test = _slot_test(m.group("where"))
            if test:
                role = m.group("role")
                hit = bool(_family_people(after_rows, test, role) - _family_people(before_rows, test, role))
        elif _SPREAD.match(rec):
            names = [n.strip().lower() for n in re.split(r",| and ", _SPREAD.match(rec).group("names")) if n.strip()]
            count = lambda rows, n: sum(1 for r in rows if (r.get("employee") or "").strip().lower() == n)
            hit = any(count(after_rows, n) < count(before_rows, n) for n in names)
        if hit:
            out.append(rec)
    return out


def _family_people(rows, test, role):
    """_people for a role FAMILY ("server" holds "Server AM" and "Server
    PM"): the sentences name a requirement's role, the rows a job code."""
    from shift_quality import role_family
    fam = role_family(role)
    return {(r.get("employee") or "").strip().lower() for r in rows if test(r) and (r.get("employee") or "").strip()
            and role_family(r.get("role")) == fam}


def _covering_minute(text):
    from schedule_rules import parse_minutes
    return parse_minutes(text)


def _covers(rows, name_low, test, minute) -> bool:
    """Whether `name_low` has a row on the slot that is on the floor at
    `minute` (a close past midnight read across it)."""
    from schedule_rules import parse_minutes
    for r in rows:
        if (r.get("employee") or "").strip().lower() != name_low or not test(r):
            continue
        s, e = parse_minutes(r.get("shift_start") or ""), parse_minutes(r.get("shift_end") or "")
        if s is None or e is None:
            continue
        e = e + 1440 if e <= s else e
        m = minute + 1440 if minute < s and minute + 1440 < e else minute
        if s <= m < e:
            return True
    return False


# ── predicting which draft rows the manager will change ───────────────────
#
# likely_edits used to flag only a row that matched an edit made repeatedly
# (the same person taken off the same Saturday twice). This predicts edits
# in general, from every row of every draft that reached a manager's final
# word: was that row removed, given to somebody else, retimed or re-roled?
# A smoothed edit rate per feature value (the person, the role, the weekday
# and daypart, the shift's length, how deep into the person's week it
# falls, and whether the model or a fill-in pass wrote it) combined in
# naive-Bayes log-odds (the strongest in full, the rest at half). Per restaurant, deterministic, no model call, no
# library — and every flagged row names the rates that flagged it.

PREDICT_WEEKS = EDIT_WEEKS       # drafts looked back over, each weighted by its age (L-27)
PREDICT_MIN_WEEKS = 3            # drafts with a manager's final word before anything is predicted
PREDICT_MIN_ROWS = 60
PREDICT_MIN_EDITED = 8
PREDICT_SMOOTHING = 3.0          # pseudo-rows pulling each feature's rate toward the overall rate
PREDICT_SUPPORT = 0.5            # features overlap (a person and their usual slot): the strongest piece of
                                 # evidence counts in full, each further one at this share, so one fact
                                 # seen through two features is not counted twice
PREDICT_THRESHOLD = 0.5          # flag a row at this likelihood or above...
PREDICT_MIN_LIFT = 0.2           # ...and at least this far above the restaurant's overall edit rate
PREDICT_MAX_FLAGS = 12

_FEATURES = ("person", "role", "slot", "weekday", "daypart", "length", "week_position", "origin")


def _origin(notes: str) -> str:
    """Who wrote the row, from the note the engine's own passes leave."""
    n = (notes or "").strip().lower()
    if not n:
        return "model"
    # "Cavnar AI:" is what the optimizer and the solver write now; "Cavnar:"
    # an older draft's rows (schedule audit 10/3/26 L-5 — the AI tag used to
    # fall through to "adjusted").
    if n.startswith(("cavnar:", "cavnar ai:")) or " cavnar:" in n or " cavnar ai:" in n:
        return "optimizer"
    if n.startswith("added") or "top-up" in n or " floor" in n:
        return "fill"
    if any(w in n for w in ("auto-capped", "staggered", "trimmed", "extended", "arrival", "(was ")):
        return "adjusted"
    return "model"


def _length(r) -> str:
    h = _hours(r)
    if h <= 0:
        return ""
    return "short" if h < 5 else "long" if h > 8 else "standard"


def row_features(rows: list) -> list:
    """[{feature: value}] per row, in row order. week_position is where the
    shift falls in that person's own week (their 1st-3rd, 4th-5th, 6th+)."""
    from schedule_rules import parse_minutes
    order = {}
    for i, r in enumerate(rows or []):
        n = (r.get("employee") or "").strip().lower()
        order.setdefault(n, []).append((r.get("date") or "", parse_minutes(r.get("shift_start") or "") or 0, i))
    nth = {}
    for n, items in order.items():
        for k, (_d, _s, i) in enumerate(sorted(items)):
            nth[i] = k + 1
    out = []
    for i, r in enumerate(rows or []):
        day = _weekday(r.get("date"), r.get("day"))
        part = _part(r.get("shift_start"))
        k = nth.get(i, 1)
        out.append({
            "person": (r.get("employee") or "").strip().lower(),
            "role": (r.get("role") or "").strip().lower(),
            "slot": f"{day}|{part}" if day and part in ("morning", "night") else "",
            "weekday": day,
            "daypart": part if part in ("morning", "night") else "",
            "length": _length(r),
            "week_position": "1-3" if k <= 3 else "4-5" if k <= 5 else "6+",
            "origin": _origin(r.get("notes")),
        })
    return out


def _edited_keys(d: dict) -> set:
    """(date, lower name, start) of every draft row the net diff changed."""
    keys = set()
    for r in d.get("removed") or []:
        keys.add((r.get("date") or "", (r.get("employee") or "").strip().lower(), r.get("shift_start") or ""))
    for m in d.get("moved") or []:
        keys.add((m.get("date") or "", (m.get("from") or "").strip().lower(), m.get("shift_start") or ""))
    for rt in d.get("retimed") or []:
        keys.add((rt.get("date") or "", (rt.get("employee") or "").strip().lower(), rt.get("old_start") or ""))
    for rc in d.get("role_changed") or []:
        keys.add((rc.get("date") or "", (rc.get("employee") or "").strip().lower(), rc.get("shift_start") or ""))
    return keys


def prediction_weeks(restaurant_id, weeks=PREDICT_WEEKS, db_path=DB_PATH) -> list:
    """One entry per calendar week whose draft reached a person's final
    word — edited, or sent as it stood by a person (a week sent out
    untouched is evidence too: every row in it was kept). [{history_id,
    week_start, rows, edited: [bool per row], weight}], oldest first.

    Read as schedule_versions.learning_weeks reads a week (schedule audit
    10/3/26): the draft against the week as it was FIRST sent — a save after
    that is a reaction (L-4); a change Cavnar AI made and the owner kept, an
    admin's hand and a change the owner called a one-off are not the
    manager changing a row (L-5, L-8, L-35); a week the automatic publish
    sent with no edit is nobody keeping anything and is left out (L-7); and
    the rows of the days an owner's redo threw away are rows the manager
    changed — the strongest "no" there is (L-26). `weight` is the week's
    recency (half-life PERSON_HALF_LIFE_DAYS, L-27)."""
    import schedule_versions as _sv
    out = []
    for w in _sv.learning_weeks(restaurant_id, weeks, db_path):
        if not w["base"]:
            continue
        keys = _edited_keys(w["diff"])
        rej = list(w["rejected"])
        rows = list(w["base"]) + rej
        edited = [(r.get("date") or "", (r.get("employee") or "").strip().lower(), r.get("shift_start") or "") in keys
                  for r in w["base"]] + [True] * len(rej)
        # A rejected row's features are read in the draft it belonged to
        # (its place in the person's week there), not beside its own redo.
        feats = row_features(w["base"])
        if rej:
            redo = set(w["redo_dates"])
            orig = [r for r in w["base"] if (r.get("date") or "")[:10] not in redo] + rej
            feats += row_features(orig)[-len(rej):]
        out.append({"history_id": w["history_id"], "week_start": w["week_start"], "rows": rows, "edited": edited,
                    "features": feats, "rejected": len(rej),
                    "weight": _sv.recency_weight(w["age_days"], _sv.PERSON_HALF_LIFE_DAYS)})
    out.sort(key=lambda w: (w["week_start"] or "", w["history_id"]))
    return out


def fit_edit_model(weeks: list) -> dict:
    """Counts behind the predictor: overall and per feature value. Not
    ready (with the reason) under the minimum history. Each week counts by
    its recency `weight` (L-27 — a draft from five months ago says less about
    next week than last week's; 1.0 when a week carries none); every table
    keeps the raw counts too ([weighted changed, weighted seen, changed,
    seen]) for the words a flag is explained in."""
    n = sum(len(w["rows"]) for w in weeks)
    e = sum(sum(1 for x in w["edited"] if x) for w in weeks)
    need = (f"{PREDICT_MIN_WEEKS} drafts you've finished with, {PREDICT_MIN_ROWS} rows and "
            f"{PREDICT_MIN_EDITED} changed rows")
    if len(weeks) < PREDICT_MIN_WEEKS or n < PREDICT_MIN_ROWS or e < PREDICT_MIN_EDITED:
        return {"ready": False, "weeks": len(weeks), "rows": n, "edited": e,
                "reason": (f"{len(weeks)} draft{'s' if len(weeks) != 1 else ''} you've finished with, {n} row"
                           f"{'s' if n != 1 else ''}, {e} changed — predicting your edits needs at least {need}.")}
    tables = {f: {} for f in _FEATURES}
    wn = we = 0.0
    for w in weeks:
        wt = float(w.get("weight", 1.0) or 0.0)
        for feats, hit in zip(w.get("features") or row_features(w["rows"]), w["edited"]):
            wn += wt
            we += wt if hit else 0.0
            for f in _FEATURES:
                v = feats.get(f)
                if not v:
                    continue
                c = tables[f].setdefault(v, [0.0, 0.0, 0, 0])
                c[0] += wt if hit else 0.0
                c[1] += wt
                c[2] += 1 if hit else 0
                c[3] += 1
    return {"ready": True, "weeks": len(weeks), "rows": n, "edited": e,
            "base_rate": (we + 1.0) / (wn + 2.0), "tables": tables}


def _logit(p):
    p = min(1 - 1e-6, max(1e-6, p))
    return math.log(p / (1 - p))


def _feature_phrase(feature, value, edits, seen, row) -> str:
    part = {"morning": "lunch", "night": "dinner"}
    if feature == "person":
        who = (row.get("employee") or value).strip()
        return f"you changed {edits} of {seen} of {who}'s shifts"
    if feature == "role":
        return f"{edits} of {seen} {(row.get('role') or value).strip()} shifts"
    if feature == "slot":
        day, p = value.split("|", 1)
        return f"{edits} of {seen} {day} {part.get(p, p)} shifts"
    if feature == "weekday":
        return f"{edits} of {seen} {value} shifts"
    if feature == "daypart":
        return f"{edits} of {seen} {part.get(value, value)} shifts"
    if feature == "length":
        return f"{edits} of {seen} {'shifts over 8 hours' if value == 'long' else 'shifts under 5 hours' if value == 'short' else '5-8 hour shifts'}"
    if feature == "week_position":
        label = {"6+": "a person's 6th shift or later that week", "4-5": "a person's 4th or 5th shift that week",
                 "1-3": "among a person's first three shifts that week"}[value]
        return f"{edits} of {seen} shifts {label}" if value == "1-3" else f"{edits} of {seen} shifts that were {label}"
    if feature == "origin":
        label = {"fill": "rows a fill-in pass added", "optimizer": "rows Cavnar AI's repair loop wrote",
                 "adjusted": "rows an automatic pass retimed or trimmed", "model": "rows the draft wrote"}[value]
        return f"{edits} of {seen} {label}"
    return f"{edits} of {seen}"


def predict_edits(model: dict, rows: list, threshold=PREDICT_THRESHOLD, limit=PREDICT_MAX_FLAGS, features=None) -> list:
    """[{index, employee, date, role, shift_start, likelihood, reason, text}]
    for draft rows at or above the threshold (and clearly above the
    restaurant's overall edit rate), likeliest first, at most `limit`."""
    if not model or not model.get("ready") or not rows:
        return []
    base = model["base_rate"]
    lb = _logit(base)
    out = []
    for i, (r, feats) in enumerate(zip(rows, features or row_features(rows))):
        contrib = []
        for f in _FEATURES:
            v = feats.get(f)
            c = (model["tables"].get(f) or {}).get(v) if v else None
            if not c:
                continue
            e, n = c[0], c[1]
            raw_e, raw_n = (c[2], c[3]) if len(c) >= 4 else (c[0], c[1])
            rate = (e + PREDICT_SMOOTHING * base) / (n + PREDICT_SMOOTHING)
            contrib.append((_logit(rate) - lb, f, v, int(raw_e), int(raw_n)))
        ranked = sorted(contrib, key=lambda c: (-abs(c[0]), c[1]))
        z = lb + sum(c[0] * (1.0 if k == 0 else PREDICT_SUPPORT) for k, c in enumerate(ranked))
        p = 1 / (1 + math.exp(-z))
        if p < threshold or p < base + PREDICT_MIN_LIFT:
            continue
        top = [c for c in sorted(contrib, key=lambda c: (-c[0], c[1])) if c[0] > 0][:2]
        reason = "; ".join(_feature_phrase(f, v, e, n, r) for _c, f, v, e, n in top)
        day = _weekday(r.get("date"), r.get("day"))
        part = {"morning": "lunch", "night": "dinner"}.get(_part(r.get("shift_start")), "")
        who = (r.get("employee") or "").strip()
        from time_utils import mdy
        pct = int(round(p * 100))
        out.append({"index": i, "kind": "predicted", "employee": who, "date": r.get("date"),
                    "role": r.get("role"), "shift_start": r.get("shift_start"),
                    "likelihood": round(p, 2), "reason": reason, "features": [c[1] for c in top],
                    "text": (f"{who}, {day[:3]} {mdy(r.get('date'))} {part} {(r.get('role') or '').strip()}".replace("  ", " ").strip()
                             + f" — {pct}% likely you'll change it: {reason}.")})
    out.sort(key=lambda x: (-x["likelihood"], x["index"]))
    return out[:limit]


def edit_predictor(restaurant_id, db_path=DB_PATH, weeks=None) -> dict:
    """The fitted model for this restaurant, or {ready: False, reason}."""
    weeks = prediction_weeks(restaurant_id, db_path=db_path) if weeks is None else weeks
    return fit_edit_model(weeks)


# The per-row "% likely you'll change it" is a naive-Bayes score, not a
# calibrated probability (CA1 L15). It stays a percentage (owner decision,
# 9/24/26) but is shown only once this restaurant's own leave-one-out
# backtest covers PREDICT_MIN_BACKTEST_WEEKS held-out weeks, and every row
# carries that backtest's hit rate beside it: "flags like this were right
# 5 of 8 times on your past drafts".
PREDICT_MIN_BACKTEST_WEEKS = 4


def predict_row_edits(restaurant_id, rows: list, db_path=DB_PATH, model=None) -> list:
    """predict_edits over a draft for this restaurant's own history —
    withheld ([]) until the backtest has PREDICT_MIN_BACKTEST_WEEKS weeks
    with at least one flag, and each row calibrated against it
    (`backtest_hit_rate`, `backtest_flagged`, `backtest_hits`,
    `backtest_weeks`, `base_rate`, `calibration_note`)."""
    weeks = prediction_weeks(restaurant_id, db_path=db_path)
    model = fit_edit_model(weeks) if model is None else model
    if not model or not model.get("ready"):
        return []
    bt = edit_prediction_backtest(weeks)
    if (bt.get("weeks") or 0) < PREDICT_MIN_BACKTEST_WEEKS or not bt.get("flagged"):
        return []
    note = (f"flags like this were right {bt['hits']} of {bt['flagged']} times on your last "
            f"{bt['weeks']} drafts")
    out = []
    for p in predict_edits(model, rows):
        out.append(dict(p, backtest_hit_rate=bt["hit_rate"], backtest_flagged=bt["flagged"],
                        backtest_hits=bt["hits"], backtest_weeks=bt["weeks"], base_rate=bt["base_rate"],
                        calibration_note=note, text=p["text"].rstrip(".") + f" ({note})."))
    return out


def edit_prediction_summary(restaurant_id, db_path=DB_PATH) -> dict:
    """What the record panel says about the predictor: ready or why not,
    and how it would have done on your own past drafts (held out one at a
    time)."""
    weeks = prediction_weeks(restaurant_id, db_path=db_path)
    model = fit_edit_model(weeks)
    out = {k: model.get(k) for k in ("ready", "weeks", "rows", "edited", "reason")}
    if model.get("ready"):
        bt = edit_prediction_backtest(weeks)
        out["backtest"] = {k: bt[k] for k in ("weeks", "flagged", "hits", "hit_rate", "recall", "base_rate")}
    return out


def edit_prediction_backtest(weeks: list, threshold=PREDICT_THRESHOLD) -> dict:
    """Leave one week out: fit on every other week, predict the held-out
    draft, and count. hit_rate is the share of flagged rows the manager did
    change; recall the share of changed rows that were flagged; base_rate
    what flagging at random would hit. Weeks whose training set is under
    the minimum are skipped, and said."""
    flagged = hits = edited = rows = skipped = 0
    per_week = []
    for i, w in enumerate(weeks):
        model = fit_edit_model(weeks[:i] + weeks[i + 1:])
        if not model.get("ready"):
            skipped += 1
            continue
        preds = predict_edits(model, w["rows"], threshold=threshold, limit=len(w["rows"]), features=w.get("features"))
        idx = {p["index"] for p in preds}
        h = sum(1 for j in idx if w["edited"][j])
        e = sum(1 for x in w["edited"] if x)
        flagged += len(idx)
        hits += h
        edited += e
        rows += len(w["rows"])
        per_week.append({"week_start": w.get("week_start"), "flagged": len(idx), "hits": h, "edited": e,
                         "rows": len(w["rows"])})
    return {"weeks": len(per_week), "skipped": skipped, "rows": rows, "edited": edited,
            "flagged": flagged, "hits": hits,
            "hit_rate": round(hits / flagged, 3) if flagged else None,
            "recall": round(hits / edited, 3) if edited else None,
            "base_rate": round(edited / rows, 3) if rows else None,
            "per_week": per_week}


# ── the predictor put to use (schedule audit 10/3/26 L-15) ────────────────
#
# The edit predictor was display-only: rows the manager changes at 50% or
# more on their own backtest were written into the draft anyway. Once its
# leave-one-out backtest on this restaurant's own drafts is right at least
# PREDICT_ACTIONABLE_HIT_RATE of the time, its flags are (a) a pre-publish
# "likely to change" review (strategy_routes._do_publish_check) and (b)
# soft-cost signals for the solver, the optimizer and the scorer
# (likely_edit_signals — the shape the next wave consumes).

PREDICT_ACTIONABLE_HIT_RATE = 0.6


def likely_edit_signals(restaurant_id, rows: list, db_path=DB_PATH, weeks=None) -> dict:
    """{"ready", "reason", "hit_rate", "backtest_weeks", "base_rate",
    "flags": [{index, employee, date, shift_start, shift_end, role,
    likelihood, features, origin, weight, reason}]} for the draft `rows`.
    `ready` only once the backtest covers PREDICT_MIN_BACKTEST_WEEKS weeks
    and its hit rate is at least PREDICT_ACTIONABLE_HIT_RATE; below that
    `flags` is empty and `reason` says what is missing. `weight` (0-1) is
    the lift over the restaurant's own edit rate times the backtest hit rate
    — what a soft cost scales by: keeping that person on that slot, or a
    row of that origin, costs the week that much."""
    weeks = prediction_weeks(restaurant_id, db_path=db_path) if weeks is None else weeks
    model = fit_edit_model(weeks)
    if not model.get("ready"):
        return {"ready": False, "reason": model.get("reason"), "hit_rate": None, "backtest_weeks": 0, "flags": []}
    bt = edit_prediction_backtest(weeks)
    hr, n = bt.get("hit_rate"), int(bt.get("weeks") or 0)
    if n < PREDICT_MIN_BACKTEST_WEEKS or hr is None or hr < PREDICT_ACTIONABLE_HIT_RATE:
        why = (f"the edit predictor has been right {bt.get('hits') or 0} of {bt.get('flagged') or 0} times over "
               f"{n} held-out draft{'s' if n != 1 else ''} — it steers the draft once it is right "
               f"{int(PREDICT_ACTIONABLE_HIT_RATE * 100)}% of the time over at least {PREDICT_MIN_BACKTEST_WEEKS}")
        return {"ready": False, "reason": why, "hit_rate": hr, "backtest_weeks": n, "flags": []}
    base = bt.get("base_rate") or 0.0
    feats = row_features(rows)
    flags = []
    for p in predict_edits(model, rows, limit=len(rows or [])):
        r = rows[p["index"]]
        flags.append({"index": p["index"], "employee": p["employee"], "date": p["date"],
                      "shift_start": r.get("shift_start"), "shift_end": r.get("shift_end"), "role": r.get("role"),
                      "likelihood": p["likelihood"], "features": p["features"], "origin": feats[p["index"]]["origin"],
                      "weight": round(max(0.0, p["likelihood"] - base) * hr, 3), "reason": p["reason"],
                      "text": p["text"]})
    return {"ready": True, "reason": None, "hit_rate": hr, "backtest_weeks": n, "base_rate": base,
            "hits": bt.get("hits"), "flagged": bt.get("flagged"), "flags": flags}


def likely_to_change(restaurant_id, rows: list, db_path=DB_PATH, limit=PREDICT_MAX_FLAGS) -> dict:
    """The pre-publish review (L-15): the rows the manager has been shown,
    by their own backtest, to change — before staff are told. {ready,
    reason, hit_rate, note, rows: [{employee, date, role, shift_start,
    likelihood, text}]} (the likeliest `limit`)."""
    sig = likely_edit_signals(restaurant_id, rows, db_path=db_path)
    if not sig["ready"]:
        return {"ready": False, "reason": sig["reason"], "hit_rate": sig["hit_rate"], "rows": []}
    note = (f"flags like these were right {sig['hits']} of {sig['flagged']} times on your last "
            f"{sig['backtest_weeks']} drafts")
    top = sorted(sig["flags"], key=lambda f: (-f["likelihood"], f["index"]))[:limit]
    return {"ready": True, "reason": None, "hit_rate": sig["hit_rate"], "note": note,
            "rows": [{k: f[k] for k in ("employee", "date", "role", "shift_start", "likelihood", "text")} for f in top]}


# ── what a save teaches, as it is made (schedule audit 10/3/26 L-4, L-5,
#    L-35) ────────────────────────────────────────────────────────────────

# The first weeks carry the most explicit intent and teach the learners
# almost nothing (two edited weeks before a pattern, seven drafts before
# the predictor). While a restaurant has fewer than this many weeks it
# drafted with Cavnar AI and sent, a big change of the manager's own asks
# one tap — always / just this week / they called off (L-35).
COLD_START_WEEKS = 4
WHY_MAX_QUESTIONS = 3
WHY_RETIME_MIN_MINUTES = 30
WHY_ANSWERS = ("always", "this_week", "call_off")
WHY_LABELS = {"always": "Always", "this_week": "Just this week", "call_off": "They called off"}


def in_cold_start(restaurant_id, db_path=DB_PATH) -> bool:
    """Fewer than COLD_START_WEEKS weeks drafted with Cavnar AI and sent."""
    conn = get_conn(db_path)
    try:
        n = conn.execute("SELECT COUNT(DISTINCT h.week_start) FROM schedule_history h WHERE h.restaurant_id=? AND "
                         "h.published_at IS NOT NULL AND EXISTS (SELECT 1 FROM schedule_versions v WHERE "
                         "v.history_id=h.id AND v.reason='generated')", (restaurant_id,)).fetchone()[0]
    finally:
        conn.close()
    return int(n or 0) < COLD_START_WEEKS


def _attendance_link(restaurant_id, name, day, db_path=DB_PATH):
    """The attendance record for a person taken off a date, when there is
    one: {id, outcome, source} — a call-out or no-show the reaction answered."""
    try:
        import staff_settings
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT id, outcome, source FROM attendance_events WHERE restaurant_id=? AND "
                               "employee_key=? AND business_date=? ORDER BY id DESC LIMIT 1",
                               (restaurant_id, staff_settings.name_key(name), str(day or "")[:10])).fetchone()
        finally:
            conn.close()
    except Exception as e:                   # a read; the reaction is observed without it
        log.warning("attendance link unavailable for %s: %s", restaurant_id, e)
        return None
    return dict(row) if row else None


def _open_episode(restaurant_id, kind, db_path=DB_PATH):
    """The key of the newest recommendation of `kind` still open (or taken
    and not yet carried out), else None."""
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT key FROM rec_instances WHERE restaurant_id=? AND kind=? AND status IN "
                               "('open', 'accepted') ORDER BY created_at DESC, rowid DESC LIMIT 1",
                               (restaurant_id, kind)).fetchone()
        finally:
            conn.close()
    except Exception as e:
        log.warning("open %s episode unreadable for %s: %s", kind, restaurant_id, e)
        return None
    return row["key"] if row else None


def _who(user) -> str:
    return ((user or {}).get("username") or (user or {}).get("email") or "").strip()[:120] if isinstance(user, dict) else ""


def capture_save(restaurant_id, history_id, version, step, user=None, published=False, db_path=DB_PATH) -> dict:
    """What a manager's save teaches, right after it is stored (the save
    route calls it after its commit; `step` is schedule_versions.step_origins
    of the save):
      * Cavnar AI's changes the owner kept credit trust in that kind of move
        — the apply-fixes, Improve and overtime-move recommendations are
        implemented (rec_ledger) — and are observed as cavnar_change_saved,
        never as the manager's habit (L-5);
      * on a week staff already have every change is a reaction (phase
        post_publish), observed with the attendance record of the person
        taken off when there is one — the learners never read it as a habit
        (L-4);
      * in the first weeks, a big change of the manager's own asks a one-tap
        why (L-35).
    Returns {why_questions, reactions, cavnar_changes, credited}."""
    import schedule_memory
    import schedule_versions as _sv
    from shift_quality import present_dayparts
    auth = _sv.authority_of(user) if user else _sv.SYSTEM
    who = _who(user) or None
    phase = _sv.POST_PUBLISH if published else _sv.PRE_PUBLISH
    out = {"why_questions": [], "reactions": 0, "cavnar_changes": 0, "credited": 0}
    week_start = None
    try:
        conn = get_conn(db_path)
        try:
            h = conn.execute("SELECT week_start FROM schedule_history WHERE id=? AND restaurant_id=?",
                             (history_id, restaurant_id)).fetchone()
            week_start = h["week_start"] if h else None
        finally:
            conn.close()
    except Exception as e:
        log.warning("week of history %s unreadable: %s", history_id, e)
    by_source = {}
    for ch in step.get("changes") or []:
        it = ch["item"]
        a, b = ch.get("after") or {}, ch.get("before") or {}
        row = a or b or it
        start = row.get("shift_start") or it.get("shift_start") or it.get("new_start") or ""
        part = (present_dayparts({"shift_start": start, "shift_end": row.get("shift_end") or ""}) or ["unknown"])[0]
        value = {"change": ch["kind"], "source": ch["source"]}
        for f in ("from", "to", "old_start", "new_start", "old_end", "new_end", "old_role", "new_role"):
            if it.get(f):
                value[f] = it[f]
        person = it.get("employee") or it.get("from") or it.get("to")
        if ch["origin"] == "cavnar":
            out["cavnar_changes"] += 1
            by_source.setdefault(ch["source"], []).append(ch)
            schedule_memory.observe(restaurant_id, "cavnar_change_saved", week_start=week_start, date=it.get("date"),
                                    daypart=part, role=row.get("role"), person=person, value=value, origin="cavnar",
                                    phase=phase, authority=auth, editor=who, source=ch["source"],
                                    history_id=history_id, db_path=db_path)
        elif published:
            off = it.get("from") if ch["kind"] == "moved" else (it.get("employee") if ch["kind"] == "removed" else None)
            if off:
                link = _attendance_link(restaurant_id, off, it.get("date"), db_path)
                if link:
                    value["attendance"] = link
            out["reactions"] += 1
            schedule_memory.observe(restaurant_id, "reaction", week_start=week_start, date=it.get("date"),
                                    daypart=part, role=row.get("role"), person=person, value=value, origin="manager",
                                    phase=phase, authority=auth, editor=who, source="save", history_id=history_id,
                                    db_path=db_path)
    # Trust in the move kind the owner kept (L-5): the recommendation that
    # proposed it is implemented — once per save.
    if by_source and auth != "admin":
        try:
            import rec_ledger as _rl
            ref = f"schedule_save:{history_id}:{version}"
            keys = []
            for ch in by_source.get("overtime_move") or []:
                it = ch["item"]
                if ch["kind"] == "moved" and it.get("from"):
                    keys.append(_rl.rec_key("overtime_move", f"{it['from']}:{it.get('date')}"))
            for src, kind in (("optimize", "optimizer"), ("apply_fixes", "apply_fixes")):
                if by_source.get(src):
                    k = _open_episode(restaurant_id, kind, db_path)
                    if k:
                        keys.append(k)
            if keys:
                out["credited"] = _rl.implemented(restaurant_id, keys, "schedule_review",
                                                  user_id=(user or {}).get("id") if isinstance(user, dict) else None,
                                                  source_ref=ref, meta={"via": "schedule_save", "module": "schedule"},
                                                  db_path=db_path)
        except Exception as e:
            import ops
            ops.capture(e, job="schedule_cavnar_credit", context=f"restaurant_id={restaurant_id} history={history_id}")
    if in_cold_start(restaurant_id, db_path):
        try:
            out["why_questions"] = why_questions(restaurant_id, history_id, version, step, phase, db_path=db_path)
        except Exception as e:
            import ops
            ops.capture(e, job="schedule_edit_why", context=f"restaurant_id={restaurant_id} history={history_id}")
    return out


def _day_label(iso) -> str:
    from time_utils import mdy
    return f"{_weekday(iso)[:3]} {mdy(iso)}".strip()


def _meal(part) -> str:
    return {"morning": "lunch", "night": "dinner"}.get(part, part or "")


def why_questions(restaurant_id, history_id, version, step, phase, db_path=DB_PATH) -> list:
    """The one-tap "why" for the biggest of a save's own changes (L-35), at
    most WHY_MAX_QUESTIONS, never one already asked about this week: a
    person taken off a slot (moved or removed), more of a role on a slot,
    a role's start or end moved WHY_RETIME_MIN_MINUTES or more, a role
    change. Each is stored (schedule_edit_answers, answer NULL) and
    returned as {key, kind, text, options: [{answer, label}], date, employee,
    role, daypart} — "They called off" only on a week staff already have."""
    import schedule_versions as _sv
    from shift_quality import present_dayparts
    from schedule_rules import parse_minutes
    mine = [ch for ch in step.get("changes") or [] if ch["origin"] == "manager"]
    if not mine:
        return []
    cands = []
    moved_from = set()

    def _part_of(r):
        return (present_dayparts({"shift_start": r.get("shift_start") or "", "shift_end": r.get("shift_end") or ""})
                or ["unknown"])[0]
    for ch in mine:
        it = ch["item"]
        if ch["kind"] in ("moved", "removed"):
            b = ch.get("before") or it
            name = it.get("from") if ch["kind"] == "moved" else it.get("employee")
            moved_from.add((it.get("date"), (name or "").strip().lower()))
            part = _part_of(b)
            subject = {"kind": "moved_off", "employee": name, "date": it.get("date"), "day": _weekday(it.get("date")),
                       "daypart": part, "role": b.get("role") or it.get("role"), "shift_start": b.get("shift_start"),
                       "replaced_by": it.get("to")}
            text = (f"{it.get('to')} in for {name} on {_day_label(it.get('date'))} {_meal(part)}"
                    if ch["kind"] == "moved" else f"{name} off {_day_label(it.get('date'))} {_meal(part)}")
            cands.append((0, f"moved_off|{it.get('date')}|{(name or '').lower()}|{part}", subject, text, ch,
                          f"Always keep {name} off {subject['day']} {_meal(part)}"))
    adds = {}
    for ch in mine:
        if ch["kind"] != "added":
            continue
        a = ch["item"]
        k = (a.get("date"), _part_of(a), (a.get("role") or "").strip())
        adds.setdefault(k, []).append(ch)
    for (d, part, role), chs in adds.items():
        n = len(chs)
        subject = {"kind": "headcount_add", "role": role, "date": d, "day": _weekday(d), "daypart": part, "delta": n}
        word = role if n == 1 else _plural(role)
        cands.append((1, f"headcount_add|{d}|{role.lower()}|{part}", subject,
                      f"{n} more {word} on {_day_label(d)} {_meal(part)}",
                      {"keys": set().union(*[c["keys"] for c in chs])},
                      f"Always draft {n} more {word} on {_weekday(d)} {_meal(part)}"))
    for ch in mine:
        if ch["kind"] != "retimed":
            continue
        it = ch["item"]
        for kind, old, new in (("retime_start", it.get("old_start"), it.get("new_start")),
                               ("retime_end", it.get("old_end"), it.get("new_end"))):
            mo, mn = parse_minutes(old or ""), parse_minutes(new or "")
            if mo is None or mn is None or abs(mn - mo) < WHY_RETIME_MIN_MINUTES:
                continue
            part = _part(it.get("new_start"))
            role = (it.get("role") or "").strip()
            when = _clock(new)
            verb = "starting" if kind == "retime_start" else "ending"
            subject = {"kind": kind, "role": role, "date": it.get("date"), "day": _weekday(it.get("date")),
                       "daypart": part, "time": when, "employee": it.get("employee")}
            cands.append((2, f"{kind}|{it.get('date')}|{role.lower()}|{part}|{when}", subject,
                          f"{_plural(role)} {verb} {when} on {_day_label(it.get('date'))} {_meal(part)}", ch,
                          f"Always {'start' if kind == 'retime_start' else 'end'} {_plural(role)} then on "
                          f"{subject['day']}s"))
    for ch in mine:
        if ch["kind"] != "role_changed":
            continue
        it = ch["item"]
        part = _part(it.get("shift_start"))
        subject = {"kind": "role_change", "employee": it.get("employee"), "role": it.get("new_role"),
                   "was_role": it.get("old_role"), "date": it.get("date"), "day": _weekday(it.get("date")),
                   "daypart": part}
        cands.append((3, f"role_change|{it.get('date')}|{(it.get('employee') or '').lower()}|{part}", subject,
                      f"{it.get('employee')} as {it.get('new_role')} on {_day_label(it.get('date'))} {_meal(part)}", ch,
                      f"Always draft {it.get('employee')} as {it.get('new_role')} on {subject['day']} {_meal(part)}"))
    if not cands:
        return []
    conn = get_conn(db_path)
    try:
        asked = {r[0] for r in conn.execute("SELECT question_key FROM schedule_edit_answers WHERE restaurant_id=? "
                                            "AND history_id=?", (restaurant_id, history_id)).fetchall()}
        out = []
        for rank, key, subject, text, ch, always in sorted(cands, key=lambda c: (c[0], c[1])):
            if key in asked or len(out) >= WHY_MAX_QUESTIONS:
                continue
            options = [{"answer": "always", "label": always}, {"answer": "this_week", "label": WHY_LABELS["this_week"]}]
            if phase == _sv.POST_PUBLISH and subject["kind"] == "moved_off":
                options.append({"answer": "call_off", "label": f"{subject['employee']} called off"})
            keys = sorted(_sv.key_str(k) for k in (ch.get("keys") or set()))
            conn.execute("INSERT OR IGNORE INTO schedule_edit_answers (restaurant_id, history_id, version, "
                         "question_key, kind, subject_json, keys_json, phase) VALUES (?,?,?,?,?,?,?,?)",
                         (restaurant_id, history_id, version, key, subject["kind"],
                          json.dumps(dict(subject, options=[o["answer"] for o in options], text=text)),
                          json.dumps(keys), phase))
            asked.add(key)
            out.append({"key": key, "kind": subject["kind"], "text": text, "options": options,
                        "date": subject.get("date"), "employee": subject.get("employee"), "role": subject.get("role"),
                        "daypart": subject.get("daypart")})
        conn.commit()
    finally:
        conn.close()
    return out


def answer_edit_question(restaurant_id, history_id, key, answer, user=None, db_path=DB_PATH) -> dict:
    """The owner's tap on a "why" (L-35). `always` makes the change a
    standing pattern at once with their authority (and tells the
    observation log — a candidate memory); `this_week` keeps it out of the
    habits; `call_off` records the person's call-out (attendance, source
    manual) and keeps it out of the habits. An admin's answer (view-as) is
    kept and counts only once the account holder adopts it. Raises
    ValueError with the owner's words."""
    import schedule_memory
    import schedule_versions as _sv
    if answer not in WHY_ANSWERS:
        raise ValueError("Pick always, just this week, or they called off.")
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM schedule_edit_answers WHERE restaurant_id=? AND history_id=? AND "
                           "question_key=?", (restaurant_id, history_id, str(key or "")[:200])).fetchone()
        if row is None:
            raise ValueError("That question isn't on this week any more.")
        subject = json.loads(row["subject_json"] or "{}") or {}
        if answer not in (subject.get("options") or WHY_ANSWERS[:2]):
            raise ValueError("That answer doesn't fit this change.")
        if row["answer"] and row["answer"] != answer:
            raise ValueError("This one is already answered.")
        auth = _sv.authority_of(user) if user else _sv.SYSTEM
        conn.execute("UPDATE schedule_edit_answers SET answer=?, authority=?, answered_by=?, answered_at=datetime('now') "
                     "WHERE id=?", (answer, auth, _who(user) or None, row["id"]))
        conn.commit()
        answer_id = row["id"]
    finally:
        conn.close()
    schedule_memory.observe(restaurant_id, "edit_why", week_start=None, date=subject.get("date"),
                            daypart=subject.get("daypart"), role=subject.get("role"), person=subject.get("employee"),
                            value={"answer": answer, "question": subject.get("text"), "kind": subject.get("kind"),
                                   "subject": {k: v for k, v in subject.items() if k not in ("options", "text")}},
                            origin="manager", phase=row["phase"] or _sv.PRE_PUBLISH, authority=auth,
                            editor=_who(user) or None, source="edit_why", history_id=history_id, db_path=db_path)
    applied = {} if auth == "admin" else apply_edit_answer(restaurant_id, answer_id, db_path=db_path)
    return {"ok": True, "key": key, "answer": answer, "counted": auth != "admin", "applied": applied}


def _said_pattern(subject) -> dict:
    """The learned-pattern shape of an answered question (pattern_key's
    fields and the standing row's words)."""
    kind, day, part = subject.get("kind"), subject.get("day"), subject.get("daypart")
    meal = _PRETTY.get(part, part)
    p = {"kind": kind, "employee": "", "role": subject.get("role") or "", "day": day, "daypart": part}
    if kind == "moved_off":
        p["employee"] = subject.get("employee") or ""
        p["role"] = ""
        p["text"] = (f"The owner said always: keep {p['employee']} off {day} {meal} — avoid scheduling them there.")
    elif kind == "headcount_add":
        p["delta"] = int(subject.get("delta") or 1)
        p["text"] = (f"The owner said always: {p['delta']} more {_plural(p['role']) if p['delta'] != 1 else p['role']} "
                     f"on {day} {meal} — draft {p['delta']} more there.")
    elif kind in ("retime_start", "retime_end"):
        p["time"] = subject.get("time")
        verb = "start" if kind == "retime_start" else "end"
        p["text"] = f"The owner said always: {verb} {_plural(p['role'])} on {day} {meal} at {p['time']} — {verb} them then."
    elif kind == "role_change":
        p["employee"] = subject.get("employee") or ""
        p["was_role"] = subject.get("was_role") or ""
        p["text"] = (f"The owner said always: {p['employee']} as {p['role']} on {day} {meal} — draft them as "
                     f"{p['role']} there.")
    return p


def apply_edit_answer(restaurant_id, answer_id, db_path=DB_PATH) -> dict:
    """What a counted answer does: `always` → the standing pattern
    (schedule_versions.owner_said_pattern); `call_off` → the call-out in
    attendance (source manual); `this_week` needs nothing — the learners
    read the answer. Returns what was applied."""
    import schedule_versions as _sv
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM schedule_edit_answers WHERE id=? AND restaurant_id=?",
                           (answer_id, restaurant_id)).fetchone()
    finally:
        conn.close()
    if row is None or not row["answer"]:
        return {}
    subject = json.loads(row["subject_json"] or "{}") or {}
    auth = row["authority"] if (row["authority"] or "") != "admin" else "principal"
    if row["answer"] == "always":
        p = _said_pattern(subject)
        if not p.get("day") or p.get("daypart") in (None, "", "unknown"):
            return {}
        return {"pattern": _sv.owner_said_pattern(restaurant_id, p, auth, history_id=row["history_id"],
                                                  who=row["answered_by"], db_path=db_path)}
    if row["answer"] == "call_off" and subject.get("employee") and subject.get("date"):
        import attendance
        wrote = attendance.record(restaurant_id, subject["employee"], subject["date"], "called_out", "manual",
                                  shift_start=subject.get("shift_start") or "", role=subject.get("role"),
                                  history_id=row["history_id"], covered_by=subject.get("replaced_by"),
                                  note="Marked a call-off when the schedule was edited", db_path=db_path)
        return {"attendance": bool(wrote)}
    return {}


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


def _solve(a, b):
    """Gaussian elimination with partial pivoting: x in a·x = b. None when
    the system is singular (it cannot be, with a ridge penalty, but a
    degenerate column is refused rather than divided by)."""
    n = len(b)
    m = [list(a[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def _ridge_fit(samples: list, keys: list, outcome: str, min_pairs: int):
    """One outcome regressed on every dimension at once, standardized, with
    a ridge penalty: {key: coefficient}, {key: shifts that dimension was
    scored on}, and the shifts used. A dimension scored on fewer than
    `min_pairs` of those shifts, or that never varied, is left out of the
    fit. A dimension a shift did not score is held at its mean there, so it
    neither helps nor hurts that shift. Joint rather than one correlation
    per dimension: coverage and the half-hour sweep move together, and
    fitted one at a time each took the credit for the other.

    At most one dimension per CALIBRATION_SAMPLES_PER_DIMENSION shifts is
    fitted (SQ-22), the best-observed first; the ones that did not fit are
    returned as `crowded`."""
    rows = [(s[0], s[1][outcome]) for s in samples if s[1].get(outcome) is not None]
    if len(rows) < min_pairs:
        return {}, {}, len(rows), []
    ys = [y for _d, y in rows]
    my = sum(ys) / len(ys)
    sy = math.sqrt(sum((y - my) ** 2 for y in ys) / len(ys))
    if sy <= 1e-9:
        return {}, {}, len(rows), []
    stats, seen = {}, {}
    for k in keys:
        vals = [d[k] for d, _y in rows if k in d]
        seen[k] = len(vals)
        if len(vals) < min_pairs:
            continue
        mu = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - mu) ** 2 for v in vals) / len(vals))
        if sd > 1e-9:
            stats[k] = (mu, sd)
    room = max(0, len(rows) // CALIBRATION_SAMPLES_PER_DIMENSION)
    ranked = sorted(stats, key=lambda k: (-seen[k], k))
    used, crowded = sorted(ranked[:room]), sorted(ranked[room:])
    if not used:
        return {}, seen, len(rows), crowded
    xs = [[((d[k] - stats[k][0]) / stats[k][1]) if k in d else 0.0 for k in used] for d, _y in rows]
    yz = [(y - my) / sy for y in ys]
    n, p = len(xs), len(used)
    xtx = [[sum(xs[r][i] * xs[r][j] for r in range(n)) for j in range(p)] for i in range(p)]
    for i in range(p):
        xtx[i][i] += CALIBRATION_RIDGE * n
    xty = [sum(xs[r][i] * yz[r] for r in range(n)) for i in range(p)]
    beta = _solve(xtx, xty)
    if beta is None:
        return {}, seen, len(rows), crowded
    # Scaled so a lone dimension's coefficient is its correlation, which is
    # what CALIBRATION_MIN_EVIDENCE was written against.
    return {k: beta[i] * (1 + CALIBRATION_RIDGE) for i, k in enumerate(used)}, seen, len(rows), crowded


# The outcomes a shift is judged by afterwards: +1 = a higher value is
# better, −1 = a lower one is. Each carries the words its explanation uses.
CALIBRATION_OUTCOMES = {
    "issues": (-1, "coverage and no-show issues", "fewer coverage or no-show issues", "more coverage or no-show issues"),
    "review_rating": (1, "the day's review rating", "better reviews that day", "worse reviews that day"),
    "labor_vs_target": (-1, "the daypart's labor % against target", "labor % nearer or under target",
                        "labor % further over target"),
}


def _calibration_samples(restaurant_id, db_path):
    """[(dims, outcomes, history_id, meta)] — one per recorded shift outcome
    that has a stored score, plus the dates Cavnar was watching (None when
    the restaurant cannot be watched at all). meta: the shift's profile key
    and label, its score, the bar it was held to and each dimension's floor
    on it (when the stored score kept them) — what the floors and bars are
    read against (SQ-22)."""
    conn = get_conn(db_path)
    try:
        # The review rating a shift is judged on is only the reviews that
        # named its meal and were posted within two days of it
        # (review_rating_attributed — memory audit 9/29/26,
        # reviews_to_labor): a dinner complaint posted on Sunday used to be
        # scored against Sunday lunch, and the calibration learned from noise.
        # Labor % is the DAYPART's own (schedule audit 10/3/26 L-13): the
        # punched hours priced at their rates over the daypart's measured
        # sales (schedule_intel.record_outcomes). The day's figure, copied
        # onto both rows, fitted lunch and dinner to the same number; a row
        # without a measured daypart figure has no labor reading at all.
        try:
            outs = conn.execute("SELECT history_id, date, daypart, issues, review_rating_attributed AS review_rating, "
                                "labor_pct_daypart AS labor_pct FROM schedule_outcomes WHERE restaurant_id=?",
                                (restaurant_id,)).fetchall()
        except Exception:
            outs = conn.execute("SELECT history_id, date, daypart, issues, NULL AS review_rating, NULL AS labor_pct "
                                "FROM schedule_outcomes WHERE restaurant_id=?", (restaurant_id,)).fetchall()
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
    dims_by, meta_by = {}, {}
    for hid, h in hist.items():
        try:
            q = json.loads(h["quality_json"] or "null") or {}
        except (TypeError, ValueError):
            continue
        for s in q.get("shifts") or []:
            if not s.get("scored"):
                continue
            key = (hid, s.get("date"), s.get("daypart"))
            dims_by[key] = {
                d["key"]: float(d["score"]) for d in s.get("dimensions") or [] if d.get("key") and d.get("score") is not None}
            prof = s.get("profile") or {}
            meta_by[key] = {"profile": prof.get("key"), "label": prof.get("label"),
                            "score": s.get("score"), "bar": prof.get("min_quality"),
                            "floors": {d["key"]: d["floor"] for d in s.get("dimensions") or []
                                       if d.get("key") and d.get("floor") is not None}}
    # A coverage or no-show issue can only have been opened on a night the
    # coverage check was watching (schedule_intel.watched_dates). A quiet
    # night nobody watched is not a clean one, so the issues outcome counts
    # only watched dates; reviews and labor % are measured either way.
    dates = sorted(o["date"] for o in outs if o["date"])
    watched = set()
    if dates:
        try:
            from schedule_intel import watched_dates
            watched = watched_dates(restaurant_id, dates[0], dates[-1], db_path=db_path)
        except Exception as e:
            log.warning("[calibration] watched dates unavailable for %s: %s", restaurant_id, e)
            watched = set()
    samples = []
    for o in outs:
        dims = dims_by.get((o["history_id"], o["date"], o["daypart"]))
        if not dims:
            continue
        target = hist[o["history_id"]]["labor_target"] if o["history_id"] in hist else None
        labor = None
        if o["labor_pct"] is not None and target:
            labor = float(o["labor_pct"]) - float(target)
        samples.append((dims, {"issues": float(o["issues"] or 0) if o["date"] in watched else None,
                               "review_rating": float(o["review_rating"]) if o["review_rating"] is not None else None,
                               "labor_vs_target": labor}, o["history_id"],
                        meta_by.get((o["history_id"], o["date"], o["daypart"])) or {}))
    return samples, watched


def calibrate_weights(restaurant_id, db_path=DB_PATH, current_weights=None) -> dict:
    """Fit the Shift Quality weights to what published shifts actually did,
    and suggest the next step toward that fit. Nothing is applied: "Apply"
    (strategy_routes._do_calibration_apply) is the owner's decision.

    Outcomes per shift (schedule_intel.record_outcomes): coverage and
    no-show issues — only on nights Cavnar was watching — the day's review
    rating, and labor % against the week's target. Each outcome is fitted
    on every dimension at once (a standardized ridge regression); a
    dimension whose higher scores went with better outcomes, holding the
    others steady, is moved up, one whose scores went with worse outcomes
    is moved down, and one the record cannot read is left alone.

    Guardrails:
      * a minimum record: CALIBRATION_MIN_WEEKS published weeks and
        CALIBRATION_MIN_SHIFTS shift outcomes, and CALIBRATION_MIN_PAIRS
        shifts per dimension per outcome before that pair says anything;
      * an evidence floor (CALIBRATION_MIN_EVIDENCE) under which nothing
        moves;
      * the fitted weight stays within CALIBRATION_MAX_NUDGE of the default;
      * one Apply moves a weight at most CALIBRATION_MAX_STEP of its default
        from where it is now, so a week of odd outcomes cannot swing the
        score — the next suggestion takes the next step if the record
        still says so;
      * every dimension says which outcome drove its change, and how many
        shifts that rests on. Deterministic.

    Floors and bars too (schedule audit 10/3/26 SQ-22): weights cannot lift
    a shift a critical floor caps, so each shift profile with
    CALIBRATION_MIN_PROFILE_SHIFTS of its own shifts on record also has its
    quality bar and each critical floor read against what those shifts did
    (`profiles`, `suggested_profiles`; _profile_calibration). Apply writes
    them (apply_profile_calibration)."""
    from shift_quality import DEFAULT_WEIGHTS
    samples, watched = _calibration_samples(restaurant_id, db_path)
    n_weeks = len({s[2] for s in samples})
    base = {"weeks": n_weeks, "shifts": len(samples), "watched_shifts": sum(1 for s in samples if s[1]["issues"] is not None),
            "needs": {"weeks": CALIBRATION_MIN_WEEKS, "shifts": CALIBRATION_MIN_SHIFTS}}
    if n_weeks < CALIBRATION_MIN_WEEKS or len(samples) < CALIBRATION_MIN_SHIFTS:
        return {"ready": False, **base,
                "reason": (f"{n_weeks} published week{'s' if n_weeks != 1 else ''} and {len(samples)} shift outcome"
                           f"{'s' if len(samples) != 1 else ''} with a stored score — calibration needs at least "
                           f"{CALIBRATION_MIN_WEEKS} weeks and {CALIBRATION_MIN_SHIFTS} shifts.")}
    if current_weights is None:
        try:
            current_weights = _models_mod.get_quality_weights(restaurant_id) or {}
        except Exception as e:
            log.warning("[calibration] current weights unavailable for %s: %s", restaurant_id, e)
            current_weights = {}
    current = dict(DEFAULT_WEIGHTS)
    current.update({k: float(v) for k, v in (current_weights or {}).items() if k in DEFAULT_WEIGHTS})
    keys = list(DEFAULT_WEIGHTS)
    fits = {}
    for outcome in CALIBRATION_OUTCOMES:
        coef, seen, used, crowded = _ridge_fit(samples, keys, outcome, CALIBRATION_MIN_PAIRS)
        fits[outcome] = {"coef": coef, "seen": seen, "shifts": used, "crowded": crowded}
    report, suggested = {}, {}
    for key, default in DEFAULT_WEIGHTS.items():
        corr, pairs_n, coefs, contrib = {}, {}, {}, {}
        for outcome, (sign, _label, _good, _bad) in CALIBRATION_OUTCOMES.items():
            pairs = [(dims[key], oc[outcome]) for dims, oc, *_rest in samples if key in dims and oc[outcome] is not None]
            pairs_n[outcome] = len(pairs)
            r = _pearson(pairs) if len(pairs) >= CALIBRATION_MIN_PAIRS else None
            corr[outcome] = round(r, 3) if r is not None else None
            b = fits[outcome]["coef"].get(key)
            coefs[outcome] = round(b, 3) if b is not None else None
            if b is not None:
                contrib[outcome] = sign * b
        ev = sum(contrib.values()) / len(contrib) if contrib else None
        target_nudge = 0.0
        if ev is not None and abs(ev) >= CALIBRATION_MIN_EVIDENCE:
            target_nudge = max(-CALIBRATION_MAX_NUDGE, min(CALIBRATION_MAX_NUDGE, ev))
        target = default * (1 + target_nudge)
        now = float(current.get(key, default))
        step = CALIBRATION_MAX_STEP * default
        weight = round(now + max(-step, min(step, target - now)), 1)
        suggested[key] = weight
        driver = None
        if contrib and target_nudge:
            same_way = {o: c for o, c in contrib.items() if (c > 0) == (target_nudge > 0)}
            if same_way:
                o = max(same_way, key=lambda k: (abs(same_way[k]), k))
                driver = {"outcome": o, "label": CALIBRATION_OUTCOMES[o][1], "effect": round(same_way[o], 3),
                          "shifts": pairs_n[o]}
        explanation = _calibration_explanation(key, now, weight, target_nudge, ev, driver, contrib, pairs_n)
        if not contrib and any(key in f["crowded"] for f in fits.values()):
            fitted = max((len(f["coef"]) for f in fits.values()), default=0)
            explanation = (f"{key.replace('_', ' ').capitalize()} was left out of this fit: the record carries "
                           f"{fitted} dimension{'s' if fitted != 1 else ''} at {CALIBRATION_SAMPLES_PER_DIMENSION} "
                           "shifts each, and the better-observed ones were read first.")
        report[key] = {"default": default, "current": round(now, 1), "suggested": weight,
                       "nudge_pct": int(round((weight / default - 1) * 100)) if default else 0,
                       "step_pct": int(round((weight - now) / default * 100)) if default else 0,
                       "target": round(target, 1),
                       "evidence": round(ev, 3) if ev is not None else None,
                       "correlation": corr, "coefficient": coefs, "pairs": pairs_n,
                       "driver": driver, "explanation": explanation,
                       "reading": ("tracked better outcomes" if target_nudge > 0 else
                                   "did not track outcomes here" if target_nudge < 0 else
                                   "no clear signal yet")}
    moving = sorted((k for k, d in report.items() if abs(d["suggested"] - d["current"]) >= 0.05),
                    key=lambda k: -abs(report[k]["suggested"] - report[k]["current"]))
    watch_note = ("" if base["watched_shifts"] else
                  " Coverage and no-show issues are not counted: none of these shifts fell on a night Cavnar AI was watching.")
    profiles, suggested_profiles = _profile_calibration(samples, _profile_settings(restaurant_id, db_path))
    return {"ready": True, **base, "applied": False, "dimensions": report, "suggested_weights": suggested,
            "moving": moving, "profiles": profiles, "suggested_profiles": suggested_profiles,
            "moving_profiles": sorted(suggested_profiles),
            "fit": {o: {"shifts": f["shifts"], "dimensions": len(f["coef"]), "left_out": f["crowded"]}
                    for o, f in fits.items()},
            "limits": {"max_step_pct": int(CALIBRATION_MAX_STEP * 100), "max_total_pct": int(CALIBRATION_MAX_NUDGE * 100),
                       "min_pairs": CALIBRATION_MIN_PAIRS, "min_evidence": CALIBRATION_MIN_EVIDENCE,
                       "shifts_per_dimension": CALIBRATION_SAMPLES_PER_DIMENSION,
                       "min_profile_shifts": CALIBRATION_MIN_PROFILE_SHIFTS,
                       "max_floor_step": CALIBRATION_THRESHOLD_STEP},
            "note": ("Suggestions only — the engine keeps its current weights, floors and bars until someone applies them. "
                     f"One apply moves a weight at most {int(CALIBRATION_MAX_STEP * 100)}% of its default, and a floor or "
                     f"a bar at most {CALIBRATION_THRESHOLD_STEP} points." + watch_note)}


def _most_common(values):
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0] if counts else None


def _threshold_fit(points: list, current: float):
    """(line, evidence, below, above) — of the candidate lines within
    CALIBRATION_THRESHOLD_RANGE of `current` (every 5 points), the one that
    best separates the shifts that went worse: for each outcome with data,
    the gap between the shifts scoring at or over the line and those under
    it, in standard deviations and signed so positive means the shifts over
    it did better; evidence is the mean over the outcomes. A line needs
    CALIBRATION_MIN_SIDE shifts on each side. None when no candidate has
    them. Ties go to the line nearest `current`. Pure."""
    best = None
    lo = max(0, int(current) - CALIBRATION_THRESHOLD_RANGE)
    hi = min(100, int(current) + CALIBRATION_THRESHOLD_RANGE)
    for t in range(lo - lo % 5, hi + 1, 5):
        effects = []
        for outcome, (sign, *_words) in CALIBRATION_OUTCOMES.items():
            over = [oc[outcome] for x, oc in points if oc.get(outcome) is not None and x >= t]
            under = [oc[outcome] for x, oc in points if oc.get(outcome) is not None and x < t]
            if len(over) < CALIBRATION_MIN_SIDE or len(under) < CALIBRATION_MIN_SIDE:
                continue
            ys = over + under
            mu = sum(ys) / len(ys)
            sd = math.sqrt(sum((y - mu) ** 2 for y in ys) / len(ys))
            if sd <= 1e-9:
                continue
            effects.append(sign * (sum(over) / len(over) - sum(under) / len(under)) / sd)
        if not effects:
            continue
        ev = sum(effects) / len(effects)
        below = sum(1 for x, _oc in points if x < t)
        if best is None or ev > best[1] + 1e-9 or (abs(ev - best[1]) <= 1e-9 and abs(t - current) < abs(best[0] - current)):
            best = (t, ev, below, len(points) - below)
    return best


def _line_reading(name: str, points: list, current) -> dict:
    """One floor's or bar's reading: {current, suggested, fitted, evidence,
    below, above, shifts, explanation} — a bounded step toward the line the
    record shows, or why it stays."""
    current = int(round(float(current)))
    out = {"current": current, "suggested": current, "fitted": None, "evidence": None, "shifts": len(points)}
    fit = _threshold_fit(points, current)
    if fit is None:
        out["explanation"] = (f"{name} stays at {current}: not enough shifts on each side of any line near it "
                              f"({CALIBRATION_MIN_SIDE} each side needed).")
        return out
    line, ev, below, above = fit
    out.update(fitted=line, evidence=round(ev, 3), below=below, above=above)
    if ev < CALIBRATION_THRESHOLD_EVIDENCE:
        out["explanation"] = (f"{name} stays at {current}: shifts under no nearby line did clearly worse "
                              f"(the best gap was {ev:.2f} standard deviations).")
        return out
    if line == current:
        out["explanation"] = (f"{name} stays at {current}, already where the record puts it: the {below} shifts "
                              f"under it did worse than the {above} at or over it.")
        return out
    step = max(-CALIBRATION_THRESHOLD_STEP, min(CALIBRATION_THRESHOLD_STEP, line - current))
    out["suggested"] = current + step
    out["explanation"] = (f"{name} {'up' if step > 0 else 'down'} to {current + step}: the {below} shifts under "
                          f"{line} did worse than the {above} at or over it (a gap of {ev:.1f} standard deviations)"
                          + ("" if current + step == line else f"; one apply moves it {CALIBRATION_THRESHOLD_STEP} "
                                                                "points toward that line") + ".")
    return out


def _profile_settings(restaurant_id, db_path=DB_PATH) -> dict:
    """{profile key: {"min_quality", "floors"}} the restaurant set itself —
    its own profiles, and the tuning applied to a built-in — which is where
    a floor or a bar stands now; a built-in nobody tuned is read from what
    its shifts were held to."""
    out = {}
    try:
        kw = {"db_path": db_path} if db_path and db_path != DB_PATH else {}
        for p in _models_mod.get_shift_profiles(restaurant_id, **kw) or []:
            out[str(p.get("key"))] = {"min_quality": p.get("min_quality"), "floors": dict(p.get("floors") or {})}
        for k, t in (_models_mod.get_quality_tuning(restaurant_id, **kw) or {}).items():
            e = out.setdefault(str(k), {"min_quality": None, "floors": {}})
            if t.get("min_quality") is not None:
                e["min_quality"] = t["min_quality"]
            e["floors"].update(t.get("floors") or {})
    except Exception as e:
        log.warning("[calibration] profile settings unavailable for %s: %s", restaurant_id, e)
    return out


def _profile_calibration(samples: list, settings: dict = None) -> tuple:
    """({profile key: reading}, {profile key: {"min_quality", "floors"}}) —
    each profile's quality bar and critical floors read against what its
    own shifts did (SQ-22), and the suggestions that move. A profile with
    fewer than CALIBRATION_MIN_PROFILE_SHIFTS shifts on record is named and
    left alone: a weekly profile is one shift a week, and its numbers are
    not moved on a handful."""
    from shift_quality import DIMENSION_LABELS
    by_profile = {}
    for sample in samples:
        meta = sample[3] if len(sample) > 3 else {}
        if (meta or {}).get("profile"):
            by_profile.setdefault(meta["profile"], []).append((sample[0], sample[1], meta))
    report, suggested = {}, {}
    for key in sorted(by_profile):
        items = by_profile[key]
        label = _most_common([m.get("label") or key for _d, _o, m in items]) or key
        entry = {"label": label, "shifts": len(items)}
        report[key] = entry
        if len(items) < CALIBRATION_MIN_PROFILE_SHIFTS:
            entry["ready"] = False
            entry["explanation"] = (f"{label}: {len(items)} of its shifts on record — its floors and bar are read "
                                    f"from {CALIBRATION_MIN_PROFILE_SHIFTS} of its own.")
            continue
        entry["ready"] = True
        own = (settings or {}).get(key) or {}
        bars = [m.get("bar") for _d, _o, m in items if m.get("bar") is not None]
        points = [(float(m["score"]), oc) for _d, oc, m in items if m.get("score") is not None]
        if bars and points:
            current = own.get("min_quality") if own.get("min_quality") is not None else _most_common(bars)
            entry["bar"] = _line_reading(f"{label}'s quality bar", points, current)
        floors = {}
        for dim in sorted({d for _d, _o, m in items for d in (m.get("floors") or {})}):
            recorded = [m["floors"][dim] for _d, _o, m in items if dim in (m.get("floors") or {})]
            current = (own.get("floors") or {}).get(dim)
            if current is None:
                current = _most_common(recorded)
            pts = [(float(d[dim]), oc) for d, oc, _m in items if dim in d]
            name = f"{label}'s {DIMENSION_LABELS.get(dim, dim).lower()} floor"
            floors[dim] = _line_reading(name, pts, current)
        entry["floors"] = floors
        move = {}
        if entry.get("bar") and entry["bar"]["suggested"] != entry["bar"]["current"]:
            move["min_quality"] = entry["bar"]["suggested"]
        moved_floors = {d: e["suggested"] for d, e in floors.items() if e["suggested"] != e["current"]}
        if moved_floors:
            move["floors"] = moved_floors
        if move:
            suggested[key] = move
    return report, suggested


def apply_profile_calibration(restaurant_id, cal: dict = None, updated_by: str = None, db_path=DB_PATH) -> dict:
    """Write the suggested floors and bars (calibrate_weights'
    `suggested_profiles`) — the owner's Apply (SQ-22). A profile the
    restaurant configured gets them in its own settings (save_shift_profile);
    a built-in gets them in the tuning the engine lays over the built-ins
    (restaurants.quality_tuning_json, shift_quality.profiles_from_config),
    so the rest of the built-in set stays as it was. Returns
    {"profiles": {key: what was written}, "before": tuning before} — {} of
    profiles when there was nothing to write."""
    cal = cal if cal is not None else calibrate_weights(restaurant_id, db_path=db_path)
    moves = (cal or {}).get("suggested_profiles") or {}
    kw = {"db_path": db_path} if db_path and db_path != DB_PATH else {}
    before = _models_mod.get_quality_tuning(restaurant_id, **kw) or {}
    if not moves:
        return {"profiles": {}, "before": before}
    own = {str(p.get("key")): p for p in (_models_mod.get_shift_profiles(restaurant_id, include_inactive=True, **kw) or [])}
    tuning = {k: dict(v) for k, v in before.items()}
    written = {}
    for key, mv in sorted(moves.items()):
        if key in own:
            p = dict(own[key])
            if mv.get("min_quality") is not None:
                p["min_quality"] = int(mv["min_quality"])
            if mv.get("floors"):
                p["floors"] = {**(p.get("floors") or {}), **{k: int(v) for k, v in mv["floors"].items()}}
            _models_mod.save_shift_profile(restaurant_id, p, updated_by=updated_by, **kw)
        else:
            t = tuning.setdefault(key, {})
            if mv.get("min_quality") is not None:
                t["min_quality"] = int(mv["min_quality"])
            if mv.get("floors"):
                t["floors"] = {**(t.get("floors") or {}), **{k: int(v) for k, v in mv["floors"].items()}}
        written[key] = dict(mv)
    if tuning != before:
        _models_mod.update_restaurant(restaurant_id, {"quality_tuning_json": json.dumps(tuning, sort_keys=True)}, **kw)
    return {"profiles": written, "before": before}


def _calibration_explanation(key, now, weight, nudge, ev, driver, contrib, pairs_n) -> str:
    """One sentence per dimension: which outcome moved it, and on how many
    shifts — or why it did not move."""
    label = key.replace("_", " ").capitalize()
    if not contrib:
        few = max(pairs_n.values()) if pairs_n else 0
        return (f"{label} has not been scored on enough shifts with a recorded outcome to read "
                f"({few} of the {CALIBRATION_MIN_PAIRS} needed).")
    if not nudge:
        return f"{label} did not line up clearly with any outcome (evidence {ev:+.2f}); left where it is."
    head = (f"{label} up" if weight > now else f"{label} down" if weight < now
            else f"{label} stays at {weight:g}, already where the record points")
    if driver:
        _sign, _l, good, bad = CALIBRATION_OUTCOMES[driver["outcome"]]
        went = good if nudge > 0 else bad
        return (f"{head}: shifts where it scored higher had {went} "
                f"(across {driver['shifts']} shift{'s' if driver['shifts'] != 1 else ''}), holding the other dimensions steady.")
    return f"{head}: the outcomes pulled in different directions, and on balance {'for' if nudge > 0 else 'against'} it."

# ── attendance by weekday ─────────────────────────────────────────────────

def attendance_by_weekday(restaurant_id, min_shifts=ATTENDANCE_MIN_WEEKDAY_SHIFTS, db_path=DB_PATH) -> dict:
    """{name: {weekday: {"shifts": n, "no_shows": k, "no_show_rate": r,
    "absence_rate", "call_out_rate"}}} — only weekdays with at least
    `min_shifts` watched shifts for that person.

    The one weighted attendance reader (staff_settings.weekday_attendance
    over attendance_events): the same window, recency weighting and
    notice-weighted call-outs as reliability (schedule audit 10/3/26 L-17,
    L-18) — this read an unweighted 360 days of its own, so the review and
    the scorer disagreed about the same person and day."""
    import staff_settings
    today = staff_settings.local_today(restaurant_id)
    return staff_settings.weekday_attendance(staff_settings.attendance_events(restaurant_id, today=today,
                                                                              db_path=db_path),
                                             today=today, min_shifts=min_shifts)


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
    # The one weighted attendance reader (staff_settings.attendance_record —
    # schedule audit 10/3/26 L-17): the window and recency weighting
    # reliability uses. Standby plans for a body missing, so it reads the
    # ABSENCE rate — a call-out counts in full whatever its notice, kept
    # apart from reliability's notice-weighted miss rate (L-18).
    import staff_settings
    rec = staff_settings.attendance_record(restaurant_id, db_path=db_path)
    tally = {k.strip().lower(): v for k, v in rec["tally"].items()}
    # Calibration (fix I9, CA1 L16): every person's rate is smoothed toward
    # this restaurant's own base rate (staff_settings.smoothed_rate), and a
    # person with no clocked record counts AT that base rate rather than
    # being left out — leaving them out biased the chance low exactly when
    # the least was known. The combination assumes one person's no-show says
    # nothing about another's, and the payload says so.
    from staff_settings import smoothed_rate
    base = rec["base"]["absence"]
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
        assumed = []
        for low, name in people.items():
            t = tally.get(low) or {}
            day_e = (t.get("days") or {}).get(wd)
            if day_e and day_e["shifts"] >= ATTENDANCE_MIN_WEEKDAY_SHIFTS:
                risks.append((name, smoothed_rate(day_e["w_absent"], day_e["w"], base), f"{wd}s"))
            elif t and t["shifts"] >= ATTENDANCE_MIN_OVERALL_SHIFTS:
                risks.append((name, smoothed_rate(t["w_absent"], t["w"], base), "overall"))
            else:
                unknown += 1
                assumed.append((name, round(base, 2), "no record — this restaurant's overall rate"))
        everyone = risks + assumed
        expected = sum(p for _n, p, _b in everyone)
        stay = 1.0
        for _n, p, _b in everyone:
            stay *= (1 - p)
        chance = 1 - stay
        if chance < min_risk:
            continue
        top = sorted((x for x in risks if x[1] > 0), key=lambda x: (-x[1], x[0]))[:3]
        out.append({"date": d, "day": wd, "scheduled": len(people), "no_record": unknown,
                    "expected_no_shows": round(expected, 2), "chance_of_a_no_show": round(chance, 2),
                    "base_rate": round(base, 3),
                    "assumption": ("Assumes no-shows are independent of each other. People with no clock-in "
                                   "record count at this restaurant's overall rate; every rate is smoothed "
                                   "toward it."),
                    "people": [{"employee": n, "no_show_rate": round(p, 2), "basis": b} for n, p, b in top]})
    out.sort(key=lambda x: (-x["chance_of_a_no_show"], x["date"]))
    out = out[:limit]
    for day in out:
        day["standby"] = _standby_person(restaurant_id, day, rows, tally, db_path, base=base)
    return out


def _standby_person(restaurant_id, day, rows, tally, db_path, base=0.0):
    """Who to put on call: somebody off that day in the role of the person
    most likely to miss, free and not on time off (labor_replacements'
    own checks), the most reliable first — a known low no-show rate before
    no record, then the operational score. None when nobody fits."""
    d = day["date"]
    working = {(r.get("employee") or "").strip() for r in rows if (r.get("date") or "")[:10] == d}
    risky = (day.get("people") or [{}])[0].get("employee")
    shift = next((r for r in rows if (r.get("date") or "")[:10] == d
                  and (r.get("employee") or "").strip() == risky), None) if risky else None
    role = (shift or {}).get("role") or ""
    try:
        import labor_replacements
        fits = labor_replacements.for_gap(restaurant_id, role, day["day"], exclude=working, db_path=db_path,
                                          limit=6, on_date=d)
    except Exception as e:
        print(f"[schedule_learning] standby candidates unavailable: {e}")
        return None
    if not fits:
        return None

    from staff_settings import smoothed_rate

    def _rate(name):
        t = tally.get(name.strip().lower()) or {}
        return (smoothed_rate(t["w_absent"], t["w"], base)
                if t and t.get("shifts", 0) >= ATTENDANCE_MIN_OVERALL_SHIFTS else None)

    ranked = sorted(fits, key=lambda f: (_rate(f["name"]) is None, _rate(f["name"]) or 0, -(f.get("score") or 0),
                                         f["name"]))
    pick = ranked[0]
    rate = _rate(pick["name"])
    return {"employee": pick["name"], "role": role, "no_show_rate": round(rate, 2) if rate is not None else None,
            "shift_start": (shift or {}).get("shift_start"), "shift_end": (shift or {}).get("shift_end")}


# ── overtime forecast ─────────────────────────────────────────────────────

OVERTIME_PREMIUM = 0.5      # time-and-a-half: the half is what moving the hours saves


def price_overtime_moves(forecast: list, role_rates=None, default_rate=None) -> list:
    """Each forecast entry with a candidate gains `saves`: the overtime
    premium the move avoids — the hours it takes off the overage, at half
    the shift role's rate (the straight-time half is paid either way, to
    whoever works it). No rate known, no dollar figure. Pure; returns the
    same list."""
    rates = {str(k).strip().lower(): float(v) for k, v in (role_rates or {}).items()
             if k and k != "_default" and v}
    # The rate every other labor dollar in the product uses: the role's, else
    # the restaurant's hourly rate.
    base = default_rate if default_rate is not None else (role_rates or {}).get("_default")
    for f in forecast or []:
        c = f.get("candidate")
        if not c:
            continue
        rate = rates.get(str(c.get("role") or "").strip().lower()) or (float(base) if base else None)
        # Only hours past the weekly OVERTIME line earn the premium. Being
        # past a personal cap (Ana's 25h) is a different flag with no
        # premium in it: pricing it as overtime told an owner a move saved
        # "$70 in overtime pay" on a 32h week (re-audit A-4). Entries built
        # before overtime_hours existed fall back to `over`.
        ot = f.get("overtime_hours", f.get("over"))
        hours_off = round(min(float(ot or 0), float(c.get("hours") or 0)), 1)
        c["overtime_hours_avoided"] = hours_off
        c["saves"] = round(hours_off * rate * OVERTIME_PREMIUM) if (rate and hours_off > 0) else None
        if c["saves"]:
            # A move not yet made: "would", never "saves" (NS3 labor #13).
            f["text"] = f["text"] + f" That move would save about ${c['saves']:,} in overtime pay."
    return forecast


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
    # A salaried person owes no overtime and their hours are not the hourly
    # pay this forecasts (owner's rule; schedule re-audit 10/4/26 SQ-5/SQ-6
    # siblings): a salaried GM at 55h was "past the ceiling", with a move
    # that "saves" overtime pay nobody owes. Their own cap is the sweep's
    # (over_max_hours on Constraints.salaried_limit).
    salaried_row = (lambda r: constraints.is_salaried(r.get("employee"))) if constraints is not None \
        else (lambda r: False)
    if constraints is not None:
        base_hours = constraints.base_hours if base_hours is None else base_hours
        bucket = constraints.bucket if bucket is None else bucket
        max_hours = constraints.max_hours if max_hours is None else max_hours
        if ceiling is None:
            ceiling = (getattr(constraints, "compliance", None) or {}).get("weekly_hours_ceiling")
    # The restaurant's weekly hours ceiling is a CAP (like a person's own
    # max_hours), never the overtime line. Overtime pay starts at
    # labor.OVERTIME_THRESHOLD_HOURS (40): a ceiling set to 35 priced "5h of
    # overtime ... saves about $50 in overtime pay" on a 40h week that owes
    # none (NS3 H5; re-audit A-4 for the personal cap).
    from labor import OVERTIME_THRESHOLD_HOURS as _OT_LINE
    ceil = float(ceiling or WEEKLY_HOURS_CEILING)
    ot_line = float(_OT_LINE)

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
        if salaried_row(r):
            continue
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
                if salaried_row({"employee": cname}):
                    # Their hours are not counted here, so their "room" is not known.
                    continue
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
                        "hours": round(_hours(r), 1), "index": i}
                break
        from time_utils import mdy
        published = round(_base(low, b), 1)
        overtime = round(max(0.0, total - ot_line), 1)
        past = (f"{over:g}h past the {ot_line:g}h overtime line" if overtime > 0.05 and abs(cap - ot_line) < 0.05
                else f"{over:g}h past their {cap:g}h limit"
                + (f", {overtime:g}h of it overtime" if overtime > 0.05 else ""))
        text = (f"{name} is scheduled for {round(total, 1):g}h in the payroll week{f' of {mdy(b)}' if b else ''} — "
                f"{past}")
        text += f" ({published:g}h already published)." if published else "."
        if pick:
            text += (f" {pick['employee']} ({pick['role'] or 'same role'}, {pick['headroom']:g}h of room) could take "
                     f"the {mdy(pick['date'])} {pick['shift_start']}–{pick['shift_end']} shift.")
        out.append({"employee": name, "bucket": b, "hours": round(total, 1), "draft_hours": round(h, 1),
                    "published_hours": published, "ceiling": cap, "over": over,
                    "overtime_line": ot_line, "overtime_hours": overtime, "candidate": pick, "text": text})
    out.sort(key=lambda x: (-x["over"], x["employee"]))
    return out
