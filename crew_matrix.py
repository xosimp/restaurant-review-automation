"""
crew_matrix.py — the Schedule Studio's Crew stage (owner, 10/10/26: "build
this matrix into the studio for every restaurant").

For every role, weekday and shift (lunch / dinner): the restaurant's usual
crew beside the draft on screen, the minimum it set, and — where its POS
sends checks — a busy / slow read of that shift's sales per person against
the role's own norm.

ONE HEADCOUNT UNIT (schedule audit 10/10/26, contract #2). Every number on
the grid is PEOPLE ON AT ONCE: `usual` / `draft` are how many of the role
are on through the core of the shift (the fewest on at any half hour of the
core window, shift_quality.concurrent_on — the exact count a staffing floor
is tested against), with the most on at once (`*_peak`) beside it. The
usual is labor.staffing_baseline's `concurrent` figure, the draft is counted
the same way from the draft's rows (no-show rows and training codes left
out). The grid used to set "how many different people worked some part of
dinner" (the usual) against rows bucketed by their start time (the draft)
— a mid-shift server read as dinner short — and Keep usual saved the first
number as a floor the restaurant's own usual night then broke (LOGIC-2,
LOGIC-5, BUGSGEN-1/2/17).

Where the draft runs leaner the owner answers once, each answer saying its
scope:

  * "Keep N every <day>" — N people on at once through that shift becomes
    its minimum for the role (restaurants.role_floors_json, the one floor
    every draft and the publish gate keep), held to what the role's whole
    family usually runs so the family's floors never add up past it; a
    review date a season out (HOLD_REVIEW_DAYS) when the cell asks again.
  * "Add 1 this week" — no standing rule: one legal shift for this draft
    (schedule_engine.crew_add_suggestion), added to the week on screen as an
    edit the owner saves (TRUST-9).
  * "Leaner is fine" — the draft's crew becomes the usual for that shift
    (bind_answers: staffing_baseline holds the usual crew there to it, so
    the requirement, the coverage score, the top-up and the trim all read
    it — LOGIC-6, TRUST-8) until its review date (LEAN_REVIEW_DAYS) or until
    the usual crew itself moves, when the cell asks again.

Undo restores exactly the minimum that was there before a hold (an explicit
"none this day" stays one), and only while the minimum is still what the
hold set — a minimum the owner changed since is theirs and stays
(BUGSGEN-18, BUGSLIFE-18). Answers and floors are written together, as one
compare-and-swap on the restaurant row (BUGSLIFE-19). Answers live in
restaurants.crew_answers_json, keyed "role|Weekday|part"; each is also an
owner observation (schedule_memory, source "crew") and an item on the
memory screen (memory_items — LEARNING-17). Deterministic; no model call.
"""
import csv
import io
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from models import DB_PATH

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
PARTS = ("morning", "night")
PART_WORD = {"morning": "lunch", "night": "dinner"}
SALES_WEEKS = 8
DEMAND_BAND = 0.3          # busy / slow: sales per person 30% off the role's own median
ANSWERS = ("hold", "lean")
CHOICES = ANSWERS + ("clear", "add")
# When an answer asks again: a kept minimum a season on (its crew may have
# changed with the season — a July summer crew is not January's), a
# "leaner is fine" after eight weeks of drafts built to it.
HOLD_REVIEW_DAYS = 91
LEAN_REVIEW_DAYS = 56
CAS_TRIES = 5


def _key(role, day, part) -> str:
    return f"{role}|{day}|{part}"


def answers_of(restaurant) -> dict:
    raw = getattr(restaurant, "crew_answers_json", None) if restaurant is not None else None
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return {k: v for k, v in (data or {}).items() if isinstance(v, dict) and v.get("answer") in ANSWERS}


def _match(name, names):
    low = str(name or "").strip().lower()
    return next((n for n in names if str(n).strip().lower() == low), None)


def _get(m, role):
    """The value under `role` in a role-keyed map, case-insensitively."""
    k = _match(role, list((m or {}).keys()))
    return (m or {}).get(k) if k is not None else None


def _split(key):
    parts = str(key).split("|")
    if len(parts) != 3:
        return None
    role, day, part = parts[0].strip(), parts[1].strip().capitalize(), parts[2].strip().lower()
    return (role, day, part) if role and day in DAYS and part in PARTS else None


def _iso(d) -> str:
    return d.isoformat() if isinstance(d, date) else str(d or "")[:10]


def _today(restaurant=None, today=None) -> date:
    if isinstance(today, datetime):
        return today.date()
    if isinstance(today, date):
        return today
    if today:
        try:
            return date.fromisoformat(str(today)[:10])
        except ValueError:
            pass
    try:
        from time_utils import restaurant_now
        return restaurant_now(restaurant, naive=True).date()
    except Exception:
        return date.today()


def _mdy(value) -> str:
    from time_utils import mdy
    return mdy(value)


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ── the usual crew, in people on at once ──────────────────────────────────

def usual_figures(baseline, role, day, part) -> dict:
    """{"held", "peak", "people"} — the role's usual crew for one shift:
    on at once through its core (`held`), the most on at once (`peak`), both
    from the baseline's `concurrent` figure, and the different people the
    requirement counts (`people`, typical_headcount — never compared with
    the other two)."""
    conc = _get((baseline.get("concurrent") or {}).get((day, part)) or {}, role) or {}
    people = _int(_get((baseline.get("typical_headcount") or {}).get((day, part)) or {}, role))
    held = conc.get("held")
    return {"held": _int(held) if held is not None else None, "peak": _int(conc.get("peak")), "people": people}


