"""
availability_changes.py — a cut in somebody's availability waits for a
manager and takes effect on a date (schedule audit 10/10/26, MARKET-15).

A server who removed every weekend on Tuesday reshaped Thursday's auto-draft
with no manager in the loop: staff_settings.save_own_availability wrote it
straight into the availability the draft reads. Now an employee's own save is
split, day by day, into what gives the restaurant MORE of them (a day back,
a wider window — applied at once, as before) and what gives it LESS (a day
off, a narrower window). The cut is kept here, pending, with the date it
would take effect — by default the first week nobody has drafted yet that is
at least the restaurant's notice days out — and:

  - with approval on (`request_autonomy.policy` "availability_approval",
    on by default), a manager approves (optionally moving the date) or denies
    it in Waiting on you / the Studio's Setup stage; the employee is told;
  - with approval off, it is approved at once for that date.

An approved change is in force from its date: build_constraints lays it over
the stored availability for every date of a drafted week on or after it
(`overlay`), so a week drafted now uses Saturday as available and next
month's as not; on the date itself the hourly open_shift_watch writes it into
the stored availability (`apply_due`) and it is just the person's availability.
"""
import json
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
STATUSES = ("pending", "approved", "denied", "withdrawn", "applied")


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def init_availability_changes(db_path=None):
    """Boot DDL (models.init_db) — never on a request path."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS staff_availability_changes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            employee_name   TEXT    NOT NULL,
            days_json       TEXT    NOT NULL,
            summary         TEXT,
            effective_from  TEXT    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'pending',
            source          TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            decided_by      TEXT,
            decided_at      TEXT,
            decision_note   TEXT,
            applied_at      TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_avail_changes_rest ON staff_availability_changes"
                     "(restaurant_id, status, effective_from)")
        # ops.prune_ledgers deletes by created_at (the retention registry).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_avail_changes_created ON staff_availability_changes(created_at)")
        conn.commit()
    finally:
        conn.close()


def _today(restaurant_id, today=None):
    if today:
        return today
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id, naive=True).date()
    except Exception:
        return date.today()


def _key(name) -> str:
    return " ".join(str(name or "").split()).casefold()


# ── what is a cut ────────────────────────────────────────────────────────────

def _state(off, bounds, windows, d):
    """One weekday as ("off", bounds) | ("window", (lo, hi)) | ("any", None)."""
    from schedule_rules import parse_minutes
    if d in (off or ()):
        b = (bounds or {}).get(d) or {}
        return ("off", {"from": b.get("from"), "until": b.get("until")} if (b.get("from") or b.get("until")) else None)
    w = (windows or {}).get(d)
    if w and (w.get("earliest") or w.get("latest")):
        return ("window", (parse_minutes(w.get("earliest") or ""), parse_minutes(w.get("latest") or ""),
                           w.get("from") or None, w.get("until") or None))
    return ("any", None)


def is_cut(before, after) -> bool:
    """Whether `after` (a weekday's state) gives the restaurant less of the
    person than `before`: a day off that wasn't, a window where there was
    none, a narrower one, an off day with wider dates. A day back, a wider
    window or no change is not a cut."""
    bk, bv = before
    ak, av = after
    if ak == "any":
        return False
    if ak == "off":
        if bk != "off":
            return True
        if bv is None:
            return False                       # off for good already
        if av is None:
            return True                        # dated off became off for good
        return bool((av.get("from") or "") < (bv.get("from") or "") or
                    (bv.get("until") and (not av.get("until") or av["until"] > bv["until"])))
    # after is a window
    if bk == "off":
        return False
    if bk == "any":
        return True
    alo, ahi = av[0], av[1]
    blo, bhi = bv[0], bv[1]
    narrower = ((alo is not None and (blo is None or alo > blo)) or
                (ahi is not None and (bhi is None or ahi < bhi)))
    return bool(narrower or (av[2], av[3]) != (bv[2], bv[3]))


def split(before_state: dict, off, bounds, windows) -> tuple:
    """(cut_days, kept): the weekdays whose new state is a cut, and the
    target {off, bounds, windows} with those days kept as they were — what is
    written at once. `before_state` is staff_settings._state_of's."""
    from staff_settings import DAYS as _D
    cuts = []
    b_off, b_bounds, b_windows = before_state["off"], before_state["bounds"], before_state["windows"]
    for d in _D:
        if is_cut(_state(b_off, b_bounds, b_windows, d), _state(off, bounds, windows, d)):
            cuts.append(d)
    kept_off = {d for d in _D if (d in b_off if d in cuts else d in off)}
    kept_bounds = {}
    for d in kept_off:
        src = b_bounds if d in cuts else (bounds or {})
        if src.get(d):
            kept_bounds[d] = src[d]
    kept_windows = {}
    for d in _D:
        src = b_windows if d in cuts else (windows or {})
        if d in kept_off:
            continue
        if src.get(d):
            kept_windows[d] = src[d]
    return cuts, {"off": kept_off, "bounds": kept_bounds, "windows": kept_windows}


def describe(days: dict) -> str:
    """"Saturdays off; Sundays not before 5:00pm" for a change's days."""
    parts = []
    for d in DAYS:
        s = (days or {}).get(d)
        if not s:
            continue
        if s.get("status") == "off":
            parts.append(f"{d}s off")
        elif s.get("status") == "window":
            lo, hi = s.get("earliest"), s.get("latest")
            parts.append(f"{d}s " + (f"{lo}–{hi}" if lo and hi else (f"not before {lo}" if lo else f"not after {hi}")))
    return "; ".join(parts)


# ── the date a cut takes effect ──────────────────────────────────────────────

def effective_default(restaurant_id, today=None, db_path=DB_PATH) -> str:
    """The date a cut takes effect by default: the start of the first week
    (the restaurant's own week start day) nobody has drafted or published
    yet, and never sooner than its notice days — a cut never reshapes a week
    already built. A week under way that nobody built takes it from today
    (plus the notice). ISO date."""
    today = _today(restaurant_id, today)
    try:
        r = _models_mod.get_restaurant(restaurant_id, db_path or _models_mod.DB_PATH)
        start_dow = int(getattr(r, "week_start_day", 0) or 0)
    except Exception:
        start_dow = 0
    try:
        import schedule_rules as _sr
        notice = int(float(_sr.compliance(restaurant_id).get("notice_days") or 0))
    except Exception:
        notice = 0
    floor = today + timedelta(days=max(0, notice))
    first = floor - timedelta(days=(floor.weekday() - start_dow) % 7)      # the week the floor falls in
    conn = get_conn(db_path)
    try:
        built = [(str(a)[:10], str(b)[:10]) for a, b in conn.execute(
            "SELECT week_start, week_end FROM schedule_history WHERE restaurant_id=? AND superseded_by IS NULL "
            "AND week_end >= ?", (restaurant_id, first.isoformat())).fetchall()]
    except Exception:
        built = []
    finally:
        conn.close()
    # After the last week anybody built: a gap week before a built one is not
    # "not yet drafted" for a cut that would reach the built one too.
    last_end = max((b for _a, b in built), default=None)
    if last_end:
        after = date.fromisoformat(last_end) + timedelta(days=1)
        start = after - timedelta(days=(after.weekday() - start_dow) % 7)
        if start < after:
            start += timedelta(days=7)
        first = max(first, start)
    return max(first, floor).isoformat()


# ── the record ───────────────────────────────────────────────────────────────

def _day_payload(state_off, state_bounds, state_windows, d) -> dict:
    """One cut weekday as filed: off, or a window, with the person's own
    dates (`from` later than the change's date holds from then)."""
    if d in state_off:
        b = (state_bounds or {}).get(d) or {}
        return {"status": "off", "from": b.get("from"), "until": b.get("until")}
    w = (state_windows or {}).get(d) or {}
    return {"status": "window", "earliest": w.get("earliest"), "latest": w.get("latest"), "from": w.get("from"),
            "until": w.get("until")}


def record(restaurant_id, employee_name, cuts, off, bounds, windows, source="app", today=None, db_path=DB_PATH,
           approval=True) -> dict:
    """File the cut days as one change (any earlier pending change of theirs
    is replaced — the newest ask is the one asked). Pending with approval on;
    approved at once for its date with it off. Returns the row."""
    days = {d: _day_payload(off, bounds, windows, d) for d in cuts}
    eff = effective_default(restaurant_id, today=today, db_path=db_path)
    name = " ".join(str(employee_name or "").split())
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE staff_availability_changes SET status='withdrawn', decided_at=datetime('now'), "
                     "decision_note='Replaced by a newer change' WHERE restaurant_id=? AND lower(employee_name)=? "
                     "AND status='pending'", (restaurant_id, name.lower()))
        cur = conn.execute(
            "INSERT INTO staff_availability_changes (restaurant_id, employee_name, days_json, summary, effective_from, "
            "status, source, decided_by, decided_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (restaurant_id, name, json.dumps(days), describe(days)[:300], eff,
             "pending" if approval else "approved", source,
             None if approval else "the restaurant's rule (no approval needed)",
             None if approval else datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        row = dict(conn.execute("SELECT * FROM staff_availability_changes WHERE id=?", (cur.lastrowid,)).fetchone())
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return _shape(row)


def _shape(row) -> dict:
    out = dict(row)
    try:
        out["days"] = json.loads(row.get("days_json") or "{}")
    except (TypeError, ValueError):
        out["days"] = {}
    out.pop("days_json", None)
    return out


def pending(restaurant_id, db_path=DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM staff_availability_changes WHERE restaurant_id=? AND status='pending' "
                            "ORDER BY created_at", (restaurant_id,)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return [_shape(dict(r)) for r in rows]


def recent(restaurant_id, db_path=DB_PATH, limit=30) -> list:
    """Pending first, then what is approved and not yet in force, then the
    last decided — the manager's list."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM staff_availability_changes WHERE restaurant_id=? AND (status IN ('pending','approved') "
            "OR created_at >= datetime('now','-30 days')) ORDER BY CASE status WHEN 'pending' THEN 0 "
            "WHEN 'approved' THEN 1 ELSE 2 END, created_at DESC LIMIT ?", (restaurant_id, int(limit))).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return [_shape(dict(r)) for r in rows]


def mine(restaurant_id, employee_name, db_path=DB_PATH) -> list:
    """This person's own changes still waiting or not yet in force."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM staff_availability_changes WHERE restaurant_id=? AND lower(employee_name)=? "
                            "AND status IN ('pending','approved') ORDER BY created_at DESC",
                            (restaurant_id, " ".join(str(employee_name or "").split()).lower())).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return [_shape(dict(r)) for r in rows]


class ChangeError(ValueError):
    pass


def decide(restaurant_id, change_id, approve, decided_by=None, effective_from=None, note=None, db_path=DB_PATH,
           today=None) -> dict:
    """A manager's answer: approved for its date (or the date given, never
    in the past) or denied. The person is told. A change whose date has
    come is put in force at once. Returns the row, or None when it is not
    pending here."""
    eff = None
    if approve and effective_from:
        try:
            eff = date.fromisoformat(str(effective_from)[:10])
        except ValueError:
            raise ChangeError("That isn't a date.")
        if eff < _today(restaurant_id, today):
            raise ChangeError("The date it starts can't be in the past.")
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE staff_availability_changes SET status=?, decided_by=?, decided_at=datetime('now'), "
            "decision_note=?, effective_from=COALESCE(?, effective_from) WHERE id=? AND restaurant_id=? "
            "AND status='pending'",
            ("approved" if approve else "denied", (decided_by or "")[:120] or None, (note or "").strip()[:200] or None,
             eff.isoformat() if eff else None, int(change_id), restaurant_id))
        conn.commit()
        if cur.rowcount != 1:
            return None
        row = _shape(dict(conn.execute("SELECT * FROM staff_availability_changes WHERE id=?",
                                       (int(change_id),)).fetchone()))
    finally:
        conn.close()
    _tell(restaurant_id, row, db_path)
    if approve:
        apply_due(restaurant_id, today=today, db_path=db_path)
        # A date the manager moved into a week staff already have: the shifts
        # it rules out are named with the answer, to cover or move.
        row["conflicts"] = published_conflicts(restaurant_id, row, db_path=db_path)
    return row


