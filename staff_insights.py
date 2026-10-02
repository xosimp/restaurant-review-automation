"""staff_insights.py — what an employee can see about their OWN work.

Employee audit fixes B6 (10/1/26): hours and tips per shift from the POS
(H11), personal stats (V2), "a guest named you" (V5), the post-shift pulse
(V6) and a calendar feed of the published week (M11). The staff routes are
in staff_me_routes.py; the owner's pulse read is a strategy_routes body.

The one rule every function here keeps — the staff-safe data contract: a
staff payload carries only the caller's OWN figures. Never anyone else's pay,
tips, hours, ratings, attendance, notes or the reliability score — and not
the caller's reliability score either (it is a ranking input, not a record
they can act on). The caller is always the session's membership; nothing
here takes a name or an id from a request.

Honesty rules:
  * POS figures are "as the POS reports them", lag a business day, and say
    so (`as_of`, `lag_note`); with no POS the answer is empty with a reason,
    never zeros.
  * Attendance nobody watched is "not tracked", never "perfect".
  * Overtime is hours only, never cost.
  * The pulse is shown to the owner only in aggregate, and below
    PULSE_MIN_N answers not at all — nobody's answer is singled out.

Tables (boot, models.init_db → init_staff_insights): staff_shift_pulse (one
answer per membership per business date) and staff_calendar_links (a
per-membership feed link, stored as its SHA-256; the link itself is an HMAC
of the row's nonce under a kept secret, so the app can show it again).
"""
import base64
import hashlib
import hmac
import re
from datetime import date, datetime, timedelta

import models as _models_mod

PULSE_MIN_N = 3            # fewer answers than this and the owner sees a count only
PULSE_NOTE_MAX = 300
PULSE_DAYS_BACK = 3        # a pulse may be for a shift up to three business days back
EARNINGS_DEFAULT_DAYS = 14
EARNINGS_MAX_DAYS = 60
ATTENDANCE_DAYS = 30
RECOGNITION_DAYS = 365     # inside person_signals' retention (people.PERSON_MENTION_DAYS)
EXCERPT_MAX = 160
CAL_PAST_DAYS = 14
CAL_AHEAD_DAYS = 60
CAL_SECRET = "staff_calendar"
_CAL_HASH_PREFIX = "sha256:"

OUTCOME_LABELS = {
    "on_time": "On time", "late": "Late", "no_show": "No-show", "called_out": "Called out",
    "left_early": "Left early", "short": "Left early", "covered": "Covered", "worked": "Worked",
}
CERT_LABELS = {
    "alcohol": "Alcohol service", "food_handler": "Food handler", "manager": "Manager",
    "keyholder": "Keyholder", "trainer": "Trainer", "allergen": "Allergen awareness", "first_aid": "First aid",
}


def get_conn(db_path=None):
    """models.get_conn at call time (CLAUDE.md, bound imports)."""
    return _models_mod.get_conn(db_path) if db_path else _models_mod.get_conn()


def _db(db_path):
    return db_path or _models_mod.DB_PATH


def _nk(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def _iso(d) -> str:
    return d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]


