"""
crew_matrix.py — the Schedule Studio's Crew stage (owner, 10/10/26: "build
this matrix into the studio for every restaurant").

For every role, weekday and shift (morning / night on the scheduler's own
3pm split, schedule_rules.daypart_of): the restaurant's usual crew beside the
draft on screen, the minimum it set, and — where its POS sends checks — a
busy / slow read of that shift's sales per person against the role's own
norm. Where the draft runs leaner than the usual crew the owner answers once:

  * "Keep usual" — the usual becomes that shift's minimum for the role
    (restaurants.role_floors_json, the one floor every draft and the publish
    gate already keep), so no budget trim takes it off again;
  * "Leaner is fine" — the saving is accepted and the shift stops asking.

Either answer can be undone; undoing a "Keep usual" puts back whatever
minimum was there before. Answers live in restaurants.crew_answers_json,
keyed "role|Weekday|part".

The usual crew is labor.staffing_baseline — the ONE baseline the draft's
requirements and the score read — so the Studio never shows a second,
disagreeing count (station logins and the salaried are already out of it).
Deterministic; no model call.
"""
import csv
import io
import json
from datetime import date, datetime, timedelta

from models import DB_PATH

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
PARTS = ("morning", "night")
SALES_WEEKS = 8
DEMAND_BAND = 0.3          # busy / slow: sales per person 30% off the role's own median
ANSWERS = ("hold", "lean")


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


def _draft_counts(restaurant_id, history_id=None, db_path=DB_PATH):
    """({(role, day, part): heads}, week_start, history_id) for the week asked
    for, else the newest one built."""
    from models import get_conn
    import schedule_rules as sr
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
    counts = {}
    if not row:
        return counts, None, None
    for r in csv.DictReader(io.StringIO(row["schedule_csv"] or "")):
        role = (r.get("role") or "").strip()
        day = (r.get("day") or "").strip().capitalize()
        if not role or day not in DAYS or not (r.get("employee") or "").strip() or sr.is_training_role(role):
            continue
        part = sr.daypart_of(r.get("shift_start") or "")
        if part not in PARTS:
            continue
        counts[(role, day, part)] = counts.get((role, day, part), 0) + 1
    return counts, row["week_start"], row["id"]


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


