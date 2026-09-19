"""
goals.py — the owner's targets, measured.

Goals used to exist only as free text in ask_memory — "wants labour under
26% by October" — which the assistant could quote back but nothing could
measure. A goal here names a metric from metrics.py, a target and an optional
deadline, and its progress is read from the same definitions outcome tracking
uses, so "on track" means the same number everywhere.

Status is conservative on purpose. "Achieved" needs the target to be met over
a full trailing window, not on one good day, and "on track" is only claimed
when the number is moving the right way by more than its own noise band.
"""
from datetime import date

import metrics
from models import get_conn, DB_PATH


def set_goal(restaurant_id, metric, target, deadline=None, note=None, user_id=None,
             db_path=DB_PATH):
    if not metrics.known(metric):
        raise ValueError(f"unknown metric {metric}")
    try:
        target = float(target)
    except (TypeError, ValueError):
        raise ValueError("target must be a number")
    if deadline:
        date.fromisoformat(str(deadline)[:10])          # validates, raises on garbage
    base = metrics.trailing(restaurant_id, metric, db_path=db_path)
    conn = get_conn(db_path)
    try:
        # One active goal per metric: two live targets for labour % is two
        # answers to "am I on track", and they would disagree.
        conn.execute("UPDATE owner_goals SET status='replaced' WHERE restaurant_id=? AND metric=? "
                     "AND status='active'", (restaurant_id, metric))
        cur = conn.execute(
            "INSERT INTO owner_goals (restaurant_id, metric, target, deadline, baseline_value, "
            "baseline_detail, note, created_by) VALUES (?,?,?,?,?,?,?,?)",
            (restaurant_id, metric, target, str(deadline)[:10] if deadline else None,
             base["value"], base["detail"], (note or "")[:200] or None, user_id))
        conn.commit()
        gid = cur.lastrowid
    finally:
        conn.close()
    return next(g for g in progress(restaurant_id, db_path=db_path) if g["id"] == gid)


def end_goal(restaurant_id, goal_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE owner_goals SET status='abandoned' WHERE id=? AND restaurant_id=? "
                           "AND status='active'", (goal_id, restaurant_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _met(info, current, target):
    return current <= target if info["lower_is_better"] else current >= target


def progress(restaurant_id, db_path=DB_PATH, today=None):
    """Every active goal with where it stands now."""
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        # Achieved goals stay visible for a week: closing one is the win the
        # owner should hear about, and dropping it from every surface the
        # moment it closed meant nobody ever did.
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM owner_goals WHERE restaurant_id=? AND (status='active' OR "
            "(status='achieved' AND achieved_at >= datetime('now','-7 days'))) ORDER BY id",
            (restaurant_id,)).fetchall()]
    finally:
        conn.close()
    out = []
    for g in rows:
        info = metrics.describe(g["metric"])
        now = metrics.trailing(restaurant_id, g["metric"], end=today, db_path=db_path)
        current = now["value"]
        g.update({"label": info["label"], "unit": info["unit"],
                  "lower_is_better": info["lower_is_better"],
                  "current": current, "current_detail": now["detail"]})
        if g["status"] == "achieved":
            g["state"] = "met"
        elif current is None:
            g["state"] = "unknown"
        elif _met(info, current, g["target"]):
            g["state"] = "met"
        else:
            cmp = metrics.compare(g["metric"], g["baseline_value"], current)
            g["state"] = {"improved": "moving_right_way",
                          "worsened": "moving_wrong_way"}.get(cmp["verdict"], "flat")
        if g.get("deadline"):
            days_left = (date.fromisoformat(g["deadline"]) - today).days
            g["days_left"] = days_left
            if days_left < 0 and g["state"] != "met":
                g["state"] = "missed"
        g["gap"] = (round(current - g["target"], 2) if current is not None else None)
        out.append(g)
    return out


def mark_achieved(restaurant_id, db_path=DB_PATH, today=None):
    """Close goals that have been met over a full trailing window — one that
    began after the goal was set. Without that, a goal set while the number
    was already on target closed itself the next morning. Scheduler entry
    point; returns the goals it closed."""
    today = today or date.today()
    closed = []
    for g in progress(restaurant_id, db_path=db_path, today=today):
        if g["status"] != "active" or g["state"] != "met":
            continue
        window = metrics.describe(g["metric"])["default_window_days"]
        try:
            set_on = date.fromisoformat(str(g["created_at"])[:10])
        except ValueError:
            continue
        if (today - set_on).days < window:
            continue
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE owner_goals SET status='achieved', achieved_at=datetime('now') "
                         "WHERE id=? AND status='active'", (g["id"],))
            conn.commit()
        finally:
            conn.close()
        closed.append(g)
    return closed


def summarise(g) -> str:
    unit = g.get("unit") or ""
    fmt = (lambda v: f"${v:,.0f}") if unit == "$" else (lambda v: f"{v:g}{unit}")
    target = fmt(g["target"])
    if g["current"] is None:
        return f"{g['label']} → {target}: can't measure it right now ({g.get('current_detail')})."
    by = f" by {g['deadline']}" if g.get("deadline") else ""
    state = {"met": "met", "moving_right_way": "moving the right way",
             "moving_wrong_way": "moving the wrong way", "flat": "no clear movement yet",
             "missed": "deadline passed without reaching it"}.get(g["state"], g["state"])
    return f"{g['label']}: {fmt(g['current'])} against a target of {target}{by} — {state}."
