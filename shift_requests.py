"""
shift_requests.py — a member of staff asking to drop a published shift, the
manager's answer, and the open shift it becomes.

The published schedule was one-way: staff read it, and a shift they could
not work became a text to the manager and a hand edit. Now the request is a
row, the manager decides it in place, an approved drop is an open shift the
rest of the roster can claim (subject to the same legality check the
what-if pass uses), and the covered shift is written back into the
published week as a new version.

A manager can also post an open shift themselves — somebody's shift, or an
extra one nobody holds yet — and offer it to one named person
(`shift_offers`). The coverage issue's "Ask Ana to cover" is such an offer:
Ana answers in the app, through the same claim path, and her answer lands
on the issue (intraday.ask_to_cover, employee audit H2).

Four invariants (SCHED audit, employee audit H9):
  - A status moves only from the status it was read in (`AND status=?` with
    a rowcount check), and the CSV change is applied to a fresh read of the
    live week inside the same write transaction as that status change and
    its version row. Two claims of one shift give one winner; two claims of
    different shifts both land (SCHED-5).
  - A claim is held to every rule the finished week is: the sweep's minor,
    certification and window rules, the person's role (both ways for a
    swap), and a shift not already started — judged on the restaurant's
    own clock, not the date alone (SCHED-6, SCHED-34, LG-08, LG-11).
  - One live request per shift, whatever the week's version: the duplicate
    check (and a partial unique index) key on the person, date and start,
    not the published row's id, and a shift that moves voids every other
    live request on it (LG-09, LG-10).
  - Nobody's shift moves without them hearing about it: a swap waits for
    the colleague's yes, and drops, open shifts, claims, swaps, withdrawals
    and declines notify the people they touch (SCHED-21, LG-29).
"""
import json
import threading
from datetime import date, datetime, timedelta

from models import DB_PATH
import models as _models

STATUSES = ("pending", "approved", "open", "denied", "declined", "covered", "withdrawn",
            "cancelled", "expired")
# approved  = a swap the manager said yes to, waiting on the colleague's yes
# cancelled = the shift moved (a cover, a swap) before this was answered
# expired   = the shift passed while the request was still live
LIVE = ("pending", "approved", "open")
_LIVE_SQL = "('pending','approved','open')"
KINDS = ("drop", "swap", "post")   # post = a manager posted it open (H2)

# How close to its start an unclaimed open shift is brought back to the
# deciders (employee audit C8 / LG-04): once, this many hours before.
OPEN_SHIFT_ESCALATE_HOURS = 12
OPEN_SHIFT_WATCH_CURSOR_KEY = "open_shift_watch_cursor"
OPEN_SHIFT_WATCH_MAX_SECONDS = 5 * 60
# Open shifts judged for one employee's "can I take it" label per load.
OPEN_SHIFT_JUDGE_LIMIT = 10


def get_conn(db_path=None):
    """Resolved through models at call time (CLAUDE.md — bound imports)."""
    return _models.get_conn(db_path or _models.DB_PATH)


class ShiftRequestError(ValueError):
    pass


class NotOnApp(ShiftRequestError):
    """The person has no active employee login, so an in-app offer could
    never be answered (intraday.ask_to_cover falls back to asking them by
    text or email)."""


def _key(name) -> str:
    return " ".join(str(name or "").split()).casefold()


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
        return datetime.utcnow().date()


def _now(restaurant_id, now=None, today=None):
    """The restaurant's own wall clock (naive local). A caller that pins
    only `today` (tests, a job judging one day) gets that day's midnight."""
    if now is not None:
        return now
    if today is not None:
        return datetime.combine(today, datetime.min.time())
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id, naive=True)
    except Exception:
        return datetime.utcnow()


def shift_span(day, start, end=None):
    """(start_dt, end_dt) local wall clock for one shift, or (None, None)
    when the times can't be read. An end at or before the start closes
    after midnight (schedule_rules.shift_span)."""
    try:
        from schedule_rules import parse_minutes
        d = date.fromisoformat(str(day)[:10])
    except (TypeError, ValueError):
        return None, None
    s = parse_minutes(start or "")
    if s is None:
        return None, None
    base = datetime.combine(d, datetime.min.time())
    s_dt = base + timedelta(minutes=s)
    e = parse_minutes(end or "")
    e_dt = None if e is None else base + timedelta(minutes=e if e > s else e + 24 * 60)
    return s_dt, e_dt


def _gate(day, start, end, now, until="start", what="that shift"):
    """Raise unless the shift can still change hands at `now`: before its
    start (a drop, a swap, a claim of a dropped shift), or — for a shift a
    manager posted or covers — before its end. The date alone let a shift
    already worked be claimed at 11pm and credited to someone else (LG-08)."""
    try:
        d = date.fromisoformat(str(day)[:10])
    except (TypeError, ValueError):
        raise ShiftRequestError("that is not a date")
    s, e = shift_span(day, start, end)
    if until == "end" and e is not None:
        if now >= e:
            raise ShiftRequestError(f"{what} is already over")
        return
    if s is not None:
        if now >= s:
            raise ShiftRequestError(f"{what} has already started" if until == "start" or e is None or now < e
                                    else f"{what} is already over")
        return
    if d < now.date():
        raise ShiftRequestError(f"{what} has already happened")


def _find(rows, name, day, start):
    low = (name or "").strip().lower()
    return next((i for i, r in enumerate(rows) if r["date"] == day and r["shift_start"] == start
                 and r["employee"].strip().lower() == low), None)


def _clean(text, n=200):
    """A free-text reason or note, one line, trimmed — it rides in a push."""
    return " ".join(str(text or "").split())[:n] or None


def _live_on_shift(conn, restaurant_id, name, day, start, exclude_id=None):
    """Live requests about one person's shift: as the holder, or as the
    colleague's side of a swap."""
    rows = conn.execute(
        "SELECT * FROM shift_change_requests WHERE restaurant_id=? AND id<>? AND status IN " + _LIVE_SQL + " AND "
        "((lower(trim(employee_name))=? AND date=? AND shift_start=?) OR "
        "(kind='swap' AND lower(trim(target_name))=? AND target_date=? AND target_start=?))",
        (restaurant_id, int(exclude_id or 0), (name or "").strip().lower(), day, start,
         (name or "").strip().lower(), day, start)).fetchall()
    return [dict(r) for r in rows]


def _has_login(restaurant_id, name, db_path=None):
    """The name as their active employee login spells it — somebody who can
    open the app and answer (LG-12) — or None."""
    try:
        from auth import get_memberships_for_restaurant
        k = _key(name)
        return next((m["employee_name"] for m in get_memberships_for_restaurant(
            restaurant_id, role="employee", db_path=db_path or _models.DB_PATH)
            if _key(m.get("employee_name")) == k), None)
    except Exception as e:
        print(f"[shift_requests] membership lookup failed rid={restaurant_id}: {e!r}")
        return None


