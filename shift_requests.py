"""
shift_requests.py — a member of staff asking to drop a published shift, the
manager's answer, and the open shift it becomes.

The published schedule was one-way: staff read it, and a shift they could
not work became a text to the manager and a hand edit. Now the request is a
row, the manager decides it in place, an approved drop is an open shift the
rest of the roster can claim (subject to the same legality check the
what-if pass uses), and the covered shift is written back into the
published week as a new version.

Three invariants (SCHED audit):
  - A status moves only from the status it was read in (`AND status=?` with
    a rowcount check), and the CSV change is applied to a fresh read of the
    live week inside the same write transaction as that status change and
    its version row. Two claims of one shift give one winner; two claims of
    different shifts both land (SCHED-5).
  - A claim is held to every rule the finished week is: the sweep's minor,
    certification and window rules, the person's role, a date not already
    past (SCHED-6, SCHED-34).
  - Nobody's shift moves without them hearing about it: a swap waits for
    the colleague's yes, and drops, open shifts, claims and swaps notify
    the people they touch (SCHED-21).
"""
from datetime import date

from models import DB_PATH
import models as _models

STATUSES = ("pending", "approved", "open", "denied", "declined", "covered", "withdrawn")
# approved = a swap the manager said yes to, waiting on the colleague's yes
LIVE = ("pending", "approved", "open")


def get_conn(db_path=None):
    """Resolved through models at call time (CLAUDE.md — bound imports)."""
    return _models.get_conn(db_path or _models.DB_PATH)


class ShiftRequestError(ValueError):
    pass


def _published(conn, restaurant_id, on_date=None):
    """The published week a shift on `on_date` belongs to: the newest
    published row whose week contains that date. Taking simply the newest
    published row meant that once next week went out, nobody could drop or
    swap a shift in the week they were actually working."""
    if on_date is None:
        return conn.execute(
            "SELECT id, schedule_csv, week_start, week_end FROM schedule_history WHERE restaurant_id=? "
            "AND published_at IS NOT NULL AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id AND nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id) ORDER BY id DESC LIMIT 1", (restaurant_id,)).fetchone()
    iso = on_date.isoformat() if hasattr(on_date, "isoformat") else str(on_date)[:10]
    return conn.execute(
        "SELECT id, schedule_csv, week_start, week_end FROM schedule_history WHERE restaurant_id=? "
        "AND published_at IS NOT NULL AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id AND nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id) AND substr(week_start,1,10) <= ? AND substr(week_end,1,10) >= ? "
        "ORDER BY generated_at DESC, id DESC LIMIT 1", (restaurant_id, iso, iso)).fetchone()


def _live_hist(conn, restaurant_id, req):
    """The published week staff now see for this request's date. A request
    opened before the week was re-published used to be written into the
    superseded copy, so the cover never reached the live week (SCHED-10)."""
    live = _published(conn, restaurant_id, req["date"])
    if live:
        return live
    return conn.execute("SELECT id, schedule_csv FROM schedule_history WHERE id=? AND restaurant_id=?",
                        (req["history_id"], restaurant_id)).fetchone()


def _today(restaurant_id, today=None):
    if today:
        return today
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id, naive=True).date()
    except Exception:
        return date.today()


