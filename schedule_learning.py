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
                               measures it on held-out weeks)
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

EDIT_WEEKS = 8
NEW_PATTERN_CAP = 8
BUSY_NIGHTS = ("Friday", "Saturday")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_PRETTY = {"morning": "lunch/day", "night": "dinner/night"}

CALIBRATION_MIN_WEEKS = 8
CALIBRATION_MIN_SHIFTS = 40
CALIBRATION_MIN_PAIRS = 20        # per dimension per outcome, before a correlation is reported
CALIBRATION_MIN_EVIDENCE = 0.1    # |mean fitted effect| below this suggests no change
CALIBRATION_MAX_NUDGE = 0.30      # the fitted weight never strays more than 30% from its default
CALIBRATION_MAX_STEP = 0.10       # one Apply moves a weight at most 10% of its default
CALIBRATION_RIDGE = 0.1           # ridge penalty, as a share of the shifts fitted (standardized)

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
        # The manager's last word, not a staff swap or cover after it: those
        # are the staff's choices, and read as the manager's they taught the
        # draft "the manager keeps taking Bob off Saturday".
        mgr = [v for v in vs if v["reason"] != "swap"]
        final = mgr[-1] if mgr else vs[-1]
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

PREDICT_WEEKS = 16               # drafts looked back over
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
    if n.startswith("cavnar:") or " cavnar:" in n:
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
    """One entry per calendar week whose draft reached the manager's final
    word — edited, or published as it stood (a week sent out untouched is
    evidence too: every row in it was kept). [{history_id, week_start,
    rows, edited: [bool per row]}], oldest first. Staff swaps after the
    manager's last save are not the manager's edits (edited_weeks' rule)."""
    from schedule_versions import diff, rows_from_csv
    conn = get_conn(db_path)
    try:
        hist = conn.execute(
            "SELECT DISTINCT v.history_id, h.week_start FROM schedule_versions v JOIN schedule_history h ON h.id=v.history_id "
            "WHERE v.restaurant_id=? AND h.restaurant_id=? AND v.reason IN ('edited','published') "
            "AND v.created_at >= datetime('now', ?) ORDER BY v.history_id DESC LIMIT 80",
            (restaurant_id, restaurant_id, f"-{int(weeks) * 7} days")).fetchall()
        newest = {}
        for h in hist:
            wk = h["week_start"] or f"#{h['history_id']}"
            if wk not in newest:
                newest[wk] = (h["history_id"], wk)
        ids = sorted(v[0] for v in newest.values())
        if not ids:
            return []
        week_of = {v[0]: v[1] for v in newest.values()}
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
        base = next((v for v in vs if v["reason"] == "generated"), None)
        if base is None:
            continue
        mgr = [v for v in vs if v["reason"] in ("edited", "published") and v["version"] > base["version"]]
        if not mgr:
            continue
        b, f = rows_from_csv(base["schedule_csv"]), rows_from_csv(mgr[-1]["schedule_csv"])
        if not b:
            continue
        keys = _edited_keys(diff(b, f))
        out.append({"history_id": hid, "week_start": week_of.get(hid),
                    "rows": b, "edited": [(r.get("date") or "", (r.get("employee") or "").strip().lower(),
                                           r.get("shift_start") or "") in keys for r in b]})
    out.sort(key=lambda w: (w["week_start"] or "", w["history_id"]))
    return out


