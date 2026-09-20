"""
intraday.py — the only part of the product that can see a day happening.

Everything else here reads yesterday: the POS syncs at 3am, labor settles
after the pay period, reviews arrive days late. So between opening and close
an owner got nothing from Cavnar that they couldn't get by looking around
the room — which is exactly the stretch of the day they are working.

Toast can be asked during service (businessDay net sales, and the labor
timeEntries feed). RPOWER cannot: the vendor confirmed month-at-a-time
extracts, so for those restaurants this module honestly reports that it
can't see today rather than guessing.

TWO THINGS, BOTH ACTIONABLE BEFORE THE NEXT SERVICE:

  pulse()          — how today is tracking against the same weekday at the
                     same hour, once there is enough history to say.
  coverage_gaps()  — who was scheduled and hasn't clocked in.

No hour-level history existed to compare against, so capture() builds one:
each hourly snapshot is this weekday's profile for later weeks. A comparison
is withheld until MIN_PROFILE_SAMPLES same-weekday, same-hour readings exist
— a "you're 30% down" built on one previous Tuesday is noise with a
percentage sign on it.
"""
import logging
from datetime import date, datetime, timedelta

from models import get_conn, DB_PATH

log = logging.getLogger(__name__)

# Same-weekday, same-hour readings needed before a comparison is offered.
MIN_PROFILE_SAMPLES = 3
# How far off a typical day has to be before it is worth interrupting for.
PULSE_BEHIND_PCT = 15
PULSE_AHEAD_PCT = 20
# How late a scheduled person has to be before it becomes the manager's problem.
COVERAGE_GRACE_MINUTES = 15


def _median(values):
    s = sorted(values)
    if not s:
        return None
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def capture(restaurant_id, now_local=None, db_path=DB_PATH, restaurant=None):
    """Store net sales so far today, under this local hour.

    Returns {"ok": False, "reason"} for a POS that cannot be read during
    service — a normal state, not an error.
    """
    import pos
    from models import get_restaurant
    from time_utils import restaurant_now
    restaurant = restaurant or get_restaurant(restaurant_id)
    local = now_local or restaurant_now(restaurant, naive=True)
    day = local.date()
    try:
        net, provider = pos.fetch_sales_today(restaurant_id, day)
    except pos.POSCapabilityError as e:
        return {"ok": False, "reason": str(e)}
    except Exception as e:
        log.warning("intraday capture failed rid=%s: %s", restaurant_id, e)
        return {"ok": False, "reason": "the POS didn't answer"}
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO pos_intraday (restaurant_id, business_date, captured_hour, weekday, "
            "net_sales, provider) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(restaurant_id, business_date, captured_hour) DO UPDATE SET "
            "net_sales=excluded.net_sales, created_at=datetime('now')",
            (restaurant_id, day.isoformat(), local.hour, local.strftime("%A"),
             float(net), provider))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "net_sales": float(net), "hour": local.hour, "provider": provider}


def pulse(restaurant_id, now_local=None, db_path=DB_PATH, restaurant=None):
    """How today is tracking at this hour against the same weekday's profile.

    {"available": False, "reason"} until the profile exists — this is the
    honest answer for the first few weeks, and for every POS that can't be
    read during service.
    """
    from models import get_restaurant
    from time_utils import restaurant_now
    restaurant = restaurant or get_restaurant(restaurant_id)
    local = now_local or restaurant_now(restaurant, naive=True)
    day, hour, weekday = local.date(), local.hour, local.strftime("%A")
    conn = get_conn(db_path)
    try:
        today_row = conn.execute(
            "SELECT net_sales, captured_hour FROM pos_intraday WHERE restaurant_id=? AND "
            "business_date=? AND captured_hour<=? ORDER BY captured_hour DESC LIMIT 1",
            (restaurant_id, day.isoformat(), hour)).fetchone()
        if not today_row:
            return {"available": False, "reason": "nothing captured from the POS today"}
        history = [r["net_sales"] for r in conn.execute(
            "SELECT net_sales FROM pos_intraday WHERE restaurant_id=? AND weekday=? "
            "AND captured_hour=? AND business_date<?",
            (restaurant_id, weekday, today_row["captured_hour"], day.isoformat())).fetchall()]
    finally:
        conn.close()
    if len(history) < MIN_PROFILE_SAMPLES:
        return {"available": False, "hour": today_row["captured_hour"],
                "net_sales": today_row["net_sales"], "samples": len(history),
                "reason": f"only {len(history)} past {weekday}s measured at this hour"}
    typical = _median(history)
    pct = round((today_row["net_sales"] / typical - 1) * 100, 1) if typical else None
    return {"available": True, "weekday": weekday, "hour": today_row["captured_hour"],
            "net_sales": round(today_row["net_sales"], 2), "typical": round(typical, 2),
            "samples": len(history), "pct": pct,
            "off": pct is not None and (pct <= -PULSE_BEHIND_PCT or pct >= PULSE_AHEAD_PCT),
            "direction": "behind" if (pct or 0) < 0 else "ahead"}


