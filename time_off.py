"""Time-off requests.

The automation audit gave staff a way to enter the days they can't work in
general; the moat audit's #5 is the specific one — "I need the 12th to the
15th off" — asked for by the person who needs it, decided by a manager, and
then a hard constraint on the next schedule draft. No text, no sticky note,
no manager retyping it.

A request is a row. The managers who can decide it are told when it is
filed (shift_requests._tell_managers), and the person who asked is told the
decision on their own channel (people.tell) as well as in the portal.
Approved ranges are read by labor.generate_optimized_schedule for the week
being drafted.
"""
from datetime import date, timedelta

import models as _models_mod


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports): the
    bound import of get_conn and the database path pinned this module to the
    file of whichever test imported it first, so a later test's time off was
    read from another test's database (test_mem_m2_ask_reach after
    test_kitchen_stations)."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()


def _path(db_path):
    """The database a call means: the one named, else models' own path as it
    is now — what the helpers this module hands a path to expect."""
    return db_path if db_path is not None else _models_mod.DB_PATH

MAX_RANGE_DAYS = 31
MAX_AHEAD_DAYS = 180
STATUSES = ("pending", "approved", "denied", "withdrawn")
# Part of a day (schedule audit 10/3/26 D-39): "off until 4pm", "off from
# 6pm", a stretch in the middle, or lunch or dinner. Time off was whole days
# only, so "off until 4pm" ended up a free-text note nothing checked.
DAYPARTS = {"morning": "morning", "lunch": "morning", "day": "morning", "am": "morning",
            "night": "night", "dinner": "night", "evening": "night", "pm": "night"}
_SMALL_HOURS = 6 * 60          # an end before this is the next morning ("from 10pm until 2am")


def clean_part(start_time=None, end_time=None, daypart=None):
    """(part, error): {"start_time", "end_time", "daypart"} for part of a
    day off, {} for the whole day, or an error in words. Times are any
    shape schedule_rules.parse_minutes reads, stored as "4:00pm"."""
    from schedule_rules import parse_minutes, _fmt_minutes
    st, et = str(start_time or "").strip(), str(end_time or "").strip()
    dp = str(daypart or "").strip().lower()
    if dp and dp not in DAYPARTS:
        return None, "Lunch, dinner, or a time."
    if dp and (st or et):
        return None, "Give a time or lunch/dinner, not both."
    if dp:
        return {"start_time": None, "end_time": None, "daypart": DAYPARTS[dp]}, None
    lo = parse_minutes(st) if st else None
    hi = parse_minutes(et) if et else None
    if (st and lo is None) or (et and hi is None):
        return None, "That isn't a time — try 4:00pm."
    if lo is None and hi is None:
        return {}, None
    if lo is not None and hi is not None and hi <= lo and hi >= _SMALL_HOURS:
        return None, "The end is before the start."
    return {"start_time": _fmt_minutes(lo) if lo is not None else None,
            "end_time": _fmt_minutes(hi) if hi is not None else None, "daypart": None}, None


def part_words(row) -> str:
    """"until 4:00pm", "from 6:00pm", "11:00am–2:00pm", "lunch", "dinner",
    or "" for a whole day."""
    if not row:
        return ""
    dp = (row.get("daypart") or "") if hasattr(row, "get") else ""
    if dp:
        return {"morning": "lunch", "night": "dinner"}.get(dp, dp)
    st, et = row.get("start_time"), row.get("end_time")
    if st and et:
        return f"{st}–{et}"
    if et:
        return f"until {et}"
    if st:
        return f"from {st}"
    return ""


def span_label(row) -> str:
    """"10/7/26, until 4:00pm" — the dates, and the part of the day when it
    is only part (owner-facing, M/D/YY)."""
    from time_utils import mdy, mdy_range
    span = mdy(row["start_date"]) if row["start_date"] == row["end_date"] else mdy_range(row["start_date"], row["end_date"])
    part = part_words(row)
    return f"{span}, {part}" if part else span