def _insert_request(conn, values: dict) -> int:
    cols = list(values)
    try:
        cur = conn.execute(f"INSERT INTO shift_change_requests ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                           tuple(values[c] for c in cols))
    except Exception as e:
        # The partial unique index (models.init_shift_requests) is the last
        # word on "one live request per shift".
        if "UNIQUE" in str(e).upper():
            raise ShiftRequestError("there is already a request on that shift")
        raise
    return cur.lastrowid


def request_drop(restaurant_id, employee_name, day, shift_start, reason=None, db_path=DB_PATH, today=None, now=None):
    """Ask to drop one of your own shifts on the published week."""
    from schedule_versions import rows_from_csv
    name = (employee_name or "").strip()
    if not name:
        raise ShiftRequestError("no employee name on this session")
    try:
        d = date.fromisoformat(str(day)[:10])
    except (TypeError, ValueError):
        raise ShiftRequestError("that is not a date")
    now = _now(restaurant_id, now, today)
    if d < now.date():
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
        _gate(mine_["date"], mine_["shift_start"], mine_["shift_end"], now)
        # The duplicate check and the insert are one write transaction: a
        # double tap used to pass the check twice (DATA-35 / MOD-EMP-9). It
        # keys on the shift, not the published row: a republish gave the
        # week a new id and the same shift could carry two requests (LG-09).
        conn.execute("BEGIN IMMEDIATE")
        if _live_on_shift(conn, restaurant_id, name, d.isoformat(), mine_["shift_start"]):
            raise ShiftRequestError("you already asked about that shift")
        rid_ = _insert_request(conn, {
            "restaurant_id": restaurant_id, "history_id": pub["id"], "employee_name": mine_["employee"],
            "date": d.isoformat(), "shift_start": mine_["shift_start"], "shift_end": mine_["shift_end"],
            "role": mine_["role"], "reason": _clean(reason)})
        conn.commit()
        row = conn.execute("SELECT * FROM shift_change_requests WHERE id=?", (rid_,)).fetchone()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    _notify(restaurant_id, "drop_asked", dict(row), db_path=db_path)
    return dict(row)


def request_swap(restaurant_id, employee_name, day, shift_start, target_name, target_date, target_start,
                 reason=None, db_path=DB_PATH, today=None, now=None):
    """Ask to trade one of your shifts for a named colleague's. Both shifts
    must be on the published week. It moves only once the manager has
    approved it AND the colleague has said yes; the role and legality check
    runs both ways now, before the colleague is asked (WF-08, LG-11), and
    again before anything moves. The colleague is told."""
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
    now = _now(restaurant_id, now, today)
    if d < now.date() or td < now.date():
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
        _gate(mine_["date"], mine_["shift_start"], mine_["shift_end"], now, what="your shift")
        _gate(theirs["date"], theirs["shift_start"], theirs["shift_end"], now, what=f"{theirs['employee']}'s shift")
    finally:
        conn.close()
    req = {"employee_name": mine_["employee"], "date": mine_["date"], "shift_start": mine_["shift_start"],
           "target_name": theirs["employee"], "target_date": theirs["date"], "target_start": theirs["shift_start"]}
    _swap_legal(restaurant_id, req, db_path, rows=rows)
    conn = get_conn(db_path)
    try:
        # The duplicate check and the insert are one write transaction: a
        # double tap used to pass the check twice (DATA-35 / MOD-EMP-9).
        conn.execute("BEGIN IMMEDIATE")
        if _live_on_shift(conn, restaurant_id, name, d.isoformat(), mine_["shift_start"]):
            raise ShiftRequestError("you already asked about that shift")
        if _live_on_shift(conn, restaurant_id, theirs["employee"], td.isoformat(), theirs["shift_start"]):
            raise ShiftRequestError(f"{theirs['employee']}'s shift already has a request on it")
        rid_ = _insert_request(conn, {
            "restaurant_id": restaurant_id, "history_id": pub["id"], "employee_name": mine_["employee"],
            "date": d.isoformat(), "shift_start": mine_["shift_start"], "shift_end": mine_["shift_end"],
            "role": mine_["role"], "reason": _clean(reason), "kind": "swap", "target_name": theirs["employee"],
            "target_date": td.isoformat(), "target_start": theirs["shift_start"], "target_end": theirs["shift_end"]})
        conn.commit()
        row = dict(conn.execute("SELECT * FROM shift_change_requests WHERE id=?", (rid_,)).fetchone())
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    _notify(restaurant_id, "swap_asked", row, db_path=db_path)
    return row


def mine(restaurant_id, employee_name, db_path=DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM shift_change_requests WHERE restaurant_id=? AND lower(employee_name)=? "
                            "ORDER BY created_at DESC, id DESC LIMIT 20",
                            (restaurant_id, (employee_name or "").strip().lower())).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def asked_of_me(restaurant_id, employee_name, db_path=DB_PATH, today=None) -> list:
    """Swaps a colleague has asked this person for that still need their
    answer — only while both shifts are still ahead (LG-07)."""
    today = _today(restaurant_id, today).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM shift_change_requests WHERE restaurant_id=? AND kind='swap' AND "
                            "lower(target_name)=? AND status IN ('pending','approved') AND target_accepted_at IS NULL "
                            "AND date >= ? AND target_date >= ? ORDER BY date, shift_start",
                            (restaurant_id, (employee_name or "").strip().lower(), today, today)).fetchall()
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
    """Open shifts nobody has claimed yet, from today on — the manager's
    list (offer-only ones included). One from yesterday is history, not an
    offer (SCHED-34)."""
    today = _today(restaurant_id, today)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM shift_change_requests WHERE restaurant_id=? AND status='open' AND date >= ? "
                            "ORDER BY date, shift_start", (restaurant_id, today.isoformat())).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def live_offers(restaurant_id, db_path=DB_PATH, name=None, today=None) -> list:
    """Offers still waiting on their person, with the shift they offer
    (the manager's list, or one person's with `name`)."""
    today = _today(restaurant_id, today)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT o.id, o.request_id, o.name, o.status, o.note, o.issue_id, o.created_at, r.date, r.shift_start, "
            "r.shift_end, r.role, r.employee_name, r.kind FROM shift_offers o JOIN shift_change_requests r "
            "ON r.id=o.request_id WHERE o.restaurant_id=? AND o.status='offered' AND r.status='open' AND r.date >= ? "
            "ORDER BY r.date, r.shift_start", (restaurant_id, today.isoformat())).fetchall()
    finally:
        conn.close()
    out = [dict(r) for r in rows]
    if name is not None:
        k = _key(name)
        out = [o for o in out if _key(o["name"]) == k]
    return out


# ── what an employee may see (SEC-04) ──────────────────────────────────────
# Whole rows went to every employee: the dropper's free-text reason ("my kid
# is sick") and the approving manager's login username among them. Each list
# is projected to what its reader needs.

_SHIFT_FIELDS = ("id", "kind", "date", "shift_start", "shift_end", "role", "status")


def _pick(row, fields):
    return {f: row.get(f) for f in fields}


def _for_requester(row) -> dict:
    out = _pick(row, _SHIFT_FIELDS + ("reason", "target_name", "target_date", "target_start", "target_end",
                                      "replacement_name", "decision_note", "decided_at", "created_at"))
    out["employee_name"] = row.get("employee_name")
    out["kind"] = row.get("kind") or "drop"
    out["target_accepted"] = bool(row.get("target_accepted_at"))
    return out


def _for_colleague(row) -> dict:
    """A swap asked of me: whose, which shifts — never their reason."""
    out = _pick(row, _SHIFT_FIELDS + ("employee_name", "target_name", "target_date", "target_start", "target_end"))
    out["kind"] = "swap"
    out["manager_approved"] = row.get("status") == "approved"
    return out


def _for_teammate(row) -> dict:
    """An open shift on the board: when, what role, whose it was."""
    out = _pick(row, _SHIFT_FIELDS)
    out["kind"] = row.get("kind") or "drop"
    out["employee_name"] = (row.get("employee_name") or "").strip() or None
    out["posted_by_manager"] = (row.get("kind") or "") == "post"
    return out


def _for_offer(o) -> dict:
    return {"id": o["id"], "request_id": o["request_id"], "status": o["status"], "date": o["date"],
            "shift_start": o["shift_start"], "shift_end": o["shift_end"], "role": o["role"],
            "employee_name": (o.get("employee_name") or "").strip() or None, "note": o.get("note"),
            "for_coverage": bool(o.get("issue_id")), "created_at": o.get("created_at")}


def for_staff(restaurant_id, employee_name, db_path=DB_PATH, now=None) -> dict:
    """GET /staff/api/shift-requests: {requests, open, asks, offers}, each
    projected for its reader. `open` leaves out the viewer's own shift and
    shifts of a role they don't hold, and labels the rest with whether the
    claim would pass (`can_take`, `why_not`) — the refusal used to arrive
    only after the confirm dialog (WF-11)."""
    now = _now(restaurant_id, now)
    name = (employee_name or "").strip()
    out = {"requests": [_for_requester(r) for r in mine(restaurant_id, name, db_path=db_path)],
           "asks": [_for_colleague(r) for r in asked_of_me(restaurant_id, name, db_path=db_path, today=now.date())],
           "offers": [], "open": []}
    for o in live_offers(restaurant_id, db_path=db_path, name=name, today=now.date()):
        item = _for_offer(o)
        item["pickup_overtime_note"] = _overtime_note(restaurant_id, name, o, now.date(), db_path)
        out["offers"].append(item)
    judged = 0
    for r in open_shifts(restaurant_id, db_path=db_path, today=now.date()):
        if r.get("offer_only"):
            continue
        if _key(r.get("employee_name")) == _key(name):
            continue
        until = "end" if (r.get("kind") or "") == "post" else "start"
        try:
            _gate(r["date"], r["shift_start"], r["shift_end"], now, until=until)
        except ShiftRequestError:
            continue
        item = _for_teammate(r)
        item["can_take"], item["why_not"] = None, None
        if judged < OPEN_SHIFT_JUDGE_LIMIT:
            judged += 1
            try:
                _judge(restaurant_id, r, name, check_role=True, db_path=db_path)
                item["can_take"] = True
            except ShiftRequestError as e:
                why = str(e)
                if why.startswith("that's a "):
                    continue            # another role's shift: not theirs to see
                item["can_take"], item["why_not"] = False, why
        item["pickup_overtime_note"] = (_overtime_note(restaurant_id, name, r, now.date(), db_path)
                                        if item["can_take"] else None)
        out["open"].append(item)
    return out


def _overtime_note(restaurant_id, name, row, today, db_path=None):
    """The viewer's own heads-up before taking this shift — "This 6h pickup
    takes you past 40 hours that week (to 43h)." — or None
    (staff_insights.pickup_overtime_note: hours only, never pay; nothing for
    a salaried person or a shift whose times can't be read). Never raises."""
    try:
        s, e = shift_span(row.get("date"), row.get("shift_start"), row.get("shift_end"))
        if s is None or e is None:
            return None
        import staff_insights
        return staff_insights.pickup_overtime_note(restaurant_id, name, row.get("date"),
                                                   round((e - s).total_seconds() / 3600.0, 2), today=today,
                                                   db_path=None if db_path == DB_PATH else db_path)
    except Exception as ex:
        print(f"[shift_requests] overtime note skipped rid={restaurant_id}: {ex!r}")
        return None