def _level(figs) -> int:
    """The grid's number for a shift: on at once through its core, or the
    most on at once where the restaurant serves none of the core window."""
    if not figs:
        return 0
    return _int(figs.get("held")) if figs.get("held") is not None else _int(figs.get("peak"))


def _review_due(a, usual_level, today) -> bool:
    """Whether an answer has come up for review: past its review date, or
    the usual crew it was given against has since moved."""
    rev = a.get("review")
    if rev:
        try:
            if date.fromisoformat(str(rev)[:10]) < today:
                return True
        except ValueError:
            pass
    else:
        # An answer from before review dates: its date plus the window.
        try:
            at = date.fromisoformat(str(a.get("at") or "")[:10])
            days = HOLD_REVIEW_DAYS if a.get("answer") == "hold" else LEAN_REVIEW_DAYS
            if at + timedelta(days=days) < today:
                return True
        except ValueError:
            pass
    unit = a.get("unit")
    if unit == "concurrent" and a.get("usual") is not None and usual_level is not None \
            and _int(a.get("usual")) != _int(usual_level):
        return True
    return False


def bind_answers(restaurant, patterns, today=None) -> dict:
    """Bind the owner's "Leaner is fine" answers into the staffing baseline
    (labor.staffing_baseline calls this): for each one in force — not past
    its review date, the usual crew still what it was answered against —
    that shift's usual crew (typical_headcount, the different people the
    requirement asks for) is held to what the owner accepted, so the
    requirement table, the coverage score, the top-up and the trim all read
    the saving (LOGIC-6, TRUST-8, LEARNING-17). Sets patterns["crew_answers"]
    = {(day, part): {role: {"people", "was", "since", "review"}}} and
    returns it. An answer saved before the accepted crew was recorded binds
    nothing (there is no number to hold to) and asks again."""
    answers = answers_of(restaurant)
    when = _today(restaurant, today)
    typical = patterns.get("typical_headcount")
    if not isinstance(typical, dict):
        typical = {}
        patterns["typical_headcount"] = typical
    bound = {}
    for key, a in answers.items():
        if a.get("answer") != "lean":
            continue
        cell = _split(key)
        accepted = a.get("accepted") or {}
        if not cell or accepted.get("people") is None:
            continue
        role, day, part = cell
        usual = _level(usual_figures(patterns, role, day, part))
        if _review_due(a, usual, when):
            continue
        people = max(0, _int(accepted.get("people")))
        slot = typical.setdefault((day, part), {})
        name = _match(role, list(slot)) or role
        was = _int(slot.get(name))
        if was > people:
            if people > 0:
                slot[name] = people
            else:
                slot.pop(name, None)
        if not slot:
            typical.pop((day, part), None)
        bound.setdefault((day, part), {})[name] = {
            "people": people, "was": was, "since": str(a.get("at") or "")[:10],
            "review": a.get("review") or ""}
    patterns["crew_answers"] = bound
    return bound


def floor_sources(restaurant, floors) -> dict:
    """{(job code lower, day, part): "M/D/YY"} — the floors kept in Crew that
    still stand as the hold set them (an owner's later change is theirs, not
    Crew's), for the breach to name its source (TRUST-9)."""
    import schedule_rules as sr
    out = {}
    for key, a in answers_of(restaurant).items():
        if a.get("answer") != "hold":
            continue
        cell = _split(key)
        if not cell:
            continue
        role, day, part = cell
        set_to = a.get("floor_set")
        if set_to is None:
            continue
        if sr.floor_for(floors or {}, role, day, part) == _int(set_to):
            out[(role.lower(), day, part)] = _mdy(a.get("at"))
    return out


# ── the draft, counted the same way ───────────────────────────────────────