def is_partial(row) -> bool:
    return bool(row and (row.get("start_time") or row.get("end_time") or row.get("daypart")))


def _as_date(s):
    return date.fromisoformat(str(s)[:10])


def _today(restaurant_id, today=None):
    """The restaurant's own date, never the server's: date.today() is UTC on
    Railway, so after 7pm Central "today" was already tomorrow (C5)."""
    if today:
        return today
    from time_utils import restaurant_now_by_id
    return restaurant_now_by_id(restaurant_id, naive=True).date()


def request_time_off(restaurant_id, employee_name, start, end, reason=None, db_path=None, today=None,
                     start_time=None, end_time=None, daypart=None):
    """The employee's own request. Returns (row, error). `start_time` /
    `end_time` / `daypart`: part of each day off ("until 4pm", "dinner") —
    none of them is the whole day (D-39)."""
    name = (employee_name or "").strip()
    if not name:
        return None, "No employee name on this session."
    part, perr = clean_part(start_time, end_time, daypart)
    if perr:
        return None, perr
    try:
        s, e = _as_date(start), _as_date(end or start)
    except (TypeError, ValueError):
        return None, "Pick a start and end date."
    today = _today(restaurant_id, today)
    if e < s:
        return None, "The end date is before the start."
    if s < today:
        return None, "That date has passed."
    if (e - s).days + 1 > MAX_RANGE_DAYS:
        return None, f"One request covers at most {MAX_RANGE_DAYS} days."
    if (s - today).days > MAX_AHEAD_DAYS:
        return None, "That is too far ahead to plan for yet."
    conn = get_conn(db_path)
    try:
        # The overlap check and the insert are one write transaction: a
        # double tap used to pass the check twice and file two requests
        # (MOD-EMP-9).
        conn.execute("BEGIN IMMEDIATE")
        dup = conn.execute(
            "SELECT id FROM staff_time_off WHERE restaurant_id=? AND LOWER(employee_name)=LOWER(?) "
            "AND status IN ('pending','approved') AND NOT (end_date < ? OR start_date > ?)",
            (restaurant_id, name, s.isoformat(), e.isoformat())).fetchone()
        if dup:
            conn.rollback()
            return None, "You already have a request over those dates."
        cur = conn.execute(
            "INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, reason, start_time, "
            "end_time, daypart) VALUES (?,?,?,?,?,?,?,?)",
            (restaurant_id, name, s.isoformat(), e.isoformat(), (reason or "").strip()[:200] or None,
             part.get("start_time"), part.get("end_time"), part.get("daypart")))
        conn.commit()
        row = conn.execute("SELECT * FROM staff_time_off WHERE id=?", (cur.lastrowid,)).fetchone()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    # The manager hears about it now, not when they next open Labor.
    try:
        import shift_requests as _sr
        from time_utils import mdy
        span = mdy(s.isoformat()) + ("" if e == s else f"–{mdy(e.isoformat())}")
        if part_words(part):
            span += f" ({part_words(part)})"
        why = _sr._clean(reason, 120)
        # The id rides along so the push offers Approve / Deny and opens
        # this request, and it reaches whoever can decide it (F2-6). The
        # reason rides too: it was stored and left out (COM-14).
        _sr._tell_managers(restaurant_id, "Time off request",
                           f"{name} asked for {span} off" + (f" — “{why}”" if why else "")
                           + ". Approve or decline it in Labor.", _path(db_path),
                           req={"id": row["id"], "request_kind": "time_off"})
    except Exception as ex:
        print(f"[time_off] manager notice failed rid={restaurant_id}: {ex!r}")
    return dict(row), None