def live_request_by_shift(restaurant_id, employee_name, db_path=DB_PATH) -> dict:
    """{(date, shift_start): {id, kind, status}} — this person's own live
    requests, keyed by the shift, for the Schedule tab's badges (C6)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT id, kind, status, date, shift_start FROM shift_change_requests WHERE restaurant_id=? "
                            "AND lower(trim(employee_name))=? AND status IN " + _LIVE_SQL + " ORDER BY id",
                            (restaurant_id, (employee_name or "").strip().lower())).fetchall()
    finally:
        conn.close()
    return {(r["date"], r["shift_start"]): {"id": r["id"], "kind": r["kind"] or "drop", "status": r["status"]}
            for r in rows}


def _get(conn, request_id):
    row = conn.execute("SELECT * FROM shift_change_requests WHERE id=?", (int(request_id),)).fetchone()
    return dict(row) if row else None


def _vanished(restaurant_id, row, db_path):
    """Raise when a request's shift is no longer on the live week — a
    republish moved it, or a swap or cover already did (LG-10)."""
    from schedule_versions import rows_from_csv
    conn = get_conn(db_path)
    try:
        hist = _live_hist(conn, restaurant_id, row)
    finally:
        conn.close()
    rows = rows_from_csv(hist["schedule_csv"]) if hist else []
    if _find(rows, row["employee_name"], row["date"], row["shift_start"]) is None:
        raise ShiftRequestError("that shift is no longer on the schedule — the week changed after this was asked. "
                                "Deny it to close it")


def decide(restaurant_id, request_id, approve, decided_by=None, replacement=None, note=None, db_path=DB_PATH,
           now=None, today=None):
    """The manager's answer. Approving makes the shift open (or covers it
    outright when a replacement is named — checked before anything changes,
    so an illegal name leaves the request pending, SCHED-20); denying leaves
    it as it was. A request is decided once: the status changes only from
    'pending', so two managers answering at once get one outcome.

    Approving re-reads the live week first: a shift a republish moved, or a
    swap already traded, is refused rather than opened as a phantom (LG-10);
    and a shift already started can't be opened or swapped (LG-07/08) — a
    named cover may still be made until it ends. `note` reaches the
    employee with the answer (COM-14)."""
    who = (decided_by or "").strip()[:120] or None
    note = _clean(note)
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM shift_change_requests WHERE id=? AND restaurant_id=? AND status='pending'",
                           (int(request_id), restaurant_id)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    row = dict(row)
    if approve:
        now = _now(restaurant_id, now, today)
        named = bool((replacement or "").strip())
        if (row["kind"] or "drop") == "swap":
            _gate(row["date"], row["shift_start"], row["shift_end"], now)
            _gate(row["target_date"], row["target_start"], row["target_end"], now,
                  what=f"{row['target_name']}'s shift")
            return _approve_swap(restaurant_id, row, who, db_path, note=note)
        _gate(row["date"], row["shift_start"], row["shift_end"], now, until="end" if named else "start")
        _vanished(restaurant_id, row, db_path)
        if named:
            return _cover(restaurant_id, row, replacement, actor=decided_by, from_status="pending",
                          check_role=False, db_path=db_path, now=now, until="end", note=note)
    status = "open" if approve else "denied"
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE shift_change_requests SET status=?, decided_by=?, decided_at=datetime('now'), "
                           "decision_note=? WHERE id=? AND restaurant_id=? AND status='pending'",
                           (status, who, note, row["id"], restaurant_id))
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
        have = staff_settings.roles_for(restaurant_id, name, db_path=db_path or _models.DB_PATH)
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


def _extra_row(row) -> dict:
    """The CSV row a manager-posted extra shift becomes once someone takes
    it (it has no holder until then)."""
    s, e = shift_span(row["date"], row["shift_start"], row["shift_end"])
    hours = round((e - s).total_seconds() / 3600, 2) if s and e else ""
    try:
        wd = date.fromisoformat(str(row["date"])[:10]).strftime("%A")
    except ValueError:
        wd = ""
    return {"date": row["date"], "day": wd, "employee": "", "role": row.get("role") or "",
            "shift_start": row["shift_start"], "shift_end": row.get("shift_end") or "",
            "scheduled_hours": str(hours), "notes": ""}


def _is_extra(row) -> bool:
    return (row.get("kind") or "") == "post" and not (row.get("employee_name") or "").strip()


def _locate(rows, row):
    """(rows, index) of the request's shift in a week's rows — for an extra
    shift, the week with an unheld copy appended."""
    if _is_extra(row):
        trial = [dict(r) for r in rows] + [_extra_row(row)]
        return trial, len(trial) - 1
    return rows, _find(rows, row["employee_name"], row["date"], row["shift_start"])


def _judge(restaurant_id, row, name, check_role, db_path, constraints=None):
    """(hist, rows, idx): raise unless `name` may take this request's shift
    on the live week — the claim's own check, shared by the offer, the open
    board's label and the claim itself."""
    from schedule_versions import rows_from_csv
    import schedule_engine as _se
    if name.lower() == (row["employee_name"] or "").strip().lower():
        raise ShiftRequestError("that is the shift being given up")
    conn = get_conn(db_path)
    try:
        hist = _live_hist(conn, restaurant_id, row)
    finally:
        conn.close()
    if not hist:
        raise ShiftRequestError("the schedule this shift belongs to is gone")
    rows, idx = _locate(rows_from_csv(hist["schedule_csv"]), row)
    if idx is None:
        raise ShiftRequestError("that shift is no longer on the schedule")
    if check_role:
        ok, why = _role_ok(restaurant_id, rows, rows[idx], name, db_path)
        if not ok:
            raise ShiftRequestError(why)
    ok, why = _se.replacement_is_legal(restaurant_id, rows, idx, name, constraints=constraints)
    if not ok:
        raise ShiftRequestError(why or f"{name} cannot take that shift")
    return hist, rows, idx


def claim(restaurant_id, request_id, claimant, actor=None, db_path=DB_PATH, today=None, check_role=True, now=None):
    """Somebody takes an open shift. Legality is the what-if pass's own
    check (availability, time off, hours, rest, a minor's latest end, the
    role's certification, the person's window), plus the role and a shift
    not yet started (a manager-posted one: not yet over); the covered shift
    is written back into the published week as a new version, in one
    transaction with the status change. An open shift offered only to named
    people is theirs to answer (respond_offer), not the board's."""
    name = (claimant or "").strip()
    if not name:
        raise ShiftRequestError("who is taking it?")
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM shift_change_requests WHERE id=? AND restaurant_id=? AND status='open'",
                           (int(request_id), restaurant_id)).fetchone()
    finally:
        conn.close()
    row = dict(row) if row else None
    if not row or row.get("offer_only"):
        raise ShiftRequestError("that shift is not open")
    return _cover(restaurant_id, row, name, actor=actor, from_status="open", check_role=check_role,
                  db_path=db_path, today=today, now=now, until="end" if (row.get("kind") or "") == "post" else "start")


def _cancel_dependents(conn, restaurant_id, moved, except_id):
    """Every other live request about a shift that just changed hands is
    void — it named a shift its holder no longer has (LG-10). `moved` is
    [(holder, date, start)]. Returns the cancelled rows (to be told)."""
    out = []
    for holder, day, start in moved:
        if not (holder or "").strip():
            continue
        for r in _live_on_shift(conn, restaurant_id, holder, day, start, exclude_id=except_id):
            cur = conn.execute("UPDATE shift_change_requests SET status='cancelled', decided_at=datetime('now'), "
                               "decision_note=? WHERE id=? AND status=?",
                               ("That shift changed hands before this was answered, so it no longer applies.",
                                r["id"], r["status"]))
            if cur.rowcount == 1:
                conn.execute("UPDATE shift_offers SET status='withdrawn', answered_at=datetime('now') "
                             "WHERE request_id=? AND status='offered'", (r["id"],))
                out.append(dict(r, status="cancelled"))
    return out


def _close_offers(conn, request_id, accepted_offer_id=None):
    """When the shift is taken, the accepted offer is marked and the other
    people it was offered to are released. Returns the released offers."""
    if accepted_offer_id:
        conn.execute("UPDATE shift_offers SET status='accepted', answered_at=datetime('now') WHERE id=?",
                     (int(accepted_offer_id),))
    released = [dict(r) for r in conn.execute(
        "SELECT * FROM shift_offers WHERE request_id=? AND status='offered' AND id<>?",
        (int(request_id), int(accepted_offer_id or 0))).fetchall()]
    conn.execute("UPDATE shift_offers SET status='withdrawn', answered_at=datetime('now') WHERE request_id=? "
                 "AND status='offered' AND id<>?", (int(request_id), int(accepted_offer_id or 0)))
    return released