def _find(rows, name, day, start):
    low = (name or "").strip().lower()
    return next((i for i, r in enumerate(rows) if r["date"] == day and r["shift_start"] == start
                 and r["employee"].strip().lower() == low), None)


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
    today = _today(restaurant_id, today)
    if d < today:
        raise ShiftRequestError("that shift has already happened")
    conn = get_conn(db_path)
    try:
        pub = _published(conn, restaurant_id, d)
        if not pub:
            raise ShiftRequestError("no published schedule to change")
        rows = rows_from_csv(pub["schedule_csv"])
        i = _find(rows, name, d.isoformat(), (shift_start or "").strip())
        if i is None:
            raise ShiftRequestError("that shift is not on your published schedule")
        mine_ = rows[i]
        # The duplicate check and the insert are one write transaction: a
        # double tap used to pass the check twice (DATA-35 / MOD-EMP-9).
        conn.execute("BEGIN IMMEDIATE")
        dup = conn.execute("SELECT id FROM shift_change_requests WHERE restaurant_id=? AND history_id=? AND "
                           "lower(employee_name)=? AND date=? AND shift_start=? AND status IN ('pending','approved','open')",
                           (restaurant_id, pub["id"], name.lower(), d.isoformat(), mine_["shift_start"])).fetchone()
        if dup:
            raise ShiftRequestError("you already asked about that shift")
        cur = conn.execute(
            "INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, shift_end, "
            "role, reason) VALUES (?,?,?,?,?,?,?,?)",
            (restaurant_id, pub["id"], mine_["employee"], d.isoformat(), mine_["shift_start"], mine_["shift_end"],
             mine_["role"], (reason or "").strip()[:200] or None))
        conn.commit()
        row = conn.execute("SELECT * FROM shift_change_requests WHERE id=?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return dict(row)


def request_swap(restaurant_id, employee_name, day, shift_start, target_name, target_date, target_start,
                 reason=None, db_path=DB_PATH, today=None):
    """Ask to trade one of your shifts for a named colleague's. Both shifts
    must be on the published week. It moves only once the manager has
    approved it AND the colleague has said yes, and the same legality check
    runs both ways before anything moves. The colleague is told."""
    from schedule_versions import rows_from_csv
    name = (employee_name or "").strip()
    other = (target_name or "").strip()
    if not name:
        raise ShiftRequestError("no employee name on this session")
    if not other or other.lower() == name.lower():
        raise ShiftRequestError("name the colleague you want to swap with")
    try:
        d = date.fromisoformat(str(day)[:10])
        td = date.fromisoformat(str(target_date)[:10])
    except (TypeError, ValueError):
        raise ShiftRequestError("that is not a date")
    today = _today(restaurant_id, today)
    if d < today or td < today:
        raise ShiftRequestError("one of those shifts has already happened")
    conn = get_conn(db_path)
    try:
        pub = _published(conn, restaurant_id, d)
        if not pub:
            raise ShiftRequestError("no published schedule to change")
        other_pub = _published(conn, restaurant_id, td)
        if not other_pub or other_pub["id"] != pub["id"]:
            raise ShiftRequestError("both shifts need to be in the same published week")
        rows = rows_from_csv(pub["schedule_csv"])
        a = _find(rows, name, d.isoformat(), (shift_start or "").strip())
        b = _find(rows, other, td.isoformat(), (target_start or "").strip())
        if a is None:
            raise ShiftRequestError("that shift is not on your published schedule")
        if b is None:
            raise ShiftRequestError(f"{other} does not have that shift on the published schedule")
        mine_, theirs = rows[a], rows[b]
        # The duplicate check and the insert are one write transaction: a
        # double tap used to pass the check twice (DATA-35 / MOD-EMP-9).
        conn.execute("BEGIN IMMEDIATE")
        dup = conn.execute("SELECT id FROM shift_change_requests WHERE restaurant_id=? AND history_id=? AND "
                           "lower(employee_name)=? AND date=? AND shift_start=? AND status IN ('pending','approved','open')",
                           (restaurant_id, pub["id"], name.lower(), d.isoformat(), mine_["shift_start"])).fetchone()
        if dup:
            raise ShiftRequestError("you already asked about that shift")
        cur = conn.execute(
            "INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, shift_end, "
            "role, reason, kind, target_name, target_date, target_start, target_end) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (restaurant_id, pub["id"], mine_["employee"], d.isoformat(), mine_["shift_start"], mine_["shift_end"],
             mine_["role"], (reason or "").strip()[:200] or None, "swap", theirs["employee"], td.isoformat(),
             theirs["shift_start"], theirs["shift_end"]))
        conn.commit()
        row = dict(conn.execute("SELECT * FROM shift_change_requests WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()
    _notify(restaurant_id, "swap_asked", row, db_path=db_path)
    return row


def mine(restaurant_id, employee_name, db_path=DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM shift_change_requests WHERE restaurant_id=? AND lower(employee_name)=? "
                            "ORDER BY created_at DESC LIMIT 20", (restaurant_id, (employee_name or "").lower())).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def asked_of_me(restaurant_id, employee_name, db_path=DB_PATH) -> list:
    """Swaps a colleague has asked this person for that still need their answer."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM shift_change_requests WHERE restaurant_id=? AND kind='swap' AND "
                            "lower(target_name)=? AND status IN ('pending','approved') AND target_accepted_at IS NULL "
                            "ORDER BY date, shift_start", (restaurant_id, (employee_name or "").strip().lower())).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def for_manager(restaurant_id, db_path=DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM shift_change_requests WHERE restaurant_id=? AND status IN ('pending','approved','open') "
                            "ORDER BY date, shift_start", (restaurant_id,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def open_shifts(restaurant_id, db_path=DB_PATH, today=None) -> list:
    """Approved drops nobody has claimed yet, from today on. One from
    yesterday is history, not an offer (SCHED-34)."""
    today = _today(restaurant_id, today)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM shift_change_requests WHERE restaurant_id=? AND status='open' AND date >= ? "
                            "ORDER BY date, shift_start", (restaurant_id, today.isoformat())).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _get(conn, request_id):
    row = conn.execute("SELECT * FROM shift_change_requests WHERE id=?", (int(request_id),)).fetchone()
    return dict(row) if row else None


def decide(restaurant_id, request_id, approve, decided_by=None, replacement=None, db_path=DB_PATH):
    """The manager's answer. Approving makes the shift open (or covers it
    outright when a replacement is named — checked before anything changes,
    so an illegal name leaves the request pending, SCHED-20); denying leaves
    it as it was. A request is decided once: the status changes only from
    'pending', so two managers answering at once get one outcome."""
    who = (decided_by or "").strip()[:120] or None
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM shift_change_requests WHERE id=? AND restaurant_id=? AND status='pending'",
                           (int(request_id), restaurant_id)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    row = dict(row)
    if approve and (row["kind"] or "drop") == "swap":
        return _approve_swap(restaurant_id, row, who, db_path)
    if approve and (replacement or "").strip():
        return _cover(restaurant_id, row, replacement, actor=decided_by, from_status="pending",
                      check_role=False, db_path=db_path)
    status = "open" if approve else "denied"
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE shift_change_requests SET status=?, decided_by=?, decided_at=datetime('now') "
                           "WHERE id=? AND restaurant_id=? AND status='pending'", (status, who, row["id"], restaurant_id))
        conn.commit()
        if cur.rowcount != 1:
            return None
        out = _get(conn, row["id"])
    finally:
        conn.close()
    _notify(restaurant_id, "opened" if approve else "denied", out, db_path=db_path)
    return out


def _role_ok(restaurant_id, rows, row, name, db_path):
    """A server picks up server shifts. The person's roles are every role
    they have worked or were added under, plus any they hold on this week;
    unknown roles do not refuse."""
    want = (row.get("role") or "").strip().lower()
    if not want:
        return True, ""
    try:
        import staff_settings
        have = staff_settings.roles_for(restaurant_id, name, db_path=db_path)
    except Exception:
        have = set()
    low = (name or "").strip().lower()
    have |= {(r.get("role") or "").strip().lower() for r in rows
             if (r.get("employee") or "").strip().lower() == low and (r.get("role") or "").strip()}
    if not have or want in have:
        return True, ""
    return False, f"that's a {row.get('role')} shift, and you're on the roster as {', '.join(sorted(have))}"


def _person_rows(rows, name):
    low = (name or "").strip().lower()
    return [r for r in rows if (r.get("employee") or "").strip().lower() == low]


def claim(restaurant_id, request_id, claimant, actor=None, db_path=DB_PATH, today=None, check_role=True):
    """Somebody takes an open shift. Legality is the what-if pass's own
    check (availability, time off, hours, rest, a minor's latest end, the
    role's certification, the person's window), plus the role and a date
    not yet past; the covered shift is written back into the published
    week as a new version, in one transaction with the status change."""
    name = (claimant or "").strip()
    if not name:
        raise ShiftRequestError("who is taking it?")
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM shift_change_requests WHERE id=? AND restaurant_id=? AND status='open'",
                           (int(request_id), restaurant_id)).fetchone()
    finally:
        conn.close()
    if not row:
        raise ShiftRequestError("that shift is not open")
    return _cover(restaurant_id, dict(row), name, actor=actor, from_status="open", check_role=check_role,
                  db_path=db_path, today=today)


def _cover(restaurant_id, row, claimant, actor, from_status, check_role, db_path, today=None):
    from schedule_versions import rows_from_csv, write_on
    import schedule_engine as _se
    name = (claimant or "").strip()
    if not name:
        raise ShiftRequestError("who is taking it?")
    if date.fromisoformat(str(row["date"])[:10]) < _today(restaurant_id, today):
        raise ShiftRequestError("that shift has already happened")
    if name.lower() == (row["employee_name"] or "").strip().lower():
        raise ShiftRequestError("that is the shift being given up")
    conn = get_conn(db_path)
    try:
        hist = _live_hist(conn, restaurant_id, row)
    finally:
        conn.close()
    if not hist:
        raise ShiftRequestError("the schedule this shift belongs to is gone")
    rows = rows_from_csv(hist["schedule_csv"])
    idx = _find(rows, row["employee_name"], row["date"], row["shift_start"])
    if idx is None:
        raise ShiftRequestError("that shift is no longer on the schedule")
    if check_role:
        ok, why = _role_ok(restaurant_id, rows, rows[idx], name, db_path)
        if not ok:
            raise ShiftRequestError(why)
    ok, why = _se.replacement_is_legal(restaurant_id, rows, idx, name)
    if not ok:
        raise ShiftRequestError(why or f"{name} cannot take that shift")

    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("UPDATE shift_change_requests SET status='covered', replacement_name=?, "
                           "decided_by=COALESCE(decided_by, ?), decided_at=COALESCE(decided_at, datetime('now')) "
                           "WHERE id=? AND restaurant_id=? AND status=?",
                           (name, (actor or "").strip()[:120] or None, row["id"], restaurant_id, from_status))
        if cur.rowcount != 1:
            raise ShiftRequestError("somebody else just took that shift" if from_status == "open"
                                    else "that request was already answered")
        # The week as it is NOW, inside the lock: another claim may have
        # landed since the check above, and writing the stale copy back
        # would erase it.
        fresh_hist = _live_hist(conn, restaurant_id, row)
        fresh = rows_from_csv(fresh_hist["schedule_csv"]) if fresh_hist else []
        j = _find(fresh, row["employee_name"], row["date"], row["shift_start"])
        if j is None:
            raise ShiftRequestError("that shift is no longer on the schedule")
        if fresh_hist["id"] != hist["id"] or fresh[j] != rows[idx] or _person_rows(fresh, name) != _person_rows(rows, name):
            # What the check judged has changed — judge the week as it is.
            ok, why = _se.replacement_is_legal(restaurant_id, fresh, j, name)
            if not ok:
                raise ShiftRequestError(why or f"{name} cannot take that shift")
        fresh[j] = dict(fresh[j], employee=name,
                        notes=((fresh[j].get("notes") or "").strip() + f" (covered for {row['employee_name']})").strip())
        write_on(conn, restaurant_id, fresh_hist["id"], "swap", _se._rows_to_csv_text(fresh), saved_by=(actor or name))
        conn.commit()
        out = _get(conn, row["id"])
    except LookupError:
        conn.rollback()
        raise ShiftRequestError("the schedule this shift belongs to is gone")
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    _notify(restaurant_id, "covered", out, db_path=db_path)
    return out


def _approve_swap(restaurant_id, row, who, db_path):
    """The manager's yes to a swap. Checked both ways now, so an illegal one
    is refused while it is still pending; executed only once the colleague
    has also said yes (SCHED-21)."""
    _swap_legal(restaurant_id, row, db_path)
    if row.get("target_accepted_at"):
        return _execute_swap(restaurant_id, row, who, from_status="pending", db_path=db_path)
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE shift_change_requests SET status='approved', decided_by=?, decided_at=datetime('now') "
                           "WHERE id=? AND restaurant_id=? AND status='pending'", (who, row["id"], restaurant_id))
        conn.commit()
        if cur.rowcount != 1:
            return None
        out = _get(conn, row["id"])
    finally:
        conn.close()
    _notify(restaurant_id, "swap_approved", out, db_path=db_path)
    return out


def respond_swap(restaurant_id, request_id, colleague, accept, db_path=DB_PATH):
    """The colleague's answer to a swap they were asked for. A yes before
    the manager has decided is recorded and waits; a yes after the
    manager's approval moves both shifts."""
    low = (colleague or "").strip().lower()
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM shift_change_requests WHERE id=? AND restaurant_id=? AND kind='swap' AND "
                           "lower(target_name)=? AND status IN ('pending','approved') AND target_accepted_at IS NULL",
                           (int(request_id), restaurant_id, low)).fetchone()
        if not row:
            raise ShiftRequestError("that swap is not waiting on you")
        row = dict(row)
        if not accept:
            cur = conn.execute("UPDATE shift_change_requests SET status='declined' WHERE id=? AND status=? "
                               "AND target_accepted_at IS NULL", (row["id"], row["status"]))
            conn.commit()
            if cur.rowcount != 1:
                raise ShiftRequestError("that swap was already answered")
            out = _get(conn, row["id"])
            conn.close()
            conn = None
            _notify(restaurant_id, "swap_declined", out, db_path=db_path)
            return out
        if row["status"] == "pending":
            cur = conn.execute("UPDATE shift_change_requests SET target_accepted_at=datetime('now') WHERE id=? "
                               "AND status='pending' AND target_accepted_at IS NULL", (row["id"],))
            conn.commit()
            if cur.rowcount != 1:
                raise ShiftRequestError("that swap was already answered")
            return _get(conn, row["id"])
    finally:
        if conn is not None:
            conn.close()
    return _execute_swap(restaurant_id, row, row.get("decided_by"), from_status="approved", db_path=db_path,
                         accepted=True)