def published_conflicts(restaurant_id, change, db_path=DB_PATH) -> list:
    """The person's shifts on live published weeks from the change's date
    that its cut days rule out ([{date, shift_start, shift_end, role,
    reason}]). Never raises: [] when they can't be read."""
    try:
        import staff_settings as _ss
        import time_off as _to
        eff = date.fromisoformat(str(change["effective_from"])[:10])
        days = change.get("days") or {}
        off = {d for d, s in days.items() if s.get("status") == "off"}
        bounds = {d: {"from": s.get("from"), "until": s.get("until")} for d, s in days.items()
                  if s.get("status") == "off" and (s.get("from") or s.get("until"))}
        wins = {d: {k: s.get(k) for k in ("earliest", "latest", "from", "until") if s.get(k)}
                for d, s in days.items() if s.get("status") == "window"}
        shifts = _to.published_conflicts(restaurant_id, change["employee_name"], eff, eff + timedelta(days=70),
                                         db_path=None if db_path in (None, DB_PATH) else db_path)
        return _ss._conflicts(shifts, {"off": off, "bounds": bounds, "windows": wins})
    except Exception as e:
        print(f"[availability_changes] conflicts unread rid={restaurant_id}: {e!r}")
        return []


def _tell(restaurant_id, row, db_path):
    """The person hears the answer (people.tell). Never raises."""
    try:
        import people
        from time_utils import mdy
        if row["status"] == "approved":
            title = "Your availability change is approved"
            lines = [f"From {mdy(row['effective_from'])}: {row.get('summary') or 'your new availability'}."]
        else:
            title = "Your availability change wasn't approved"
            lines = [f"Your manager kept your availability as it was ({row.get('summary') or 'the change you asked for'}). "
                     "Talk to them if that is a problem."]
        if row.get("decision_note"):
            lines.append(f"Their note: {row['decision_note']}")
        people.tell(restaurant_id, row["employee_name"], title, lines, email_type="staff_request",
                    db_path=db_path if db_path not in (None, DB_PATH) else _models_mod.DB_PATH)
    except Exception as e:
        print(f"[availability_changes] decision notice failed rid={restaurant_id}: {e!r}")