def _cover(restaurant_id, row, claimant, actor, from_status, check_role, db_path, today=None, now=None,
           until="start", note=None, offer_id=None):
    from schedule_versions import rows_from_csv, write_on
    import schedule_engine as _se
    name = (claimant or "").strip()
    if not name:
        raise ShiftRequestError("who is taking it?")
    now = _now(restaurant_id, now, today)
    _gate(row["date"], row["shift_start"], row.get("shift_end"), now, until=until)
    hist, rows, idx = _judge(restaurant_id, row, name, check_role, db_path)

    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("UPDATE shift_change_requests SET status='covered', replacement_name=?, "
                           "decided_by=COALESCE(decided_by, ?), decided_at=COALESCE(decided_at, datetime('now')), "
                           "decision_note=COALESCE(?, decision_note) "
                           "WHERE id=? AND restaurant_id=? AND status=?",
                           (name, (actor or "").strip()[:120] or None, note, row["id"], restaurant_id, from_status))
        if cur.rowcount != 1:
            raise ShiftRequestError("somebody else just took that shift" if from_status == "open"
                                    else "that request was already answered")
        # The week as it is NOW, inside the lock: another claim may have
        # landed since the check above, and writing the stale copy back
        # would erase it.
        fresh_hist = _live_hist(conn, restaurant_id, row)
        fresh, j = _locate(rows_from_csv(fresh_hist["schedule_csv"]) if fresh_hist else [], row)
        if j is None:
            raise ShiftRequestError("that shift is no longer on the schedule")
        if fresh_hist["id"] != hist["id"] or fresh[j] != rows[idx] or _person_rows(fresh, name) != _person_rows(rows, name):
            # What the check judged has changed — judge the week as it is.
            ok, why = _se.replacement_is_legal(restaurant_id, fresh, j, name)
            if not ok:
                raise ShiftRequestError(why or f"{name} cannot take that shift")
        tag = f"(covered for {row['employee_name']})" if not _is_extra(row) else "(extra shift)"
        fresh[j] = dict(fresh[j], employee=name, notes=((fresh[j].get("notes") or "").strip() + " " + tag).strip())
        write_on(conn, restaurant_id, fresh_hist["id"], "swap", _se._rows_to_csv_text(fresh), saved_by=(actor or name))
        if not _is_extra(row):
            _move_section(conn, restaurant_id, row["date"], row["shift_start"], row["employee_name"], name)
        cancelled = _cancel_dependents(conn, restaurant_id, [(row["employee_name"], row["date"], row["shift_start"])],
                                       row["id"])
        released = _close_offers(conn, row["id"], offer_id)
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
    _refresh_task_sheets(restaurant_id, [row["date"]])
    _notify(restaurant_id, "covered", out, db_path=db_path)
    for c in cancelled:
        _notify(restaurant_id, "cancelled", c, db_path=db_path)
    for o in released:
        _notify_offer(restaurant_id, "offer_taken", o, out, db_path)
    return out


def _move_section(conn, restaurant_id, day, start, from_name, to_name):
    """A shift that changes hands keeps its dining-room section
    (models.move_shift_section, inside the caller's write transaction).
    Guarded: it lands with the sections fix (employee audit B8)."""
    fn = getattr(_models, "move_shift_section", None)
    if fn:
        fn(restaurant_id, day, start, from_name, to_name, conn=conn)


def _refresh_task_sheets(restaurant_id, days):
    """A same-day cover or swap re-points today's task sheet at the person
    now on the shift (task_sheets.refresh_assignees — a no-op unless the
    date is today's business day; never raises). Guarded: it lands with
    the tasks fix (employee audit B4)."""
    try:
        import task_sheets
        fn = getattr(task_sheets, "refresh_assignees", None)
        if fn:
            for d in sorted({str(x)[:10] for x in days if x}):
                fn(restaurant_id, d)
    except Exception as e:
        print(f"[shift_requests] task sheets not refreshed rid={restaurant_id}: {e!r}")


def _approve_swap(restaurant_id, row, who, db_path, note=None):
    """The manager's yes to a swap. Checked both ways now, so an illegal one
    is refused while it is still pending; executed only once the colleague
    has also said yes (SCHED-21)."""
    _swap_legal(restaurant_id, row, db_path)
    if row.get("target_accepted_at"):
        return _execute_swap(restaurant_id, row, who, from_status="pending", db_path=db_path, note=note)
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE shift_change_requests SET status='approved', decided_by=?, decided_at=datetime('now'), "
                           "decision_note=? WHERE id=? AND restaurant_id=? AND status='pending'",
                           (who, note, row["id"], restaurant_id))
        conn.commit()
        if cur.rowcount != 1:
            return None
        out = _get(conn, row["id"])
    finally:
        conn.close()
    _notify(restaurant_id, "swap_approved", out, db_path=db_path)
    return out


def respond_swap(restaurant_id, request_id, colleague, accept, db_path=DB_PATH, now=None, today=None):
    """The colleague's answer to a swap they were asked for. A yes before
    the manager has decided is recorded, and the requester and deciders are
    told (it was silent, WF-09); a yes after the manager's approval moves
    both shifts. A yes to a shift that has already started is refused."""
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
            _notify(restaurant_id, "swap_declined", dict(out, prior_status=row["status"]), db_path=db_path)
            return out
        now = _now(restaurant_id, now, today)
        _gate(row["date"], row["shift_start"], row["shift_end"], now, what="that swap's shift")
        _gate(row["target_date"], row["target_start"], row["target_end"], now, what="your shift")
        if row["status"] == "pending":
            cur = conn.execute("UPDATE shift_change_requests SET target_accepted_at=datetime('now') WHERE id=? "
                               "AND status='pending' AND target_accepted_at IS NULL", (row["id"],))
            conn.commit()
            if cur.rowcount != 1:
                raise ShiftRequestError("that swap was already answered")
            out = _get(conn, row["id"])
            conn.close()
            conn = None
            _notify(restaurant_id, "swap_agreed", out, db_path=db_path)
            return out
    finally:
        if conn is not None:
            conn.close()
    return _execute_swap(restaurant_id, row, row.get("decided_by"), from_status="approved", db_path=db_path,
                         accepted=True)


def colleague_agreed(restaurant_id, request_id, actor, db_path=DB_PATH, now=None, today=None):
    """A decider records the colleague's yes given in person — for a
    colleague who isn't on the app, or can't open it (LG-12). Audited by the
    caller; the row keeps who said so (target_agreed_by). An approved swap
    then goes ahead."""
    who = (actor or "").strip()[:120] or "manager"
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM shift_change_requests WHERE id=? AND restaurant_id=? AND kind='swap' AND "
                           "status IN ('pending','approved') AND target_accepted_at IS NULL",
                           (int(request_id), restaurant_id)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    row = dict(row)
    now = _now(restaurant_id, now, today)
    _gate(row["date"], row["shift_start"], row["shift_end"], now)
    _gate(row["target_date"], row["target_start"], row["target_end"], now, what=f"{row['target_name']}'s shift")
    _swap_legal(restaurant_id, row, db_path)
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE shift_change_requests SET target_agreed_by=?, target_accepted_at=CASE WHEN "
                           "status='pending' THEN datetime('now') END WHERE id=? AND status=? "
                           "AND target_accepted_at IS NULL", (who, row["id"], row["status"]))
        conn.commit()
        if cur.rowcount != 1:
            return None
        out = _get(conn, row["id"])
    finally:
        conn.close()
    if row["status"] == "pending":
        _notify(restaurant_id, "swap_agreed", dict(out, agreed_by_manager=True), db_path=db_path)
        return out
    return _execute_swap(restaurant_id, row, who, from_status="approved", db_path=db_path, accepted=True)


def _swap_legal(restaurant_id, req, db_path, rows=None, constraints=None):
    """Raise unless each side can legally take the other's shift — their
    role (it skipped the role check claims enforce, LG-11), then every rule
    — judged against a week in which the other side has already moved."""
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
    for idx, taker, holder in ((a, req["target_name"], req["employee_name"]), (b, req["employee_name"], req["target_name"])):
        if not _role_ok(restaurant_id, rows, rows[idx], taker, db_path)[0]:
            raise ShiftRequestError(f"{taker} cannot take {holder}'s shift: it's a {rows[idx].get('role')} shift, "
                                    f"and {taker} doesn't work that role")
    trial = [dict(r) for r in rows]
    trial[a]["employee"], trial[b]["employee"] = "", ""
    ok1, why1 = replacement_is_legal(restaurant_id, trial, a, req["target_name"], constraints=constraints)
    if not ok1:
        raise ShiftRequestError(f"{req['target_name']} cannot take {req['employee_name']}'s shift: {why1}")
    ok2, why2 = replacement_is_legal(restaurant_id, trial, b, req["employee_name"], constraints=constraints)
    if not ok2:
        raise ShiftRequestError(f"{req['employee_name']} cannot take {req['target_name']}'s shift: {why2}")
    return a, b