def _swap_legal(restaurant_id, req, db_path, rows=None):
    """Raise unless each side can legally take the other's shift, judged
    against a week in which the other side has already moved."""
    from schedule_versions import rows_from_csv
    from schedule_engine import replacement_is_legal
    if rows is None:
        conn = get_conn(db_path)
        try:
            hist = _live_hist(conn, restaurant_id, req)
        finally:
            conn.close()
        if not hist:
            raise ShiftRequestError("the schedule this swap belongs to is gone")
        rows = rows_from_csv(hist["schedule_csv"])
    a, b = _find(rows, req["employee_name"], req["date"], req["shift_start"]), _find(rows, req["target_name"], req["target_date"], req["target_start"])
    if a is None or b is None:
        raise ShiftRequestError("one of the shifts is no longer on the schedule")
    trial = [dict(r) for r in rows]
    trial[a]["employee"], trial[b]["employee"] = "", ""
    ok1, why1 = replacement_is_legal(restaurant_id, trial, a, req["target_name"])
    if not ok1:
        raise ShiftRequestError(f"{req['target_name']} cannot take {req['employee_name']}'s shift: {why1}")
    ok2, why2 = replacement_is_legal(restaurant_id, trial, b, req["employee_name"])
    if not ok2:
        raise ShiftRequestError(f"{req['employee_name']} cannot take {req['target_name']}'s shift: {why2}")
    return a, b