def fit_edit_model(weeks: list) -> dict:
    """Counts behind the predictor: overall and per feature value. Not
    ready (with the reason) under the minimum history."""
    n = sum(len(w["rows"]) for w in weeks)
    e = sum(sum(1 for x in w["edited"] if x) for w in weeks)
    need = (f"{PREDICT_MIN_WEEKS} drafts you've finished with, {PREDICT_MIN_ROWS} rows and "
            f"{PREDICT_MIN_EDITED} changed rows")
    if len(weeks) < PREDICT_MIN_WEEKS or n < PREDICT_MIN_ROWS or e < PREDICT_MIN_EDITED:
        return {"ready": False, "weeks": len(weeks), "rows": n, "edited": e,
                "reason": (f"{len(weeks)} draft{'s' if len(weeks) != 1 else ''} you've finished with, {n} row"
                           f"{'s' if n != 1 else ''}, {e} changed — predicting your edits needs at least {need}.")}
    tables = {f: {} for f in _FEATURES}
    for w in weeks:
        for feats, hit in zip(row_features(w["rows"]), w["edited"]):
            for f in _FEATURES:
                v = feats.get(f)
                if not v:
                    continue
                c = tables[f].setdefault(v, [0, 0])
                c[0] += 1 if hit else 0
                c[1] += 1
    return {"ready": True, "weeks": len(weeks), "rows": n, "edited": e,
            "base_rate": (e + 1.0) / (n + 2.0), "tables": tables}


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
        label = {"fill": "rows a fill-in pass added", "optimizer": "rows Cavnar's repair loop wrote",
                 "adjusted": "rows an automatic pass retimed or trimmed", "model": "rows the draft wrote"}[value]
        return f"{edits} of {seen} {label}"
    return f"{edits} of {seen}"


def predict_edits(model: dict, rows: list, threshold=PREDICT_THRESHOLD, limit=PREDICT_MAX_FLAGS) -> list:
    """[{index, employee, date, role, shift_start, likelihood, reason, text}]
    for draft rows at or above the threshold (and clearly above the
    restaurant's overall edit rate), likeliest first, at most `limit`."""
    if not model or not model.get("ready") or not rows:
        return []
    base = model["base_rate"]
    lb = _logit(base)
    out = []
    for i, (r, feats) in enumerate(zip(rows, row_features(rows))):
        contrib = []
        for f in _FEATURES:
            v = feats.get(f)
            c = (model["tables"].get(f) or {}).get(v) if v else None
            if not c:
                continue
            e, n = c
            rate = (e + PREDICT_SMOOTHING * base) / (n + PREDICT_SMOOTHING)
            contrib.append((_logit(rate) - lb, f, v, e, n))
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
        preds = predict_edits(model, w["rows"], threshold=threshold, limit=len(w["rows"]))
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
    fitted one at a time each took the credit for the other."""
    rows = [(dims, oc[outcome]) for dims, oc, _h in samples if oc.get(outcome) is not None]
    if len(rows) < min_pairs:
        return {}, {}, len(rows)
    ys = [y for _d, y in rows]
    my = sum(ys) / len(ys)
    sy = math.sqrt(sum((y - my) ** 2 for y in ys) / len(ys))
    if sy <= 1e-9:
        return {}, {}, len(rows)
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
    used = sorted(stats)
    if not used:
        return {}, seen, len(rows)
    xs = [[((d[k] - stats[k][0]) / stats[k][1]) if k in d else 0.0 for k in used] for d, _y in rows]
    yz = [(y - my) / sy for y in ys]
    n, p = len(xs), len(used)
    xtx = [[sum(xs[r][i] * xs[r][j] for r in range(n)) for j in range(p)] for i in range(p)]
    for i in range(p):
        xtx[i][i] += CALIBRATION_RIDGE * n
    xty = [sum(xs[r][i] * yz[r] for r in range(n)) for i in range(p)]
    beta = _solve(xtx, xty)
    if beta is None:
        return {}, seen, len(rows)
    # Scaled so a lone dimension's coefficient is its correlation, which is
    # what CALIBRATION_MIN_EVIDENCE was written against.
    return {k: beta[i] * (1 + CALIBRATION_RIDGE) for i, k in enumerate(used)}, seen, len(rows)


# The outcomes a shift is judged by afterwards: +1 = a higher value is
# better, −1 = a lower one is. Each carries the words its explanation uses.
CALIBRATION_OUTCOMES = {
    "issues": (-1, "coverage and no-show issues", "fewer coverage or no-show issues", "more coverage or no-show issues"),
    "review_rating": (1, "the day's review rating", "better reviews that day", "worse reviews that day"),
    "labor_vs_target": (-1, "labor % against target", "labor % nearer or under target", "labor % further over target"),
}


def _calibration_samples(restaurant_id, db_path):
    """[(dims, outcomes, history_id)] — one per recorded shift outcome that
    has a stored score, plus the dates Cavnar was watching (None when the
    restaurant cannot be watched at all)."""
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
                               "labor_vs_target": labor}, o["history_id"]))
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
        shifts that rests on. Deterministic."""
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
        coef, seen, used = _ridge_fit(samples, keys, outcome, CALIBRATION_MIN_PAIRS)
        fits[outcome] = {"coef": coef, "seen": seen, "shifts": used}
    report, suggested = {}, {}
    for key, default in DEFAULT_WEIGHTS.items():
        corr, pairs_n, coefs, contrib = {}, {}, {}, {}
        for outcome, (sign, _label, _good, _bad) in CALIBRATION_OUTCOMES.items():
            pairs = [(dims[key], oc[outcome]) for dims, oc, _h in samples if key in dims and oc[outcome] is not None]
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
                  " Coverage and no-show issues are not counted: none of these shifts fell on a night Cavnar was watching.")
    return {"ready": True, **base, "applied": False, "dimensions": report, "suggested_weights": suggested,
            "moving": moving,
            "fit": {o: {"shifts": f["shifts"], "dimensions": len(f["coef"])} for o, f in fits.items()},
            "limits": {"max_step_pct": int(CALIBRATION_MAX_STEP * 100), "max_total_pct": int(CALIBRATION_MAX_NUDGE * 100),
                       "min_pairs": CALIBRATION_MIN_PAIRS, "min_evidence": CALIBRATION_MIN_EVIDENCE},
            "note": ("Suggestions only — the engine keeps its current weights until someone applies them. One apply moves "
                     f"a weight at most {int(CALIBRATION_MAX_STEP * 100)}% of its default." + watch_note)}


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
    # Calibration (fix I9, CA1 L16): every person's rate is smoothed toward
    # this restaurant's own base rate (staff_settings.smoothed_rate), and a
    # person with no clocked record counts AT that base rate rather than
    # being left out — leaving them out biased the chance low exactly when
    # the least was known. The combination assumes one person's no-show says
    # nothing about another's, and the payload says so.
    from staff_settings import no_show_base_rate, smoothed_rate
    base = no_show_base_rate(tally)
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
            day_e, all_e = t.get(wd), t.get("all")
            if day_e and day_e[0] >= ATTENDANCE_MIN_WEEKDAY_SHIFTS:
                risks.append((name, smoothed_rate(day_e[1], day_e[0], base), f"{wd}s"))
            elif all_e and all_e[0] >= ATTENDANCE_MIN_OVERALL_SHIFTS:
                risks.append((name, smoothed_rate(all_e[1], all_e[0], base), "overall"))
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
        day["standby"] = _standby_person(restaurant_id, day, rows, tally, db_path)
    return out