def _execute_swap(restaurant_id, req: dict, actor, from_status, db_path, accepted=False, note=None, now=None):
    """Both rows trade names, each checked as a replacement for the other,
    against the live week read inside the write transaction. Never for a
    shift already started (LG-07)."""
    from schedule_versions import rows_from_csv, write_on
    from schedule_engine import _rows_to_csv_text
    now = _now(restaurant_id, now)
    _gate(req["date"], req["shift_start"], req.get("shift_end"), now, what="that swap's shift")
    _gate(req["target_date"], req["target_start"], req.get("target_end"), now, what="that swap's shift")
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("UPDATE shift_change_requests SET status='covered', replacement_name=?, "
                           "decided_by=COALESCE(decided_by, ?), decided_at=COALESCE(decided_at, datetime('now')), "
                           "decision_note=COALESCE(?, decision_note), "
                           "target_accepted_at=COALESCE(target_accepted_at, CASE WHEN ? THEN datetime('now') END) "
                           "WHERE id=? AND restaurant_id=? AND status=?",
                           (req["target_name"], (actor or "").strip()[:120] or None, note, 1 if accepted else 0,
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
        _move_section(conn, restaurant_id, req["date"], req["shift_start"], req["employee_name"], req["target_name"])
        _move_section(conn, restaurant_id, req["target_date"], req["target_start"], req["target_name"], req["employee_name"])
        cancelled = _cancel_dependents(conn, restaurant_id,
                                       [(req["employee_name"], req["date"], req["shift_start"]),
                                        (req["target_name"], req["target_date"], req["target_start"])], req["id"])
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
    _refresh_task_sheets(restaurant_id, [req["date"], req["target_date"]])
    _notify(restaurant_id, "swapped", out, db_path=db_path)
    for c in cancelled:
        _notify(restaurant_id, "cancelled", c, db_path=db_path)
    return out


def withdraw(restaurant_id, request_id, employee_name, db_path=DB_PATH) -> bool:
    """The requester takes back a live request. The people waiting on it —
    the colleague asked, the deciders holding an Approve button, the
    teammates told the shift was open — are told it's off (LG-29)."""
    conn = get_conn(db_path)
    try:
        # A shift a manager posted open is theirs to take down, not the
        # holder's (cancel_open).
        row = conn.execute("SELECT * FROM shift_change_requests WHERE id=? AND restaurant_id=? AND lower(employee_name)=? "
                           "AND COALESCE(kind,'drop')<>'post' AND status IN " + _LIVE_SQL,
                           (int(request_id), restaurant_id, (employee_name or "").strip().lower())).fetchone()
        if not row:
            return False
        row = dict(row)
        cur = conn.execute("UPDATE shift_change_requests SET status='withdrawn' WHERE id=? AND status=?",
                           (row["id"], row["status"]))
        released = []
        if cur.rowcount == 1:
            released = _close_offers(conn, row["id"])
        conn.commit()
        if cur.rowcount != 1:
            return False
    finally:
        conn.close()
    _notify(restaurant_id, "withdrawn", dict(row, prior_status=row["status"]), db_path=db_path)
    for o in released:
        _notify_offer(restaurant_id, "offer_withdrawn", o, row, db_path)
    return True


def cancel_open(restaurant_id, request_id, actor=None, db_path=DB_PATH):
    """A decider takes an open shift off the board (one they posted, or an
    approved drop they've now covered by hand). The holder, the people it
    was offered to and the teammates told about it hear it's gone."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM shift_change_requests WHERE id=? AND restaurant_id=? AND status='open'",
                           (int(request_id), restaurant_id)).fetchone()
        if not row:
            return None
        row = dict(row)
        cur = conn.execute("UPDATE shift_change_requests SET status='withdrawn', decision_note=COALESCE(decision_note, ?) "
                           "WHERE id=? AND status='open'", (f"Taken off the board by {(actor or 'a manager')[:60]}.", row["id"]))
        released = _close_offers(conn, row["id"]) if cur.rowcount == 1 else []
        conn.commit()
        if cur.rowcount != 1:
            return None
        out = _get(conn, row["id"])
    finally:
        conn.close()
    _notify(restaurant_id, "open_cancelled", dict(row, prior_status="open"), db_path=db_path)
    for o in released:
        _notify_offer(restaurant_id, "offer_withdrawn", o, row, db_path)
    return out


# ── a manager posting an open shift, and offering it to one person (H2) ─────

def post_open_shift(restaurant_id, day, shift_start, shift_end=None, role=None, employee=None, actor=None,
                    note=None, offer_to=None, issue_id=None, broadcast=True, db_path=DB_PATH, now=None, today=None):
    """Put a shift on the open board: somebody's shift on the live week
    (`employee`), or an extra one nobody holds (`shift_end` and `role`
    required). With `offer_to`, it is offered to that one person instead of
    the whole team. Returns {"request", "offer"}.

    An open row already on that shift is reused: offered to the named
    person, or (offer-only so far) put on the board for everyone."""
    from schedule_versions import rows_from_csv
    try:
        d = date.fromisoformat(str(day)[:10])
    except (TypeError, ValueError):
        raise ShiftRequestError("that is not a date")
    start = (shift_start or "").strip()
    holder = (employee or "").strip()
    now = _now(restaurant_id, now, today)
    from schedule_rules import parse_minutes
    if parse_minutes(start) is None:
        raise ShiftRequestError("give the shift's start time, like 5:00pm")
    note = _clean(note)
    conn = get_conn(db_path)
    try:
        pub = _published(conn, restaurant_id, d)
        if not pub:
            raise ShiftRequestError("no published schedule covers that date — publish the week first")
        rows = rows_from_csv(pub["schedule_csv"])
        if holder:
            i = _find(rows, holder, d.isoformat(), start)
            if i is None:
                raise ShiftRequestError(f"{holder} has no {start} shift on the published schedule that day")
            held = rows[i]
            values = {"employee_name": held["employee"], "shift_end": held["shift_end"], "role": held["role"]}
        else:
            end = (shift_end or "").strip()
            if parse_minutes(end) is None or not (role or "").strip():
                raise ShiftRequestError("an extra shift needs an end time and a role")
            values = {"employee_name": "", "shift_end": end, "role": (role or "").strip()[:60]}
        _gate(d.isoformat(), start, values["shift_end"], now, until="end")
        conn.execute("BEGIN IMMEDIATE")
        existing = _live_on_shift(conn, restaurant_id, values["employee_name"], d.isoformat(), start) if holder else []
        reuse = None
        for r in existing:
            if r["status"] == "open" and r["employee_name"].strip().lower() == holder.lower():
                reuse = r
            else:
                raise ShiftRequestError(f"{holder} already has a request on that shift — answer it in Waiting on you")
        if reuse is None:
            rid_ = _insert_request(conn, {
                "restaurant_id": restaurant_id, "history_id": pub["id"], "date": d.isoformat(), "shift_start": start,
                "kind": "post", "status": "open", "decided_by": (actor or "").strip()[:120] or None,
                "decided_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), "decision_note": note,
                "offer_only": 1 if (offer_to or "").strip() else 0, **values})
            row = _get(conn, rid_)
            fresh = True
        else:
            row, fresh = reuse, False
            if not (offer_to or "").strip() and row.get("offer_only"):
                conn.execute("UPDATE shift_change_requests SET offer_only=0 WHERE id=?", (row["id"],))
                row = _get(conn, row["id"])
                fresh = True
            elif not (offer_to or "").strip():
                raise ShiftRequestError("that shift is already open")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    offer = None
    if (offer_to or "").strip():
        try:
            offer = offer_shift(restaurant_id, row["id"], offer_to, actor=actor, note=note, issue_id=issue_id,
                                db_path=db_path, now=now)
        except ShiftRequestError:
            if reuse is None:
                _drop_unused(restaurant_id, row["id"], db_path)
            raise
    elif fresh and broadcast:
        _notify(restaurant_id, "posted", row, db_path=db_path)
    return {"request": _get_row(restaurant_id, row["id"], db_path), "offer": offer}


def _get_row(restaurant_id, request_id, db_path):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM shift_change_requests WHERE id=? AND restaurant_id=?",
                         (int(request_id), restaurant_id)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def _drop_unused(restaurant_id, request_id, db_path):
    """An offer-only row whose one offer could not be made is not left on
    the board as an orphan."""
    conn = get_conn(db_path)
    try:
        # Created moments ago in this same call, offered to nobody, seen by
        # nobody: removed rather than left as a withdrawn row in history.
        conn.execute("DELETE FROM shift_change_requests WHERE id=? AND restaurant_id=? "
                     "AND status='open' AND offer_only=1 AND NOT EXISTS (SELECT 1 FROM shift_offers o "
                     "WHERE o.request_id=shift_change_requests.id)", (int(request_id), restaurant_id))
        conn.commit()
    finally:
        conn.close()


def offer_shift(restaurant_id, request_id, name, actor=None, note=None, issue_id=None, db_path=DB_PATH, now=None):
    """Offer an open shift to one named person, checked first by the claim's
    own rules (role aside — the manager chose them), and tell them in the
    app (people.tell). Returns the offer with `via` ("push", "sms", "email",
    or "app" when it waits in their app with no notice delivered)."""
    person = (name or "").strip()
    if not person:
        raise ShiftRequestError("who should it be offered to?")
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM shift_change_requests WHERE id=? AND restaurant_id=? AND status='open'",
                           (int(request_id), restaurant_id)).fetchone()
    finally:
        conn.close()
    if not row:
        raise ShiftRequestError("that shift is not open")
    row = dict(row)
    now = _now(restaurant_id, now)
    _gate(row["date"], row["shift_start"], row["shift_end"], now,
          until="end" if (row.get("kind") or "") == "post" else "start")
    login = _has_login(restaurant_id, person, db_path)
    if not login:
        raise NotOnApp(f"{person} isn't on the Cavnar AI app, so they can't answer an offer — call them directly")
    person = login
    _judge(restaurant_id, row, person, check_role=False, db_path=db_path)
    import people as _people
    try:
        channel = _people.reach(restaurant_id, [person], db_path=db_path).get(person) or {}
    except Exception:
        channel = {}
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM shift_offers WHERE request_id=? AND name_key=? AND status='offered'",
                        (row["id"], _key(person))).fetchone():
            raise ShiftRequestError(f"{person} has already been offered that shift")
        cur = conn.execute("INSERT INTO shift_offers (restaurant_id, request_id, name, name_key, note, issue_id, "
                           "created_by) VALUES (?,?,?,?,?,?,?)",
                           (restaurant_id, row["id"], person, _key(person), _clean(note), issue_id,
                            (actor or "").strip()[:120] or None))
        conn.commit()
        offer = dict(conn.execute("SELECT * FROM shift_offers WHERE id=?", (cur.lastrowid,)).fetchone())
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    via = _notify_offer(restaurant_id, "offered", offer, row, db_path, channel=channel) or "app"
    offer["via"] = via
    return offer


def respond_offer(restaurant_id, offer_id, name, accept, db_path=DB_PATH, now=None):
    """The named person's answer to an offer. Yes is the claim itself —
    the same legality check, the week written in one transaction; no is
    recorded and the manager told. Either way an offer from a coverage issue
    writes the answer onto that issue (intraday.mark_cover_answer), and a
    yes resolves it. Returns {"offer", "request"}."""
    conn = get_conn(db_path)
    try:
        o = conn.execute("SELECT * FROM shift_offers WHERE id=? AND restaurant_id=? AND status='offered'",
                         (int(offer_id), restaurant_id)).fetchone()
        o = dict(o) if o else None
        if not o or o["name_key"] != _key(name):
            raise ShiftRequestError("that offer is not waiting on you")
        row = _get(conn, o["request_id"])
        if not row or row["status"] != "open":
            conn.execute("UPDATE shift_offers SET status='withdrawn', answered_at=datetime('now') WHERE id=? "
                         "AND status='offered'", (o["id"],))
            conn.commit()
            raise ShiftRequestError("that shift was already covered")
        if not accept:
            cur = conn.execute("UPDATE shift_offers SET status='declined', answered_at=datetime('now') WHERE id=? "
                               "AND status='offered'", (o["id"],))
            conn.commit()
            if cur.rowcount != 1:
                raise ShiftRequestError("that offer was already answered")
            o = dict(conn.execute("SELECT * FROM shift_offers WHERE id=?", (o["id"],)).fetchone())
    finally:
        conn.close()
    if not accept:
        _cover_answer(restaurant_id, o, False, db_path)
        _notify_offer(restaurant_id, "offer_declined", o, row, db_path)
        return {"offer": o, "request": row}
    out = _cover(restaurant_id, row, o["name"], actor=o["name"], from_status="open", check_role=False,
                 db_path=db_path, now=now, until="end" if (row.get("kind") or "") == "post" else "start",
                 offer_id=o["id"])
    o = dict(o, status="accepted")
    _cover_answer(restaurant_id, o, True, db_path)
    return {"offer": o, "request": out}


def _cover_answer(restaurant_id, offer, accepted, db_path):
    """An offer made from a coverage issue answers that issue: the answer is
    kept on its ask, and a yes resolves it. Never raises."""
    if not offer.get("issue_id"):
        return
    try:
        import intraday
        intraday.mark_cover_answer(restaurant_id, int(offer["issue_id"]), offer["name"], accepted,
                                   db_path=db_path or _models.DB_PATH)
        if accepted:
            import issues
            issues.resolve(restaurant_id, int(offer["issue_id"]),
                           note=f"{offer['name']} took the shift in the app.", db_path=db_path or _models.DB_PATH)
    except Exception as e:
        print(f"[shift_requests] cover answer not kept rid={restaurant_id}: {e!r}")


# ── the hourly watch: expire the past, escalate the unclaimed (C8, H9) ──────

def expire_past(restaurant_id, db_path=DB_PATH, now=None) -> int:
    """Live requests whose shift has passed become 'expired': a drop or an
    open shift once it has ended, a swap once either shift has started (it
    can no longer go ahead). A swap for last Tuesday used to execute when
    the colleague finally tapped Accept (LG-07). Returns how many."""
    now = _now(restaurant_id, now)
    conn = get_conn(db_path)
    n = 0
    try:
        rows = conn.execute("SELECT * FROM shift_change_requests WHERE restaurant_id=? AND status IN " + _LIVE_SQL +
                            " AND (date <= ? OR (kind='swap' AND target_date <= ?))",
                            (restaurant_id, now.date().isoformat(), now.date().isoformat())).fetchall()
        for r in rows:
            r = dict(r)
            gone = False
            legs = [(r["date"], r["shift_start"], r["shift_end"])]
            if (r["kind"] or "") == "swap":
                legs.append((r["target_date"], r["target_start"], r["target_end"]))
            for day, s, e in legs:
                try:
                    _gate(day, s, e, now, until="start" if len(legs) > 1 else "end")
                except ShiftRequestError:
                    gone = True
            if not gone:
                continue
            cur = conn.execute("UPDATE shift_change_requests SET status='expired' WHERE id=? AND status=?",
                               (r["id"], r["status"]))
            if cur.rowcount == 1:
                conn.execute("UPDATE shift_offers SET status='expired', answered_at=datetime('now') WHERE request_id=? "
                             "AND status='offered'", (r["id"],))
                n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def escalate_unclaimed(restaurant_id, db_path=DB_PATH, now=None, hours=OPEN_SHIFT_ESCALATE_HOURS) -> int:
    """An open shift nobody has claimed `hours` before it starts is brought
    back to the deciders, once — "nobody told" fired only when no teammate
    could be reached, not when nobody acted (LG-04). Returns how many."""
    now = _now(restaurant_id, now)
    horizon = now + timedelta(hours=hours)
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM shift_change_requests WHERE restaurant_id=? AND status='open' AND escalated_at IS NULL "
            "AND date BETWEEN ? AND ?", (restaurant_id, (now.date() - timedelta(days=1)).isoformat(),
                                         horizon.date().isoformat())).fetchall()]
    finally:
        conn.close()
    n = 0
    for r in rows:
        s, _e = shift_span(r["date"], r["shift_start"], r["shift_end"])
        if s is None or not (now < s <= horizon):
            continue
        conn = get_conn(db_path)
        try:
            cur = conn.execute("UPDATE shift_change_requests SET escalated_at=datetime('now') WHERE id=? "
                               "AND escalated_at IS NULL AND status='open'", (r["id"],))
            conn.commit()
        finally:
            conn.close()
        if cur.rowcount == 1:
            left = int((s - now).total_seconds() // 3600)
            _notify(restaurant_id, "unclaimed", dict(r, hours_left=left), db_path=db_path)
            n += 1
    return n


def run_open_shift_watch(db_path=None):
    """Hourly: for each restaurant with a live shift request, expire the
    ones whose shift has passed and tell the deciders about open shifts
    still unclaimed close to their start. Bounded and resumable
    (scheduler.resumable_sweep with its own cursor)."""
    import threading
    import scheduler as _sched
    db = db_path or _models.DB_PATH
    conn = get_conn(db)
    try:
        ids = sorted({r[0] for r in conn.execute(
            "SELECT DISTINCT restaurant_id FROM shift_change_requests WHERE status IN " + _LIVE_SQL).fetchall()})
    finally:
        conn.close()
    tally = {"attempted": 0, "failed": 0, "expired": 0, "escalated": 0}
    lock = threading.Lock()

    def _one(rid):
        with lock:
            tally["attempted"] += 1
        try:
            e = expire_past(rid, db_path=db)
            u = escalate_unclaimed(rid, db_path=db)
        except Exception:
            with lock:
                tally["failed"] += 1
            raise
        with lock:
            tally["expired"] += e
            tally["escalated"] += u

    _done, ran_out = _sched.resumable_sweep(OPEN_SHIFT_WATCH_CURSOR_KEY, ids, _one, OPEN_SHIFT_WATCH_MAX_SECONDS,
                                            workers=1, job="open_shift_watch")
    return {"attempted": tally["attempted"], "ok": tally["attempted"] - tally["failed"], "failed": tally["failed"],
            "skipped": 0, "hit_bound": bool(ran_out), "expired": tally["expired"], "escalated": tally["escalated"]}


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
    """Tell each named person, on the channel they signed up with
    (people.tell: the app, a text they agreed to, email as the fallback).
    Returns how many were reached. The name is historical: it emailed only,
    so a person with no address on file never heard their drop was approved
    or their swap went through (F2-12)."""
    return len(_tell_staff(restaurant_id, people, subject, lines, db_path))


# The request a notice is about, for the staff notice's own routing (set by
# _notify for the duration of one event).
_ctx = threading.local()


def _tell_extras(req, offer=None) -> dict:
    """What people.tell can carry about a request — the shift's date (quiet
    hours send a same-day change at once), the request to open, the
    Requests tab — passed only where people.tell takes it (the notification
    rebuild, employee audit B2)."""
    if not req and not offer:
        return {}
    try:
        import inspect
        import people as _people
        accepts = set(inspect.signature(_people.tell).parameters)
    except Exception:
        return {}
    want = {"purpose": "request", "nav": "requests",
            "shift_date": str((req or {}).get("date") or "")[:10] or None,
            "data": {k: v for k, v in (("request_id", (req or {}).get("id")),
                                       ("offer_id", (offer or {}).get("id"))) if v}}
    return {k: v for k, v in want.items() if k in accepts and v}


def _tell_staff(restaurant_id, people, subject, lines, db_path, channels=None) -> list:
    """_email_staff, returning the names actually reached."""
    import people as _people
    names = [p for p in (people or []) if (p or "").strip()]
    if not names:
        return []
    if channels is None:
        try:
            channels = _people.reach(restaurant_id, names, db_path=db_path)
        except Exception as e:
            print(f"[shift_requests] reach failed rid={restaurant_id}: {e!r}")
            channels = {}
    reached = []
    extra = _tell_extras(getattr(_ctx, "req", None))
    for person in names:
        try:
            if _people.tell(restaurant_id, person, subject, lines, email_type="shift_request",
                            channel=channels.get(person) or {}, db_path=db_path, **extra):
                reached.append(person)
        except Exception as e:
            print(f"[shift_requests] staff notice failed rid={restaurant_id}: {e!r}")
    return reached


def _tell_managers(restaurant_id, title, body, db_path, req=None):
    # Its own type (re-audit A-6). As "coverage" it was P1 (broke Focus
    # mode), labelled "Someone hasn't clocked in", opened Reviews on iOS
    # and was dropped by the "calm" level and the briefing budget.
    if req and req.get("id"):
        # A request waiting on an answer goes through THE deciders helper
        # (people.tell_deciders): it carries its id, so the push can offer
        # Approve / Deny and open that request (push.CATEGORY_REQUEST,
        # friction audit #22) — a time-off request as "time_off" — and it
        # reaches the people who can decide it, whether or not their morning
        # brief is on (F2-6). Never raises.
        import people
        people.tell_deciders(restaurant_id, title, body, request_id=req["id"],
                             request_kind=req.get("request_kind") or "shift", db_path=db_path)
        return
    try:
        # A notice with nothing to decide (a swap done, a shift covered)
        # goes to the brief's audience, as every other heads-up does.
        import strategy_jobs
        strategy_jobs._reach(restaurant_id, "shift_request", title, body, {"tab": "labor"}, db_path, lines=[body])
    except Exception as e:
        print(f"[shift_requests] manager notice failed rid={restaurant_id}: {e!r}")


# How far back an open-shift notice counts toward a person's turn in the
# rotation (_who_could_take).
OPEN_SHIFT_ROTATION_DAYS = 30


def _who_could_take(restaurant_id, req, db_path) -> list:
    """Active roster members in the shift's role who could legally take it,
    at most OPEN_SHIFT_NOTICE_LIMIT, in turn (schedule audit 10/3/26 E-3).

    The roster was walked alphabetically and the walk stopped at the 25th
    legal taker: whoever left in June was told about every open shift, and
    people late in the alphabet never were. Now everyone legal is found and
    the notice goes round: fewest open-shift notices in the last
    OPEN_SHIFT_ROTATION_DAYS days first, then the fewest hours that payroll
    week (the most room), then whoever was told longest ago. Nobody who has
    stopped working (Constraints.dormant) and nobody whose own unconfirmed
    note rules out that day (note_caution) is told — Constraints.fillable."""
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
        rows, idx = _locate(rows_from_csv(hist["schedule_csv"]) if hist else [], req)
        if idx is None:
            return []
        dates = sorted({r["date"] for r in rows if r.get("date")})
        c = _rules.build_constraints(restaurant_id, dates, [datetime.strptime(d, "%Y-%m-%d").strftime("%A") for d in dates])
        row = rows[idx]
        day = row.get("date") or ""
        asker = c.key(req.get("employee_name") or "")
        legal = []
        for e in staff_settings.roster(restaurant_id, db_path=db_path):
            n = e["name"]
            if asker and c.key(n) == asker:
                continue
            if not c.fillable(n, day, daypart=_rules.daypart_of(row.get("shift_start") or ""))[0] \
                    or not c.can_work(n, day)[0]:
                continue
            if not _role_ok(restaurant_id, rows, row, n, db_path)[0]:
                continue
            if _se.replacement_is_legal(restaurant_id, rows, idx, n, constraints=c)[0]:
                legal.append(n)
        told = _recent_notices(restaurant_id, db_path, c)
        bucket = c.bucket(day) if day else ""

        def _turn(n):
            k = c.key(n)
            hours = sum(_rules.row_hours(r) for r in rows if c.key(r.get("employee")) == k
                        and r.get("date") and c.bucket(r["date"]) == bucket)
            hours += float((c.base_hours.get(k) or {}).get(bucket, 0.0) or 0.0)
            count, last = told.get(k, (0, ""))
            return (count, round(hours, 2), last, n.lower())
        return sorted(legal, key=_turn)[:OPEN_SHIFT_NOTICE_LIMIT]
    except Exception as e:
        print(f"[shift_requests] could not list who can take rid={restaurant_id}: {e!r}")
        return []


def _recent_notices(restaurant_id, db_path, c=None) -> dict:
    """{person key: (open-shift notices in the last OPEN_SHIFT_ROTATION_DAYS
    days, when the newest was)} from who each open shift was told
    (told_json) — the open-shift broadcast's rotation."""
    out = {}
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT told_json, created_at FROM shift_change_requests WHERE restaurant_id=? "
                            "AND told_json IS NOT NULL AND created_at >= datetime('now', ?)",
                            (restaurant_id, f"-{int(OPEN_SHIFT_ROTATION_DAYS)} days")).fetchall()
    except Exception as e:
        print(f"[shift_requests] open-shift rotation unread rid={restaurant_id}: {e!r}")
        rows = []
    finally:
        conn.close()
    for r in rows:
        for n in _told({"told_json": r["told_json"]}):
            k = c.key(n) if c is not None else " ".join(n.split()).lower()
            count, last = out.get(k, (0, ""))
            out[k] = (count + 1, max(last, str(r["created_at"] or "")))
    return out


