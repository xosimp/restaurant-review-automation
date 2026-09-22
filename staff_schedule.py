"""staff_schedule.py — one employee's own shifts, for the staff portal.

Reads from the most recently generated schedule in schedule_history, which is
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


def _newest_published(restaurant_id: int):
    """The id of the newest schedule that was actually sent: stamped
    published_at, or (for weeks published before the stamp existed) one
    with a share row."""
    from models import get_conn, _ensure_history_columns
    conn = get_conn()
    try:
        _ensure_history_columns(conn)
        row = conn.execute(
            "SELECT h.id FROM schedule_history h WHERE h.restaurant_id=? AND (h.published_at IS NOT NULL "
            "OR EXISTS (SELECT 1 FROM schedule_shares s WHERE s.schedule_id=h.id)) "
            "ORDER BY h.generated_at DESC, h.id DESC LIMIT 1", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    return row["id"] if row else None


def shifts_for_employee(restaurant_id: int, employee_name: str, today=None) -> dict:
    """{today, upcoming, week, week_start, published} for one employee.

    `week` is the seven days from today forward, each either carrying that
    person's shift or explicitly marked off — an employee needs to see the
    days they are NOT on as much as the days they are.
    """
    today = today or date.today()
    # Only a PUBLISHED week reaches staff. The newest row in history used to
    # be shown whatever its state, so a Thursday auto-draft, a regeneration
    # or a manager's half-finished edit appeared in the portal as "your
    # schedule" and changed under people who had already planned around it.
    published = _newest_published(restaurant_id)
    if not published:
        return {"today": None, "upcoming": [], "week": [], "week_start": today.isoformat(),
                "published": False}
    detail = get_schedule_history_detail(published, restaurant_id)
    if not detail:
        return {"today": None, "upcoming": [], "week": [], "week_start": today.isoformat(),
                "published": False}

    mine = employee_shifts_from_csv(detail.get("schedule_csv") or "", employee_name)

    by_day = {}
    for s in mine:
        d = _parse_day(s.get("date"))
        if d:
            by_day[d] = s

    week = []
    for offset in range(7):
        d = today + timedelta(days=offset)
        shift = by_day.get(d)
        week.append({
            "date": d.isoformat(),
            "weekday": d.strftime("%A"),
            "is_today": offset == 0,
            "shift": shift,
            "off": shift is None,
        })

    upcoming = [
        {**s, "date_iso": _parse_day(s.get("date")).isoformat()}
        for s in mine
        if _parse_day(s.get("date")) and _parse_day(s.get("date")) >= today
    ]
    upcoming.sort(key=lambda s: s["date_iso"])

    return {
        "today": by_day.get(today),
        "upcoming": upcoming,
        "week": week,
        "week_start": today.isoformat(),
        "published": True,
        "week_of": detail.get("week_start") or history[0].get("week_start"),
    }