def tell_deciders(restaurant_id, row, db_path=DB_PATH):
    """The managers hear a cut is waiting on them — no Approve button on the
    push (it is answered in Waiting on you). Never raises."""
    try:
        import strategy_jobs
        from permissions import SCHEDULE_PUBLISH
        from time_utils import mdy
        body = (f"{row['employee_name']} asked to change their availability from {mdy(row['effective_from'])}: "
                f"{row.get('summary') or 'less availability'}. Approve or deny it in Labor — nothing changes "
                "until you do.")
        strategy_jobs._reach(restaurant_id, "shift_request", "Availability change to approve", body, {"tab": "labor"},
                             db_path, lines=[body], deciders=True, permissions=[SCHEDULE_PUBLISH])
    except Exception as e:
        print(f"[availability_changes] decider notice failed rid={restaurant_id}: {e!r}")


# ── in force ─────────────────────────────────────────────────────────────────

def approved_ahead(restaurant_id, until_iso, db_path=DB_PATH) -> list:
    """Approved changes not yet written into the stored availability whose
    date is on or before `until_iso`."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM staff_availability_changes WHERE restaurant_id=? AND status='approved' "
                            "AND effective_from <= ? ORDER BY effective_from, id", (restaurant_id, until_iso)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return [_shape(dict(r)) for r in rows]


def overlay(c, restaurant_id, db_path=DB_PATH):
    """Lay every approved change not yet in force over a Constraints for the
    dates of its week on or after the change's date (build_constraints): an
    off day blocks those dates, a window blocks the hours outside it. The
    stored availability already holds for the dates before."""
    from schedule_rules import LABELS, parse_minutes, _fmt_minutes
    from time_utils import mdy
    if not c.week_dates:
        return
    for ch in approved_ahead(restaurant_id, max(c.week_dates), db_path=db_path):
        key = c.key(ch["employee_name"])
        for d in c.week_dates:
            if d < ch["effective_from"]:
                continue
            try:
                wd = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
            except (TypeError, ValueError):
                continue
            s = (ch.get("days") or {}).get(wd)
            if not s or (s.get("until") and d > s["until"]) or (s.get("from") and d < s["from"]):
                continue
            if s.get("status") == "off":
                c.blocked_dates.setdefault(key, {}).setdefault(d, LABELS["unavailable_day"])
            elif s.get("status") == "window":
                lo, hi = parse_minutes(s.get("earliest") or ""), parse_minutes(s.get("latest") or "")
                said = f"{LABELS['outside_window']} (from {mdy(ch['effective_from'])})"
                if lo is not None:
                    c.blocked_parts.setdefault(key, {}).setdefault(d, []).append(
                        {"from": None, "until": lo, "daypart": None, "reason": f"{said}, not before {_fmt_minutes(lo)}"})
                if hi is not None:
                    hi2 = hi if lo is None or hi > lo else hi + 24 * 60
                    c.blocked_parts.setdefault(key, {}).setdefault(d, []).append(
                        {"from": hi2, "until": None, "daypart": None, "reason": f"{said}, not after {_fmt_minutes(hi)}"})


def apply_due(restaurant_id, today=None, db_path=DB_PATH) -> int:
    """Write every approved change whose date has come into the stored
    availability (the weekday's off flag or window), mark it applied.
    Returns how many. Never raises (a change left is applied next hour)."""
    today = _today(restaurant_id, today)
    n = 0
    for ch in approved_ahead(restaurant_id, today.isoformat(), db_path=db_path):
        try:
            _apply(restaurant_id, ch, db_path)
            n += 1
        except Exception as e:
            print(f"[availability_changes] change {ch.get('id')} not applied rid={restaurant_id}: {e!r}")
    return n


def _apply(restaurant_id, ch, db_path):
    import staff_settings as _ss
    from models import staff_availability_for, write_staff_availability
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = staff_availability_for(restaurant_id, ch["employee_name"], conn=conn)
        ss_name, windows = _ss._windows_for(restaurant_id, ch["employee_name"], conn)
        st = _ss._state_of(row, windows)
        off, bounds, wins = set(st["off"]), dict(st["bounds"]), dict(windows or {})
        for d, s in (ch.get("days") or {}).items():
            if d not in DAYS:
                continue
            # The person's own later start date still holds once in force.
            own_from = s.get("from") if (s.get("from") or "") > str(ch.get("effective_from") or "") else None
            if s.get("status") == "off":
                off.add(d)
                bounds.pop(d, None)
                if s.get("until") or own_from:
                    bounds[d] = {"from": own_from, "until": s.get("until")}
                wins.pop(d, None)
            elif s.get("status") == "window":
                off.discard(d)
                bounds.pop(d, None)
                w = {"earliest": s.get("earliest"), "latest": s.get("latest")}
                if s.get("until"):
                    w["until"] = s["until"]
                if own_from:
                    w["from"] = own_from
                wins[d] = w
        write_staff_availability(conn, restaurant_id, row["employee_name"] if row else ch["employee_name"], off,
                                 (row or {}).get("notes"), bounds, previous=(row or {}).get("updated_at"))
        if ss_name:
            conn.execute("UPDATE staff_settings SET time_windows=?, updated_at=datetime('now') WHERE restaurant_id=? "
                         "AND employee_name=?", (json.dumps(wins), restaurant_id, ss_name))
        elif wins:
            conn.execute("INSERT INTO staff_settings (restaurant_id, employee_name, time_windows, updated_by, updated_at) "
                         "VALUES (?,?,?,?,datetime('now'))",
                         (restaurant_id, ch["employee_name"], json.dumps(wins), "availability change"))
        conn.execute("UPDATE staff_availability_changes SET status='applied', applied_at=datetime('now') WHERE id=? "
                     "AND status='approved'", (ch["id"],))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def restaurants_due(db_path=DB_PATH) -> list:
    """Restaurant ids with an approved change whose date has come or is near
    (open_shift_watch's sweep)."""
    conn = get_conn(db_path)
    try:
        return sorted({r[0] for r in conn.execute(
            "SELECT DISTINCT restaurant_id FROM staff_availability_changes WHERE status='approved' "
            "AND effective_from <= date('now', '+1 day')").fetchall()})
    except Exception:
        return []
    finally:
        conn.close()