def withdraw(restaurant_id, request_id, employee_name, db_path=None) -> bool:
    """The employee takes back their own request while it is still pending,
    so a mistyped range can be corrected — the overlap rule used to block
    the fix with no way out (MOD-EMP-7)."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE staff_time_off SET status='withdrawn' WHERE id=? AND restaurant_id=? "
                           "AND LOWER(employee_name)=LOWER(?) AND status='pending'",
                           (int(request_id), restaurant_id, (employee_name or "").strip()))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def cancel_approved(restaurant_id, request_id, employee_name, db_path=None, today=None):
    """The employee calls off approved time off they no longer need — before
    it starts. Direct, with a notice to the deciders rather than a second
    approval: giving days back only makes the person available again, it
    can't leave a shift uncovered, and "a changed mind is a new request" was
    refused by the overlap rule (WF-21). The row reads 'withdrawn' with a
    note, so the manager's list says what happened. Returns (row, error)."""
    today = _today(restaurant_id, today)
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM staff_time_off WHERE id=? AND restaurant_id=? AND LOWER(employee_name)=LOWER(?) "
                           "AND status='approved'", (int(request_id), restaurant_id, (employee_name or "").strip())).fetchone()
        if not row:
            return None, "That time off isn't yours, or isn't approved."
        if _as_date(row["start_date"]) <= today:
            return None, "That time off has already started — ask your manager to change it."
        cur = conn.execute("UPDATE staff_time_off SET status='withdrawn', decision_note=? WHERE id=? AND status='approved'",
                           ("Cancelled by the employee after approval.", row["id"]))
        conn.commit()
        if cur.rowcount != 1:
            return None, "That time off was already changed."
        row = dict(conn.execute("SELECT * FROM staff_time_off WHERE id=?", (row["id"],)).fetchone())
    finally:
        conn.close()
    try:
        import shift_requests as _sr
        from time_utils import mdy_range
        _sr._tell_managers(restaurant_id, "Time off cancelled",
                           f"{row['employee_name']} no longer needs {mdy_range(row['start_date'], row['end_date'])} off — "
                           "they're available again for the next draft.", _path(db_path))
    except Exception as ex:
        print(f"[time_off] cancel notice failed rid={restaurant_id}: {ex!r}")
    return row, None


def published_conflicts(restaurant_id, employee_name, start, end, db_path=None, part=None) -> list:
    """[{date, day, shift_start, shift_end, role}] — this person's shifts on
    the live published weeks inside [start, end]. Approving time off over a
    week staff already have used to leave them scheduled with nobody told
    (SCHED-22 / MOD-EMP-8). `part` (a row's start_time / end_time /
    daypart): only the shifts that reach into that part of the day."""
    from schedule_versions import rows_from_csv
    s, e = _as_date(start).isoformat(), _as_date(end).isoformat()
    key = " ".join((employee_name or "").split()).casefold()
    conn = get_conn(db_path)
    try:
        weeks = conn.execute(
            "SELECT schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL "
            "AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE "
            "nw.restaurant_id=schedule_history.restaurant_id AND nw.week_start=schedule_history.week_start "
            "AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id) "
            "AND substr(week_start,1,10) <= ? AND substr(week_end,1,10) >= ?", (restaurant_id, e, s)).fetchall()
    finally:
        conn.close()
    out = []
    for w in weeks:
        for r in rows_from_csv(w["schedule_csv"]):
            if " ".join(r["employee"].split()).casefold() == key and s <= r["date"] <= e:
                if is_partial(part) and not shift_hits_part(r, part):
                    continue
                out.append({"date": r["date"], "day": r.get("day"), "shift_start": r["shift_start"],
                            "shift_end": r["shift_end"], "role": r.get("role")})
    return sorted(out, key=lambda x: (x["date"], x["shift_start"]))