def _remember_told(restaurant_id, req, names, db_path):
    """Who was told a shift is open — the people to tell when it isn't."""
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE shift_change_requests SET told_json=? WHERE id=? AND restaurant_id=?",
                         (json.dumps(sorted(set(names)))[:4000], req["id"], restaurant_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[shift_requests] told list not kept rid={restaurant_id}: {e!r}")


def _told(req) -> list:
    try:
        return [n for n in json.loads(req.get("told_json") or "[]") if isinstance(n, str)]
    except (TypeError, ValueError):
        return []


def _note_line(req):
    return [f"Their note: {req['decision_note']}"] if req.get("decision_note") else []


def _broadcast_open(restaurant_id, req, db_path, opener_line):
    who, when, role = (req.get("employee_name") or "").strip(), _when(req), req.get("role") or ""
    takers = _who_could_take(restaurant_id, req, db_path)
    told = _tell_staff(restaurant_id, takers, f"Open shift: {when}",
                       [opener_line,
                        "It's yours if you want it — pick it up in the Cavnar AI app (Requests) before someone else does."],
                       db_path)
    _remember_told(restaurant_id, req, told, db_path)
    if not told:
        _tell_managers(restaurant_id, "Open shift, nobody told",
                       (f"{who}'s {when}" if who else f"The extra {when}") + (f" {role}" if role else "")
                       + " is open, and no teammate who could take it could be reached — assign someone from Labor.",
                       db_path)


def _notify(restaurant_id, event, req, db_path=DB_PATH):
    if not req:
        return
    _ctx.req = req
    try:
        _notify_event(restaurant_id, event, req, db_path)
    finally:
        _ctx.req = None


def _notify_event(restaurant_id, event, req, db_path):
    try:
        who, when, role = req.get("employee_name") or "", _when(req), req.get("role") or ""
        reason = f" — “{req['reason']}”" if req.get("reason") else ""
        if event == "drop_asked":
            # The employee's reason rides in the decider's push (COM-14).
            _tell_managers(restaurant_id, "Shift drop request",
                           f"{who} asked to drop {when}" + (f" ({role})" if role else "") + reason
                           + ". Approve or decline it in Labor.", db_path, req=req)
        elif event == "opened":
            # They are still on the published week until someone claims it:
            # "Your shift is off your schedule" was false (WF-06 / LG-04).
            _email_staff(restaurant_id, [who], "Your drop is approved — you're still on it for now",
                         [f"Your manager approved your request to drop {when}. You're still on it until someone "
                          "picks it up — we'll tell you when they do."] + _note_line(req), db_path)
            _broadcast_open(restaurant_id, req, db_path, f"{who} can't work {when}" + (f" ({role})" if role else "") + ".")
        elif event == "posted":
            if who.strip():
                _email_staff(restaurant_id, [who], "Your shift is posted as open",
                             [f"Your manager posted your {when} as an open shift. You're still on it until someone "
                              "picks it up — we'll tell you when they do."] + _note_line(req), db_path)
            _broadcast_open(restaurant_id, req, db_path,
                            (f"{who}'s {when}" if who.strip() else f"An extra {when} shift")
                            + (f" ({role})" if role else "") + " is open." + (f" {req['decision_note']}" if req.get("decision_note") else ""))
        elif event == "denied":
            _email_staff(restaurant_id, [who], "Your shift request was declined",
                         [f"Your manager kept you on {when}."]
                         + (_note_line(req) or ["Talk to them if that is a problem."]), db_path)
        elif event == "covered":
            taker = req.get("replacement_name") or ""
            if who.strip():
                _email_staff(restaurant_id, [who], "Your shift is covered",
                             [f"{taker} is working your {when}. You are off it."] + _note_line(req), db_path)
            _email_staff(restaurant_id, [taker], "You picked up a shift",
                         [f"You are on {when}" + (f" ({role})" if role else "")
                          + (f", covering for {who}." if who.strip() else ".")], db_path)
            _tell_managers(restaurant_id, "Shift covered",
                           (f"{taker} is covering {who}'s {when}" if who.strip() else f"{taker} took the extra {when}")
                           + (f" ({role})" if role else "") + ".", db_path)
            # Teammates told it was open, other than the taker: it's gone.
            gone = [n for n in _told(req) if _key(n) != _key(taker)]
            if gone:
                _email_staff(restaurant_id, gone, f"Open shift taken: {when}",
                             [f"{taker} picked up the open {when}. Nothing for you to do."], db_path)
        elif event == "swap_asked":
            _email_staff(restaurant_id, [req.get("target_name")], f"{who} asked to swap shifts",
                         [f"{who} would like to trade their {when} for your {_when(req, 'target_')}.",
                          "Nothing moves unless you say yes — answer in the Cavnar AI app (Requests)."], db_path)
            # A colleague with no app login can't say yes themselves: the
            # decider can record a yes given in person (LG-12).
            off_app = "" if _has_login(restaurant_id, req.get("target_name"), db_path) else (
                f" {req.get('target_name')} isn't on the Cavnar AI app — if they agree in person, mark it in Labor.")
            _tell_managers(restaurant_id, "Shift swap request",
                           f"{who} asked to trade their {when} for {req.get('target_name')}'s {_when(req, 'target_')}"
                           + reason + ". Approve or decline it in Labor." + off_app, db_path, req=req)
        elif event == "swap_agreed":
            # The colleague's yes before the manager has answered (WF-09).
            target = req.get("target_name") or ""
            said = "agreed in person" if req.get("agreed_by_manager") else "said yes"
            _email_staff(restaurant_id, [who], f"{target} {said} to your swap",
                         [f"{target} {said} to trading your {when} for their {_target_when(req)}. "
                          "It's waiting on your manager now."], db_path)
            if not req.get("agreed_by_manager"):
                _tell_managers(restaurant_id, "Swap agreed — your answer next",
                               f"{target} said yes to trading their {_target_when(req)} for {who}'s {when}. "
                               "Approve or decline it in Labor.", db_path, req=req)
        elif event == "swap_approved":
            _email_staff(restaurant_id, [req.get("target_name")], "A swap is waiting on your yes",
                         [f"Your manager approved {who}'s request to trade their {when} for your {_when(req, 'target_')}.",
                          "It only goes ahead if you accept it in the Cavnar AI app (Requests)."], db_path)
        elif event == "swap_declined":
            _email_staff(restaurant_id, [who], "Your swap was declined",
                         [f"{req.get('target_name')} said no to trading for your {when}. You are still on it."], db_path)
            # The deciders hold an Approve button for it (pending) or are
            # waiting on it (approved): it's off (LG-29).
            _tell_managers(restaurant_id, "Swap declined",
                           f"{req.get('target_name')} said no to {who}'s swap of {when}. {who} is still on it.", db_path)
        elif event == "swapped":
            other = req.get("target_name") or ""
            _email_staff(restaurant_id, [who], "Your swap went through",
                         [f"You now work {_when(req, 'target_')} instead of {when}."], db_path)
            _email_staff(restaurant_id, [other], "Your swap went through",
                         [f"You now work {when} instead of {_when(req, 'target_')}."], db_path)
            _tell_managers(restaurant_id, "Shifts swapped", f"{who} and {other} traded {when} and {_when(req, 'target_')}.", db_path)
        elif event == "withdrawn":
            prior = req.get("prior_status")
            if (req.get("kind") or "") == "swap":
                _email_staff(restaurant_id, [req.get("target_name")], "A swap request was withdrawn",
                             [f"{who} withdrew their request to trade for your {_when(req, 'target_')}. Nothing changes."],
                             db_path)
                _tell_managers(restaurant_id, "Swap withdrawn",
                               f"{who} withdrew their swap of {when} with {req.get('target_name')}. Nothing to answer.",
                               db_path)
            elif prior == "open":
                gone = _told(req)
                if gone:
                    _email_staff(restaurant_id, gone, f"Open shift gone: {when}",
                                 [f"{who} is working {when} after all, so it's no longer open."], db_path)
                _tell_managers(restaurant_id, "Open shift withdrawn",
                               f"{who} took back their drop — they're working {when}.", db_path)
            else:
                _tell_managers(restaurant_id, "Drop request withdrawn",
                               f"{who} withdrew their request to drop {when}. Nothing to answer.", db_path)
        elif event == "open_cancelled":
            if who.strip():
                _email_staff(restaurant_id, [who], "Your shift is off the open board",
                             [f"Your manager took {when} off the open board. Check your schedule in the app."], db_path)
            gone = _told(req)
            if gone:
                _email_staff(restaurant_id, gone, f"Open shift gone: {when}",
                             [f"The open {when} isn't available any more."], db_path)
        elif event == "cancelled":
            # A request whose shift changed hands under it (LG-10).
            _email_staff(restaurant_id, [who], "Your shift request no longer applies",
                         [f"Your request about {when} was closed: that shift changed hands before it was answered. "
                          "Check your schedule in the app."], db_path)
            if (req.get("kind") or "") == "swap" and req.get("status") != "pending":
                _email_staff(restaurant_id, [req.get("target_name")], "A swap request no longer applies",
                             [f"{who}'s swap for your {_when(req, 'target_')} was closed: a shift in it changed hands."],
                             db_path)
        elif event == "unclaimed":
            left = req.get("hours_left")
            _tell_managers(restaurant_id, "Open shift still unclaimed",
                           (f"{who}'s {when}" if who.strip() else f"The extra {when}") + (f" ({role})" if role else "")
                           + (f" starts in about {left} hours" if left else " starts soon")
                           + " and nobody has picked it up."
                           + (f" {who} is still on the schedule for it." if who.strip() else "")
                           + " Assign someone or offer it in Labor.", db_path)
    except Exception as e:
        print(f"[shift_requests] notify {event} failed rid={restaurant_id}: {e!r}")


def _target_when(req):
    return _when(req, "target_")


def _notify_offer(restaurant_id, event, offer, req, db_path, channel=None):
    """Notices about one offer. Returns people.tell's channel for "offered"
    (None when it reached nobody). Never raises."""
    try:
        name, when, role = offer.get("name") or "", _when(req or {}), (req or {}).get("role") or ""
        holder = ((req or {}).get("employee_name") or "").strip()
        if event == "offered":
            import people as _people
            lines = [f"Your manager is asking if you can work {when}" + (f" ({role})" if role else "")
                     + (f", covering for {holder}." if holder else "."),
                     "Answer in the Cavnar AI app (Requests) — Accept puts you on it, checked against your hours and availability."]
            if offer.get("note"):
                lines.append(f"Their note: {offer['note']}")
            return _people.tell(restaurant_id, name, "Can you take a shift?", lines, email_type="shift_request",
                                channel=channel, db_path=db_path, **_tell_extras(req, offer))
        if event == "offer_declined":
            _tell_managers(restaurant_id, "Offer declined",
                           f"{name} can't take {when}" + (f" ({role})" if role else "") + ". Offer it to someone else in Labor.",
                           db_path)
        elif event == "offer_taken":
            _email_staff(restaurant_id, [name], f"Shift taken: {when}",
                         [f"The {when} you were offered is covered now. Nothing for you to do."], db_path)
        elif event == "offer_withdrawn":
            _email_staff(restaurant_id, [name], f"Offer withdrawn: {when}",
                         [f"The {when} you were offered isn't open any more. Nothing for you to do."], db_path)
    except Exception as e:
        print(f"[shift_requests] offer notice {event} failed rid={restaurant_id}: {e!r}")
    return None
