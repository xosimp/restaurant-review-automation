"""staff_schedule.py — one employee's own shifts, for the staff portal.

Reads from the published weeks in schedule_history, which is
the same source the existing /s/<token> share page uses. Deliberately NOT
labor.load_shifts_for_restaurant(): that falls back to bundled SAMPLE data
when a restaurant has no shifts CSV, and showing an employee invented shifts
as their own roster would be the worst possible version of this feature. No
published schedule means an empty week and a plain statement that nothing is
posted yet.
"""
from datetime import date, datetime, timedelta

from labor import employee_shifts_from_csv
from models import get_schedule_history, get_schedule_history_detail


def _parse_day(value):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(str(value).strip()[:10], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _published_weeks(restaurant_id: int, today):
    """Every published week still relevant from `today` on, newest first.

    A week counts as sent when it is stamped published_at or (for weeks
    published before the stamp existed) has a share row. Reading only the
    single newest one meant that the moment next week was published, the
    rest of this week vanished from the portal and everybody read "off"
    from Friday to Sunday."""
    from models import get_conn, _ensure_history_columns
    conn = get_conn()
    try:
        _ensure_history_columns(conn)
        rows = conn.execute(
            "SELECT h.id, h.week_start, h.week_end FROM schedule_history h WHERE h.restaurant_id=? "
            "AND (h.published_at IS NOT NULL AND h.superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=h.restaurant_id AND nw.week_start=h.week_start AND nw.published_at IS NOT NULL AND nw.id > h.id) OR EXISTS (SELECT 1 FROM schedule_shares s WHERE s.schedule_id=h.id)) "
            "ORDER BY h.generated_at DESC, h.id DESC LIMIT 60", (restaurant_id,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        ws, we = _parse_day(r["week_start"]), _parse_day(r["week_end"])
        if we and we < today:
            continue
        out.append({"id": r["id"], "start": ws, "end": we})
    return out


def _owner_of(weeks, d):
    """The newest published week that covers date d (a re-published week
    replaces the older copy of the same days)."""
    for w in weeks:
        if (w["start"] is None or w["start"] <= d) and (w["end"] is None or d <= w["end"]):
            return w["id"]
    return None


def shifts_for_employee(restaurant_id: int, employee_name: str, today=None) -> dict:
    """{today, upcoming, week, week_start, published} for one employee.

    `week` is the seven days from today forward, each either carrying that
    person's shifts or explicitly marked off — an employee needs to see the
    days they are NOT on as much as the days they are. A double is two
    shifts on one day: `shift` is the first leg (older clients read it),
    `shifts` carries every leg, and `today` lists them all under `legs`.
    """
    today = today or date.today()
    # Only a PUBLISHED week reaches staff. The newest row in history used to
    # be shown whatever its state, so a Thursday auto-draft, a regeneration
    # or a manager's half-finished edit appeared in the portal as "your
    # schedule" and changed under people who had already planned around it.
    weeks = _published_weeks(restaurant_id, today)
    if not weeks:
        return {"today": None, "upcoming": [], "week": [], "week_start": today.isoformat(),
                "published": False}

    meal_after = None
    try:
        import schedule_rules as _sr
        meal_after = _sr.compliance(restaurant_id).get("meal_break_after_hours")
    except Exception:
        meal_after = None

    mine, week_of = [], None
    for w in weeks:
        detail = get_schedule_history_detail(w["id"], restaurant_id)
        if not detail:
            continue
        for s in employee_shifts_from_csv(detail.get("schedule_csv") or "", employee_name,
                                          meal_break_after_hours=meal_after):
            d = _parse_day(s.get("date"))
            # Each day comes from the newest published week that covers it.
            if d and _owner_of(weeks, d) == w["id"]:
                mine.append(s)
        if week_of is None and w["start"] and w["start"] <= today and (w["end"] is None or today <= w["end"]):
            week_of = detail.get("week_start")
    mine.sort(key=lambda s: (_parse_day(s.get("date")), s.get("start") or ""))

    by_day = {}
    for s in mine:
        d = _parse_day(s.get("date"))
        if d:
            by_day.setdefault(d, []).append(s)

    def _with_legs(legs):
        if not legs:
            return None
        first = dict(legs[0])
        if len(legs) > 1:
            first["legs"] = legs
        return first

    week = []
    for offset in range(7):
        d = today + timedelta(days=offset)
        legs = by_day.get(d) or []
        week.append({
            "date": d.isoformat(),
            "weekday": d.strftime("%A"),
            "is_today": offset == 0,
            "shift": legs[0] if legs else None,
            "shifts": legs,
            "off": not legs,
        })

    upcoming = [
        {**s, "date_iso": _parse_day(s.get("date")).isoformat()}
        for s in mine
        if _parse_day(s.get("date")) and _parse_day(s.get("date")) >= today
    ]
    upcoming.sort(key=lambda s: (s["date_iso"], s.get("start") or ""))

    return {
        "today": _with_legs(by_day.get(today)),
        "upcoming": upcoming,
        "week": week,
        "week_start": today.isoformat(),
        "published": True,
        "week_of": week_of or (weeks[-1]["start"].isoformat() if weeks[-1]["start"] else None),
    }