def _todays_scheduled(restaurant_id, day, db_path=DB_PATH):
    """[{employee, role, shift_start}] from the most recent generated
    schedule that actually covers today."""
    from models import get_schedule_history, get_schedule_history_detail
    # The MOST RECENT schedule is not necessarily today's: from Thursday the
    # newest one is next week's (the auto-draft), and reading only that left
    # coverage blind for the rest of the week. Walk back until one actually
    # covers today.
    rows = []
    for entry in (get_schedule_history(restaurant_id, db_path=db_path) or [])[:4]:
        detail = get_schedule_history_detail(entry["id"], restaurant_id, db_path=db_path)
        csv_text = (detail or {}).get("schedule_csv") or ""
        if day.isoformat() in csv_text:
            break
    else:
        return []
    for line in csv_text.split("\n")[1:]:
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 5 or not parts[2]:
            continue
        if parts[0][:10] != day.isoformat():
            continue
        rows.append({"employee": parts[2], "role": parts[3], "shift_start": parts[4]})
    return rows


def _parse_clock(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def coverage_gaps(restaurant_id, now_local=None, db_path=DB_PATH, restaurant=None,
                  grace_minutes=COVERAGE_GRACE_MINUTES):
    """Who was on the schedule for a shift that has already started and has
    not clocked in. {"available": False, "reason"} where the POS has no live
    clock-in feed, or where no schedule covers today."""
    import pos
    from models import get_restaurant
    from time_utils import restaurant_now
    restaurant = restaurant or get_restaurant(restaurant_id)
    local = now_local or restaurant_now(restaurant, naive=True)
    day = local.date()
    scheduled = _todays_scheduled(restaurant_id, day, db_path=db_path)
    if not scheduled:
        return {"available": False, "reason": "no published schedule covers today"}
    try:
        clocked, provider = pos.fetch_clock_ins_today(restaurant_id, day)
    except pos.POSCapabilityError as e:
        return {"available": False, "reason": str(e)}
    except Exception as e:
        log.warning("coverage check failed rid=%s: %s", restaurant_id, e)
        return {"available": False, "reason": "the POS didn't answer"}
    here = {str(c.get("employee", "")).strip().lower() for c in clocked}
    missing = []
    for s in scheduled:
        start = None
        for fmt in ("%I:%M%p", "%I:%M %p", "%H:%M"):
            try:
                start = datetime.strptime(s["shift_start"].strip().lower().replace(" ", ""),
                                          fmt.replace(" ", ""))
                break
            except ValueError:
                continue
        if start is None:
            continue
        due = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        if local < due + timedelta(minutes=grace_minutes):
            continue                      # not late yet
        if s["employee"].strip().lower() in here:
            continue                      # clocked in
        missing.append({**s, "minutes_late": int((local - due).total_seconds() // 60)})
    return {"available": True, "provider": provider, "missing": missing,
            "scheduled": len(scheduled), "clocked_in": len(here)}


def day_total(restaurant_id, day, db_path=DB_PATH):
    """The last net-sales reading captured for a business date, and the hour
    it was taken. That last reading IS the day's total — capture() writes a
    running figure, so the final one is the day."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT net_sales, captured_hour FROM pos_intraday WHERE restaurant_id=? AND "
            "business_date=? ORDER BY captured_hour DESC LIMIT 1",
            (restaurant_id, day.isoformat())).fetchone()
    finally:
        conn.close()
    return (row["net_sales"], row["captured_hour"]) if row else (None, None)


def closing_summary(restaurant_id, day=None, db_path=DB_PATH, restaurant=None):
    """How tonight went, against a typical same weekday.

    The morning brief tells an owner how YESTERDAY went. Nothing told them how
    TODAY went, while they still remember the room — which is the one moment
    the number means something specific rather than being a figure in a table.

    Same honesty rules as pulse(): a comparison is withheld until there are
    MIN_PROFILE_SAMPLES same-weekday closes to compare against, and a POS that
    cannot be read during service says so instead of guessing.
    """
    from models import get_restaurant
    from time_utils import restaurant_now
    restaurant = restaurant or get_restaurant(restaurant_id)
    day = day or restaurant_now(restaurant, naive=True).date()
    weekday = day.strftime("%A")
    net, hour = day_total(restaurant_id, day, db_path)
    if net is None:
        return {"available": False, "reason": "nothing captured from the POS today"}
    conn = get_conn(db_path)
    try:
        # One figure per past same-weekday: that day's own last reading.
        # The bare net_sales alongside MAX() is SQLite's documented
        # min/max-picks-the-row behaviour, not an accident.
        history = [r["net_sales"] for r in conn.execute(
            "SELECT MAX(captured_hour) AS h, net_sales FROM pos_intraday "
            "WHERE restaurant_id=? AND weekday=? AND business_date<? "
            "GROUP BY business_date ORDER BY business_date DESC LIMIT 8",
            (restaurant_id, weekday, day.isoformat())).fetchall()]
    finally:
        conn.close()
    out = {"available": False, "weekday": weekday, "net_sales": round(float(net), 2),
           "hour": hour, "samples": len(history)}
    if len(history) < MIN_PROFILE_SAMPLES:
        out["reason"] = f"only {len(history)} past {weekday}s to compare with"
        return out
    typical = _median(history)
    pct = round((net / typical - 1) * 100, 1) if typical else None
    out.update({"available": True, "typical": round(typical, 2), "pct": pct,
                "direction": "behind" if (pct or 0) < 0 else "ahead"})
    return out