def part_minutes(part):
    """(from_min, until_min, daypart) for part of a day off: minutes past
    the date's midnight (an until in the small hours is the next morning);
    None for an open end. (None, None, None) for a whole day."""
    from schedule_rules import parse_minutes
    if not is_partial(part):
        return None, None, None
    if part.get("daypart"):
        return None, None, part["daypart"]
    lo = parse_minutes(part.get("start_time") or "")
    hi = parse_minutes(part.get("end_time") or "")
    if hi is not None and hi < _SMALL_HOURS and (lo is None or hi <= lo):
        hi += 24 * 60
    return lo, hi, None


def shift_hits_part(row, part) -> bool:
    """Whether a shift row reaches into part of a day off: overlaps the
    times, or reaches into the daypart by any minute of its service
    (shift_quality.touched_dayparts — an hour of presence let a shift run
    55 minutes into an approved dinner off, schedule re-audit 10/4/26
    RULES-11)."""
    from schedule_rules import parse_minutes
    lo, hi, dp = part_minutes(part)
    if dp:
        from shift_quality import touched_dayparts
        return dp in (touched_dayparts(row) or [])
    s, e = parse_minutes(row.get("shift_start") or ""), parse_minutes(row.get("shift_end") or "")
    if s is None or e is None:
        return True
    if e <= s:
        e += 24 * 60
    return (lo is None or e > lo) and (hi is None or s < hi)


def mine(restaurant_id, employee_name, db_path=None, today=None):
    """This employee's requests, upcoming first; decided ones from the last
    60 days stay visible so the answer is not lost. An approved one still
    ahead carries `still_scheduled` — the published shifts inside it that
    nobody has moved yet — and `can_cancel` (WF-21)."""
    today = _today(restaurant_id, today)
    floor = (today - timedelta(days=60)).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM staff_time_off WHERE restaurant_id=? AND LOWER(employee_name)=LOWER(?) "
            "AND end_date >= ? ORDER BY start_date", (restaurant_id, (employee_name or "").strip(), floor)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        r = dict(r)
        r.pop("decided_by", None)       # a login id: nothing an employee needs (SEC-04)
        ahead = _as_date(r["end_date"]) >= today
        r["can_cancel"] = r["status"] == "approved" and _as_date(r["start_date"]) > today
        r["still_scheduled"] = []
        if r["status"] == "approved" and ahead:
            try:
                r["still_scheduled"] = published_conflicts(restaurant_id, r["employee_name"],
                                                           max(_as_date(r["start_date"]), today).isoformat(),
                                                           r["end_date"], db_path=db_path, part=r)
            except Exception:
                r["still_scheduled"] = []
        r["span_label"] = span_label(r)
        out.append(r)
    return out


def pending(restaurant_id, db_path=None):
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM staff_time_off WHERE restaurant_id=? AND status='pending' "
                            "ORDER BY start_date", (restaurant_id,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def recent(restaurant_id, limit=30, db_path=None, today=None):
    """Pending first, then everything decided that is still ahead or ended
    in the last 30 days — the manager's whole picture on one screen."""
    today = _today(restaurant_id, today)
    floor = (today - timedelta(days=30)).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM staff_time_off WHERE restaurant_id=? AND (status='pending' OR end_date >= ?) "
            "ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, start_date LIMIT ?",
            (restaurant_id, floor, int(limit))).fetchall()
    finally:
        conn.close()
    # "10/7/26, until 4:00pm" — what the manager is deciding (D-39).
    return [dict(dict(r), span_label=span_label(dict(r))) for r in rows]