def _parse_day(value):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(str(value).strip()[:10], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _mdy(value) -> str:
    from time_utils import mdy
    return mdy(value)


def _clock(dt) -> str:
    """6:45pm, or 6pm on the hour (DESIGN_SYSTEM.md → Dates and times)."""
    if dt is None:
        return ""
    h = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{h}{ampm}" if dt.minute == 0 else f"{h}:{dt.minute:02d}{ampm}"


def _hours(x) -> float:
    return round(float(x or 0), 2)


def init_staff_insights(db_path=None):
    """Boot DDL (models.init_db), never on a request path."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS staff_shift_pulse (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            membership_id   INTEGER NOT NULL,
            business_date   TEXT    NOT NULL,
            rating          INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
            note            TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, membership_id, business_date)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_pulse_day ON staff_shift_pulse(restaurant_id, business_date)")
        conn.execute("""CREATE TABLE IF NOT EXISTS staff_calendar_links (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            membership_id   INTEGER NOT NULL,
            nonce           TEXT    NOT NULL,
            key_version     INTEGER NOT NULL DEFAULT 1,
            token_hash      TEXT    NOT NULL UNIQUE,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            revoked_at      TEXT,
            last_fetched_at TEXT,
            fetch_count     INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_cal_member ON staff_calendar_links(restaurant_id, membership_id)")
        conn.commit()
    finally:
        conn.close()


# ── the restaurant's day ────────────────────────────────────────────────────

def local_now(restaurant_id, db_path=None):
    """The restaurant's wall clock, naive — the server's is UTC, and a 7pm
    Chicago shift is already "tomorrow" there."""
    from time_utils import restaurant_now
    r = _models_mod.get_restaurant(restaurant_id, _db(db_path))
    return restaurant_now(r, naive=True)


def business_today(restaurant_id, now_local=None, db_path=None) -> date:
    """The service day it is at the restaurant: before the business day's
    start hour it is still last night (time_utils.business_date)."""
    from time_utils import business_date
    r = _models_mod.get_restaurant(restaurant_id, _db(db_path))
    local = now_local or local_now(restaurant_id, db_path)
    try:
        return business_date(r, local) if r else local.date()
    except Exception:
        return local.date()


def payroll_week(restaurant_id, day, db_path=None):
    """(start, end) of the payroll week `day` falls in — the restaurant's own
    week start (restaurants.week_start_day, 0 = Monday)."""
    r = _models_mod.get_restaurant(restaurant_id, _db(db_path))
    wsd = int(getattr(r, "week_start_day", 0) or 0) if r else 0
    start = day - timedelta(days=(day.weekday() - wsd) % 7)
    return start, start + timedelta(days=6)


# ── the published weeks, as this person is on them ──────────────────────────

def published_shifts(restaurant_id, name, start, end, db_path=None) -> list:
    """This person's shifts on [start, end] from the copy of each week staff
    are on now — published and not superseded (the newest (re)publish
    first), or, for a week sent before the publish stamp, the newest shared
    copy (models._live_week_row). A draft, an auto-draft or a replaced copy
    never reaches staff. [{date, day, role, start, end, hours, notes}]."""
    from labor import employee_shifts_from_csv
    if not name:
        return []
    conn = get_conn(db_path)
    try:
        weeks = conn.execute("SELECT week_start, MAX(week_end) AS week_end, MAX(id) AS newest FROM schedule_history "
                             "WHERE restaurant_id=? GROUP BY week_start ORDER BY newest DESC LIMIT 120",
                             (restaurant_id,)).fetchall()
        live = []
        for w in weeks:
            ws, we = _parse_day(w["week_start"]), _parse_day(w["week_end"])
            if ws is None:
                continue
            if ws > end or (we is not None and we < start) or (we is None and ws + timedelta(days=6) < start):
                continue
            row = _models_mod._live_week_row(conn, restaurant_id, w["week_start"])
            if row:
                live.append(row)
    finally:
        conn.close()
    out, owned = [], set()
    # The newest live copy owns a date (overlapping weeks are rare: a
    # re-cut week); within one copy, every leg of a double is kept.
    for row in sorted(live, key=lambda r: (str(r["live_at"] or ""), r["id"]), reverse=True):
        days_here = set()
        for s in employee_shifts_from_csv(row["schedule_csv"] or "", name):
            d = _parse_day(s.get("date"))
            if d is None or d < start or d > end or d in owned:
                continue
            days_here.add(d)
            out.append(dict(s, date=d.isoformat()))
        ws, we = _parse_day(row["week_start"]), _parse_day(row["week_end"])
        cur = ws
        while cur and cur <= (we or ws + timedelta(days=6)):
            owned.add(cur)
            cur += timedelta(days=1)
        owned |= days_here
    out.sort(key=lambda s: (s["date"], s.get("start") or ""))
    return out


# ── H11: my hours and tips, per shift ───────────────────────────────────────

LAG_NOTE = ("Hours and tips come from the POS once each business day closes, so last night usually shows up "
            "the next morning.")
TIPS_NOTE = ("These are the figures the POS recorded on your punches. Tips shared another way — a pool, a "
             "tip-out or cash handed out — may not be in them, and payroll has the final word.")


def _provider(restaurant_id, db_path=None):
    """The connected POS that archives punches, else None (pos_archive)."""
    try:
        import pos_archive
        name, _mod = pos_archive.provider_for(restaurant_id)
        return name
    except Exception:
        return None


def _my_pos_ids(restaurant_id, name, provider, db_path=None) -> set:
    """This person's ids on the POS, from the identity layer's aliases
    (people: an id the POS sync or the owner tied to this person)."""
    try:
        import people
        ids = people.external_ids(restaurant_id, provider, db_path=_db(db_path))
        keys = {_nk(name)}
        try:
            keys.add(people.canonical_key(restaurant_id, name, db_path=_db(db_path)))
        except Exception:
            pass
        return {ids[k] for k in keys if ids.get(k)}, set(ids.values())
    except Exception:
        return set(), set()


def my_punch_rows(restaurant_id, name, start, end, provider=None, db_path=None):
    """(rows, reason): the caller's own punches on [start, end] — by their
    POS id when the identity layer knows it, else by exact name when every
    punch under that name carries one id nobody else holds. Two people with
    one name in the POS is never guessed ("ambiguous"). Station logins (a
    shared bar login) are never anyone's."""
    provider = provider or _provider(restaurant_id, db_path)
    conn = get_conn(db_path)
    try:
        if provider is None:
            row = conn.execute("SELECT provider FROM pos_punches WHERE restaurant_id=? ORDER BY business_date DESC "
                               "LIMIT 1", (restaurant_id,)).fetchone()
            if not row:
                return [], "pos_not_connected"
            provider = row["provider"]
        mine, everyones = _my_pos_ids(restaurant_id, name, provider, db_path)
        cols = ("business_date, employee_id, employee_name, role, clock_in, clock_out, reg_hours, ot_hours, "
                "dt_hours, tips, tips_net, grats")
        if mine:
            marks = ",".join("?" * len(mine))
            rows = conn.execute(f"SELECT {cols} FROM pos_punches WHERE restaurant_id=? AND provider=? AND "
                                f"is_station=0 AND employee_id IN ({marks}) AND business_date>=? AND business_date<=? "
                                "ORDER BY business_date DESC, clock_in DESC",
                                (restaurant_id, provider, *sorted(mine), _iso(start), _iso(end))).fetchall()
            return [dict(r) for r in rows], None
        key = _nk(name)
        named = [r for r in conn.execute("SELECT DISTINCT employee_id, employee_name FROM pos_punches WHERE "
                                         "restaurant_id=? AND provider=? AND is_station=0",
                                         (restaurant_id, provider)).fetchall() if _nk(r["employee_name"]) == key]
        ids = {r["employee_id"] for r in named}
        if not named:
            return [], "not_found"
        if len(ids) != 1 or (next(iter(ids)) in everyones):
            # Two POS people under this name, or the one id is somebody
            # else's in the identity layer.
            return [], "ambiguous"
        eid = next(iter(ids))
        q = (f"SELECT {cols} FROM pos_punches WHERE restaurant_id=? AND provider=? AND is_station=0 AND "
             "business_date>=? AND business_date<=? AND ")
        args = [restaurant_id, provider, _iso(start), _iso(end)]
        if eid is None:
            rows = [r for r in conn.execute(q + "employee_id IS NULL ORDER BY business_date DESC, clock_in DESC",
                                            args).fetchall() if _nk(r["employee_name"]) == key]
        else:
            rows = conn.execute(q + "employee_id=? ORDER BY business_date DESC, clock_in DESC",
                                args + [eid]).fetchall()
        return [dict(r) for r in rows], None
    except Exception:
        return [], "pos_not_connected"
    finally:
        conn.close()


def archived_through(restaurant_id, provider=None, db_path=None):
    """The newest business date the POS archive holds for this restaurant."""
    conn = get_conn(db_path)
    try:
        sql = "SELECT MAX(business_date) AS d FROM pos_archive_days WHERE restaurant_id=?"
        args = [restaurant_id]
        if provider:
            sql += " AND provider=?"
            args.append(provider)
        row = conn.execute(sql, args).fetchone()
        return row["d"] if row and row["d"] else None
    except Exception:
        return None
    finally:
        conn.close()


def _stamp(value):
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


_EARNINGS_REASONS = {
    "pos_not_connected": "Your restaurant's POS isn't connected to Cavnar AI, so there are no punches to show.",
    "not_found": "No punches under your name yet. If you clock in under a different name, ask your manager to "
                 "link it to you.",
    "ambiguous": "More than one person in the POS has your name, so Cavnar AI won't guess which punches are yours. "
                 "Ask your manager to link your POS id to you.",
}


def my_earnings(restaurant_id, name, days=EARNINGS_DEFAULT_DAYS, today=None, db_path=None) -> dict:
    """The caller's own punches over the last `days` business days, newest
    first: date, role, clock in and out, hours, and tips, tip net and grats
    as the POS reports them. Pay is deliberately not shown: the POS's rate is
    what its payroll engine was told, not necessarily what payroll pays."""
    try:
        days = max(1, min(int(days or EARNINGS_DEFAULT_DAYS), EARNINGS_MAX_DAYS))
    except (TypeError, ValueError):
        days = EARNINGS_DEFAULT_DAYS
    today = today or business_today(restaurant_id, db_path=db_path)
    start, end = today - timedelta(days=days - 1), today
    provider = _provider(restaurant_id, db_path)
    rows, reason = my_punch_rows(restaurant_id, name, start, end, provider=provider, db_path=db_path)
    as_of = archived_through(restaurant_id, provider, db_path) if reason != "pos_not_connected" else None
    shifts = []
    for r in rows:
        cin, cout = _stamp(r.get("clock_in")), _stamp(r.get("clock_out"))
        d = _parse_day(r["business_date"])
        shifts.append({
            "business_date": r["business_date"], "date_label": _mdy(r["business_date"]),
            "weekday": d.strftime("%A") if d else "",
            "role": r.get("role") or None,
            "clock_in": r.get("clock_in"), "clock_out": r.get("clock_out"),
            "clock_in_label": _clock(cin), "clock_out_label": _clock(cout),
            "still_open": cout is None,
            "hours": _hours((r.get("reg_hours") or 0) + (r.get("ot_hours") or 0) + (r.get("dt_hours") or 0)),
            "overtime_hours": _hours((r.get("ot_hours") or 0) + (r.get("dt_hours") or 0)),
            "tips_total": round(float(r.get("tips") or 0), 2),
            "tip_net": round(float(r.get("tips_net") or 0), 2),
            "grats": round(float(r.get("grats") or 0), 2),
        })
    totals = {"shifts": len(shifts), "hours": _hours(sum(s["hours"] for s in shifts)),
              "tips_total": round(sum(s["tips_total"] for s in shifts), 2),
              "tip_net": round(sum(s["tip_net"] for s in shifts), 2),
              "grats": round(sum(s["grats"] for s in shifts), 2)}
    return {"available": reason is None, "reason": reason,
            "message": _EARNINGS_REASONS.get(reason) if reason else (
                None if shifts else "No punches of yours in these days yet."),
            "provider": provider, "days": days,
            "window": {"start": _iso(start), "end": _iso(end), "label": f"{_mdy(start)} – {_mdy(end)}"},
            "as_of": as_of, "as_of_label": _mdy(as_of) if as_of else None,
            "lag_note": LAG_NOTE, "tips_note": TIPS_NOTE,
            "shifts": shifts, "totals": totals}


# ── V2: personal stats ──────────────────────────────────────────────────────

def _salaried(restaurant_id, name, db_path=None) -> bool:
    try:
        from models import salaried_staff, salaried_name_key
        r = _models_mod.get_restaurant(restaurant_id, _db(db_path))
        return salaried_name_key(name) in {salaried_name_key(s["name"]) for s in salaried_staff(r)}
    except Exception:
        return False


def _ot_line() -> float:
    from labor import OVERTIME_THRESHOLD_HOURS
    return float(OVERTIME_THRESHOLD_HOURS)


def _fmt_h(h) -> str:
    return f"{round(float(h), 1):g}"


def week_hours(restaurant_id, name, day, today=None, db_path=None) -> dict:
    """The payroll week `day` is in: hours scheduled on the published copy,
    hours the POS recorded (through the archive's last day), and the
    projection — worked hours through that day plus what is scheduled after
    it. Hours only, never cost."""
    today = today or business_today(restaurant_id, db_path=db_path)
    start, end = payroll_week(restaurant_id, day, db_path)
    sched = published_shifts(restaurant_id, name, start, end, db_path=db_path)
    scheduled = _hours(sum(float(s.get("hours") or 0) for s in sched))
    provider = _provider(restaurant_id, db_path)
    rows, reason = my_punch_rows(restaurant_id, name, start, end, provider=provider, db_path=db_path)
    as_of = archived_through(restaurant_id, provider, db_path) if reason != "pos_not_connected" else None
    actual = None
    projected = scheduled
    if reason is None:
        actual = _hours(sum((r.get("reg_hours") or 0) + (r.get("ot_hours") or 0) + (r.get("dt_hours") or 0)
                            for r in rows))
        cut = _parse_day(as_of) if as_of else None
        if cut is not None and cut >= start:
            projected = _hours(actual + sum(float(s.get("hours") or 0) for s in sched
                                            if _parse_day(s["date"]) and _parse_day(s["date"]) > cut))
    return {"start": _iso(start), "end": _iso(end), "label": f"{_mdy(start)} – {_mdy(end)}",
            "scheduled_hours": scheduled, "scheduled_shifts": len(sched),
            "actual_hours": actual, "actual_reason": reason, "as_of": as_of,
            "as_of_label": _mdy(as_of) if as_of else None, "projected_hours": projected}


def overtime_heads_up(restaurant_id, name, today=None, db_path=None, week=None) -> dict:
    """{"applies", "line_hours", "projected_hours", "headroom_hours", "over",
    "message"} for this payroll week. A salaried person owes no overtime, so
    it does not apply to them."""
    today = today or business_today(restaurant_id, db_path=db_path)
    line = _ot_line()
    if _salaried(restaurant_id, name, db_path):
        return {"applies": False, "line_hours": line, "projected_hours": None, "headroom_hours": None,
                "over": False, "message": None}
    week = week or week_hours(restaurant_id, name, today, today=today, db_path=db_path)
    projected = float(week["projected_hours"] or 0)
    headroom = round(line - projected, 1)
    if headroom < 0:
        msg = f"You're on track for {_fmt_h(projected)} hours this week — past {_fmt_h(line)}."
    elif headroom < 12:
        msg = f"A pickup longer than {_fmt_h(headroom)}h takes you past {_fmt_h(line)} hours this week."
    else:
        msg = None
    return {"applies": True, "line_hours": line, "projected_hours": _hours(projected),
            "headroom_hours": max(headroom, 0.0), "over": headroom < 0, "message": msg}


def pickup_overtime_note(restaurant_id, name, shift_date, shift_hours, today=None, db_path=None):
    """The heads-up for taking one more shift: "This 6h pickup takes you
    past 40 hours that week (to 43h)." — or None when it does not, or when
    overtime does not apply to them. For the open-shift claim screen."""
    d = _parse_day(shift_date)
    if d is None or _salaried(restaurant_id, name, db_path):
        return None
    week = week_hours(restaurant_id, name, d, today=today, db_path=db_path)
    line = _ot_line()
    after = float(week["projected_hours"] or 0) + float(shift_hours or 0)
    if after <= line:
        return None
    return (f"This {_fmt_h(shift_hours)}h pickup takes you past {_fmt_h(line)} hours that week "
            f"(to {_fmt_h(after)}h).")


def my_attendance(restaurant_id, name, today=None, days=ATTENDANCE_DAYS, db_path=None) -> dict:
    """The caller's own watched shifts over the last `days`: each outcome as
    the restaurant recorded it. A shift nobody watched is not in it, and a
    restaurant that watches nobody says "not tracked" — never a clean
    record. No notes, no who-covered, no score."""
    import attendance
    today = today or business_today(restaurant_id, db_path=db_path)
    since = today - timedelta(days=days)
    key = _nk(name)
    try:
        keys = {key}
        import people
        keys.add(people.canonical_key(restaurant_id, name, db_path=_db(db_path)))
    except Exception:
        pass
    try:
        evs = attendance.reliability_events(restaurant_id, since=since.isoformat(), db_path=_db(db_path))
    except Exception:
        evs = []
    tracked = bool(evs) or _watched_ever(restaurant_id, db_path)
    mine = [(d, o) for n, d, o in evs if _nk(n) in keys and str(d)[:10] <= today.isoformat()]
    late = {}
    try:
        for e in attendance.events(restaurant_id, since=since.isoformat(), db_path=_db(db_path)):
            if _nk(e["employee_name"]) in keys and e.get("minutes_late"):
                late[e["business_date"]] = int(e["minutes_late"])
    except Exception:
        pass
    counts = {}
    for _d, o in mine:
        counts[o] = counts.get(o, 0) + 1
    recent = [{"date": str(d)[:10], "date_label": _mdy(d), "outcome": o, "label": OUTCOME_LABELS.get(o, o),
               "minutes_late": late.get(str(d)[:10]) if o == "late" else None}
              for d, o in sorted(mine, key=lambda x: str(x[0]), reverse=True)]
    if not tracked:
        msg = "Attendance isn't tracked here yet, so there's nothing to show."
    elif not mine:
        msg = f"None of your shifts in the last {days} days were checked."
    else:
        msg = None
    return {"tracked": tracked, "days": days, "shifts_checked": len(mine), "counts": counts,
            "recent": recent[:20], "message": msg}


def _watched_ever(restaurant_id, db_path=None) -> bool:
    try:
        import attendance
        return attendance.watched(restaurant_id, days=90, db_path=_db(db_path))
    except Exception:
        return False


def my_certifications(restaurant_id, name, db_path=None) -> dict:
    """The certifications on the caller's own roster row. No expiry date is
    recorded anywhere yet, so none is shown — and the payload says so rather
    than implying they never lapse."""
    try:
        import staff_settings
        certs = staff_settings.for_name(restaurant_id, name, db_path=_db(db_path)).get("certifications") or []
    except Exception:
        certs = []
    items = [{"key": c, "label": CERT_LABELS.get(c, c.replace("_", " ").capitalize()), "expires_on": None}
             for c in certs]
    return {"items": items, "expiry_tracked": False,
            "note": ("Expiry dates aren't recorded yet — check with your manager before one runs out."
                     if items else None)}


def my_stats(restaurant_id, name, today=None, db_path=None) -> dict:
    today = today or business_today(restaurant_id, db_path=db_path)
    week = week_hours(restaurant_id, name, today, today=today, db_path=db_path)
    actual_note = None
    if week["actual_reason"]:
        actual_note = _EARNINGS_REASONS.get(week["actual_reason"])
    elif week["as_of"]:
        actual_note = f"Worked hours through {week['as_of_label']}. " + LAG_NOTE
    return {
        "today": today.isoformat(),
        "week": {"start": week["start"], "end": week["end"], "label": week["label"]},
        "scheduled": {"hours": week["scheduled_hours"], "shifts": week["scheduled_shifts"]},
        "actual": {"available": week["actual_reason"] is None, "hours": week["actual_hours"],
                   "as_of": week["as_of"], "as_of_label": week["as_of_label"], "note": actual_note},
        "overtime": overtime_heads_up(restaurant_id, name, today=today, db_path=db_path, week=week),
        "attendance": my_attendance(restaurant_id, name, today=today, db_path=db_path),
        "certifications": my_certifications(restaurant_id, name, db_path=db_path),
    }


# ── V5: a guest named you ───────────────────────────────────────────────────

_SCRUB = (re.compile(r"\S+@\S+\.\S+"), re.compile(r"https?://\S+|www\.\S+", re.I),
          re.compile(r"\+?\d[\d\s().-]{7,}\d"), re.compile(r"@\w+"))


def _first(name) -> str:
    toks = [t for t in re.split(r"[^A-Za-z'\-]+", str(name or "")) if t]
    return toks[0].lower() if toks else ""


def safe_excerpt(text, name, colleagues=()):
    """The sentence of a review that names this person, and nothing that
    identifies the guest: no reviewer name or platform (never read), and
    emails, links, phone numbers and @handles cut. A sentence that also
    names a colleague is not shown at all — what a guest said about someone
    else is theirs, not this person's. At most EXCERPT_MAX characters."""
    first = _first(name)
    if not text or len(first) < 2:
        return None
    pat = re.compile(rf"\b{re.escape(first)}\b", re.I)
    sentences = re.split(r"(?<=[.!?])\s+|\n+", str(text))
    hit = next((s for s in sentences if pat.search(s)), None)
    if not hit:
        return None
    others = {c for c in (_first(n) for n in colleagues) if len(c) >= 3 and c != first}
    if any(re.search(rf"\b{re.escape(o)}\b", hit, re.I) for o in others):
        return None
    for rx in _SCRUB:
        hit = rx.sub("", hit)
    hit = " ".join(hit.split()).strip(" ,;:-")
    if len(hit) > EXCERPT_MAX:
        hit = hit[:EXCERPT_MAX].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return hit or None


def _colleagues(restaurant_id, name, db_path=None) -> list:
    """Everyone else this restaurant knows by name: the roster, the roster
    settings, staff logins and the identity layer's people."""
    import staff_settings
    key, names = _nk(name), set()
    try:
        names |= {e["name"] for e in staff_settings.roster(restaurant_id, db_path=_db(db_path), include_inactive=True)}
    except Exception:
        pass
    try:
        names |= set(staff_settings.get_all(restaurant_id, db_path=_db(db_path)))
    except Exception:
        pass
    conn = get_conn(db_path)
    try:
        for sql in ("SELECT employee_name AS n FROM memberships WHERE restaurant_id=? AND employee_name IS NOT NULL",
                    "SELECT display_name AS n FROM people WHERE restaurant_id=?"):
            try:
                names |= {r["n"] for r in conn.execute(sql, (restaurant_id,)).fetchall() if r["n"]}
            except Exception:
                pass
    finally:
        conn.close()
    return sorted(n for n in names if _nk(n) != key)


def _mention_rows(restaurant_id, name, db_path=None, since=None, signal_id=None):
    keys = {_nk(name)}
    try:
        import people
        keys.add(people.canonical_key(restaurant_id, name, db_path=_db(db_path)))
    except Exception:
        pass
    conn = get_conn(db_path)
    try:
        sql = ("SELECT s.id, s.employee_name, s.employee_key, s.signal_date, s.ref, s.detail, r.text AS review_text, "
               "r.rating AS review_rating, r.deleted_at AS review_deleted FROM person_signals s "
               "LEFT JOIN reviews r ON r.restaurant_id=s.restaurant_id AND s.ref = 'review:' || r.id "
               "WHERE s.restaurant_id=? AND s.kind='review_mention' AND s.status='confirmed' AND s.polarity=1")
        args = [restaurant_id]
        if signal_id is not None:
            sql += " AND s.id=?"
            args.append(int(signal_id))
        else:
            sql += f" AND s.employee_key IN ({','.join('?' * len(keys))}) AND s.signal_date>=?"
            args += sorted(keys) + [str(since or "0000-00-00")[:10]]
        sql += " ORDER BY s.signal_date DESC, s.id DESC LIMIT 50"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def _mention_item(row, name, colleagues):
    # A review rated under 4 is not "a guest praised you", whatever the
    # sentiment read said.
    if row.get("review_rating") is not None and int(row["review_rating"]) < 4:
        return None
    text = row.get("review_text") if not row.get("review_deleted") else None
    excerpt = safe_excerpt(text or row.get("detail") or "", name, colleagues)
    return {"id": row["id"], "date": str(row["signal_date"])[:10], "date_label": _mdy(row["signal_date"]),
            "excerpt": excerpt}


def my_recognition(restaurant_id, name, today=None, db_path=None) -> dict:
    """Guests who named the caller in a positive review, as the owner
    confirmed — newest first, the date and a short safe excerpt, never who
    the guest was. Private: there is no team view of this."""
    today = today or business_today(restaurant_id, db_path=db_path)
    since = today - timedelta(days=RECOGNITION_DAYS)
    colleagues = _colleagues(restaurant_id, name, db_path)
    items = [i for i in (_mention_item(r, name, colleagues)
                         for r in _mention_rows(restaurant_id, name, db_path=db_path, since=since)) if i]
    return {"items": items[:20], "count": len(items), "days": RECOGNITION_DAYS}


def tell_recognition(restaurant_id, signal_id, db_path=None):
    """When the owner confirms a guest's positive mention of someone, tell
    them (people.tell: the app, a text they agreed to, else email). Returns
    the channel, or None (not positive, not confirmed, or unreachable)."""
    rows = _mention_rows(restaurant_id, "", db_path=db_path, signal_id=signal_id)
    if not rows:
        return None
    row = rows[0]
    item = _mention_item(row, row["employee_name"], _colleagues(restaurant_id, row["employee_name"], db_path))
    if not item:
        return None
    lines = [f"A guest named you in a review on {item['date_label']}."]
    if item["excerpt"]:
        lines.append(f"“{item['excerpt']}”")
    import people
    return people.tell(restaurant_id, row["employee_name"], "A guest named you", lines,
                       email_type="staff_notice", db_path=_db(db_path))


# ── V6: the post-shift pulse ────────────────────────────────────────────────

class PulseError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _shift_started(shifts, now_local) -> bool:
    import schedule_rules
    starts = [schedule_rules.parse_minutes(s.get("start")) for s in shifts]
    starts = [m for m in starts if m is not None]
    if not starts:
        return True
    return now_local.hour * 60 + now_local.minute >= min(starts)


def record_pulse(restaurant_id, membership_id, name, business_date, rating, note=None,
                 now_local=None, db_path=None) -> dict:
    """One answer for one of the caller's own shifts: how was it (1–5), and
    an optional note. Only for a day they were on the published schedule,
    after the shift started, up to PULSE_DAYS_BACK days back, once."""
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        raise PulseError("rating is a whole number from 1 to 5")
    day = _parse_day(business_date) if business_date else None
    if day is None:
        raise PulseError("date is the shift's date, YYYY-MM-DD")
    note = " ".join(str(note or "").split()) or None
    if note and len(note) > PULSE_NOTE_MAX:
        raise PulseError(f"Keep the note under {PULSE_NOTE_MAX} characters.")
    now_local = now_local or local_now(restaurant_id, db_path)
    today = business_today(restaurant_id, now_local=now_local, db_path=db_path)
    if day > today:
        raise PulseError("That shift hasn't happened yet.", 409)
    if day < today - timedelta(days=PULSE_DAYS_BACK):
        raise PulseError("That shift was too long ago to rate.", 409)
    shifts = published_shifts(restaurant_id, name, day, day, db_path=db_path)
    if not shifts:
        raise PulseError("You weren't on the schedule that day.", 409)
    if day == today and day == now_local.date() and not _shift_started(shifts, now_local):
        raise PulseError("Tell us after your shift.", 409)
    conn = get_conn(db_path)
    try:
        try:
            conn.execute("INSERT INTO staff_shift_pulse (restaurant_id, membership_id, business_date, rating, note) "
                         "VALUES (?,?,?,?,?)", (restaurant_id, int(membership_id), day.isoformat(), rating, note))
            conn.commit()
        except Exception as e:
            if "UNIQUE" in str(e).upper():
                raise PulseError("You already answered for that shift.", 409)
            raise
    finally:
        conn.close()
    return {"date": day.isoformat(), "date_label": _mdy(day), "rating": rating, "note": note}


def my_pulse(restaurant_id, membership_id, name, now_local=None, db_path=None) -> dict:
    """The caller's own recent answers, and the one shift (if any) still
    waiting for one — the newest of their shifts in the last
    PULSE_DAYS_BACK days that has started and has no answer."""
    now_local = now_local or local_now(restaurant_id, db_path)
    today = business_today(restaurant_id, now_local=now_local, db_path=db_path)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT business_date, rating, note FROM staff_shift_pulse WHERE restaurant_id=? AND "
                            "membership_id=? ORDER BY business_date DESC LIMIT 14",
                            (restaurant_id, int(membership_id))).fetchall()
    finally:
        conn.close()
    answered = {r["business_date"] for r in rows}
    due = None
    shifts = published_shifts(restaurant_id, name, today - timedelta(days=PULSE_DAYS_BACK), today, db_path=db_path)
    for d in sorted({s["date"] for s in shifts}, reverse=True):
        if d in answered:
            continue
        day_shifts = [s for s in shifts if s["date"] == d]
        if d == today.isoformat() and today == now_local.date() and not _shift_started(day_shifts, now_local):
            continue
        due = {"date": d, "date_label": _mdy(d), "start": day_shifts[0].get("start"),
               "role": day_shifts[0].get("role") or None}
        break
    return {"due": due, "recent": [{"date": r["business_date"], "date_label": _mdy(r["business_date"]),
                                    "rating": r["rating"], "note": r["note"]} for r in rows]}


def pulse_summary(restaurant_id, days=14, today=None, db_path=None) -> dict:
    """The owner's read: how shifts felt, in aggregate only. Below
    PULSE_MIN_N answers in the window it is a count and nothing else; a day
    is listed only with PULSE_MIN_N answers of its own, and a note carries
    its date only when its day does. Never a name, a role or an id — the
    floor is what keeps one person's answer from being read off a quiet
    night."""
    try:
        days = max(1, min(int(days or 14), 90))
    except (TypeError, ValueError):
        days = 14
    today = today or business_today(restaurant_id, db_path=db_path)
    start = today - timedelta(days=days - 1)
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT business_date, rating, note FROM staff_shift_pulse WHERE restaurant_id=? AND business_date>=? "
            "AND business_date<=? ORDER BY business_date DESC", (restaurant_id, start.isoformat(),
                                                                 today.isoformat())).fetchall()]
    finally:
        conn.close()
    n = len(rows)
    base = {"days": days, "window": {"start": start.isoformat(), "end": today.isoformat(),
                                     "label": f"{_mdy(start)} – {_mdy(today)}"},
            "responses": n, "min_responses": PULSE_MIN_N}
    if n < PULSE_MIN_N:
        return dict(base, enough=False, average=None, distribution=None, by_day=[], notes=[],
                    message=(f"{n} answer{'' if n == 1 else 's'} so far. Shown once {PULSE_MIN_N} people have "
                             "answered, so nobody's answer is singled out."))
    by = {}
    for r in rows:
        by.setdefault(r["business_date"], []).append(r)
    by_day = [{"date": d, "date_label": _mdy(d), "responses": len(rs),
               "average": round(sum(x["rating"] for x in rs) / len(rs), 1)}
              for d, rs in sorted(by.items(), reverse=True) if len(rs) >= PULSE_MIN_N]
    shown_days = {b["date"] for b in by_day}
    notes = [{"text": r["note"], "date_label": _mdy(r["business_date"]) if r["business_date"] in shown_days else None}
             for r in rows if r.get("note")]
    dist = {str(k): sum(1 for r in rows if r["rating"] == k) for k in range(1, 6)}
    return dict(base, enough=True, average=round(sum(r["rating"] for r in rows) / n, 1), distribution=dist,
                by_day=by_day, other_days_responses=n - sum(b["responses"] for b in by_day), notes=notes[:30],
                message=None)


