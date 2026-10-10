"""
schedule_fallback.py — a week drafted without the model, and the switch that
says where a draft comes from (scheduler audit 10/10/26: AI-11, AI-26, AI-1,
AI-8; cross-workstream contract #7).

The model was a hard dependency: an outage, an AI budget pause, the run's
cap or a refusal left the owner with no schedule ("No shifts could be
written") or a week with empty days that "no backstop staffs" — although
code holds the requirements per half hour, the manager plan and a complete
solver. Here, the days the model did not write are drafted in code, and
labelled so; the same drafts are arms of the generation pipeline.

THE SOURCES (draft_source; DRAFT_SOURCES):

  model           the model writes the week (labor.generate_optimized_schedule)
                  — the default
  carry_forward   the last PUBLISHED week (else, before any, the last week
                  worked — the punches) mapped onto the new dates weekday for
                  weekday; people who left, are off or unavailable that day
                  are taken out; the manager plan's rows are merged by the
                  job as for any draft; then the repair loop and the solver
                  run as on any draft — floors, coverage, closers, the manager
                  rule, overtime, the budget all fixed there
  solver          code shapes the week: SHIFT REQUIREMENTS' people per role
                  and daypart laid onto the restaurant's own usual shifts
                  (shift_templates), people chosen by the solver
                  (schedule_engine.assign_shape_slots), then the repair loop.
                  v0 — workstream F replaces the shaping with the stage-1 MIP
                  and CP-SAT (RM-8, RM-11) behind this same name

  Which one: SCHEDULE_DRAFT_SOURCE (env, every restaurant) wins; then the
  week's arm of the "draft_v1" experiment (schedule_experiments — a
  restaurant's pin there is its own override); else "model". A source
  other than "model" makes no model call at all.

THE FALLBACK (fallback_whys): on the model source, a trading day the model
could not write — the provider down, the AI budget paused, the run's cap,
a refusal, the job's time, a call cut short, a day left empty — is drafted
by carry_forward (else solver), never left empty; the review says which days
were "built without the AI model" and why. A redo's unwritten day keeps the
owner's rows (the caller puts them back), as before. Data that is not
current is never papered over: those days stay unwritten.

Pure but for the reads of the published week; never raises into a
generation — a fallback that cannot be built leaves the days as they were.
"""
import os
from datetime import date as _date, timedelta as _td

DRAFT_SOURCES = ("model", "carry_forward", "solver")
DRAFT_SOURCE_ENV = "SCHEDULE_DRAFT_SOURCE"
EXPERIMENT = "draft_v1"
# The unwritten reasons (schedule_engine._UNWRITTEN_WHY) a code draft fills.
# Not "data" (the inputs are not current — a draft would hide that) and not
# "generation" (a sentence the job already owes the owner, e.g. Stop).
FALLBACK_WHYS = frozenset({"provider_down", "provider", "budget", "refused", "capped", "calls", "time",
                           "timeout", "config", "other", "cut", "skipped"})
# How far back the carried week may be.
CARRY_WEEKS = 8
# The words each reason reads as in the owner's line.
_WHY_WORDS = {
    "provider_down": "the AI model's provider wasn't responding",
    "provider": "the AI model's provider failed",
    "budget": "AI is paused — this account reached its AI limit",
    "refused": "the AI model declined to write it",
    "capped": "the draft reached its spending cap",
    "calls": "the draft used every model call it is allowed",
    "time": "the model ran out of time",
    "timeout": "the model took too long",
    "config": "Cavnar AI couldn't reach its model",
    "other": "the model failed partway",
    "cut": "the model ran out of room",
    "skipped": "the model left them empty",
    "chosen": "this week's drafts are built in code",
}


def draft_source(restaurant_id=None, arms=None) -> str:
    """Where this week's draft comes from: SCHEDULE_DRAFT_SOURCE when it
    names a source, else the draft_v1 arm's flag, else "model"."""
    env = str(os.getenv(DRAFT_SOURCE_ENV, "") or "").strip().lower()
    if env in DRAFT_SOURCES:
        return env
    for a in arms or []:
        v = str(((a or {}).get("flags") or {}).get("draft_source") or "").strip().lower()
        if v in DRAFT_SOURCES:
            return v
    return "model"


