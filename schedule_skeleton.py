"""
schedule_skeleton.py — the part of a week the code decides before the model
writes anything: the managers' shifts.

"There can be ZERO shifts without ONE manager. Always." (owner, 10/2/26).
Erik's first generated week had no manager on any day. Managers barely punch
(Erik 1, Jim 0, Anthony 2, Andrew 10 shifts on file), so the history the
draft was built from never asked for one; the rule sat ~70% of the way into
the prompt, outside PRIORITIES, below a SHIFT REQUIREMENTS table that named
no manager; and the backstop (schedule_rules.cover_manager_gaps) patched each
gap afterwards with a block of at least four hours shaped to the gap — a
manager walking in mid-afternoon (schedule audit 10/3/26 PR-1, P-8, D-4,
D-5, PR-32).

plan_manager_coverage plans each trading day's manager coverage first, from
the first person on to the last one out, as real opener and closer shifts
that hand over with an overlap. Its inputs, in order:

  1. each manager's standing shifts — the days and hours they always work
     (Constraints.standing_shifts, the owner's word);
  2. their availability, time windows, time off and every legal limit:
     each row is asked Constraints.can_add (rest, days in a row, shift
     length, the overtime line) and Constraints.fillable, and a salaried
     manager stops at their weekly cap (their own, the restaurant's, else
     SALARIED_WEEK_CAP);
  3. the days and hours they usually work, from published weeks AND
     punches — a salaried manager rarely punches, so the published week is
     their record;
  4. a fair split of what is left: the manager furthest from their weekly
     cap first, fewest days next — never "salaried first" (E-17: a salaried
     owner was loaded to 66h because their hours "cost nothing").

Acting managers (Constraints.acting_managers) take a date's shift only where
no real manager can. A stretch nobody can legally cover comes back with why;
managers with no standing shifts and no record come back for the owner's
question on the generate screen ("Which days and hours do Erik, Jim, Anthony
and Andrew work?").

The rows reach the model as fixed (labor.generate_optimized_schedule: the
MANAGER COVERAGE block and PRIORITIES 1a), are merged into its answer by
code (merge_pinned), and carry "_pinned": "manager_plan" through the job
(restore_pinned): no pass removes, re-times or re-assigns a pinned row.
cover_manager_gaps stays the backstop for whatever the plan could not cover.
"""
import re
from dataclasses import replace
from datetime import datetime, timedelta

import models as _models_mod
from models import DB_PATH
import schedule_rules as _rules


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


PLAN_SOURCE = "manager_plan"            # the "_pinned" value on every row planned here
# The notes column of a planned row. staff_facing_note strips "Cavnar AI: …",
# so staff never read it; the job's parser and the missing-day check know a
# planned line by it.
PLAN_NOTE = "Cavnar AI: manager plan"
# The "_pinned" value on a row a redo keeps as the owner saved it (re-audit
# 10/4/26 PIPE-1): no pass changes it; it counts as coverage and hours.
KEPT_SOURCE = "kept"
HANDOFF_MIN = 30                        # two managers overlap this long at a handover
SINGLE_SHIFT_MAX_MIN = 10 * 60          # a day this long or shorter is one manager's shift
MIN_SHIFT_MIN = _rules.MANAGER_MIN_SHIFT_MIN   # no planned manager shift is shorter (4h)
# A salaried manager with no weekly cap of their own, when the restaurant
# set none either (E-17: SALARIED_HOURS_CAP is seven 12-hour days).
SALARIED_WEEK_CAP = _rules.SALARIED_HOURS_CAP   # the one default (Constraints.salaried_limit)
HISTORY_WEEKS = 12                      # the window "usually works" is read over (models.USUAL_WEEKS)
USUAL_MIN_WEEKS = 2                     # a weekday is usual once it recurs in two weeks ...
USUAL_SHARE = 0.5                       # ... and in half the weeks the person appears in
EARLIEST_REAL_START = 4 * 60            # a row "starting" before 4am is last night's tail
MAX_STEPS_PER_DAY = 16


# ── small helpers ──────────────────────────────────────────────────────────

def _weekday(iso) -> str:
    try:
        return datetime.strptime(str(iso)[:10], "%Y-%m-%d").strftime("%A")
    except (TypeError, ValueError):
        return ""


_DAY_NAMES = {d[:3].lower(): d for d in _rules.DAYS}


def _day_name(value):
    """'Mon', 'monday', 'MONDAY' -> 'Monday'; None when it names no day."""
    low = str(value or "").strip().lower()
    return _DAY_NAMES.get(low[:3]) if len(low) >= 3 else None


def _clock(value):
    """Minutes past midnight for '9:30pm', '21:30' and a CSV's '24:00'."""
    raw = str(value or "").strip().lower().replace(" ", "")
    if raw.startswith("24:"):
        try:
            return 24 * 60 + int(raw[3:5] or 0)
        except ValueError:
            return None
    return _rules.parse_minutes(raw)


def _span(row):
    """(start, end) minutes on the row's own date, an end past midnight
    beyond 1440; None when unreadable or longer than a day could be."""
    s, e = _clock(row.get("shift_start")), _clock(row.get("shift_end"))
    if s is None or e is None:
        return None
    if e <= s:
        e += 24 * 60
    return (s, e) if 0 < e - s <= 16 * 60 else None