def _draft(restaurant_id, history_id=None, db_path=DB_PATH):
    """(rows, week_start, history_id) of the week asked for, else the newest
    one built; ([], None, None) when there is none."""
    from models import get_conn
    conn = get_conn(db_path)
    try:
        if history_id:
            row = conn.execute("SELECT id, week_start, schedule_csv FROM schedule_history WHERE id=? AND restaurant_id=?",
                               (int(history_id), restaurant_id)).fetchone()
        else:
            row = conn.execute("SELECT id, week_start, schedule_csv FROM schedule_history WHERE restaurant_id=? "
                               "ORDER BY id DESC LIMIT 1", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return [], None, None
    rows = [dict(r) for r in csv.DictReader(io.StringIO(row["schedule_csv"] or ""))]
    return rows, row["week_start"], row["id"]


def _clean_rows(rows) -> list:
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        x = {k: str(r.get(k) or "").strip()[:200] for k in
             ("date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes")}
        if x["date"] and x["employee"] and x["role"]:
            out.append(x)
    return out


def draft_figures(restaurant_id, rows, restaurant=None, constraints=None, db_path=DB_PATH) -> dict:
    """{(job code, weekday, part): {"held", "peak", "people"}} for a week's
    draft rows — counted exactly as the usual crew is (concurrent_groups for
    people on at once, present_dayparts for the different people), each
    person once, a row that will not stand (time off, a double booking, off
    the roster — the sweep's no-shows) and a training code left out
    (LOGIC-5, BUGSGEN-1, BUGSGEN-17)."""
    import schedule_rules as sr
    import shift_quality as sq
    rows = _clean_rows(rows)
    if not rows:
        return {}
    dates = sorted({r["date"] for r in rows})
    c = constraints
    if c is None:
        try:
            c = sr.build_constraints(restaurant_id, dates,
                                     [datetime.strptime(d, "%Y-%m-%d").strftime("%A") for d in dates],
                                     restaurant=restaurant, db_path=db_path)
        except Exception as e:
            print(f"[crew] the draft's rules unreadable for {restaurant_id}: {e}")
            c = None
    skip_ids = set()
    if c is not None:
        try:
            skip_ids = {id(rows[v["index"]]) for v in sr.violations(rows, c, person_only=True)
                        if v.get("no_show") and isinstance(v.get("index"), int) and v["index"] < len(rows)}
        except Exception as e:
            print(f"[crew] the draft's no-shows unreadable for {restaurant_id}: {e}")
    open_times = (getattr(c, "open_times", None) or {}) if c is not None else {}
    close_times = (getattr(c, "close_times", None) or {}) if c is not None else {}
    families = (getattr(c, "role_families", None) or None) if c is not None else None
    out = {}
    for d in dates:
        try:
            day = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except ValueError:
            continue
        mine = [r for r in rows if r["date"] == d and id(r) not in skip_ids]
        open_m = sq._slot_minutes(open_times.get(day) or "")
        close_m = sq._slot_minutes(close_times.get(day) or "")
        for part in PARTS:
            groups = sq.concurrent_groups(mine, part, open_m, close_m, families)["role"]
            for role, figs in groups.items():
                people = sq.present_people(mine, part, role)
                if not (figs.get("peak") or people):
                    continue
                key = (role, day, part)
                prev = out.get(key)
                if prev is None or _level(figs) > _level(prev):
                    out[key] = {"held": figs.get("held"), "peak": _int(figs.get("peak")), "people": people}
    return out


def _shift_sales(restaurant_id, today=None, db_path=DB_PATH) -> dict:
    """{(day, part): median net sales of that weekday's shift} from the POS's
    checks over the last SALES_WEEKS weeks, the check's open time on the
    same 3pm split; {} when the POS sends no checks."""
    from models import get_conn
    today = today or date.today()
    since = (today - timedelta(days=7 * SALES_WEEKS)).isoformat()
    conn = get_conn(db_path)
    try:
        try:
            rows = conn.execute("SELECT business_date, opened_at, net_sales FROM pos_tickets WHERE restaurant_id=? "
                                "AND business_date >= ? AND cancelled=0 AND opened_at IS NOT NULL",
                                (restaurant_id, since)).fetchall()
        except Exception:
            rows = []
    finally:
        conn.close()
    per = {}
    for t in rows:
        try:
            opened = datetime.fromisoformat(str(t["opened_at"])[:19])
            bd = date.fromisoformat(str(t["business_date"])[:10])
        except ValueError:
            continue
        part = "morning" if 5 <= opened.hour < 15 else "night"
        k = (bd.isoformat(), part)
        per[k] = per.get(k, 0.0) + float(t["net_sales"] or 0)
    by = {}
    for (d, part), v in per.items():
        by.setdefault((DAYS[date.fromisoformat(d).weekday()], part), []).append(v)
    out = {}
    for k, vals in by.items():
        vals.sort()
        out[k] = round(vals[len(vals) // 2])
    return out


def _week_date(week_start, day):
    """The ISO date of `day` in the week starting `week_start`, or None."""
    try:
        first = date.fromisoformat(str(week_start)[:10])
    except (TypeError, ValueError):
        return None
    for i in range(7):
        d = first + timedelta(days=i)
        if d.strftime("%A") == day:
            return d.isoformat()
    return None


def build(restaurant_id, history_id=None, db_path=DB_PATH, today=None, baseline=None, rows=None) -> dict:
    """The Crew stage's payload: {ok, week_start, history_id, has_sales,
    has_usual, differences, open, unit, days, roles: [{role, usual_week,
    draft_week, cells: [...]}]}.

    Each cell: {day, part, usual, usual_peak, draft, draft_peak, floor,
    status, peak_note, demand, sales, trend {was, since M/D/YY}, answer,
    answer_state ("in_force" | "review"), since, review (M/D/YY), kept,
    accepted, can_add}. `usual` / `draft` / `floor` are people on at once
    through the core of the shift — one unit (`unit`: "concurrent").
    status: match / under (draft leaner) / over (draft heavier) / below_rule
    (under a minimum the owner set) / usual (no week built yet: nothing to
    compare, nothing to answer). A "Leaner is fine" in force judges the
    draft against what was accepted, not the usual.

    `rows` — the week as it stands on screen (unsaved edits included); the
    stored draft of `history_id` otherwise."""
    from models import get_restaurant
    import schedule_rules as sr
    r = get_restaurant(restaurant_id, db_path=db_path)
    if baseline is None:
        from labor import staffing_baseline
        baseline = staffing_baseline(restaurant_id) or {}
    when = _today(r, today)
    trends = {(t.get("day"), t.get("part"), str(t.get("role") or "").lower()): t
              for t in (baseline.get("concurrent_trends") or [])}
    floors = sr.role_floors(r) if r is not None else {}
    answers = answers_of(r)
    stored_rows, week_start, hid = _draft(restaurant_id, history_id, db_path=db_path)
    draft_rows = _clean_rows(rows) if rows else stored_rows
    draft = draft_figures(restaurant_id, draft_rows, restaurant=r, db_path=db_path) if hid is not None else {}
    sales = _shift_sales(restaurant_id, today=when, db_path=db_path)

    usual = {}
    for (day, part), roles in (baseline.get("concurrent") or {}).items():
        if day not in DAYS or part not in PARTS:
            continue
        for role in (roles or {}):
            if sr.is_training_role(role):
                continue
            figs = usual_figures(baseline, role, day, part)
            if _level(figs) > 0 or figs["peak"] > 0:
                usual[(role, day, part)] = figs
    names = []
    for role in [k[0] for k in usual] + [k[0] for k in draft] + list(floors):
        # Managers and owners are the manager-on-the-floor rule's, and the
        # salaried never count toward the usual crew (labor.historical_patterns
        # D-4), so every manager shift read as "heavier than usual" (EJ's,
        # 10/10/26: 17 of them). Left off the grid.
        if sr.is_manager_role(role):
            continue
        if not _match(role, names):
            names.append(role)

    def _at(m, role, day, part):
        for (rr, d, p), v in m.items():
            if d == day and p == part and rr.strip().lower() == role.strip().lower():
                return v
        return None

    roles_out, differences, open_n = [], 0, 0
    for role in names:
        per_head = []
        for day in DAYS:
            for part in PARTS:
                u = _level(_at(usual, role, day, part))
                if u and sales.get((day, part)):
                    per_head.append(sales[(day, part)] / float(u))
        per_head.sort()
        norm = per_head[len(per_head) // 2] if per_head else None
        cells, uw, dw = [], 0, 0
        for day in DAYS:
            for part in PARTS:
                uf, df = _at(usual, role, day, part) or {}, _at(draft, role, day, part) or {}
                u, d = _level(uf), _level(df)
                up, dp = _int(uf.get("peak")), _int(df.get("peak"))
                fl = sr.floor_for(floors, role, day, part) if floors else 0
                if not (u or up or d or dp or fl):
                    continue
                uw += u
                dw += d
                a = answers.get(_key(role, day, part)) or next(
                    (x for k, x in answers.items() if k.lower() == _key(role, day, part).lower()), None)
                due = bool(a) and _review_due(a, u if a.get("unit") == "concurrent" else None, when)
                if a and a.get("answer") == "lean" and (a.get("accepted") or {}).get("held") is None:
                    # Said before the accepted crew was recorded: there is
                    # no number it binds (bind_answers), so it asks again.
                    due = True
                lean_bar = None
                if a and a.get("answer") == "lean" and not due and (a.get("accepted") or {}).get("held") is not None:
                    lean_bar = _int(a["accepted"]["held"])
                # With no week built there is nothing to compare: the usual
                # crew alone, and nothing to answer.
                if hid is None:
                    status = "usual"
                elif d < fl:
                    status = "below_rule"
                else:
                    status = "under" if d < u else ("over" if d > u else "match")
                peak_note = None
                if hid is not None and status in ("match", "over") and dp < up:
                    peak_note = f"{up - dp} fewer at the busiest half hour"
                demand = None
                s = sales.get((day, part))
                if u and s and norm:
                    v = s / float(u)
                    demand = "busy" if v > (1 + DEMAND_BAND) * norm else ("slow" if v < (1 - DEMAND_BAND) * norm else None)
                t = trends.get((day, part, role.lower()))
                answer_out, state = None, None
                if a:
                    # A lean that no longer covers the draft (leaner than what
                    # was accepted) asks again; one in force answers the cell.
                    if a.get("answer") == "lean" and lean_bar is not None and d < lean_bar and hid is not None:
                        answer_out, state = None, "short_of_accepted"
                    else:
                        answer_out, state = a.get("answer"), ("review" if due else "in_force")
                if status not in ("match", "usual"):
                    differences += 1
                if hid is not None and ((status == "under" and not answer_out) or state == "review"):
                    open_n += 1
                cells.append({
                    "day": day, "part": part, "usual": u, "usual_peak": up, "draft": d, "draft_peak": dp,
                    "usual_people": _int(uf.get("people")), "draft_people": _int(df.get("people")),
                    "floor": fl, "status": status, "peak_note": peak_note, "demand": demand, "sales": s,
                    "trend": ({"was": t.get("was"), "since": _mdy(t.get("since"))} if t else None),
                    "answer": answer_out, "answer_state": state,
                    "since": _mdy((a or {}).get("at")) if a else None,
                    "review": _mdy(_review_date(a)) if a else None,
                    "kept": _int((a or {}).get("floor_set")) if a and a.get("answer") == "hold" else None,
                    "accepted": lean_bar,
                    "can_add": hid is not None and status in ("under", "below_rule"),
                    "date": _week_date(week_start, day) if week_start else None})
        if cells:
            roles_out.append({"role": role, "usual_week": uw, "draft_week": dw, "cells": cells})
    roles_out.sort(key=lambda x: (-x["usual_week"], x["role"].lower()))
    # The columns in the restaurant's own week order (Wednesday first at
    # Simple EJ's, restaurants.week_start_day).
    from schedule_engine import week_day_order, week_start_day_of
    return {"ok": True, "week_start": week_start, "history_id": hid, "has_sales": bool(sales),
            "has_usual": bool(usual), "differences": differences, "open": open_n, "roles": roles_out,
            "unit": "concurrent", "days": week_day_order(week_start_day_of(r)),
            "review_days": {"hold": HOLD_REVIEW_DAYS, "lean": LEAN_REVIEW_DAYS}}


def _review_date(a):
    if not a:
        return None
    if a.get("review"):
        return a["review"]
    try:
        at = date.fromisoformat(str(a.get("at") or "")[:10])
    except ValueError:
        return None
    return (at + timedelta(days=HOLD_REVIEW_DAYS if a.get("answer") == "hold" else LEAN_REVIEW_DAYS)).isoformat()


# ── answers: one compare-and-swap of the answers and the floors ───────────

def _snapshot(restaurant_id, db_path=DB_PATH):
    """The row's version, answers and floors as stored now — read past the
    request's memoised restaurant, so a retry after a lost race sees the
    other write."""
    from models import get_conn
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT row_version, crew_answers_json, role_floors_json FROM restaurants WHERE id=?",
                           (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return SimpleNamespace(version=_int(row["row_version"]), crew_answers_json=row["crew_answers_json"],
                           role_floors_json=row["role_floors_json"])


def _floor_name(floors, role):
    return _match(role, list(floors)) or role


def _day_spec(floors, role, day, part) -> dict:
    """{"present": the day has its own override for the part, "value": it}."""
    spec = floors.get(_floor_name(floors, role)) or {}
    dspec = (spec.get("days") or {}).get(day) or {}
    if part in dspec:
        return {"present": True, "value": _int(dspec.get(part))}
    return {"present": False, "value": None}


def _put_floor(floors, role, day, part, spec) -> dict:
    """`floors` with that one shift's day override set to spec["value"] (an
    explicit 0 — "none this day" — kept as one) or removed (present False),
    every other floor as it was."""
    import schedule_rules as sr
    out = json.loads(json.dumps(floors or {}))
    name = _floor_name(out, role)
    entry = out.get(name) or {"morning": 0, "night": 0, "days": {}}
    entry.setdefault("days", {})
    dspec = dict(entry["days"].get(day) or {})
    if spec.get("present"):
        dspec[part] = _int(spec.get("value"))
    else:
        dspec.pop(part, None)
    if dspec:
        entry["days"][day] = dspec
    else:
        entry["days"].pop(day, None)
    return sr.merge_role_map(out, {name: entry}, sr.clean_floor_spec)


def _hold_value(baseline, floors, role, day, part, usual_level) -> int:
    """The minimum a hold writes: the role's usual on at once through the
    shift, no more than its family usually runs once the family's other
    floors that shift are counted (the coverage_floor rule adds a family's
    floors up — two codes' usual crews summed could ask for more than the
    family's own usual night), never under the minimum already there."""
    import schedule_rules as sr
    from shift_quality import role_family
    value = int(usual_level)
    families = baseline.get("_families")
    fam = role_family(role, families)
    fam_figs = None
    for f, figs in ((baseline.get("concurrent_family") or {}).get((day, part)) or {}).items():
        if f == fam:
            fam_figs = figs
            break
    if fam_figs is not None and fam_figs.get("held") is not None:
        others = sum(sr.floor_for(floors, r, day, part) for r in floors
                     if r.strip().lower() != role.strip().lower() and role_family(r, families) == fam)
        value = min(value, max(0, _int(fam_figs["held"]) - others))
    return max(value, sr.floor_for(floors, role, day, part))


def _undo_hold(floors, role, day, part, before) -> tuple:
    """(floors, note) after taking a hold back: the exact prior day override
    restored — only while the minimum is still what the hold set; one the
    owner changed since stays theirs."""
    import schedule_rules as sr
    set_to = before.get("floor_set")
    prior = before.get("floor_before")
    if isinstance(prior, dict):
        prior_spec = {"present": bool(prior.get("present")), "value": prior.get("value")}
    else:
        # An answer from before the exact spec was kept: its number was
        # written as a day override (0 removed one).
        prior_spec = {"present": bool(_int(prior)), "value": _int(prior)}
        if set_to is None:
            set_to = max(_int(before.get("usual")), _int(prior))
    now = sr.floor_for(floors, role, day, part)
    if set_to is not None and now != _int(set_to):
        return floors, (f"Your minimum there was changed to {now} since you kept it, so it stays at {now}.")
    return _put_floor(floors, role, day, part, prior_spec), None


def _who(user):
    if isinstance(user, dict):
        return user.get("username") or user.get("email")
    return None


def answer(restaurant_id, role, day, part, choice, user=None, history_id=None, rows=None, db_path=DB_PATH,
           today=None, baseline=None) -> tuple:
    """Record the owner's answer for one shift — "hold" (keep the usual on
    at once there every week: its minimum), "lean" (the draft's crew is
    fine: the usual for that shift until its review), "clear" (undo) — or
    "add" (one legal shift for this week only, returned for the week on
    screen to take as an edit; nothing is saved). Returns ({ok, ...build of
    the SAME week, message?}, status) — the week the answer was given on
    (`history_id`, TRUST-10), never the newest one built."""
    import schedule_rules as sr
    role = str(role or "").strip()[:60]
    day = str(day or "").strip().capitalize()
    part = str(part or "").strip().lower()
    if not role or day not in DAYS or part not in PARTS or choice not in CHOICES:
        return {"ok": False, "error": "That shift couldn't be read. Reload and try again."}, 400
    try:
        history_id = int(history_id) if history_id not in (None, "") else None
    except (TypeError, ValueError):
        history_id = None
    if choice == "add":
        return add_one(restaurant_id, role, day, part, history_id=history_id, rows=rows, db_path=db_path)
    if baseline is None:
        from labor import staffing_baseline
        baseline = staffing_baseline(restaurant_id) or {}
    from models import get_restaurant, update_restaurant, StaleWrite
    try:
        from schedule_rules import role_families as _rf
        baseline = dict(baseline, _families=_rf(get_restaurant(restaurant_id, db_path=db_path)))
    except Exception:
        pass
    view = build(restaurant_id, history_id=history_id, db_path=db_path, today=today, baseline=baseline, rows=rows)
    cell = next((c for x in view["roles"] if x["role"].lower() == role.lower() for c in x["cells"]
                 if c["day"] == day and c["part"] == part), None)
    when = _today(get_restaurant(restaurant_id, db_path=db_path), today)
    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    note, before, wrote = None, None, None
    for _try in range(CAS_TRIES):
        snap = _snapshot(restaurant_id, db_path)
        if snap is None:
            return {"ok": False, "error": "That restaurant couldn't be read."}, 404
        data = answers_of(snap)
        floors = sr.role_floors(snap)
        key = next((k for k in data if k.lower() == _key(role, day, part).lower()), _key(role, day, part))
        before = data.get(key)
        new_floors, note = floors, None
        # Undo a hold first: its minimum goes back to exactly what it was.
        if before and before.get("answer") == "hold" and choice != "hold":
            new_floors, note = _undo_hold(new_floors, role, day, part, before)
        if choice == "clear":
            data.pop(key, None)
        elif choice == "hold":
            usual = _int((cell or {}).get("usual"))
            if usual <= 0:
                return {"ok": False, "error": f"Cavnar AI has no usual crew for {role} on {day} to keep."}, 400
            if before and before.get("answer") == "hold":
                # A renewal keeps the minimum that was there before the
                # first hold, so Undo still goes all the way back.
                prior = before.get("floor_before")
                if not isinstance(prior, dict):
                    prior = {"present": bool(_int(prior)), "value": _int(prior)}
            else:
                prior = _day_spec(new_floors, role, day, part)
            value = _hold_value(baseline, new_floors, role, day, part, usual)
            if value != sr.floor_for(new_floors, role, day, part):
                new_floors = _put_floor(new_floors, role, day, part, {"present": True, "value": value})
            if value < usual:
                note = (f"Kept {value} on at once: with the other {role} minimums that shift, that is what your "
                        f"whole crew usually runs.")
            data[key] = {"answer": "hold", "unit": "concurrent", "usual": usual, "floor_set": value,
                         "floor_before": prior, "at": stamp, "by": _who(user),
                         "review": (when + timedelta(days=HOLD_REVIEW_DAYS)).isoformat()}
        else:
            if view.get("history_id") is None or cell is None:
                return {"ok": False, "error": "Build a week first: leaner is fine is said against a draft."}, 400
            data[key] = {"answer": "lean", "unit": "concurrent", "usual": _int(cell.get("usual")),
                         "accepted": {"held": _int(cell.get("draft")), "peak": _int(cell.get("draft_peak")),
                                      "people": _int(cell.get("draft_people"))},
                         "history_id": view.get("history_id"), "at": stamp, "by": _who(user),
                         "review": (when + timedelta(days=LEAN_REVIEW_DAYS)).isoformat()}
        fields = {"crew_answers_json": json.dumps(data) if data else None}
        if new_floors != floors:
            fields["role_floors_json"] = json.dumps(new_floors) if new_floors else None
        try:
            update_restaurant(restaurant_id, fields, db_path=db_path, expected_version=snap.version)
            wrote = data.get(key)
            break
        except StaleWrite:
            continue
    else:
        return {"ok": False, "error": "Somebody else changed these answers just now. Reload Crew and try again."}, 409
    try:
        import change_log
        kw = {"user": user} if user else {}
        change_log.record(restaurant_id, "labor", "crew_answers_json", (before or {}).get("answer"),
                          None if choice == "clear" else choice,
                          subject=f"Crew: {role}, {day} {PART_WORD[part]}", **kw)
    except Exception:
        pass
    _observe(restaurant_id, role, day, part, choice, wrote, before, user, view.get("history_id"), db_path)
    out = build(restaurant_id, history_id=history_id, db_path=db_path, today=today, baseline=baseline, rows=rows)
    if note:
        out["message"] = note
    return out, 200


def _observe(restaurant_id, role, day, part, choice, wrote, before, user, history_id, db_path):
    """The answer into the scheduling memory's log — an owner's (or a
    manager's) explicit word about a role's crew, never lost to learning
    (LEARNING-17)."""
    try:
        import schedule_memory as smem
        from permissions import answer_authority
        authority = answer_authority(user) if user else "principal"
        smem.observe(restaurant_id, "crew_answer", daypart=part, role=role,
                     value={"day": day, "answer": None if choice == "clear" else choice,
                            "was": (before or {}).get("answer"), "unit": "concurrent",
                            "usual": (wrote or {}).get("usual"), "floor_set": (wrote or {}).get("floor_set"),
                            "accepted": (wrote or {}).get("accepted"), "review": (wrote or {}).get("review")},
                     origin="owner" if authority == "principal" else "manager", phase="pre_publish",
                     authority=authority, editor=_who(user), source="crew", history_id=history_id,
                     db_path=db_path if db_path != DB_PATH else None)
    except Exception as e:
        print(f"[crew] answer not observed for {restaurant_id}: {e}")


def add_one(restaurant_id, role, day, part, history_id=None, rows=None, db_path=DB_PATH) -> tuple:
    """One legal shift of `role` for that shift of THIS week — no standing
    rule (TRUST-9): schedule_engine.crew_add_suggestion over the week on
    screen (`rows`, else the stored draft of `history_id`). ({ok,
    suggestion: row, text, history_id}, status); nothing is saved — the
    Studio adds the row as an edit the owner saves."""
    stored, week_start, hid = _draft(restaurant_id, history_id, db_path=db_path)
    if hid is None:
        return {"ok": False, "error": "Build a week first, then add to it."}, 400
    week_rows = _clean_rows(rows) if rows else _clean_rows(stored)
    the_date = _week_date(week_start, day)
    if not the_date:
        return {"ok": False, "error": "That day isn't in this week."}, 400
    import schedule_engine as se
    got = se.crew_add_suggestion(restaurant_id, week_rows, the_date, role, part)
    if not got:
        return {"ok": False, "error": (f"Nobody who works {role} can legally take another {day} "
                                       f"{PART_WORD[part]} this week (time off, their hours or rest).")}, 200
    row, hours_after = got
    text = (f"Add {row['employee']}, {day[:3]} {row['shift_start']}–{row['shift_end']} "
            f"({hours_after:g}h this week)")
    return {"ok": True, "suggestion": row, "text": text, "history_id": hid, "week_start": week_start}, 200


# ── floors set in the old unit, translated ────────────────────────────────

def floors_on_at_once(restaurant_id, roles=None, baseline=None, apply=False, db_path=DB_PATH) -> dict:
    """The restaurant's staffing floors read as what they now mean — people
    ON AT ONCE through the core of the shift (contract #2) — where a floor
    was set as a count of different people (a Keep usual before 10/10/26,
    or a usual crew typed in by hand): for each weekday and daypart whose
    role family's floors add up to more than the family's own usual night
    holds on at once (its `concurrent_family` held figure), each code's
    floor is lowered to that code's own usual on at once, then — largest
    first — until the family's total is what its usual night holds. Floors
    the usual night already keeps are left as they are; a family with no
    history on that shift is left and named. Never raises a floor.

    `roles` keeps it to those job codes (case-insensitive). Dry run unless
    `apply`: then the changed shifts are written as day overrides and every
    Crew hold on them is restated in the new unit (its `floor_set`, so Undo
    and the breach's source still recognise it), in one compare-and-swap.
    {"changes": [{role, day, part, old, new, family_usual, code_usual}],
     "untouched": [...no history...], "floors": the result, "applied": bool}."""
    import schedule_rules as sr
    from models import get_restaurant, update_restaurant, StaleWrite
    from shift_quality import role_family
    r = get_restaurant(restaurant_id, db_path=db_path)
    if baseline is None:
        from labor import staffing_baseline
        baseline = staffing_baseline(restaurant_id) or {}
    families = sr.role_families(r) if r is not None else {}
    want = {str(x).strip().lower() for x in (roles or ()) if str(x).strip()}
    for _try in range(CAS_TRIES):
        snap = _snapshot(restaurant_id, db_path)
        if snap is None:
            return {"changes": [], "untouched": [], "floors": {}, "applied": False}
        floors = sr.role_floors(snap)
        answers = answers_of(snap)
        new_floors, changes, untouched = floors, [], []
        for day in DAYS:
            for part in PARTS:
                by_fam = {}
                for code in floors:
                    n = sr.floor_for(floors, code, day, part)
                    if n > 0:
                        by_fam.setdefault(role_family(code, families), []).append((code, n))
                for fam, codes in by_fam.items():
                    if want and not any(c.lower() in want for c, _n in codes):
                        continue
                    fam_figs = ((baseline.get("concurrent_family") or {}).get((day, part)) or {}).get(fam)
                    if not fam_figs or fam_figs.get("held") is None:
                        untouched.extend({"role": c, "day": day, "part": part, "floor": n} for c, n in codes)
                        continue
                    fam_held = _int(fam_figs["held"])
                    if sum(n for _c, n in codes) <= fam_held:
                        continue
                    new = {}
                    for c, n in codes:
                        own = _get((baseline.get("concurrent") or {}).get((day, part)) or {}, c)
                        # A floor named by the family ("Kitchen" over Line
                        # Cook and Prep) has no figure of its own: only the
                        # family's total below holds it, never a zero.
                        if own is not None and (not want or c.lower() in want):
                            new[c] = min(n, _level(own))
                        else:
                            new[c] = n
                    while sum(new.values()) > fam_held:
                        top = max((c for c in new if new[c] > 0 and (not want or c.lower() in want)),
                                  key=lambda c: (new[c], c), default=None)
                        if top is None:
                            break
                        new[top] -= 1
                    for c, n in codes:
                        if new[c] != n:
                            own = _get((baseline.get("concurrent") or {}).get((day, part)) or {}, c)
                            changes.append({"role": c, "day": day, "part": part, "old": n, "new": new[c],
                                            "family_usual": fam_held,
                                            "code_usual": _level(own) if own is not None else None})
                            new_floors = _put_floor(new_floors, c, day, part, {"present": True, "value": new[c]})
        if not apply or not changes:
            return {"changes": changes, "untouched": untouched, "floors": new_floors, "applied": False}
        stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        for ch in changes:
            key = next((k for k in answers if k.lower() == _key(ch["role"], ch["day"], ch["part"]).lower()), None)
            if key and answers[key].get("answer") == "hold":
                a = dict(answers[key])
                if not isinstance(a.get("floor_before"), dict):
                    a["floor_before"] = {"present": bool(_int(a.get("floor_before"))),
                                         "value": _int(a.get("floor_before"))}
                a.update(unit="concurrent", floor_set=ch["new"], restated_at=stamp,
                         usual=ch["code_usual"] if ch["code_usual"] is not None else ch["new"])
                answers[key] = a
        try:
            update_restaurant(restaurant_id, {"role_floors_json": json.dumps(new_floors) if new_floors else None,
                                              "crew_answers_json": json.dumps(answers) if answers else None},
                              db_path=db_path, expected_version=snap.version)
            return {"changes": changes, "untouched": untouched, "floors": new_floors, "applied": True}
        except StaleWrite:
            continue
    raise RuntimeError("the restaurant row kept changing; nothing written")


# ── the memory screen ─────────────────────────────────────────────────────

def memory_items(restaurant, today=None) -> list:
    """The Crew answers as items on "What the schedule has learned"
    (schedule_memory.memory_view): what was said, since when, until when it
    stands, and that it is held in code — answered in Crew, so no buttons
    here (LEARNING-17)."""
    when = _today(restaurant, today)
    items = []
    for key, a in sorted(answers_of(restaurant).items()):
        cell = _split(key)
        if not cell:
            continue
        role, day, part = cell
        due = _review_due(a, None, when)
        if a.get("answer") == "hold":
            n = a.get("floor_set") if a.get("floor_set") is not None else a.get("usual")
            text = f"Keep at least {_int(n)} {role} on at once every {day} {PART_WORD[part]}"
            status, label = "rule", "A minimum you kept"
        else:
            acc = (a.get("accepted") or {}).get("held")
            text = (f"{role} on {day} {PART_WORD[part]}: leaner is fine"
                    + (f" ({_int(acc)} on at once)" if acc is not None else ""))
            status, label = "active", "Leaner is fine"
        bits = [f"said {_mdy(a.get('at'))}"]
        rev = _review_date(a)
        if rev:
            bits.append(("review due " if due else "review ") + _mdy(rev))
        items.append({
            "key": f"crew:{key}", "kind": "crew_answer", "fact_class": "crew", "class_label": "Your Crew answers",
            "person": None, "role": role, "day": day, "daypart": part, "text": text, "value": dict(a),
            "status": status, "status_label": label, "enforcement": "rule" if status == "rule" else "requirement",
            "held_in_code": not due or a.get("answer") == "hold", "bound_by": "Answered in the Studio's Crew",
            "confidence": None, "confidence_pct": "—", "opportunities": 0, "hits": 0, "misses_by_hand": 0,
            "last_confirmed_by_hand": _mdy(a.get("at")), "first_seen": _mdy(a.get("at")), "last_seen": None,
            "source": "crew", "origin": "owner", "retired_reason": None, "retired_words": None,
            "owner_said": a.get("answer"), "rule": None, "answered": " · ".join(bits),
            "keep_label": "Keep", "can_keep": False, "can_let_go": False, "can_be_rule": False})
    return items


def api_body(restaurant_id, method, data=None, args=None, user=None, may_answer=False, db_path=DB_PATH) -> tuple:
    """The one body of GET/POST /api/labor/crew-matrix and its phone twin.
    POST {role, day, part, answer: hold | lean | clear | add, history_id,
    rows?}: the answer for the week on screen; the payload is that week's."""
    if method == "POST":
        if not may_answer:
            return {"ok": False, "error": "Only someone who builds the schedule can answer these."}, 403
        d = data or {}
        rows = d.get("rows") if isinstance(d.get("rows"), list) and len(d.get("rows")) <= 2000 else None
        return answer(restaurant_id, d.get("role"), d.get("day"), d.get("part"), d.get("answer"), user=user,
                      history_id=d.get("history_id"), rows=rows, db_path=db_path)
    hid = None
    try:
        hid = int((args or {}).get("history_id") or 0) or None
    except (TypeError, ValueError):
        hid = None
    return build(restaurant_id, history_id=hid, db_path=db_path), 200