def pulse_for_closeout(restaurant_id, business_date, db_path=None):
    """Tonight's pulse as close-out context: {"responses", "average", "line"}
    once PULSE_MIN_N people answered for the night, else None."""
    day = _iso(business_date)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT rating FROM staff_shift_pulse WHERE restaurant_id=? AND business_date=?",
                            (restaurant_id, day)).fetchall()
    except Exception:
        return None
    finally:
        conn.close()
    if len(rows) < PULSE_MIN_N:
        return None
    avg = round(sum(r["rating"] for r in rows) / len(rows), 1)
    return {"responses": len(rows), "average": avg,
            "line": f"Staff rated tonight {avg:g} out of 5 ({len(rows)} answers)."}


# ── M11: the calendar feed ──────────────────────────────────────────────────

def _cal_hash(token) -> str:
    return _CAL_HASH_PREFIX + hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _cal_token(restaurant_id, membership_id, nonce, version, db_path=None):
    from models import kept_secret
    key = kept_secret(CAL_SECRET, version, db_path=_db(db_path))
    if not key:
        return None
    mac = hmac.new(key, f"cal:{int(restaurant_id)}:{int(membership_id)}:{nonce}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")[:32]


def _cal_urls(token):
    import config
    https = f"{config.base_url()}/staff/cal/{token}.ics"
    return https, re.sub(r"^https?://", "webcal://", https)