def _execute_swap(restaurant_id, req: dict, actor, from_status, db_path, accepted=False):
    """Both rows trade names, each checked as a replacement for the other,
    against the live week read inside the write transaction."""
    from schedule_versions import rows_from_csv, write_on
    from schedule_engine import _rows_to_csv_text
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("UPDATE shift_change_requests SET status='covered', replacement_name=?, "
                           "decided_by=COALESCE(decided_by, ?), decided_at=COALESCE(decided_at, datetime('now')), "
                           "target_accepted_at=COALESCE(target_accepted_at, CASE WHEN ? THEN datetime('now') END) "
                           "WHERE id=? AND restaurant_id=? AND status=?",
                           (req["target_name"], (actor or "").strip()[:120] or None, 1 if accepted else 0,
                            req["id"], restaurant_id, from_status))
        if cur.rowcount != 1:
            raise ShiftRequestError("that swap was already answered")
        hist = _live_hist(conn, restaurant_id, req)
        if not hist:
            raise ShiftRequestError("the schedule this swap belongs to is gone")
        rows = rows_from_csv(hist["schedule_csv"])
        a, b = _swap_legal(restaurant_id, req, db_path, rows=rows)
        rows[a] = dict(rows[a], employee=req["target_name"], notes=((rows[a].get("notes") or "").strip() + f" (swapped with {req['employee_name']})").strip())
        rows[b] = dict(rows[b], employee=req["employee_name"], notes=((rows[b].get("notes") or "").strip() + f" (swapped with {req['target_name']})").strip())
        write_on(conn, restaurant_id, hist["id"], "swap", _rows_to_csv_text(rows), saved_by=(actor or "swap"))
        conn.commit()
        out = _get(conn, req["id"])
    except LookupError:
        conn.rollback()
        raise ShiftRequestError("the schedule this shift belongs to is gone")
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    _notify(restaurant_id, "swapped", out, db_path=db_path)
    return out