def build(restaurant_id, history_id=None, db_path=DB_PATH, today=None, baseline=None) -> dict:
    """The Crew stage's payload: {ok, week_start, history_id, has_sales,
    differences, open, roles: [{role, usual_week, draft_week, cells: [{day,
    part, usual, draft, floor, status, demand, sales, trend, answer}]}]}.
    status: match / under (draft leaner) / over (draft heavier) / below_rule
    (under a minimum the owner set) / usual (no week built yet: nothing to
    compare, nothing to answer)."""
    from models import get_restaurant
    import schedule_rules as sr
    r = get_restaurant(restaurant_id, db_path=db_path)
    if baseline is None:
        from labor import staffing_baseline
        baseline = staffing_baseline(restaurant_id) or {}
    typical = baseline.get("typical_headcount") or {}
    trends = {(t.get("day"), t.get("part"), str(t.get("role") or "").lower()): t
              for t in (baseline.get("headcount_trends") or [])}
    floors = sr.role_floors(r) if r is not None else {}
    answers = answers_of(r)
    draft, week_start, hid = _draft_counts(restaurant_id, history_id, db_path=db_path)
    sales = _shift_sales(restaurant_id, today=today, db_path=db_path)

    usual = {}
    for (day, part), roles in typical.items():
        if day not in DAYS or part not in PARTS:
            continue
        for role, n in (roles or {}).items():
            if not sr.is_training_role(role) and int(n or 0) > 0:
                usual[(role, day, part)] = int(n)
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

    def _get(m, role, day, part):
        for (rr, d, p), v in m.items():
            if d == day and p == part and rr.strip().lower() == role.strip().lower():
                return v
        return 0

    roles_out, differences, open_n = [], 0, 0
    for role in names:
        per_head = []
        for day in DAYS:
            for part in PARTS:
                u = _get(usual, role, day, part)
                if u and sales.get((day, part)):
                    per_head.append(sales[(day, part)] / float(u))
        per_head.sort()
        norm = per_head[len(per_head) // 2] if per_head else None
        cells, uw, dw = [], 0, 0
        for day in DAYS:
            for part in PARTS:
                u, d = _get(usual, role, day, part), _get(draft, role, day, part)
                fl = sr.floor_for(floors, role, day, part) if floors else 0
                if not (u or d or fl):
                    continue
                uw += u
                dw += d
                # With no week built there is nothing to compare: the usual
                # crew alone, and nothing to answer.
                if hid is None:
                    status = "usual"
                else:
                    status = "below_rule" if d < fl else ("under" if d < u else ("over" if d > u else "match"))
                demand = None
                s = sales.get((day, part))
                if u and s and norm:
                    v = s / float(u)
                    demand = "busy" if v > (1 + DEMAND_BAND) * norm else ("slow" if v < (1 - DEMAND_BAND) * norm else None)
                t = trends.get((day, part, role.lower()))
                ans = answers.get(_key(role, day, part)) or next(
                    (a for k, a in answers.items() if k.lower() == _key(role, day, part).lower()), None)
                if status not in ("match", "usual"):
                    differences += 1
                    if status == "under" and not ans:
                        open_n += 1
                cells.append({"day": day, "part": part, "usual": u, "draft": d, "floor": fl, "status": status,
                              "demand": demand, "sales": s,
                              "trend": ({"was": t.get("was"), "since": t.get("since")} if t else None),
                              "answer": (ans or {}).get("answer")})
        if cells:
            roles_out.append({"role": role, "usual_week": uw, "draft_week": dw, "cells": cells})
    roles_out.sort(key=lambda x: (-x["usual_week"], x["role"].lower()))
    # The columns in the restaurant's own week order (Wednesday first at
    # Simple EJ's, restaurants.week_start_day).
    from schedule_engine import week_day_order, week_start_day_of
    return {"ok": True, "week_start": week_start, "history_id": hid, "has_sales": bool(sales),
            "has_usual": bool(usual), "differences": differences, "open": open_n, "roles": roles_out,
            "days": week_day_order(week_start_day_of(r))}


def _save_answers(restaurant_id, data, db_path=DB_PATH):
    from models import update_restaurant
    update_restaurant(restaurant_id, {"crew_answers_json": json.dumps(data) if data else None}, db_path=db_path)


def _set_floor(restaurant_id, role, day, part, value, db_path=DB_PATH):
    """That one shift's minimum for the role set to `value` (0 clears it),
    every other floor as it was."""
    import schedule_rules as sr
    from models import get_restaurant
    r = get_restaurant(restaurant_id, db_path=db_path)
    stored = sr.role_floors(r)
    name = _match(role, stored) or role
    spec = json.loads(json.dumps(stored.get(name) or {"morning": 0, "night": 0, "days": {}}))
    spec.setdefault("days", {})
    day_spec = dict(spec["days"].get(day) or {})
    if value:
        day_spec[part] = int(value)
    else:
        day_spec.pop(part, None)
    if day_spec:
        spec["days"][day] = day_spec
    else:
        spec["days"].pop(day, None)
    new = sr.merge_role_map(stored, {name: spec}, sr.clean_floor_spec)
    return sr.save_role_floors(restaurant_id, new, db_path=db_path)


def answer(restaurant_id, role, day, part, choice, user=None, db_path=DB_PATH) -> tuple:
    """Record the owner's answer for one shift: "hold" (the usual becomes its
    minimum), "lean" (the saving is accepted) or "clear" (undo; a held
    shift gets its earlier minimum back). ({ok, ...build}, status)."""
    import schedule_rules as sr
    from models import get_restaurant
    role = str(role or "").strip()[:60]
    day = str(day or "").strip().capitalize()
    part = str(part or "").strip().lower()
    if not role or day not in DAYS or part not in PARTS or choice not in ANSWERS + ("clear",):
        return {"ok": False, "error": "That shift couldn't be read. Reload and try again."}, 400
    r = get_restaurant(restaurant_id, db_path=db_path)
    data = answers_of(r)
    key = next((k for k in data if k.lower() == _key(role, day, part).lower()), _key(role, day, part))
    before = data.get(key)
    floors_before = sr.role_floors(r)
    # Undo a hold first: its minimum goes back to what it was.
    if before and before.get("answer") == "hold" and choice != "hold":
        _set_floor(restaurant_id, role, day, part, int(before.get("floor_before") or 0), db_path=db_path)
    if choice == "clear":
        data.pop(key, None)
    elif choice == "hold":
        cur = build(restaurant_id, db_path=db_path)
        cell = next((c for x in cur["roles"] if x["role"].lower() == role.lower() for c in x["cells"]
                     if c["day"] == day and c["part"] == part), None)
        usual = int((cell or {}).get("usual") or 0)
        if usual <= 0:
            return {"ok": False, "error": f"Cavnar AI has no usual crew for {role} on {day} to keep."}, 400
        prior = before.get("floor_before") if (before and before.get("answer") == "hold") else \
            sr.floor_for(floors_before, role, day, part)
        _set_floor(restaurant_id, role, day, part, max(usual, int(prior or 0)), db_path=db_path)
        data[key] = {"answer": "hold", "usual": usual, "floor_before": int(prior or 0),
                     "at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                     "by": (user or {}).get("username") if isinstance(user, dict) else None}
    else:
        data[key] = {"answer": "lean", "at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                     "by": (user or {}).get("username") if isinstance(user, dict) else None}
    _save_answers(restaurant_id, data, db_path=db_path)
    try:
        import change_log
        kw = {"user": user} if user else {}
        change_log.record(restaurant_id, "labor", "crew_answers_json", (before or {}).get("answer"),
                          None if choice == "clear" else choice,
                          subject=f"Crew: {role}, {day} {'lunch' if part == 'morning' else 'dinner'}", **kw)
    except Exception:
        pass
    out = build(restaurant_id, db_path=db_path)
    return out, 200


def api_body(restaurant_id, method, data=None, args=None, user=None, may_answer=False, db_path=DB_PATH) -> tuple:
    """The one body of GET/POST /api/labor/crew-matrix and its phone twin."""
    if method == "POST":
        if not may_answer:
            return {"ok": False, "error": "Only someone who builds the schedule can answer these."}, 403
        d = data or {}
        return answer(restaurant_id, d.get("role"), d.get("day"), d.get("part"), d.get("answer"), user=user,
                      db_path=db_path)
    hid = None
    try:
        hid = int((args or {}).get("history_id") or 0) or None
    except (TypeError, ValueError):
        hid = None
    return build(restaurant_id, history_id=hid, db_path=db_path), 200
