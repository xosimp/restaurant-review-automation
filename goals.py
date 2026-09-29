"""
goals.py — the owner's targets, measured.

Goals used to exist only as free text in ask_memory — "wants labor under
26% by October" — which the assistant could quote back but nothing could
measure. A goal here names a metric from metrics.py, a target and an optional
deadline, and its progress is read from the same definitions outcome tracking
uses, so "on track" means the same number everywhere.

Status is conservative on purpose. "Achieved" needs the target to be met over
a full trailing window, not on one good day, and "on track" is only claimed
when the number is moving the right way by more than its own noise band.
"Met" itself needs the reading past the target by the noise band
(target_band, CA1 O15); a reading just across the line is "at_target".
"""
from datetime import date

import metrics
from models import get_conn, DB_PATH


def _checked(metric, target, deadline):
    if not metrics.known(metric):
        raise ValueError(f"unknown metric {metric}")
    try:
        target = float(target)
    except (TypeError, ValueError):
        raise ValueError("target must be a number")
    if deadline:
        date.fromisoformat(str(deadline)[:10])          # validates, raises on garbage
    return metric, target


def _targets_changed(restaurant_id):
    """A goal is the target every module judges against (owner_memory.
    target_for, memory audit 9/29/26 owner_goals): drop its short cache and
    Ask's snapshot so the new target reads at once."""
    try:
        import owner_memory
        owner_memory.invalidate_targets(restaurant_id)
        owner_memory.invalidate(restaurant_id)
    except Exception:
        pass


def _goal_value(g):
    """What the change log keeps of a goal: its target and deadline."""
    if not g:
        return None
    return {"target": g["target"], "deadline": g["deadline"] or None}


def _active(conn, restaurant_id, metric):
    row = conn.execute("SELECT id, target, deadline FROM owner_goals WHERE restaurant_id=? AND metric=? "
                       "AND status='active' ORDER BY id DESC LIMIT 1", (restaurant_id, metric)).fetchone()
    return dict(row) if row else None


def _log_goal(restaurant_id, metric, before, after, user_id=None, authority=None, db_path=None):
    """One attributed change_log row for the goal on `metric` — set,
    replaced, confirmed or ended (memory audit 9/29/26 change_log, INT #24).
    A proposal changes no target and is not a change. Never raises."""
    try:
        import change_log
        source = {"principal": "owner", "delegate": "manager", "admin": "admin"}.get(authority)
        change_log.record(restaurant_id, "goal", metric, before, after, subject=metric, actor_user_id=user_id,
                          source=source, db_path=None if db_path == DB_PATH else db_path)
    except Exception as e:
        print(f"[goals] change not logged rid={restaurant_id} {metric}: {e}")


def set_goal(restaurant_id, metric, target, deadline=None, note=None, user_id=None,
             db_path=DB_PATH, authority=None, source=None):
    """Set the ACTIVE goal on a metric, replacing the one before it.

    `authority` is permissions.answer_authority of the login setting it: a
    goal is the target Labor, Food Cost, the DSR and the alerts judge the
    figure against, so only an account holder's (or an internal caller's,
    None) goal takes effect here — a teammate's or an admin's is stored as
    PROPOSED instead (propose_goal) and an account holder confirms it."""
    if authority in ("delegate", "admin"):
        return propose_goal(restaurant_id, metric, target, deadline=deadline, note=note, user_id=user_id,
                            db_path=db_path, authority=authority, source=source)
    metric, target = _checked(metric, target, deadline)
    base = metrics.trailing(restaurant_id, metric, db_path=db_path)
    conn = get_conn(db_path)
    try:
        before = _active(conn, restaurant_id, metric)
        # One active goal per metric: two live targets for labor % is two
        # answers to "am I on track", and they would disagree.
        conn.execute("UPDATE owner_goals SET status='replaced' WHERE restaurant_id=? AND metric=? "
                     "AND status='active'", (restaurant_id, metric))
        cur = conn.execute(
            "INSERT INTO owner_goals (restaurant_id, metric, target, deadline, baseline_value, "
            "baseline_detail, note, created_by, authority, source, confirmed_by, confirmed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (restaurant_id, metric, target, str(deadline)[:10] if deadline else None,
             base["value"], base["detail"], (note or "")[:200] or None, user_id,
             authority or ("system" if user_id is None else None), (source or "")[:20] or None, user_id))
        conn.commit()
        gid = cur.lastrowid
    finally:
        conn.close()
    _log_goal(restaurant_id, metric, _goal_value(before),
              {"target": target, "deadline": str(deadline)[:10] if deadline else None},
              user_id=user_id, authority=authority, db_path=db_path)
    _targets_changed(restaurant_id)
    return next(g for g in progress(restaurant_id, db_path=db_path) if g["id"] == gid)