def withdraw(restaurant_id, request_id, employee_name, db_path=DB_PATH) -> bool:
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE shift_change_requests SET status='withdrawn' WHERE id=? AND restaurant_id=? AND "
                           "lower(employee_name)=? AND status IN ('pending','approved','open')",
                           (int(request_id), restaurant_id, (employee_name or "").lower()))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── telling people (SCHED-21) ──────────────────────────────────────────────
# A drop, an open shift, a claim and a swap each change somebody's week.
# Staff are reached by the email on their contact card; managers through
# strategy_jobs._reach (push to the app, email otherwise). Never raises:
# the request already happened, and a failed notice must not undo it.

OPEN_SHIFT_NOTICE_LIMIT = 25


def _when(req, prefix=""):
    d = req.get(prefix + "date") or ""
    try:
        from time_utils import mdy
        wd = date.fromisoformat(str(d)[:10]).strftime("%a")
        pretty = f"{wd} {mdy(d)}"
    except Exception:
        pretty = str(d)
    start, end = req.get(prefix + "start" if prefix else "shift_start"), req.get(prefix + "end" if prefix else "shift_end")
    return f"{pretty} {start or ''}" + (f"–{end}" if end else "")


def _contacts(restaurant_id, db_path):
    try:
        from models import get_staff_contacts
        return {(c["employee_name"] or "").strip().lower(): c for c in get_staff_contacts(restaurant_id, db_path=db_path)}
    except Exception:
        return {}


