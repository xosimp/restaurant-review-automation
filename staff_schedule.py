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


def restaurant_today(restaurant_id: int, now_local=None):
    """The restaurant's own service date: the calendar day on its clock,
    except that a closer at 12:30am is still on last night
    (time_utils.business_date, the tasks tab's rule). The server's
    date.today() is UTC on Railway, so from 7pm Central the portal showed
    tomorrow as today and tonight's shift vanished (employee audit C5)."""
    try:
        from models import get_restaurant
        from time_utils import business_date, restaurant_now
        r = get_restaurant(restaurant_id)
        local = now_local or restaurant_now(r, naive=True)
        return business_date(r, local) if r else local.date()
    except Exception:
        from time_utils import restaurant_now_by_id
        return (now_local or restaurant_now_by_id(restaurant_id, naive=True)).date()


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


def _leg(s, live_requests, now):
    """One shift as the app needs it: its own times under both spellings
    (`start`/`end` and the `shift_start`/`shift_end` a drop or swap sends
    back), the live request on it if any, and what can still be done."""
    leg = dict(s)
    leg["shift_start"], leg["shift_end"] = s.get("start"), s.get("end")
    d = _parse_day(s.get("date"))
    req = live_requests.get((d.isoformat() if d else s.get("date"), s.get("start")))
    leg["request"] = dict(req) if req else None
    actions = []
    if req is None and d is not None:
        try:
            import shift_requests as _sr
            _sr._gate(d.isoformat(), s.get("start"), s.get("end"), now)
            actions = ["drop", "swap"]
        except Exception:
            actions = []
    leg["actions"] = actions
    return leg


def shifts_for_employee(restaurant_id: int, employee_name: str, today=None, now=None) -> dict:
    """{today, upcoming, week, week_start, published, week_hours} for one
    employee.

    `week` is the seven days from today forward, each either carrying that
    person's shifts or explicitly marked off — an employee needs to see the
    days they are NOT on as much as the days they are. `posted` says whether
    a published week covers the day at all: a day nobody has published is
    "not posted yet", not "off" (WF-19; `off` keeps its old meaning for
    older apps). A double is two shifts on one day: `shift` is the first leg
    (older clients read it), `shifts` carries every leg, and `today` lists
    them all under `legs`. Every leg carries its own `shift_start` /
    `shift_end`, the live `request` on it ({id, kind, status} or null) and
    its `actions` (["drop", "swap"] while it can still change hands) — C6.

    `today` is the restaurant's service date, never the server's (C5);
    `now` its local wall clock (the actions' start-time gate).
    """
    if now is None:
        try:
            from time_utils import restaurant_now_by_id
            now = restaurant_now_by_id(restaurant_id, naive=True)
        except Exception:
            now = datetime.utcnow()
    today = today or restaurant_today(restaurant_id, now)
    # Only a PUBLISHED week reaches staff. The newest row in history used to
    # be shown whatever its state, so a Thursday auto-draft, a regeneration
    # or a manager's half-finished edit appeared in the portal as "your
    # schedule" and changed under people who had already planned around it.
    weeks = _published_weeks(restaurant_id, today)
    if not weeks:
        return {"today": None, "upcoming": [], "week": [], "week_start": today.isoformat(),
                "published": False, "week_hours": 0, "_week_ids": []}

    meal_after = None
    try:
        import schedule_rules as _sr
        meal_after = _sr.compliance(restaurant_id).get("meal_break_after_hours")
    except Exception:
        meal_after = None

    # Each shift's dining-room section, where labor can say (the sections
    # fix, employee audit B8): passed only when that reader takes it.
    import inspect as _inspect
    _section_kw = ({"restaurant_id": restaurant_id}
                   if "restaurant_id" in _inspect.signature(employee_shifts_from_csv).parameters else {})
    mine, week_of = [], None
    for w in weeks:
        detail = get_schedule_history_detail(w["id"], restaurant_id)
        if not detail:
            continue
        # The cook's kitchen station on each shift, as the draft assigned
        # it (schedule_engine.station_report, kept in the week's review):
        # "Grill tonight" in the staff app (owner, 9/30/26).
        stations = {}
        for a in (((detail.get("review") or {}).get("stations") or {}).get("assigned") or []):
            if " ".join(str(a.get("employee") or "").lower().split()) == " ".join(str(employee_name or "").lower().split()):
                parts = [p for p in (a.get("morning"), a.get("night")) if p]
                if parts:
                    stations[(a.get("date"), a.get("shift_start"))] = " then ".join(dict.fromkeys(parts))
        for s in employee_shifts_from_csv(detail.get("schedule_csv") or "", employee_name,
                                          meal_break_after_hours=meal_after, **_section_kw):
            d = _parse_day(s.get("date"))
            # Each day comes from the newest published week that covers it.
            if d and _owner_of(weeks, d) == w["id"]:
                st = stations.get((d.isoformat(), s.get("start")))
                if st:
                    s["station"] = st
                mine.append(s)
        if week_of is None and w["start"] and w["start"] <= today and (w["end"] is None or today <= w["end"]):
            week_of = detail.get("week_start")
    mine.sort(key=lambda s: (_parse_day(s.get("date")), s.get("start") or ""))

    try:
        import shift_requests as _sr
        live_requests = _sr.live_request_by_shift(restaurant_id, employee_name)
    except Exception:
        live_requests = {}
    mine = [_leg(s, live_requests, now) for s in mine]

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
            "posted": _owner_of(weeks, d) is not None,
        })
    week_hours = round(sum(float(s.get("hours") or 0) for day in week for s in day["shifts"]), 2)

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
        # The scheduled total over the seven days of `week` (H5 / INV-07).
        "week_hours": week_hours,
        # Not for the response: the published weeks shown, so the route can
        # count them seen (COM-07). api_shifts pops it.
        "_week_ids": [w["id"] for w in weeks],
    }