def propose_goal(restaurant_id, metric, target, deadline=None, note=None, user_id=None, db_path=DB_PATH,
                 authority="delegate", source=None):
    """A goal that waits for an account holder: stored 'proposed', read by
    nothing that judges a figure until confirm_goal makes it active. A new
    proposal on the same metric replaces the one still waiting. Returns the
    proposal (with "proposed": True and its summary)."""
    metric, target = _checked(metric, target, deadline)
    base = metrics.trailing(restaurant_id, metric, db_path=db_path)
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE owner_goals SET status='replaced' WHERE restaurant_id=? AND metric=? "
                     "AND status='proposed'", (restaurant_id, metric))
        cur = conn.execute(
            "INSERT INTO owner_goals (restaurant_id, metric, target, deadline, baseline_value, baseline_detail, "
            "note, created_by, status, authority, source) VALUES (?,?,?,?,?,?,?,?,'proposed',?,?)",
            (restaurant_id, metric, target, str(deadline)[:10] if deadline else None, base["value"], base["detail"],
             (note or "")[:200] or None, user_id, authority, (source or "")[:20] or None))
        conn.commit()
        gid = cur.lastrowid
    finally:
        conn.close()
    _targets_changed(restaurant_id)
    g = next((p for p in proposed(restaurant_id, db_path=db_path) if p["id"] == gid), {"id": gid})
    return dict(g, proposed=True, summary=describe_target(g) if g.get("metric") else None)


def proposed(restaurant_id, db_path=DB_PATH) -> list:
    """Goals waiting for an account holder to confirm, newest first, each
    with its label and unit."""
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM owner_goals WHERE restaurant_id=? AND status='proposed' ORDER BY id DESC",
            (restaurant_id,)).fetchall()]
    except Exception:
        rows = []
    finally:
        conn.close()
    for g in rows:
        try:
            info = metrics.describe(g["metric"])
            g.update({"label": info["label"], "unit": info["unit"], "lower_is_better": info["lower_is_better"]})
        except Exception:
            g.update({"label": g["metric"], "unit": ""})
        g["summary"] = describe_target(g)
    return rows


def confirm_goal(restaurant_id, goal_id, user_id=None, db_path=DB_PATH):
    """An account holder confirms a proposed goal: it becomes the active one
    on its metric (replacing the one before) and the target from now on.
    Returns the goal, or None when there is no such proposal here."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT metric, target, deadline FROM owner_goals WHERE id=? AND restaurant_id=? "
                           "AND status='proposed'", (goal_id, restaurant_id)).fetchone()
        if not row:
            return None
        before = _active(conn, restaurant_id, row["metric"])
        conn.execute("UPDATE owner_goals SET status='replaced' WHERE restaurant_id=? AND metric=? "
                     "AND status='active'", (restaurant_id, row["metric"]))
        conn.execute("UPDATE owner_goals SET status='active', confirmed_by=?, confirmed_at=datetime('now') "
                     "WHERE id=? AND restaurant_id=?", (user_id, goal_id, restaurant_id))
        conn.commit()
    finally:
        conn.close()
    # The target changed when the account holder confirmed it.
    _log_goal(restaurant_id, row["metric"], _goal_value(before), _goal_value(dict(row)), user_id=user_id,
              authority="principal", db_path=db_path)
    _targets_changed(restaurant_id)
    return next((g for g in progress(restaurant_id, db_path=db_path) if g["id"] == goal_id), None)


def decline_goal(restaurant_id, goal_id, user_id=None, db_path=DB_PATH) -> bool:
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE owner_goals SET status='declined', confirmed_by=?, confirmed_at=datetime('now') "
                           "WHERE id=? AND restaurant_id=? AND status='proposed'", (user_id, goal_id, restaurant_id))
        conn.commit()
        ok = cur.rowcount > 0
    finally:
        conn.close()
    _targets_changed(restaurant_id)
    return ok


def describe_target(g) -> str:
    """"Labor % 25% by 12/31/26" — a goal's target in a line, M/D/YY."""
    from time_utils import mdy
    unit = g.get("unit")
    if unit is None:
        try:
            unit = metrics.describe(g["metric"])["unit"]
        except Exception:
            unit = ""
    label = g.get("label") or g.get("metric")
    try:
        t = float(g["target"])
        shown = f"${t:,.0f}" if unit == "$" else f"{t:g}{unit}"
    except (TypeError, ValueError, KeyError):
        shown = str(g.get("target"))
    by = f" by {mdy(g['deadline'])}" if g.get("deadline") else ""
    return f"{label} {shown}{by}"