def _email_staff(restaurant_id, people, subject, lines, db_path) -> int:
    """Email each named person who has an address on file. Returns how many."""
    import html as _h
    try:
        import emails as _emails
        from config import base_url
        from models import get_restaurant
        r = get_restaurant(restaurant_id, db_path)
        place = (getattr(r, "location_name", None) or getattr(r, "name", None) or "your restaurant") if r else "your restaurant"
    except Exception:
        return 0
    book = _contacts(restaurant_id, db_path)
    sent = 0
    for person in people:
        c = book.get((person or "").strip().lower()) or {}
        if not c.get("email"):
            continue
        try:
            html = _emails.report_shell(kicker=place, title=subject, subtitle="",
                                        sections=[_emails.report_paragraph(_h.escape(x)) for x in lines],
                                        cta_label="Open the staff portal", cta_url=base_url() + "/staff")
            res = _emails.deliver(email_type="shift_request", restaurant_id=restaurant_id, payload={
                "from": _emails.sender("client"), "to": [c["email"]],
                "subject": f"{subject} — {place}", "preheader": lines[0][:120] if lines else subject, "html": html})
            if getattr(res, "ok", False):
                sent += 1
        except Exception as e:
            print(f"[shift_requests] staff email failed rid={restaurant_id}: {e!r}")
    return sent


def _tell_managers(restaurant_id, title, body, db_path):
    try:
        import strategy_jobs
        strategy_jobs._reach(restaurant_id, "coverage", title, body, {"tab": "labor"}, db_path, lines=[body])
    except Exception as e:
        print(f"[shift_requests] manager notice failed rid={restaurant_id}: {e!r}")


