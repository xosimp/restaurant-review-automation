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

from models import get_conn, DB_PATH

MAX_RANGE_DAYS = 31
MAX_AHEAD_DAYS = 180
STATUSES = ("pending", "approved", "denied", "withdrawn")


def _as_date(s):
    return date.fromisoformat(str(s)[:10])


def _today(restaurant_id, today=None):
    """The restaurant's own date, never the server's: date.today() is UTC on
    Railway, so after 7pm Central "today" was already tomorrow (C5)."""
    if today:
        return today
    from time_utils import restaurant_now_by_id
    return restaurant_now_by_id(restaurant_id, naive=True).date()


def request_time_off(restaurant_id, employee_name, start, end, reason=None, db_path=DB_PATH, today=None):
    """The employee's own request. Returns (row, error)."""
    name = (employee_name or "").strip()
    if not name:
        return None, "No employee name on this session."
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
            "INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, reason) "
            "VALUES (?,?,?,?,?)", (restaurant_id, name, s.isoformat(), e.isoformat(), (reason or "").strip()[:200] or None))
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
        why = _sr._clean(reason, 120)
        # The id rides along so the push offers Approve / Deny and opens
        # this request, and it reaches whoever can decide it (F2-6). The
        # reason rides too: it was stored and left out (COM-14).
        _sr._tell_managers(restaurant_id, "Time off request",
                           f"{name} asked for {span} off" + (f" — “{why}”" if why else "")
                           + ". Approve or decline it in Labor.", db_path,
                           req={"id": row["id"], "request_kind": "time_off"})
    except Exception as ex:
        print(f"[time_off] manager notice failed rid={restaurant_id}: {ex!r}")
    return dict(row), None


def withdraw(restaurant_id, request_id, employee_name, db_path=DB_PATH) -> bool:
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


def cancel_approved(restaurant_id, request_id, employee_name, db_path=DB_PATH, today=None):
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
                           "they're available again for the next draft.", db_path)
    except Exception as ex:
        print(f"[time_off] cancel notice failed rid={restaurant_id}: {ex!r}")
    return row, None


def published_conflicts(restaurant_id, employee_name, start, end, db_path=DB_PATH) -> list:
    """[{date, day, shift_start, shift_end, role}] — this person's shifts on
    the live published weeks inside [start, end]. Approving time off over a
    week staff already have used to leave them scheduled with nobody told
    (SCHED-22 / MOD-EMP-8)."""
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
                out.append({"date": r["date"], "day": r.get("day"), "shift_start": r["shift_start"],
                            "shift_end": r["shift_end"], "role": r.get("role")})
    return sorted(out, key=lambda x: (x["date"], x["shift_start"]))


def mine(restaurant_id, employee_name, db_path=DB_PATH, today=None):
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
                                                           r["end_date"], db_path=db_path)
            except Exception:
                r["still_scheduled"] = []
        out.append(r)
    return out


def pending(restaurant_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM staff_time_off WHERE restaurant_id=? AND status='pending' "
                            "ORDER BY start_date", (restaurant_id,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def recent(restaurant_id, limit=30, db_path=DB_PATH, today=None):
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
    return [dict(r) for r in rows]


def decide(restaurant_id, request_id, approve, decided_by=None, note=None, db_path=DB_PATH):
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


def _tell_requester(restaurant_id, row, db_path=DB_PATH):
    """The person who asked hears the answer — on the app, a text they
    agreed to, or email (people.tell). A decision used to reach nobody: the
    employee found out only if they opened the portal (F2-5). Never raises."""
    try:
        import people
        from time_utils import mdy_range
        span = mdy_range(row["start_date"], row["end_date"])
        if row["status"] == "approved":
            title, lines = "Your time off is approved", [f"Your manager approved your time off {span}."]
            # Approval doesn't move shifts already published: name them, so
            # nobody assumes they're off a shift they're still on (WF-21).
            try:
                still = published_conflicts(restaurant_id, row["employee_name"], row["start_date"], row["end_date"],
                                            db_path=db_path)
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
        people.tell(restaurant_id, row["employee_name"], title, lines, email_type="time_off", db_path=db_path)
    except Exception as e:
        print(f"[time_off] decision notice failed rid={restaurant_id}: {e!r}")


def approved_in_window(restaurant_id, start, end, db_path=DB_PATH):
    """{employee_name: [iso dates]} of approved time off touching [start, end]
    — what the schedule draft must honour."""
    s, e = _as_date(start), _as_date(end)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT employee_name, start_date, end_date FROM staff_time_off WHERE restaurant_id=? "
            "AND status='approved' AND NOT (end_date < ? OR start_date > ?)",
            (restaurant_id, s.isoformat(), e.isoformat())).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        a, b = max(_as_date(r["start_date"]), s), min(_as_date(r["end_date"]), e)
        days = [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]
        out.setdefault(r["employee_name"], []).extend(days)
    return {k: sorted(set(v)) for k, v in out.items()}
