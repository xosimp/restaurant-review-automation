"""
shift_requests.py — a member of staff asking to drop a published shift, the
manager's answer, and the open shift it becomes.

The published schedule was one-way: staff read it, and a shift they could
not work became a text to the manager and a hand edit. Now the request is a
row, the manager decides it in place, an approved drop is an open shift the
rest of the roster can claim (subject to the same legality check the
what-if pass uses), and the covered shift is written back into the
published week as a new version.
"""
from datetime import date

from models import get_conn, DB_PATH

STATUSES = ("pending", "open", "denied", "covered", "withdrawn")


class ShiftRequestError(ValueError):
    pass


def _published(conn, restaurant_id):
    return conn.execute(
        "SELECT id, schedule_csv, week_start, week_end FROM schedule_history WHERE restaurant_id=? "
        "AND published_at IS NOT NULL ORDER BY id DESC LIMIT 1", (restaurant_id,)).fetchone()


def request_drop(restaurant_id, employee_name, day, shift_start, reason=None, db_path=DB_PATH, today=None):
    """Ask to drop one of your own shifts on the published week."""
    from schedule_versions import rows_from_csv
    name = (employee_name or "").strip()
    if not name:
        raise ShiftRequestError("no employee name on this session")
    try:
        d = date.fromisoformat(str(day)[:10])
    except (TypeError, ValueError):
        raise ShiftRequestError("that is not a date")
    today = today or date.today()
    if d < today:
        raise ShiftRequestError("that shift has already happened")
    conn = get_conn(db_path)
    try:
        pub = _published(conn, restaurant_id)
        if not pub:
            raise ShiftRequestError("no published schedule to change")
        rows = rows_from_csv(pub["schedule_csv"])
        mine = next((r for r in rows if r["date"] == d.isoformat() and r["employee"].lower() == name.lower()
                     and r["shift_start"] == (shift_start or "").strip()), None)
        if not mine:
            raise ShiftRequestError("that shift is not on your published schedule")
        dup = conn.execute("SELECT id FROM shift_change_requests WHERE restaurant_id=? AND history_id=? AND "
                           "lower(employee_name)=? AND date=? AND shift_start=? AND status IN ('pending','open')",
                           (restaurant_id, pub["id"], name.lower(), d.isoformat(), mine["shift_start"])).fetchone()
        if dup:
            raise ShiftRequestError("you already asked about that shift")
        cur = conn.execute(
            "INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, shift_end, "
            "role, reason) VALUES (?,?,?,?,?,?,?,?)",
            (restaurant_id, pub["id"], mine["employee"], d.isoformat(), mine["shift_start"], mine["shift_end"],
             mine["role"], (reason or "").strip()[:200] or None))
        conn.commit()
        row = conn.execute("SELECT * FROM shift_change_requests WHERE id=?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return dict(row)


def mine(restaurant_id, employee_name, db_path=DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM shift_change_requests WHERE restaurant_id=? AND lower(employee_name)=? "
                            "ORDER BY created_at DESC LIMIT 20", (restaurant_id, (employee_name or "").lower())).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def for_manager(restaurant_id, db_path=DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM shift_change_requests WHERE restaurant_id=? AND status IN ('pending','open') "
                            "ORDER BY date, shift_start", (restaurant_id,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def open_shifts(restaurant_id, db_path=DB_PATH) -> list:
    """Approved drops nobody has claimed yet."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM shift_change_requests WHERE restaurant_id=? AND status='open' "
                            "ORDER BY date, shift_start", (restaurant_id,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def decide(restaurant_id, request_id, approve, decided_by=None, replacement=None, db_path=DB_PATH):
    """The manager's answer. Approving makes the shift open (or covers it
    outright when a replacement is named); denying leaves it as it was."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM shift_change_requests WHERE id=? AND restaurant_id=? AND status='pending'",
                           (int(request_id), restaurant_id)).fetchone()
        if not row:
            return None
        status = "open" if approve else "denied"
        conn.execute("UPDATE shift_change_requests SET status=?, decided_by=?, decided_at=datetime('now') WHERE id=?",
                     (status, (decided_by or "").strip()[:120] or None, row["id"]))
        conn.commit()
    finally:
        conn.close()
    if approve and replacement:
        return claim(restaurant_id, request_id, replacement, actor=decided_by, db_path=db_path)
    conn = get_conn(db_path)
    try:
        return dict(conn.execute("SELECT * FROM shift_change_requests WHERE id=?", (int(request_id),)).fetchone())
    finally:
        conn.close()


def claim(restaurant_id, request_id, claimant, actor=None, db_path=DB_PATH):
    """Somebody takes an open shift. Legality is the what-if pass's own
    check (availability, staff notes, double booking, hours ceiling, time
    off, rest), and the covered shift is written back into the published
    week as a new version so the portal and the printout agree."""
    from schedule_versions import rows_from_csv, append
    from schedule_engine import _rows_to_csv_text, replacement_is_legal
    from models import update_schedule_history_rows
    name = (claimant or "").strip()
    if not name:
        raise ShiftRequestError("who is taking it?")
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM shift_change_requests WHERE id=? AND restaurant_id=? AND status='open'",
                           (int(request_id), restaurant_id)).fetchone()
        if not row:
            raise ShiftRequestError("that shift is not open")
        hist = conn.execute("SELECT id, schedule_csv FROM schedule_history WHERE id=? AND restaurant_id=?",
                            (row["history_id"], restaurant_id)).fetchone()
    finally:
        conn.close()
    if not hist:
        raise ShiftRequestError("the schedule this shift belongs to is gone")
    rows = rows_from_csv(hist["schedule_csv"])
    idx = next((i for i, r in enumerate(rows) if r["date"] == row["date"] and r["shift_start"] == row["shift_start"]
                and r["employee"].lower() == row["employee_name"].lower()), None)
    if idx is None:
        raise ShiftRequestError("that shift is no longer on the schedule")
    ok, why = replacement_is_legal(restaurant_id, rows, idx, name)
    if not ok:
        raise ShiftRequestError(why or f"{name} cannot take that shift")
    rows[idx] = dict(rows[idx], employee=name,
                     notes=((rows[idx].get("notes") or "").strip() + f" (covered for {row['employee_name']})").strip())
    csv_text = _rows_to_csv_text(rows)
    update_schedule_history_rows(restaurant_id, csv_text, history_id=hist["id"],
                                 edited_by=(actor or name), db_path=db_path)
    append(restaurant_id, hist["id"], "edited", csv_text, saved_by=(actor or name), db_path=db_path)
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE shift_change_requests SET status='covered', replacement_name=?, decided_by=COALESCE(decided_by, ?), "
                     "decided_at=COALESCE(decided_at, datetime('now')) WHERE id=?",
                     (name, (actor or "").strip()[:120] or None, row["id"]))
        conn.commit()
        return dict(conn.execute("SELECT * FROM shift_change_requests WHERE id=?", (row["id"],)).fetchone())
    finally:
        conn.close()


def withdraw(restaurant_id, request_id, employee_name, db_path=DB_PATH) -> bool:
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE shift_change_requests SET status='withdrawn' WHERE id=? AND restaurant_id=? AND "
                           "lower(employee_name)=? AND status IN ('pending','open')",
                           (int(request_id), restaurant_id, (employee_name or "").lower()))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