def end_goal(restaurant_id, goal_id, db_path=DB_PATH, user_id=None, authority=None):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT metric, target, deadline FROM owner_goals WHERE id=? AND restaurant_id=? "
                           "AND status='active'", (goal_id, restaurant_id)).fetchone()
        cur = conn.execute("UPDATE owner_goals SET status='abandoned' WHERE id=? AND restaurant_id=? "
                           "AND status='active'", (goal_id, restaurant_id))
        conn.commit()
        ok = cur.rowcount > 0
    finally:
        conn.close()
    if ok and row:
        _log_goal(restaurant_id, row["metric"], _goal_value(dict(row)), None, user_id=user_id, authority=authority,
                  db_path=db_path)
    _targets_changed(restaurant_id)
    return ok


def _met(info, current, target):
    return current <= target if info["lower_is_better"] else current >= target


def target_band(restaurant_id, metric, target, today=None, db_path=DB_PATH) -> dict:
    """How far past its target a reading must be to count as MET, not a
    wobble across the line (CA1 O15): the larger of the metric's stated
    band at the target and BAND_K times the spread of this restaurant's own
    windows of that length (metrics.noise_band's sigma — one reading against
    a fixed number, so no second reading's noise is added). With the stated
    band only, `false_alarm_rate` is None."""
    nb = metrics.noise_band(restaurant_id, metric, end=today, db_path=db_path)
    stated = metrics.fixed_band(metric, target)
    own = metrics.BAND_K * nb["sigma"] if nb.get("sigma") else 0.0
    band = max(stated, own)
    return {"band": round(band, 4), "sigma": nb.get("sigma"), "baseline_band": nb.get("band"),
            "false_alarm_rate": metrics.false_alarm_rate(band, nb.get("sigma")) if nb.get("sigma") else None,
            "basis": nb.get("basis")}


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
        tb = (target_band(restaurant_id, g["metric"], g["target"], today=today, db_path=db_path)
              if current is not None and g["status"] != "achieved" else {})
        g["band"], g["false_alarm_rate"] = tb.get("band"), tb.get("false_alarm_rate")
        if g["status"] == "achieved":
            g["state"] = "met"
        elif current is None:
            g["state"] = "unknown"
        elif _met(info, current, g["target"]):
            # Met only past the band (CA1 O15): a reading a hair over the
            # line is inside the number's own week-to-week movement.
            g["state"] = "met" if abs(current - g["target"]) >= (tb.get("band") or 0) else "at_target"
        else:
            cmp = metrics.compare(g["metric"], g["baseline_value"], current, band=tb.get("baseline_band"))
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
    if closed:
        _targets_changed(restaurant_id)
    return closed


def summarise(g) -> str:
    unit = g.get("unit") or ""
    fmt = (lambda v: f"${v:,.0f}") if unit == "$" else (lambda v: f"{v:g}{unit}")
    target = fmt(g["target"])
    if g["current"] is None:
        return f"{g['label']} → {target}: can't measure it right now ({g.get('current_detail')})."
    # M/D/YY, never the stored ISO deadline (re-audit A34).
    from time_utils import mdy
    by = f" by {mdy(g['deadline'])}" if g.get("deadline") else ""
    state = {"met": "met", "at_target": "at the target, but within its normal week-to-week movement — not yet a "
                                        "clear hit",
             "moving_right_way": "moving the right way",
             "moving_wrong_way": "moving the wrong way", "flat": "no clear movement yet",
             "missed": "deadline passed without reaching it"}.get(g["state"], g["state"])
    return f"{g['label']}: {fmt(g['current'])} against a target of {target}{by} — {state}."
