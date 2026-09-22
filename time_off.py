"""Time-off requests.

The automation audit gave staff a way to enter the days they can't work in
general; the moat audit's #5 is the specific one — "I need the 12th to the
15th off" — asked for by the person who needs it, decided by a manager, and
then a hard constraint on the next schedule draft. No text, no sticky note,
no manager retyping it.

A request is a row. Nothing here sends anything: the manager sees pending
requests on Labor (web and phone) and the employee sees the decision in the
portal. Approved ranges are read by labor.generate_optimized_schedule for
the week being drafted.
"""
from datetime import date, timedelta

from models import get_conn, DB_PATH

MAX_RANGE_DAYS = 31
MAX_AHEAD_DAYS = 180
STATUSES = ("pending", "approved", "denied", "withdrawn")


def _as_date(s):
    return date.fromisoformat(str(s)[:10])


def request_time_off(restaurant_id, employee_name, start, end, reason=None, db_path=DB_PATH, today=None):
    """The employee's own request. Returns (row, error)."""
    name = (employee_name or "").strip()
    if not name:
        return None, "No employee name on this session."
    try:
        s, e = _as_date(start), _as_date(end or start)
    except (TypeError, ValueError):
        return None, "Pick a start and end date."
    today = today or date.today()
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
    60 days stay visible so the answer is not lost."""
    today = today or date.today()
    floor = (today - timedelta(days=60)).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM staff_time_off WHERE restaurant_id=? AND LOWER(employee_name)=LOWER(?) "
            "AND end_date >= ? ORDER BY start_date", (restaurant_id, (employee_name or "").strip(), floor)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


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
    today = today or date.today()
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
    return dict(row)


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