def _who_could_take(restaurant_id, req, db_path) -> list:
    """Active roster members in the shift's role who could legally take it."""
    try:
        from schedule_versions import rows_from_csv
        import schedule_engine as _se
        import schedule_rules as _rules
        import staff_settings
        conn = get_conn(db_path)
        try:
            hist = _live_hist(conn, restaurant_id, req)
        finally:
            conn.close()
        rows = rows_from_csv(hist["schedule_csv"]) if hist else []
        idx = _find(rows, req["employee_name"], req["date"], req["shift_start"])
        if idx is None:
            return []
        dates = sorted({r["date"] for r in rows if r.get("date")})
        from datetime import datetime as _dt
        c = _rules.build_constraints(restaurant_id, dates, [_dt.strptime(d, "%Y-%m-%d").strftime("%A") for d in dates])
        out = []
        for e in staff_settings.roster(restaurant_id, db_path=db_path):
            n = e["name"]
            if n.strip().lower() == (req["employee_name"] or "").strip().lower():
                continue
            if not _role_ok(restaurant_id, rows, rows[idx], n, db_path)[0]:
                continue
            if _se.replacement_is_legal(restaurant_id, rows, idx, n, constraints=c)[0]:
                out.append(n)
            if len(out) >= OPEN_SHIFT_NOTICE_LIMIT:
                break
        return out
    except Exception as e:
        print(f"[shift_requests] could not list who can take rid={restaurant_id}: {e!r}")
        return []


def _notify(restaurant_id, event, req, db_path=DB_PATH):
    if not req:
        return
    try:
        who, when, role = req.get("employee_name") or "", _when(req), req.get("role") or ""
        if event == "opened":
            _email_staff(restaurant_id, [who], "Your shift is off your schedule",
                         [f"Your manager approved your request to drop {when}. It is now an open shift for the team."], db_path)
            takers = _who_could_take(restaurant_id, req, db_path)
            told = _email_staff(restaurant_id, takers, f"Open shift: {when}",
                                [f"{who} can't work {when}" + (f" ({role})" if role else "") + ".",
                                 "It's yours if you want it — take it in the staff portal before someone else does."], db_path)
            if not told:
                _tell_managers(restaurant_id, "Open shift, nobody told",
                               f"{who}'s {when}" + (f" {role}" if role else "") + " is open, and no teammate who could "
                               "take it has an email on file — assign someone from Labor.", db_path)
        elif event == "denied":
            _email_staff(restaurant_id, [who], "Your shift request was declined",
                         [f"Your manager kept you on {when}. Talk to them if that is a problem."], db_path)
        elif event == "covered":
            taker = req.get("replacement_name") or ""
            _email_staff(restaurant_id, [who], "Your shift is covered",
                         [f"{taker} is working your {when}. You are off it."], db_path)
            _email_staff(restaurant_id, [taker], "You picked up a shift",
                         [f"You are on {when}" + (f" ({role})" if role else "") + f", covering for {who}."], db_path)
            _tell_managers(restaurant_id, "Shift covered", f"{taker} is covering {who}'s {when}" + (f" ({role})" if role else "") + ".", db_path)
        elif event == "swap_asked":
            _email_staff(restaurant_id, [req.get("target_name")], f"{who} asked to swap shifts",
                         [f"{who} would like to trade their {when} for your {_when(req, 'target_')}.",
                          "Nothing moves unless you say yes — answer in the staff portal."], db_path)
        elif event == "swap_approved":
            _email_staff(restaurant_id, [req.get("target_name")], "A swap is waiting on your yes",
                         [f"Your manager approved {who}'s request to trade their {when} for your {_when(req, 'target_')}.",
                          "It only goes ahead if you accept it in the staff portal."], db_path)
        elif event == "swap_declined":
            _email_staff(restaurant_id, [who], "Your swap was declined",
                         [f"{req.get('target_name')} said no to trading for your {when}. You are still on it."], db_path)
        elif event == "swapped":
            other = req.get("target_name") or ""
            _email_staff(restaurant_id, [who], "Your swap went through",
                         [f"You now work {_when(req, 'target_')} instead of {when}."], db_path)
            _email_staff(restaurant_id, [other], "Your swap went through",
                         [f"You now work {when} instead of {_when(req, 'target_')}."], db_path)
            _tell_managers(restaurant_id, "Shifts swapped", f"{who} and {other} traded {when} and {_when(req, 'target_')}.", db_path)
    except Exception as e:
        print(f"[shift_requests] notify {event} failed rid={restaurant_id}: {e!r}")