def calendar_link(restaurant_id, membership_id, rotate=False, db_path=None) -> dict:
    """The caller's calendar feed link, made on first ask. The same link
    every time — a calendar app subscribed to it keeps working — until
    `rotate` (or a revoke) retires it. Only its hash is stored."""
    import secrets as _secrets
    from models import kept_secret_version
    conn = get_conn(db_path)
    try:
        if rotate:
            conn.execute("UPDATE staff_calendar_links SET revoked_at=datetime('now') WHERE restaurant_id=? AND "
                         "membership_id=? AND revoked_at IS NULL", (restaurant_id, int(membership_id)))
            conn.commit()
        row = conn.execute("SELECT id, nonce, key_version, created_at, last_fetched_at FROM staff_calendar_links "
                           "WHERE restaurant_id=? AND membership_id=? AND revoked_at IS NULL ORDER BY id DESC LIMIT 1",
                           (restaurant_id, int(membership_id))).fetchone()
    finally:
        conn.close()
    if row:
        token = _cal_token(restaurant_id, membership_id, row["nonce"], row["key_version"], db_path)
        created, fetched = row["created_at"], row["last_fetched_at"]
    else:
        nonce = _secrets.token_urlsafe(12)
        version = kept_secret_version(CAL_SECRET, _db(db_path))
        token = _cal_token(restaurant_id, membership_id, nonce, version, db_path)
        conn = get_conn(db_path)
        try:
            conn.execute("INSERT INTO staff_calendar_links (restaurant_id, membership_id, nonce, key_version, "
                         "token_hash) VALUES (?,?,?,?,?)",
                         (restaurant_id, int(membership_id), nonce, version, _cal_hash(token)))
            conn.commit()
        finally:
            conn.close()
        created, fetched = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), None
    url, webcal = _cal_urls(token)
    return {"url": url, "webcal_url": webcal, "created_at": created, "last_fetched_at": fetched,
            "note": ("Your published shifts, in your phone's calendar. Changes show up within a few hours. "
                     "Anyone with this link can see your shifts — reset it if you shared it by mistake.")}