def _standby_person(restaurant_id, day, rows, tally, db_path):
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

    from staff_settings import no_show_base_rate, smoothed_rate
    _base = no_show_base_rate(tally)

    def _rate(name):
        t = tally.get(name.strip().lower()) or {}
        all_e = t.get("all")
        return (smoothed_rate(all_e[1], all_e[0], _base)
                if all_e and all_e[0] >= ATTENDANCE_MIN_OVERALL_SHIFTS else None)

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
            f["text"] = f["text"] + f" That saves about ${c['saves']:,} in overtime pay."
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
    if constraints is not None:
        base_hours = constraints.base_hours if base_hours is None else base_hours
        bucket = constraints.bucket if bucket is None else bucket
        max_hours = constraints.max_hours if max_hours is None else max_hours
        if ceiling is None:
            ceiling = (getattr(constraints, "compliance", None) or {}).get("weekly_hours_ceiling")
    # The weekly overtime line: 40, or the restaurant's own compliance
    # ceiling. A person's cap (max_hours) may sit below it; that is a limit,
    # not overtime (re-audit A-4).
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
                        "hours": round(_hours(r), 1), "index": i}
                break
        from time_utils import mdy
        published = round(_base(low, b), 1)
        overtime = round(max(0.0, total - ceil), 1)
        past = (f"{over:g}h past the {ceil:g}h overtime line" if overtime > 0.05 and cap >= ceil - 0.05
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
                    "overtime_line": ceil, "overtime_hours": overtime, "candidate": pick, "text": text})
    out.sort(key=lambda x: (-x["over"], x["employee"]))
    return out