def decide(restaurant_id, request_id, approve, decided_by=None, note=None, db_path=None):
    """A manager's answer. Only a pending request can be decided; the
    decision is final (a changed mind is a new request)."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE staff_time_off SET status=?, decided_by=?, decided_at=datetime('now'), decision_note=? "
            "WHERE id=? AND restaurant_id=? AND status='pending'",
            ("approved" if approve else "denied", decided_by, (note or "").strip()[:200] or None,
             int(request_id), restaurant_id))
        conn.commit()
        if cur.rowcount != 1:
            return None
        row = conn.execute("SELECT * FROM staff_time_off WHERE id=?", (int(request_id),)).fetchone()
    finally:
        conn.close()
    row = dict(row)
    _tell_requester(restaurant_id, row, db_path)
    return row


def _tell_requester(restaurant_id, row, db_path=None):
    """The person who asked hears the answer — on the app, a text they
    agreed to, or email (people.tell). A decision used to reach nobody: the
    employee found out only if they opened the portal (F2-5). Never raises."""
    try:
        import people
        from time_utils import mdy_range
        span = mdy_range(row["start_date"], row["end_date"])
        if part_words(row):
            span += f" ({part_words(row)})"
        if row["status"] == "approved":
            title, lines = "Your time off is approved", [f"Your manager approved your time off {span}."]
            # Approval doesn't move shifts already published: name them, so
            # nobody assumes they're off a shift they're still on (WF-21).
            try:
                still = published_conflicts(restaurant_id, row["employee_name"], row["start_date"], row["end_date"],
                                            db_path=db_path, part=row)
            except Exception:
                still = []
            if still:
                from time_utils import mdy
                listed = ", ".join(f"{(c.get('day') or '')[:3]} {mdy(c['date'])} {c['shift_start']}".strip()
                                   for c in still[:4]) + (f" and {len(still) - 4} more" if len(still) > 4 else "")
                lines.append(f"You're still on the published schedule for {listed} — you're on it until your "
                             "manager moves it, so check with them.")
        else:
            title, lines = "Your time off request was declined", [
                f"Your manager declined your time off request for {span}. Talk to them if that is a problem."]
        if row.get("decision_note"):
            lines.append(f"Their note: {row['decision_note']}")
        people.tell(restaurant_id, row["employee_name"], title, lines, email_type="time_off", db_path=_path(db_path))
    except Exception as e:
        print(f"[time_off] decision notice failed rid={restaurant_id}: {e!r}")


def _approved_rows(restaurant_id, start, end, db_path=None):
    s, e = _as_date(start), _as_date(end)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM staff_time_off WHERE restaurant_id=? "
            "AND status='approved' AND NOT (end_date < ? OR start_date > ?)",
            (restaurant_id, s.isoformat(), e.isoformat())).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        r = dict(r)
        a, b = max(_as_date(r["start_date"]), s), min(_as_date(r["end_date"]), e)
        r["_days"] = [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]
        out.append(r)
    return out


def approved_in_window(restaurant_id, start, end, db_path=None, whole_days_only=False):
    """{employee_name: [iso dates]} of approved time off touching [start, end]
    — what the schedule draft must honour. A request for part of a day
    counts as its day here (the safe reading for a reader that only knows
    days); `whole_days_only` leaves those out for a reader that also reads
    approved_parts_in_window (D-39)."""
    out = {}
    for r in _approved_rows(restaurant_id, start, end, db_path):
        if whole_days_only and is_partial(r):
            continue
        out.setdefault(r["employee_name"], []).extend(r["_days"])
    return {k: sorted(set(v)) for k, v in out.items()}


def approved_parts_in_window(restaurant_id, start, end, db_path=None):
    """{employee_name: {iso date: [{"from", "until", "daypart", "words"}]}} —
    approved time off for PART of a day ("off until 4pm", "dinner") touching
    [start, end] (schedule audit 10/3/26 D-39): minutes past each date's
    midnight (an until in the small hours is past 24h), None for an open
    end; `words` the owner-facing "until 4:00pm"."""
    out = {}
    for r in _approved_rows(restaurant_id, start, end, db_path):
        if not is_partial(r):
            continue
        lo, hi, dp = part_minutes(r)
        for d in r["_days"]:
            out.setdefault(r["employee_name"], {}).setdefault(d, []).append(
                {"from": lo, "until": hi, "daypart": dp, "words": part_words(r)})
    return out