def revoke_calendar_links(restaurant_id, membership_id=None, db_path=None) -> int:
    """Retire a membership's feed links (or every one at the restaurant)."""
    conn = get_conn(db_path)
    try:
        sql = "UPDATE staff_calendar_links SET revoked_at=datetime('now') WHERE restaurant_id=? AND revoked_at IS NULL"
        args = [restaurant_id]
        if membership_id is not None:
            sql += " AND membership_id=?"
            args.append(int(membership_id))
        n = conn.execute(sql, args).rowcount or 0
        conn.commit()
        return n
    finally:
        conn.close()


def expire_links_for(restaurant_id, name, db_path=None) -> dict:
    """Every link that shows this person's shifts, ended: their /s/ schedule
    links and their calendar feeds. For any deactivation path (roster,
    Account → Staff deactivate or unlink). The feed and the /s/ page also
    check the person is still active on every open, so a path that forgets
    to call this still cuts them off."""
    schedule = _models_mod.expire_schedule_shares_for(restaurant_id, name, db_path=_db(db_path))
    key = _nk(name)
    conn = get_conn(db_path)
    try:
        mids = [r["id"] for r in conn.execute("SELECT id, employee_name FROM memberships WHERE restaurant_id=? "
                                              "AND employee_name IS NOT NULL", (restaurant_id,)).fetchall()
                if _nk(r["employee_name"]) == key]
    except Exception:
        mids = []
    finally:
        conn.close()
    cal = sum(revoke_calendar_links(restaurant_id, m, db_path=db_path) for m in mids)
    return {"schedule_links": schedule, "calendar_links": cal}