def _weekday(d):
    try:
        return _date.fromisoformat(str(d)[:10]).strftime("%A")
    except (TypeError, ValueError):
        return None


def base_week(restaurant_id, week_dates, shifts=None, db_path=None) -> dict:
    """The week a carry-forward copies: {"rows", "source": "published" |
    "punches", "week_start", "week_end"} — the newest PUBLISHED week (its
    live version) ending before this one within CARRY_WEEKS weeks, else the
    last seven days of `shifts` (the history the generation already read)
    before it, else {}."""
    first = str((week_dates or [""])[0])[:10]
    if not first:
        return {}
    try:
        import models
        import schedule_versions as _v
        conn = models.get_conn(db_path) if db_path else models.get_conn()
        try:
            r = conn.execute(
                "SELECT schedule_csv, week_start, week_end FROM schedule_history WHERE restaurant_id=? "
                "AND published_at IS NOT NULL AND superseded_by IS NULL AND week_end < ? AND week_end >= date(?, ?) "
                "AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id "
                "AND nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL "
                "AND nw.id > schedule_history.id) ORDER BY week_end DESC, id DESC LIMIT 1",
                (restaurant_id, first, first, f"-{7 * CARRY_WEEKS} days")).fetchone()
        finally:
            conn.close()
        if r and (r["schedule_csv"] or "").strip():
            rows = _v.rows_from_csv(r["schedule_csv"])
            if rows:
                return {"rows": rows, "source": "published", "week_start": r["week_start"], "week_end": r["week_end"]}
    except Exception as e:
        print(f"[schedule_fallback] published week not read: {e!r}")
    dated = sorted({str(s.get("date") or "")[:10] for s in shifts or [] if str(s.get("date") or "")[:10] < first
                    and len(str(s.get("date") or "")[:10]) == 10})
    if not dated:
        return {}
    last = _date.fromisoformat(dated[-1])
    since = (last - _td(days=6)).isoformat()
    rows = []
    for s in shifts or []:
        d = str(s.get("date") or "")[:10]
        if since <= d <= last.isoformat() and (s.get("employee") or "").strip() and s.get("shift_start") \
                and s.get("shift_end"):
            rows.append({"date": d, "day": _weekday(d) or "", "employee": str(s["employee"]).strip(),
                         "role": str(s.get("role") or "").strip(), "shift_start": str(s["shift_start"]),
                         "shift_end": str(s["shift_end"]), "scheduled_hours": str(s.get("scheduled_hours") or ""),
                         "notes": ""})
    return {"rows": rows, "source": "punches", "week_start": since, "week_end": last.isoformat()} if rows else {}


def carry_forward_rows(base, dates, constraints=None, roster_names=None, plan_dates=()) -> dict:
    """{"rows", "dropped": [{"employee", "date", "why"}]}: the base week's
    rows on the same weekday of each of `dates`. Left out: a person not on
    the roster now (left, deactivated, dormant — never re-added by code), a
    person who cannot work that day or daypart (approved time off,
    availability, a held note — Constraints.can_work), a planned manager row
    on a date the manager plan covers (the job merges the plan's own), and a
    row whose times cannot be read. Notes carry over only from the fixed
    note list (schedule_output.vocabulary_note); engine marks never do."""
    import schedule_output as _so
    import schedule_skeleton as _skel
    import shift_quality as _sq
    by_day = {}
    for r in (base or {}).get("rows") or []:
        wd = _weekday(r.get("date"))
        if wd:
            by_day.setdefault(wd, []).append(r)
    roster = None
    if roster_names is not None:
        c = constraints
        roster = {(c.key(n) if c is not None else " ".join(str(n).lower().split())) for n in roster_names}
    out, dropped = [], []
    plan_dates = set(plan_dates or ())
    for d in dates or []:
        wd = _weekday(d)
        for r in by_day.get(wd, []):
            name = " ".join(str(r.get("employee") or "").split())
            if not name:
                continue
            notes = str(r.get("notes") or "")
            if _skel.is_plan_note(notes) and d in plan_dates:
                continue                   # the plan's own row is merged by the job
            key = constraints.key(name) if constraints is not None else name.lower()
            if roster is not None and key not in roster:
                dropped.append({"employee": name, "date": d, "why": "not on the roster now"})
                continue
            row = {"date": d, "day": wd, "employee": name, "role": str(r.get("role") or "").strip(),
                   "shift_start": str(r.get("shift_start") or "").strip(),
                   "shift_end": str(r.get("shift_end") or "").strip(),
                   "scheduled_hours": str(r.get("scheduled_hours") or "").strip(),
                   "notes": _so.vocabulary_note(notes.split(" — ")[0].strip().lower())}
            if not row["shift_start"] or not row["shift_end"]:
                continue
            if constraints is not None:
                why = None
                for part in (_sq.touched_dayparts(row) or [_sq.daypart_of(row["shift_start"])]):
                    ok, reason = constraints.can_work(name, d, part)
                    if not ok:
                        why = reason
                        break
                if why is None:
                    ok, reason = constraints.window_ok(name, d, row["shift_start"], row["shift_end"])
                    if not ok:
                        why = reason
                if why:
                    dropped.append({"employee": name, "date": d, "why": why})
                    continue
            out.append(row)
    return {"rows": out, "dropped": dropped}