def _median(values):
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _down(m, step=15):
    return int(m // step * step)


def _up(m, step=15):
    return int(-(-m // step) * step)


def _fmt(m) -> str:
    return _rules._fmt_minutes(int(m) % (24 * 60))


def _hours_text(minutes) -> str:
    return str(round(minutes / 60.0, 2)).rstrip("0").rstrip(".")


def _names(names) -> str:
    """'A', 'A and B', 'A, B and C'."""
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _overlap(a, b) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def is_plan_note(notes) -> bool:
    """Whether a row's notes mark it as one this module planned."""
    return str(notes or "").strip().startswith(PLAN_NOTE)


def _line_row(line):
    """A schedule CSV line as a row dict, or None when it has too few
    columns to be a shift (the job's parser drops it and counts it)."""
    cols = [c.strip().strip('"').strip() for c in str(line or "").split(",", 7)]
    if len(cols) < 6:
        return None
    cols += [""] * (8 - len(cols))
    return dict(zip(("date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes"),
                    cols))


def is_plan_line(line) -> bool:
    """Whether a schedule CSV line is a planned manager row."""
    r = _line_row(line)
    return bool(r) and is_plan_note(r.get("notes"))


def _row_line(row) -> str:
    return ",".join(str(row.get(c, "") or "").replace(",", ";")
                    for c in ("date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes"))


# ── what the restaurant's own record says ──────────────────────────────────

def history_rows(restaurant_id, week_start, shifts=None, weeks: int = HISTORY_WEEKS, db_path=None) -> list:
    """Every row worked or published in the `weeks` weeks before
    `week_start`: published schedules (the record of a manager who never
    clocks in) and the punches (shift_facts, else `shifts`, the file's rows).
    Each row carries `_source` ("published" or "punch")."""
    try:
        first = datetime.strptime(str(week_start)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return []
    since = (first - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
    until = (first - timedelta(days=1)).strftime("%Y-%m-%d")
    out = []
    try:
        conn = get_conn(db_path)
        try:
            found = conn.execute(
                "SELECT schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL "
                "AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE "
                "nw.restaurant_id=schedule_history.restaurant_id AND nw.week_start=schedule_history.week_start "
                "AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id) "
                "AND substr(week_start,1,10) >= ? AND substr(week_start,1,10) <= ? ORDER BY week_start",
                (restaurant_id, since, until)).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        # The plan still runs from the hours and a fair split; the miss is
        # recorded, never silent.
        import ops
        ops.capture(exc, job="schedule_manager_plan", context=f"restaurant {restaurant_id} published weeks")
        found = []
    from schedule_versions import rows_from_csv
    for row in found:
        for r in rows_from_csv(row["schedule_csv"]):
            if since <= str(r.get("date") or "")[:10] <= until:
                out.append(dict(r, _source="published"))
    punches = []
    try:
        import shift_facts as _sf
        kw = {"db_path": db_path} if db_path and db_path != DB_PATH else {}
        punches = _sf.person_rows(restaurant_id, since=since, until=until, **kw) or []
    except Exception as exc:
        print(f"[schedule] punches unavailable to the manager plan for {restaurant_id}: {exc}")
        punches = []
    if not punches:
        punches = [s for s in (shifts or []) if since <= str(s.get("date") or "")[:10] <= until]
    out.extend(dict(r, _source="punch") for r in punches)
    return out


def day_spans(history) -> dict:
    """{weekday: {"start", "end", "dates"}} — when a working day here usually
    starts and ends: per date the first start and the last end of anybody's
    row, the median over that weekday's dates on file."""
    per_date = {}
    for r in history or []:
        d = str(r.get("date") or "")[:10]
        sp = _span(r)
        if len(d) != 10 or not sp or sp[0] < EARLIEST_REAL_START:
            continue
        lo, hi = per_date.get(d, sp)
        per_date[d] = (min(lo, sp[0]), max(hi, sp[1]))
    by_day = {}
    for d, sp in per_date.items():
        wd = _weekday(d)
        if wd:
            by_day.setdefault(wd, []).append(sp)
    return {wd: {"start": _median([a for a, _b in v]), "end": _median([b for _a, b in v]), "dates": len(v)}
            for wd, v in by_day.items()}


def trading_weekdays(history):
    """The weekdays this restaurant's own record shows it trading — on at
    least half the weeks on file (schedule_engine._trading_weekdays' rule);
    None (no opinion) with fewer than two weeks on file."""
    weeks, by_day = set(), {}
    for r in history or []:
        try:
            dt = datetime.strptime(str(r.get("date") or "")[:10], "%Y-%m-%d")
        except ValueError:
            continue
        wk = dt.strftime("%G-%V")
        weeks.add(wk)
        by_day.setdefault(dt.strftime("%A"), set()).add(wk)
    if len(weeks) < 2:
        return None
    return {wd for wd, w in by_day.items() if len(w) * 2 >= len(weeks)}


def usual_shifts(history, names) -> dict:
    """{lower name: {"weeks", "days": {weekday: {"start", "end", "weeks",
    "share"}}, "parts": {"morning": n, "night": n}}} — the days a person
    usually works and their usual hours on each. A weekday is usual once it
    recurs in USUAL_MIN_WEEKS distinct weeks and in USUAL_SHARE of the weeks
    they appear in at all; a single week is no pattern."""
    want = {str(n).strip().lower() for n in (names or ()) if n and str(n).strip()}
    per = {}
    for r in history or []:
        key = (r.get("employee") or "").strip().lower()
        if key not in want:
            continue
        d = str(r.get("date") or "")[:10]
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            continue
        p = per.setdefault(key, {"weeks": set(), "dates": {}, "roles": {}})
        p["weeks"].add(dt.strftime("%G-%V"))
        sp = _span(r)
        cur = p["dates"].get(d)
        if sp and sp[0] >= EARLIEST_REAL_START:
            p["dates"][d] = (min(cur[0], sp[0]), max(cur[1], sp[1])) if cur else sp
        else:
            p["dates"].setdefault(d, None)
        role = (r.get("role") or "").strip()
        if role:
            p["roles"].setdefault(d, {})
            p["roles"][d][role] = p["roles"][d].get(role, 0) + 1
    out = {}
    for key, p in per.items():
        n_weeks = len(p["weeks"])
        by_day, parts = {}, {"morning": 0, "night": 0}
        for d, sp in p["dates"].items():
            dt = datetime.strptime(d, "%Y-%m-%d")
            slot = by_day.setdefault(dt.strftime("%A"), {"weeks": set(), "spans": [], "roles": {}})
            slot["weeks"].add(dt.strftime("%G-%V"))
            for role, n in (p["roles"].get(d) or {}).items():
                slot["roles"][role] = slot["roles"].get(role, 0) + n
            if sp:
                slot["spans"].append(sp)
                parts["night" if sp[0] >= 15 * 60 else "morning"] += 1
        days = {}
        for wd, slot in by_day.items():
            k = len(slot["weeks"])
            if n_weeks < USUAL_MIN_WEEKS or k < USUAL_MIN_WEEKS or k < USUAL_SHARE * n_weeks or not slot["spans"]:
                continue
            # The role they usually work that day in (a manager who
            # bartends Tuesdays is pinned as the bartender — still the
            # manager on the floor, the rule counts the person).
            role = max(sorted(slot["roles"]), key=lambda x: slot["roles"][x]) if slot["roles"] else None
            days[wd] = {"start": _down(_median([a for a, _b in slot["spans"]])),
                        "end": _up(_median([b for _a, b in slot["spans"]])),
                        "weeks": k, "share": round(k / float(n_weeks), 2), "role": role}
        out[key] = {"weeks": n_weeks, "days": days, "parts": parts}
    return out


def _stay_after_close(c) -> int:
    """The longest any role stays past close: its after-close allowance
    (role_buffers, which caps every generated row) or its required stay
    (close_mins). A manager is on until the last of them leaves."""
    vals = [0]
    for src in (getattr(c, "role_buffers", None) or {}, getattr(c, "close_mins", None) or {}):
        for v in src.values():
            try:
                vals.append(int(v))
            except (TypeError, ValueError):
                continue
    return max(0, min(max(vals), 4 * 60))


def day_window(c, date_iso, open_times=None, close_times=None, spans=None):
    """(start, end, source) minutes of the day a manager has to be on: from
    the earlier of the opening time and the usual first start on file (prep
    arrives before the doors open) to the close plus the longest stay after
    it — or, with no close on file, the usual last end. None when neither
    the hours nor the record say when this day runs."""
    wd = _weekday(date_iso)
    o = _clock((open_times or {}).get(wd))
    cl = _clock((close_times or {}).get(wd))
    if cl is not None and (cl <= o if o is not None else cl < _rules._OVERNIGHT_LATEST_BEFORE):
        cl += 24 * 60                       # a close in the small hours is the next morning
    hist = (spans or {}).get(wd) or {}
    hs, he = hist.get("start"), hist.get("end")
    starts = [x for x in (o, hs) if x is not None]
    if not starts:
        return None
    start = min(starts)
    if cl is not None:
        end = cl + _stay_after_close(c)
    elif he is not None:
        end = he
    else:
        return None
    start, end = _down(start), _up(end)
    if end - start < 60 or end - start > 24 * 60:
        return None
    if o is not None and cl is not None and start == _down(o):
        source = "your opening and closing times"
    elif o is not None or cl is not None:
        source = "your hours and the shifts on file"
    else:
        source = "the shifts on file"
    return start, end, source


# ── the plan ───────────────────────────────────────────────────────────────

def _week_cap(c, name, line=None) -> float:
    """The most a manager is planned in a payroll week: a salaried person's
    own limit, else the restaurant's salaried cap, else SALARIED_WEEK_CAP —
    never the 84h ceiling (E-17); an hourly person's overtime line."""
    if c.is_salaried(name):
        return float(c.salaried_limit(name))
    return float(_rules.overtime_line(c, name, line))


def _standing_for(c, key, date_iso):
    """The standing shifts a person has on this date's weekday (inside any
    from/until the entry carries)."""
    wd = _weekday(date_iso)
    out = []
    for st in (getattr(c, "standing_shifts", None) or {}).get(key) or []:
        if not isinstance(st, dict) or _day_name(st.get("day")) != wd:
            continue
        f, u = str(st.get("from") or "")[:10], str(st.get("until") or "")[:10]
        if (f and date_iso < f) or (u and date_iso > u):
            continue
        out.append(st)
    return out


def plan_manager_coverage(c, week_dates, open_times=None, close_times=None, *, history=None,
                          prior_rows=None, roster_roles=None, line=None, handoff=HANDOFF_MIN) -> dict:
    """The week's manager shifts, planned before the model writes the rest.

    c            — the week's Constraints (managers, acting managers, standing
                   shifts, availability, limits, closed dates, the published
                   tail).
    week_dates   — the dates to plan: the week, or the days of a redo.
    open_times / close_times — {weekday: "11:00am"}; the Constraints' own
                   when None.
    history      — rows worked or published before the week (history_rows):
                   the usual day here and each manager's usual days and hours.
    prior_rows   — rows already fixed on other dates (a redo's kept days):
                   their hours, rest and runs count.
    roster_roles — {name: role}: an acting manager's row takes their role.

    Returns {"rows", "dates", "windows", "uncovered", "unplanned", "skipped",
    "hours", "managers", "unknown_pattern", "question"}. Each row is a
    schedule row with "_pinned": "manager_plan", "_pin_reason" (why this
    manager, these hours) and "_plan_source" (standing / usual / split /
    extended)."""
    open_times = c.open_times if open_times is None else open_times
    close_times = c.close_times if close_times is None else close_times
    display = {n.strip().lower(): n for n in (c.roster_names or []) if n and n.strip()}
    roster_roles = {str(n).strip().lower(): (r or "").strip() for n, r in (roster_roles or {}).items() if n}
    closed = set(c.closed_dates or ())
    dates = sorted({str(d)[:10] for d in (week_dates or []) if d and str(d)[:10] not in closed})
    order = {n.strip().lower(): i for i, n in enumerate(c.roster_names or [])}
    # In the owner's own roster order, so every list the owner reads (why a
    # stretch is uncovered, the question) follows it.
    managers = sorted((k for k in (c.managers or {}) if not c.active or k in c.active),
                      key=lambda k: (order.get(k, 1e9), k))
    acting = {k: set(v or ()) for k, v in (getattr(c, "acting_managers", None) or {}).items()
              if k not in (c.managers or {})}
    max_shift = int(float(c.compliance.get("max_shift_hours") or _rules.DEFAULTS["max_shift_hours"]) * 60)
    min_len = min(MIN_SHIFT_MIN, max_shift)
    # Rounding a split to the half hour can add up to an hour to a middle
    # leg: the target stays that far under the shift-length rule.
    single_max = max(min_len, min(SINGLE_SHIFT_MAX_MIN, max_shift - 60))
    spans = day_spans(history)
    trading = trading_weekdays(history)
    usual = usual_shifts(history, managers + sorted(acting))
    prior = [dict(r) for r in (prior_rows or []) if (r.get("employee") or "").strip()]
    plan, skipped, uncovered, unplanned, windows = [], [], [], [], {}
    if not managers and not acting:
        # Nobody to plan: the sweep's one no_manager_roster note says so
        # (a line per day here would only repeat it).
        return {"rows": [], "dates": dates, "windows": {}, "uncovered": [], "unplanned": [], "skipped": [],
                "hours": {}, "managers": [], "acting": [], "unknown_pattern": [], "question": None,
                "no_managers": True}

    def name_of(key):
        return display.get(key) or key.title()

    def role_of(key, standing=None):
        if standing and str(standing.get("role") or "").strip():
            return str(standing["role"]).strip()
        return (c.managers or {}).get(key) or roster_roles.get(key) or "Manager"

    def pool(d):
        """Who may manage on `d`: the managers, then anybody standing in as
        one that date (Constraints.manages — the one answer to "counts as
        the manager on the floor")."""
        return [(k, False) for k in managers] + [(k, True) for k in sorted(acting) if c.manages(name_of(k), d)]

    def hours_in(key, bucket):
        total = float((c.base_hours.get(key) or {}).get(bucket, 0.0) or 0.0)
        for r in prior + plan:
            if (r.get("employee") or "").strip().lower() == key and r.get("date") and c.bucket(r["date"]) == bucket:
                total += _rules.row_hours(r)
        return total

    def days_on(key):
        return len({r["date"] for r in prior + plan if (r.get("employee") or "").strip().lower() == key})

    def on_day(key, d):
        return [r for r in plan if r["date"] == d and (r.get("employee") or "").strip().lower() == key]

    def make_row(key, d, s, e, role, source, reason):
        return {"date": d, "day": _weekday(d), "employee": name_of(key), "role": role,
                "shift_start": _fmt(s), "shift_end": _fmt(e), "scheduled_hours": _hours_text(e - s),
                "notes": f"{PLAN_NOTE} — {reason}", "_pinned": PLAN_SOURCE, "_pin_reason": reason,
                "_plan_source": source}

    # A standing shift is the owner writing the person in, not code choosing
    # them: "dormant" (no shift in weeks — a guess, and managers rarely
    # punch) does not stand against it; an unconfirmed note about the day
    # still does (fillable, with the dormant guess set aside).
    owner_fill = replace(c, dormant={}) if getattr(c, "dormant", None) else c

    def legal(row, without=None, standing=False):
        """(ok, why): the one legality question (can_add), who code may
        choose (fillable), and a salaried manager's weekly cap."""
        others = [r for r in prior + plan if r is not without]
        ok, why = c.can_add(row, others, line=line, overtime=True)
        if not ok:
            return False, why
        ok, why = (owner_fill if standing else c).fillable(row["employee"], row["date"])
        if not ok:
            return False, why
        key = row["employee"].strip().lower()
        bucket = c.bucket(row["date"])
        cap = _week_cap(c, row["employee"], line)
        have = hours_in(key, bucket) - (_rules.row_hours(without) if without is not None
                                        and c.bucket(without["date"]) == bucket else 0.0)
        if have + _rules.row_hours(row) > cap + 0.05:
            return False, f"would take them to {have + _rules.row_hours(row):g}h, past their {cap:g}h week"
        return True, ""

    def coverage(d):
        return _rules._merge([sp for sp in (_span(r) for r in plan if r["date"] == d) if sp])

    # The day each date has to be covered for. A weekday with no hours set
    # that the record shows the restaurant never trades is not a day to
    # plan (the generation accepts it empty, schedule_engine._trading_weekdays).
    for d in dates:
        wd = _weekday(d)
        hours_set = _clock((open_times or {}).get(wd)) is not None or _clock((close_times or {}).get(wd)) is not None
        if trading is not None and wd not in trading and not hours_set:
            continue
        w = day_window(c, d, open_times, close_times, spans)
        if w is None:
            unplanned.append({"date": d, "day": _weekday(d),
                              "why": f"no opening or closing time is set for {_weekday(d)}s and there are no "
                                     f"shifts on file to read the day from"})
            continue
        windows[d] = {"start": w[0], "end": w[1], "from": _fmt(w[0]), "to": _fmt(w[1]), "source": w[2]}
    planned = [d for d in dates if d in windows]

    # 1. Standing shifts — the owner's word on when a manager works.
    for d in planned:
        for key, is_acting in pool(d):
            for st in _standing_for(c, key, d):
                s, e = _clock(st.get("start")), _clock(st.get("end"))
                if s is None or e is None:
                    skipped.append({"employee": name_of(key), "date": d, "why": "its times could not be read"})
                    continue
                if e <= s:
                    e += 24 * 60
                row = make_row(key, d, s, e, role_of(key, st), "standing",
                               f"{name_of(key)}'s standing {_weekday(d)} shift"
                               + (" (acting manager this date)" if is_acting else ""))
                ok, why = legal(row, standing=True)
                if ok:
                    plan.append(row)
                else:
                    skipped.append({"employee": name_of(key), "date": d, "shift": f"{_fmt(s)}–{_fmt(e)}",
                                    "why": why})

    # 2. The days and hours each one usually works, where they add cover.
    for d in planned:
        wd = _weekday(d)
        S, E = windows[d]["start"], windows[d]["end"]
        by_share = sorted(pool(d), key=lambda ka: (ka[1], -((usual.get(ka[0]) or {}).get("days", {})
                                                           .get(wd, {}).get("share") or 0)))
        for key, is_acting in by_share:
            u = ((usual.get(key) or {}).get("days") or {}).get(wd)
            if not u or on_day(key, d):
                continue
            s, e = max(u["start"], S), min(u["end"], E)
            if e - s < min(min_len, E - S):
                continue
            if not _rules._uncovered([(s, e)], coverage(d)):
                continue                    # somebody planned already covers those hours
            n = (usual.get(key) or {}).get("weeks") or 0
            row = make_row(key, d, s, e, u.get("role") or role_of(key), "usual",
                           f"{name_of(key)} usually works {wd}s ({u['weeks']} of the last {n} weeks on file)"
                           + (" (acting manager this date)" if is_acting else ""))
            if legal(row)[0]:
                plan.append(row)

    # 3. The rest, as opener and closer shifts: the hardest dates first.
    def free_count(d):
        return sum(1 for k, _a in pool(d) if c.can_work(name_of(k), d)[0] and c.fillable(name_of(k), d)[0])

    def legs(gap, S, E, cov):
        a, b = gap
        if a > S and any(cs < a <= ce for cs, ce in cov):
            a = max(S, a - handoff)          # hand over: overlap the manager going off
        if b < E and any(cs <= b < ce for cs, ce in cov):
            b = min(E, b + handoff)
        while b - a < min_len and (a > S or b < E):
            # Too short to be a shift: run on into the covered time beside
            # it, never outside the day.
            if b < E:
                b = min(E, b + 15)
            if b - a < min_len and a > S:
                a = max(S, a - 15)
        span_len = b - a
        if span_len <= single_max:
            return [(a, b)]
        k = 2
        while (span_len + (k - 1) * handoff) / float(k) > single_max and k < 6:
            k += 1
        ell = (span_len + (k - 1) * handoff) / float(k)
        out = []
        for i in range(k):
            s = a if i == 0 else _down(a + i * (ell - handoff), 30)
            e = b if i == k - 1 else min(b, _up(a + i * (ell - handoff) + ell, 30))
            out.append((s, e))
        return out

    def shapes(key, d, leg, S, E):
        """The leg itself, then the part of it this person can take: inside
        their hours window or daypart, inside their weekly cap, then shorter
        by the half hour from its open side — a start their rest after last
        night's close allows (the leg's anchored end, the close for a closer,
        is kept)."""
        s, e = leg
        out = [(s, e)]
        wd = _weekday(d)
        win = (c.time_windows.get(key) or {}).get(wd)
        if win:
            lo, hi = win
            if hi is not None and ((lo is not None and hi < lo) or (lo is None and hi < _rules._OVERNIGHT_LATEST_BEFORE)):
                hi += 24 * 60
            cs = max(s, lo) if lo is not None else s
            ce = min(e, hi) if hi is not None else e
            out.append((cs, ce))
        part = (c.daypart_avail.get(key) or {}).get(wd)
        if part in ("morning", "night"):
            from shift_quality import CORE_WINDOWS, PRESENCE_MIN_OVERLAP
            if part == "morning":
                out.append((s, min(e, CORE_WINDOWS["night"][0] + PRESENCE_MIN_OVERLAP - 15)))
            else:
                out.append((max(s, CORE_WINDOWS["morning"][1] - PRESENCE_MIN_OVERLAP + 15), e))
        opens = s <= S and e < E
        left = int((_week_cap(c, name_of(key), line) - hours_in(key, c.bucket(d))) * 60)
        if 0 < left < e - s:
            # Short of a whole leg: the end of the day before its start (a
            # manager at close matters most), unless the leg opens the day.
            out.append((s, s + left) if opens else (e - left, e))
        for cut in range(30, e - s - min_len + 1, 30):
            out.append((s, e - cut) if opens else (s + cut, e))
        seen, uniq = set(), []
        for x in out:
            if x not in seen and x[1] - x[0] > 0:
                seen.add(x)
                uniq.append(x)
        return uniq

    def usual_day(key, d):
        return 1 if _weekday(d) in ((usual.get(key) or {}).get("days") or {}) else 0

    def likes_part(key, kind):
        """1 when the leg is the half of the day they mostly work (openers
        for a morning person) — a tie-breaker between people about as far
        from their caps, never a reason to load one person up."""
        parts = (usual.get(key) or {}).get("parts") or {}
        likes = ("night" if parts.get("night", 0) > parts.get("morning", 0) else
                 "morning" if parts.get("morning", 0) > parts.get("night", 0) else None)
        return 1 if (kind == "closer" and likes == "night") or (kind == "opener" and likes == "morning") else 0

    def best_new(d, leg, gap, S, E, tried):
        kind = "closer" if leg[1] >= E else "opener" if leg[0] <= S else "middle"
        best = None
        for key, is_acting in pool(d):
            if on_day(key, d):
                continue
            for s, e in shapes(key, d, leg, S, E):
                full = (s, e) == leg
                covered = _overlap((s, e), gap)
                if covered <= 0 or (not full and e - s < min_len and covered < gap[1] - gap[0]):
                    continue
                reason = (f"{'closes' if kind == 'closer' else 'opens' if kind == 'opener' else 'covers the middle of'} "
                          f"{_weekday(d)}" + (" — acting manager this date" if is_acting else "")
                          + ("" if full else " — as much of it as they can work"))
                row = make_row(key, d, s, e, role_of(key), "split", reason)
                ok, why = legal(row)
                if not ok:
                    if full:
                        tried.setdefault(name_of(key), why)
                    continue
                cap = _week_cap(c, name_of(key), line) or 1.0
                load = (hours_in(key, c.bucket(d)) + (e - s) / 60.0) / cap
                # A day they usually work first; then the fair split — the
                # share of their own cap the week would have used, in tenths
                # (so the half of the day they prefer can break a near-tie),
                # then days already worked.
                rank = (is_acting, not full, -covered, -usual_day(key, d), round(load, 1),
                        -likes_part(key, kind), round(load, 4), days_on(key), key)
                if best is None or rank < best[0]:
                    best = (rank, row)
                break
        return best[1] if best else None

    def best_extend(d, gap, tried, note=" — runs on: nobody else can manage that stretch"):
        """A planned (not standing) shift that day runs on toward the gap,
        as far as the shift-length rule, their rest and their cap allow."""
        best = None
        for r in [x for x in plan if x["date"] == d and x.get("_plan_source") != "standing"]:
            sp = _span(r)
            if not sp:
                continue
            s0, e0 = sp
            key = r["employee"].strip().lower()
            if gap[0] >= e0:
                tries = [(s0, ne) for ne in range(min(gap[1], s0 + max_shift), e0, -30)]
            elif gap[1] <= s0:
                tries = [(ns, e0) for ns in range(max(gap[0], e0 - max_shift), s0, 30)]
            else:
                continue
            if not tries or _overlap(tries[0], gap) <= 0:
                tried.setdefault(r["employee"], f"already on {_fmt(s0)}–{_fmt(e0)}, the longest shift the rules allow"
                                 if e0 - s0 >= max_shift - 30 else f"already on {_fmt(s0)}–{_fmt(e0)} that day")
                continue
            for ns, ne in tries:
                covered = _overlap((ns, ne), gap)
                if covered <= 0:
                    break
                row = make_row(key, d, ns, ne, r.get("role") or role_of(key), "extended",
                               r.get("_pin_reason", "") + note)
                ok, why = legal(row, without=r)
                if not ok:
                    tried.setdefault(r["employee"], why)
                    continue
                rank = (key in acting, -covered, key)
                if best is None or rank < best[0]:
                    best = (rank, r, row)
                break
        return best

    marked = {d: [] for d in planned}

    def step(d):
        """One move on `d`: the latest gap's closing leg to the best legal
        manager, else a manager already on that day running on, else the
        gap is marked uncovered with why. False when nothing is left."""
        S, E = windows[d]["start"], windows[d]["end"]
        cov = coverage(d)
        gaps = [g for g in _rules._uncovered([(S, E)], cov) if g[1] > g[0]
                and not any(m0 <= g[0] and g[1] <= m1 for m0, m1, _w in marked[d])]
        if not gaps:
            return False
        gap = max(gaps, key=lambda g: g[1])         # the close first: a manager at close matters most
        tried = {}
        # A gap shorter than a shift (the bartenders' hour after a usual
        # 4-10pm) is the manager beside it staying on, not somebody else's
        # four hours; a longer one is a new opener or closer.
        short = gap[1] - gap[0] < min_len
        ext = best_extend(d, gap, tried, f" — stays on {_fmt(gap[0])}–{_fmt(gap[1])}, less than a shift for "
                                         f"anybody else") if short else None
        if ext:
            _rank, old, new = ext
            plan[plan.index(old)] = new
            return True
        for leg in reversed(legs(gap, S, E, cov)):
            row = best_new(d, leg, gap, S, E, tried)
            if row:
                plan.append(row)
                return True
        ext = None if short else best_extend(d, gap, tried)
        if ext:
            _rank, old, new = ext
            plan[plan.index(old)] = new
            return True
        if not pool(d):
            why = "nobody on the roster is a manager"
        elif tried:
            why = "; ".join(f"{n}: {w}" for n, w in list(tried.items())[:4])
        else:
            why = "every manager is already on that day for as long as the rules allow"
        marked[d].append((gap[0], gap[1], why))
        return True

    # One move per date per round, the hardest dates first: when the
    # managers' hours cannot cover the week, every night's close is covered
    # before any day's morning — not Monday to Thursday end to end and the
    # weekend left bare.
    hardest_first = sorted(planned, key=lambda x: (free_count(x), x))
    for _round in range(MAX_STEPS_PER_DAY):
        if not [d for d in hardest_first if step(d)]:
            break
    for d in planned:
        cov = coverage(d)
        for m0, m1, why in marked[d]:
            for a, b in _rules._uncovered([(m0, m1)], cov):
                if b > a:
                    uncovered.append({"date": d, "day": _weekday(d), "from": _fmt(a), "to": _fmt(b),
                                      "start": a, "end": b, "minutes": b - a, "why": why})

    plan.sort(key=lambda r: (r["date"], (_span(r) or (0, 0))[0], r["employee"]))
    uncovered.sort(key=lambda u: (u["date"], u["start"]))
    hours = {}
    for r in plan:
        hours[r["employee"]] = round(hours.get(r["employee"], 0.0) + _rules.row_hours(r), 2)
    # The managers' hours on the week's other days, already fixed (a redo's
    # kept days): "Planned manager hours this week" summed only the redone
    # dates, beside a seam line that counted the whole week (schedule
    # re-audit 10/4/26 PROMPT-13).
    week = set(c.week_dates or ())
    mine = set(managers) | set(acting)
    kept_hours = {}
    for r in prior:
        d = str(r.get("date") or "")[:10]
        k = c.key(r.get("employee"))
        if d in dates or (week and d not in week) or k not in mine:
            continue
        kept_hours[name_of(k)] = round(kept_hours.get(name_of(k), 0.0) + _rules.row_hours(r), 2)
    known = {k for k in managers if (usual.get(k) or {}).get("days")}
    standing = {k for k in managers if (getattr(c, "standing_shifts", None) or {}).get(k)}
    unknown = [name_of(k) for k in managers if k not in known and k not in standing]
    # Availability is not a pattern (when they CAN work, not which days they
    # DO), but the question shows it so an owner who saved it isn't asked as
    # if nothing were on file (owner, 10/9/26): each unknown manager's week,
    # "off" on a blocked day, else the daypart they gave ("any" when unset).
    unknown_availability = {}
    for k in managers:
        if k in known or k in standing:
            continue
        blocked = (getattr(c, "unavailable_days", None) or {}).get(k) or set()
        parts = (getattr(c, "daypart_avail", None) or {}).get(k) or {}
        if not blocked and not parts:
            continue
        unknown_availability[name_of(k)] = {
            d: ("off" if d in blocked or parts.get(d) == "off" else (parts.get(d) or "any"))
            for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")}
    return {
        "rows": plan,
        "dates": dates,
        "windows": windows,
        "uncovered": uncovered,
        "unplanned": unplanned,
        "skipped": skipped,
        "hours": hours,
        "kept_hours": kept_hours,
        "managers": [{"name": name_of(k), "role": (c.managers or {}).get(k) or "",
                      "salaried": bool(c.is_salaried(name_of(k))),
                      "pattern": ("standing" if k in standing else "usual" if k in known else "none")}
                     for k in managers],
        "acting": [{"name": name_of(k), "dates": sorted(v)} for k, v in sorted(acting.items())],
        "unknown_pattern": unknown,
        "unknown_availability": unknown_availability,
        "question": (f"Which days and hours do {_names(unknown)} work?" if unknown else None),
    }


def failed_plan(c, error=None) -> dict:
    """What the generation carries when the plan could not be made: no rows
    (cover_manager_gaps still backstops the draft), the managers named so
    PRIORITIES 1a still states the rule."""
    order = {n.strip().lower(): i for i, n in enumerate(c.roster_names or [])}
    display = {n.strip().lower(): n for n in (c.roster_names or []) if n and n.strip()}
    return {"rows": [], "dates": [], "windows": {}, "uncovered": [], "unplanned": [], "skipped": [], "hours": {},
            "managers": [{"name": display.get(k) or k.title(), "role": r or "", "salaried": False, "pattern": "none"}
                         for k, r in sorted((c.managers or {}).items(), key=lambda kv: (order.get(kv[0], 1e9), kv[0]))],
            "acting": [], "unknown_pattern": [], "question": None, "failed": True,
            "error": (f"{type(error).__name__}" if error is not None else None)}


def plan_for_generation(restaurant_id, c, week_dates, shifts=None, roster_roles=None, dates=None,
                        prior_rows=None, db_path=None) -> dict:
    """The plan a generation hands the model: the week (or a redo's
    `dates`, with the kept days' rows as `prior_rows`), read against the
    restaurant's published weeks and punches."""
    week_dates = [str(d)[:10] for d in (week_dates or []) if d]
    if not week_dates:
        return failed_plan(c)
    history = history_rows(restaurant_id, week_dates[0], shifts=shifts, db_path=db_path)
    return plan_manager_coverage(c, sorted(set(dates)) if dates else week_dates, history=history,
                                 prior_rows=prior_rows, roster_roles=roster_roles)


# ── what the model is told, and the merge of its answer ────────────────────

def _plan_rows(pinned_rows, dates=None):
    want = set(dates) if dates else None
    return [r for r in (pinned_rows or []) if r.get("date") and (want is None or r["date"] in want)
            and r.get("_pinned", PLAN_SOURCE) == PLAN_SOURCE]


def priority_line(pinned_rows, plan=None, dates=None) -> str:
    """This restaurant's half of PRIORITIES 1a: who the managers are, and
    how the week covers them — the rule itself is a standing instruction,
    the same words for every restaurant (schedule_prompt.PRIORITIES, cached
    — schedule audit 10/3/26 PR-26), so the names live here, at the head
    of MANAGER COVERAGE. '' with no manager to name."""
    plan = plan or {}
    names = [f"{m['name']} ({m['role']})" if m.get("role") else m["name"] for m in (plan.get("managers") or [])]
    if not names:
        names = sorted({f"{r['employee']} ({r.get('role') or 'manager'})" for r in (pinned_rows or [])})
    if not names:
        return ""
    want = set(dates) if dates else None
    rows = _plan_rows(pinned_rows, dates)
    gaps = [u for u in (plan.get("uncovered") or []) if want is None or u["date"] in want]
    ran = bool(rows) or bool(gaps) or any(want is None or d in want for d in (plan.get("windows") or {}))
    text = (", ".join(names)
            + (f"; standing in as the manager on their dates: {', '.join(a['name'] for a in plan['acting'])}"
               if plan.get("acting") else "") + ".")
    if ran:
        text += (" Their shifts are planned in MANAGER COVERAGE below and code adds them to your answer: do not "
                 "write them, never write another shift for those managers on those dates, and keep every other "
                 "shift inside each date's manager window.")
        if gaps:
            text += (" Where it names a stretch no manager can legally cover, staff that stretch as usual — the owner "
                     "is told in the review.")
    else:
        text += (" Their shifts were not planned this time: overlap them so there is never a gap; if the managers "
                 "cannot cover a day, cover the longest stretches — the owner is told the minutes left uncovered.")
    return text


def _model_why(why) -> str:
    """A reason as the model may read it: a person's own note words
    ("their note: …", "your note: …" — free text staff typed) never reach the
    prompt unfenced; that there is a note about the day is enough."""
    out = []
    for part in str(why or "").split("; "):
        name, sep, reason = part.partition(": ")
        if sep and "note" in reason.lower():
            out.append(f"{name}: a scheduling note about that day")
        elif not sep and "note" in part.lower():
            out.append("a scheduling note about that day")
        else:
            out.append(part)
    return "; ".join(out)


def prompt_block(pinned_rows, plan=None, dates=None) -> str:
    """MANAGER COVERAGE — ALREADY SCHEDULED: the planned rows for the dates
    this call writes, each date's manager window, the stretches nobody can
    legally cover (with why), and the week's planned manager hours. A
    separate block, not a SHIFT REQUIREMENTS line: those numbers are built
    from punches, where managers rarely appear (P-8, D-4)."""
    plan = plan or {}
    want = set(dates) if dates else None
    rows = _plan_rows(pinned_rows, dates)
    windows = plan.get("windows") or {}
    unc = [u for u in (plan.get("uncovered") or []) if want is None or u["date"] in want]
    unplanned = [x for x in (plan.get("unplanned") or []) if want is None or x["date"] in want]
    days = sorted({r["date"] for r in rows} | {u["date"] for u in unc} | {x["date"] for x in unplanned}
                  | {d for d in windows if want is None or d in want})
    if not days:
        return ""
    lines = []
    for d in days:
        w = windows.get(d) or {}
        bits = [f"{r['employee']} {r['shift_start']}–{r['shift_end']} ({r.get('role') or 'manager'})"
                for r in sorted((r for r in rows if r["date"] == d), key=lambda r: (_span(r) or (0, 0))[0])]
        bits += [f"NO MANAGER {u['from']}–{u['to']} — nobody can legally cover it ({_model_why(u['why'])}); staff it as "
                 f"usual, the owner is told in the review" for u in unc if u["date"] == d]
        bits += [f"not planned ({x['why']}) — keep a manager on every minute anyone is" for x in unplanned
                 if x["date"] == d]
        head = f"  {_weekday(d)[:3]} {d}" + (f", manager window {w['from']}–{w['to']}" if w else "")
        lines.append(head + ": " + ("; ".join(bits) if bits else "no manager rows"))
    totals = {}
    for r in _plan_rows(pinned_rows):
        totals[r["employee"]] = totals.get(r["employee"], 0.0) + _rules.row_hours(r)
    # A redo plans only its dates: the kept days' manager hours are part of
    # the same week, so "this week" counts them (PROMPT-13).
    kept = {n: float(h) for n, h in (plan.get("kept_hours") or {}).items() if h}
    for n, h in kept.items():
        totals[n] = totals.get(n, 0.0) + h
    # The model is told NOT to write these rows (AI cost audit 10/7/26 #35):
    # labor.generate_optimized_schedule merges them in by code and drops any
    # copy the model still writes (merge_pinned_lines, an item_dropped
    # quality event), so "keep every one exactly as written" only paid for
    # output tokens that were thrown away.
    block = ("\n\nMANAGER COVERAGE — ALREADY SCHEDULED (PRIORITIES 1a). The owner's rule: a manager or owner on "
             "the floor every minute anyone is. Cavnar AI planned these shifts before you, from each manager's "
             "standing shifts, availability, time off and usual days. They are fixed rows that code adds to your "
             "answer exactly as listed: do NOT write them, and never write another shift for these people on "
             "these dates. Keep every other shift on a date inside its manager window. These rows count toward "
             "SHIFT REQUIREMENTS for their role.\n" + "\n".join(lines))
    if totals:
        block += ("\n  Manager hours this week, the kept days included: " if kept else
                  "\n  Planned manager hours this week: ") \
            + ", ".join(f"{n} {round(h, 2):g}h" for n, h in sorted(totals.items())) + "."
    return block


def requirements_note() -> str:
    """One line under SHIFT REQUIREMENTS so the table cannot contradict the
    plan (P-8): its manager-role numbers come from punches."""
    return ("\n  Manager coverage is not decided by this table: the MANAGER COVERAGE rows are fixed and added by "
            "code, and they count toward any manager-role number above.")


def _pins(pinned, dates=None, people=None) -> list:
    want = set(dates) if dates else None
    return [dict(p, _pinned=p.get("_pinned") or PLAN_SOURCE) for p in (pinned or [])
            if p.get("date") and (want is None or p["date"] in want)
            and (people is None or (p.get("employee") or "").strip().lower() in people)]


def _pin_spans(pins) -> dict:
    by = {}
    for p in pins:
        by.setdefault(((p.get("employee") or "").strip().lower(), p["date"]), []).append(_span(p))
    return by


def _over_a_pin(row, by) -> bool:
    """A row for a planned manager on that date that duplicates or overlaps
    their planned row (an unreadable one included)."""
    spans = by.get(((row.get("employee") or "").strip().lower(), row.get("date")))
    if not spans:
        return False
    sp = _span(row)
    return sp is None or any(p is None or _overlap(sp, p) > 0 for p in spans)


def merge_pinned(rows, pinned, dates=None, people=None, strip_marks=True) -> tuple:
    """(rows, dropped): the model's rows with the planned rows merged in.
    A model row for a planned manager on that date that duplicates or
    overlaps their planned row is dropped — the plan wins; any other row
    stands. Only planned rows for `dates` (and, given `people`, for those
    lower-case names — a department's call) are added. With `strip_marks`
    a model row never keeps the plan's mark in its notes (only a planned row
    may carry it: the job's parser leaves marked lines to the plan)."""
    pins = _pins(pinned, dates, people)
    by = _pin_spans(pins)
    out, dropped = [], []
    for r in rows or []:
        r = dict(r)
        if strip_marks and is_plan_note(r.get("notes")):
            r["notes"] = ""
        if _over_a_pin(r, by):
            dropped.append(r)
            continue
        out.append(r)
    return out + pins, dropped


def merge_pinned_lines(lines, pinned, dates=None, people=None) -> tuple:
    """merge_pinned over the call's CSV data lines (no header). A line too
    short to be a shift is left as it is for the job's parser to count."""
    pins = _pins(pinned, dates, people)
    if not pins:
        return list(lines or []), []
    by = _pin_spans(pins)
    out, dropped = [], []
    for ln in lines or []:
        r = _line_row(ln)
        if r is None:
            out.append(ln)
            continue
        if _over_a_pin(r, by):
            dropped.append(r)
            continue
        if is_plan_note(r.get("notes")):
            r["notes"] = ""
            ln = _row_line(r)
        out.append(ln)
    out.extend(_row_line(p) for p in pins)
    return out, dropped


def plan_reason(notes):
    """Why the plan placed a row, read from its notes ("Cavnar AI: manager
    plan — covers open" → "covers open"), or None."""
    n = _rules.strip_review_mark(notes)
    if not is_plan_note(n):
        return None
    rest = n[len(PLAN_NOTE):].strip()
    rest = rest[1:].strip() if rest[:1] in ("—", "-", "–") else rest
    rest = re.sub(r"\s*\(auto-capped to close time\)\s*$", "", rest).strip()
    return rest or None


def kept_rows(rows) -> list:
    """A redo's kept days as rows (re-audit 10/4/26 PIPE-1): exactly as the
    owner saved them — never through the job's parser, whose close cap
    re-timed a planned manager and opened a no-manager stretch on a day no
    pass may repair — each carrying "_pinned": PLAN_SOURCE (with its
    "_pin_reason") for a planned manager row, KEPT_SOURCE for any other.
    A NEEDS REVIEW mark comes off (PIPE-6): the final sweep marks what is
    still broken, with today's reason."""
    out = []
    for r in rows or []:
        r = dict(r)
        r["notes"] = _rules.strip_review_mark(r.get("notes"))
        if is_plan_note(r["notes"]):
            r["_pinned"] = PLAN_SOURCE
            r["_pin_reason"] = plan_reason(r["notes"])
        else:
            r["_pinned"] = KEPT_SOURCE
        out.append(r)
    return out


def mark_pins(rows, sent=None) -> list:
    """`rows` with every planned manager row carrying "_pinned" and its
    "_pin_reason" again (re-audit 10/4/26 UI-6). The pin lives in the week
    the way it is stored and sent back: the plan's mark in the row's notes
    (PLAN_NOTE — staff never read it, labor.staff_facing_note). A plan pin a
    client sent (`sent`, the request's own rows in order, or the row's own
    "_pinned") is kept too, and a pinned row whose notes lost the mark gets
    it back, so the stored week keeps it. Apply fixes and Improve used to
    read only the eight columns and drop the pin, and a reopened week had
    none. Only the plan's pin is read from a client: a redo's KEPT_SOURCE
    holds for that job alone. Copies; the input is not changed."""
    out = []
    sent = list(sent or [])
    for i, r in enumerate(rows or []):
        r = dict(r)
        src = sent[i] if i < len(sent) and isinstance(sent[i], dict) else r
        why = str(src.get("_pin_reason") or r.get("_pin_reason") or "").strip()[:200] or None
        pinned = PLAN_SOURCE in (str(src.get("_pinned") or "").strip(), str(r.get("_pinned") or "").strip())
        if is_plan_note(r.get("notes")):
            pinned = True
            why = why or plan_reason(r.get("notes"))
        elif pinned:
            mark = f"{PLAN_NOTE} — {why}" if why else PLAN_NOTE
            n = str(r.get("notes") or "").strip()
            r["notes"] = f"{mark} — {n}" if n else mark
        if pinned:
            r["_pinned"] = PLAN_SOURCE
            if why:
                r["_pin_reason"] = why
        rid = src.get("_rid")
        if rid and not r.get("_rid"):
            r["_rid"] = str(rid)[:64]
        out.append(r)
    return out


def carry_pins(rows, stored_rows) -> list:
    """`rows` (a save) with the plan's mark carried from the week as stored
    (`stored_rows`) onto the row that is still that manager's shift that
    day — the one row for that person and date — when the save sent it
    without the mark (re-audit 10/4/26 UI-6): no client can drop a pin. A
    shift the owner deleted or gave to somebody else is no longer the plan's.
    Then mark_pins."""
    plan = {}
    for r in stored_rows or []:
        if is_plan_note(r.get("notes")):
            k = ((r.get("employee") or "").strip().lower(), r.get("date") or "")
            plan.setdefault(k, []).append(r)
    count = {}
    for r in rows or []:
        k = ((r.get("employee") or "").strip().lower(), r.get("date") or "")
        count[k] = count.get(k, 0) + 1
    out = []
    for r in rows or []:
        r = dict(r)
        k = ((r.get("employee") or "").strip().lower(), r.get("date") or "")
        was = plan.get(k) or []
        if len(was) == 1 and count.get(k) == 1 and not is_plan_note(r.get("notes")):
            why = plan_reason(was[0].get("notes"))
            mark = f"{PLAN_NOTE} — {why}" if why else PLAN_NOTE
            tail = str(r.get("notes") or "").strip()
            r["notes"] = f"{mark} — {tail}" if tail else mark
        out.append(r)
    return mark_pins(out)


def restore_pinned(rows, pinned, closed=()) -> list:
    """After the job's parser (which leaves the planned lines out): every
    planned row exactly as planned, carrying "_pinned" — so the parser's
    close cap never re-times one (a manager stays until the last role's
    after-close stay) — except on a date the generation accepted as closed.
    Any parsed row for that manager on that date that overlaps it is
    dropped. Other rows, a redo's kept days included, are left as they are."""
    closed = set(closed or ())
    pins = [p for p in _pins(pinned) if p["date"] not in closed]
    kept, _dropped = merge_pinned(rows, pins, strip_marks=False)
    return kept


# ── what the owner reads ───────────────────────────────────────────────────

def review_lines(plan, gaps=None) -> list:
    """The plan's lines for the generation review: why a stretch has no
    manager, the owner's question when the managers' days are unknown, a
    standing shift that could not be used. Dates M/D/YY. With `gaps` (the
    finished week's schedule_rules.manager_gaps) a stretch the plan could
    not cover is named only where the week really has somebody on and no
    manager, by those minutes."""
    from time_utils import mdy
    out = []
    if (plan or {}).get("failed"):
        out.append("Cavnar AI couldn't plan the managers' shifts before writing this draft, so its manager "
                   "coverage was filled in afterwards — check every day has a manager on from open to close.")
    for u in (plan or {}).get("uncovered") or []:
        spans = [(u["start"], u["end"])] if gaps is None else \
            [(max(gs, u["start"]), min(ge, u["end"])) for gs, ge, _i in (gaps.get(u["date"]) or [])
             if min(ge, u["end"]) > max(gs, u["start"])]
        for s, e in spans:
            out.append(f"No manager can be on {u['day']} {mdy(u['date'])} from {_fmt(s)} to {_fmt(e)} — "
                       f"{u['why']}.")
    if (plan or {}).get("question"):
        out.append(plan["question"] + " Cavnar AI has no standing shifts or past schedules for them, so this "
                   "week's manager shifts are an even split. Set each one's standing shifts in Team and every "
                   "draft will keep them.")
    for s in ((plan or {}).get("skipped") or [])[:3]:
        out.append(f"{s['employee']}'s standing shift on {_weekday(s['date'])} {mdy(s['date'])} wasn't used — "
                   f"{s['why']}.")
    return out


def payload(plan) -> dict:
    """The plan for the screens (generate result `manager_plan`)."""
    plan = plan or {}
    return {
        "planned": bool(plan.get("rows")),
        "failed": bool(plan.get("failed")),
        "shifts": [{"date": r["date"], "day": r.get("day"), "employee": r["employee"], "role": r.get("role"),
                    "shift_start": r["shift_start"], "shift_end": r["shift_end"],
                    "hours": _rules.row_hours(r), "source": r.get("_plan_source"), "reason": r.get("_pin_reason")}
                   for r in plan.get("rows") or []],
        "windows": {d: {k: w.get(k) for k in ("from", "to", "source")} for d, w in (plan.get("windows") or {}).items()},
        "uncovered": [{k: u.get(k) for k in ("date", "day", "from", "to", "minutes", "why")}
                      for u in plan.get("uncovered") or []],
        "unplanned": list(plan.get("unplanned") or []),
        "skipped": list(plan.get("skipped") or []),
        "hours": dict(plan.get("hours") or {}),
        "managers": list(plan.get("managers") or []),
        "acting": list(plan.get("acting") or []),
        "unknown_pattern": list(plan.get("unknown_pattern") or []),
        "unknown_availability": dict(plan.get("unknown_availability") or {}),
        "question": plan.get("question"),
    }