def _ics_escape(s) -> str:
    return (str(s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _ics_fold(line) -> str:
    """RFC 5545 §3.1: lines over 75 octets continue on the next with a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def build_ics(restaurant_id, membership_id, restaurant_name, tz, shifts, now_utc=None) -> str:
    """A VCALENDAR of `shifts`, each at the restaurant's local time written
    as UTC. The UID is stable per shift (date and start), so a moved shift
    replaces its old event and a dropped one disappears on the next fetch."""
    import schedule_rules
    from datetime import timezone
    now_utc = now_utc or datetime.utcnow()
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Cavnar AI//Staff schedule//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_escape(f'{restaurant_name} shifts' if restaurant_name else 'My shifts')}",
             "X-PUBLISHED-TTL:PT3H", "REFRESH-INTERVAL;VALUE=DURATION:PT3H"]
    stamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
    for s in shifts:
        d = _parse_day(s.get("date"))
        a, b = schedule_rules.parse_minutes(s.get("start")), schedule_rules.parse_minutes(s.get("end"))
        if d is None or a is None or b is None:
            continue
        start = datetime.combine(d, datetime.min.time()) + timedelta(minutes=a)
        end = datetime.combine(d, datetime.min.time()) + timedelta(minutes=b)
        if end <= start:
            end += timedelta(days=1)

        def _utc(x):
            return x.replace(tzinfo=tz).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        role = s.get("role") or "Shift"
        desc = [x for x in (s.get("notes") or "",
                            "From your published schedule. Swaps, time off and tasks are in the Cavnar AI app.") if x]
        lines += ["BEGIN:VEVENT",
                  f"UID:shift-{int(restaurant_id)}-{int(membership_id)}-{d.isoformat()}-{a}@cavnar.ai",
                  f"DTSTAMP:{stamp}", f"DTSTART:{_utc(start)}", f"DTEND:{_utc(end)}",
                  f"SUMMARY:{_ics_escape(f'{role} at {restaurant_name}' if restaurant_name else role)}",
                  f"DESCRIPTION:{_ics_escape(chr(10).join(desc))}", "TRANSP:OPAQUE", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(x) for x in lines) + "\r\n"


def calendar_feed(token, today=None, db_path=None):
    """(ics_text, 200) for a live link; (None, 404) for an unknown one;
    (None, 410) for a revoked link or one whose holder was deactivated — the
    link is retired on the spot. Only the holder's own published shifts,
    CAL_PAST_DAYS back to CAL_AHEAD_DAYS ahead."""
    from time_utils import restaurant_tz
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT id, restaurant_id, membership_id, revoked_at FROM staff_calendar_links "
                           "WHERE token_hash=?", (_cal_hash(token),)).fetchone()
        if not row:
            return None, 404
        if row["revoked_at"]:
            return None, 410
        m = conn.execute("SELECT m.employee_name, m.is_active, COALESCE(u.is_active, 1) AS user_active "
                         "FROM memberships m LEFT JOIN users u ON u.id=m.user_id WHERE m.id=? AND m.restaurant_id=?",
                         (row["membership_id"], row["restaurant_id"])).fetchone()
        name = (m["employee_name"] or "") if m else ""
        live = bool(m) and bool(m["is_active"]) and bool(m["user_active"]) and bool(name)
        if live and _models_mod._share_holder_inactive(conn, row["restaurant_id"], name):
            live = False
        if not live:
            conn.execute("UPDATE staff_calendar_links SET revoked_at=datetime('now') WHERE id=?", (row["id"],))
            conn.commit()
            return None, 410
        conn.execute("UPDATE staff_calendar_links SET last_fetched_at=datetime('now'), fetch_count=fetch_count+1 "
                     "WHERE id=?", (row["id"],))
        conn.commit()
    finally:
        conn.close()
    rid = row["restaurant_id"]
    r = _models_mod.get_restaurant(rid, _db(db_path))
    today = today or business_today(rid, db_path=db_path)
    shifts = published_shifts(rid, name, today - timedelta(days=CAL_PAST_DAYS),
                              today + timedelta(days=CAL_AHEAD_DAYS), db_path=db_path)
    place = (getattr(r, "location_name", None) or getattr(r, "name", None) or "") if r else ""
    return build_ics(rid, row["membership_id"], place, restaurant_tz(r), shifts), 200