def solver_rows(dates, requirements_by_date, constraints, base_rows=None, context_rows=None, open_times=None,
                close_times=None, signals=None, weights=None, roster_roles=None, max_seconds=10.0) -> dict:
    """{"rows", "unassigned", "stats"} — the "solver" source, v0: for each
    date and daypart, SHIFT REQUIREMENTS' people per role (requirements_by_
    date, "date|daypart" -> {role: n}) as slots on the restaurant's usual
    shifts (shift_templates from `base_rows`; else the daypart's opening or
    4:30pm to its close), people chosen by the solver over every rule
    (schedule_engine.assign_shape_slots) with `context_rows` (the manager
    plan) held as they are. A slot nobody can legally work is left out,
    never filled illegally."""
    import shift_templates as _tpl
    import schedule_engine as _se
    tpl = _tpl.templates(base_rows or [])
    slots = []
    for d in dates or []:
        wd = _weekday(d)
        for part in ("morning", "night"):
            need = (requirements_by_date or {}).get(f"{d}|{part}") or {}
            for role, n in sorted(need.items()):
                try:
                    n = int(round(float(n or 0)))
                except (TypeError, ValueError):
                    n = 0
                for i in range(max(0, n)):
                    t = _tpl.pick(tpl, role, wd, part, i=i, n=n)
                    if t:
                        start, end = t["start"], t["end"]
                    else:
                        start, end = _default_times(wd, part, open_times, close_times)
                    slots.append({"date": d, "day": wd, "employee": "", "role": role, "shift_start": start,
                                  "shift_end": end, "scheduled_hours": "", "notes": ""})
    if not slots:
        return {"rows": [], "unassigned": [], "stats": {"status": "nothing_to_solve"}}
    got = _se.assign_shape_slots(slots, list(context_rows or []), constraints, signals=signals, weights=weights,
                                 roster_roles=roster_roles, only_dates=list(dates or []), max_seconds=max_seconds)
    return {"rows": got["assigned"], "unassigned": got["unassigned"], "stats": got["stats"]}


def _default_times(day, part, open_times=None, close_times=None):
    o = (open_times or {}).get(day) or "11:00am"
    c = (close_times or {}).get(day) or "10:00pm"
    return (o, "3:00pm") if part == "morning" else ("4:30pm", c)


def note_line(source, why, dates, base=None) -> str:
    """The review's line for days drafted in code: which days, from what,
    and why — "Built without the AI model: Saturday 10/17/26 and Sunday
    10/18/26 — your published week of 10/3/26 carried forward and checked
    against every rule (the AI model's provider wasn't responding)."."""
    from time_utils import mdy
    days = [f"{_weekday(d)} {mdy(d)}" for d in sorted(dates or [])]
    which = days[0] if len(days) == 1 else (", ".join(days[:-1]) + " and " + days[-1] if days else "this week")
    if source == "carry_forward" and base:
        what = ("your published week of " + mdy(base.get("week_start"))
                if base.get("source") == "published" else
                "the week your team last worked (" + mdy(base.get("week_start")) + "–" + mdy(base.get("week_end")) + ")")
        how = f"{what} carried forward and checked against every rule"
    else:
        how = "your usual crew and shifts, solved against every rule"
    words = _WHY_WORDS.get(why, _WHY_WORDS["other"])
    return f"Built without the AI model: {which} — {how} ({words})."
