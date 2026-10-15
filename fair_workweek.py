"""
fair_workweek.py — the written artifacts a predictive-scheduling law asks
for beyond the schedule itself (schedule audit 10/10/26 MARKET-4, RM-15):

  * GOOD-FAITH ESTIMATES. Chicago, Philadelphia and Evanston want a written
    estimate at hire of the hours, days and times a new employee can expect
    over their first 90 days (Seattle and Oregon a median; San Francisco
    the minimum shifts a month; New York City a regular schedule). The
    draft is computed from what the product knows — the person's role,
    their weeks worked, the shifts they usually hold, their availability
    and the hours they asked for — the owner edits and issues it, and the
    person acknowledges it in the staff app or portal (time-stamped).
  * WRITTEN ANSWERS to a request to change that estimate: within 3 days in
    Chicago and Evanston. A request is a row with its due time; the owner's
    answer is written, stamped and shown to the person.
  * ACCESS TO HOURS: additional hours offered to existing qualified staff
    before anyone new (posted 72 hours in New York City, Seattle and
    Philadelphia; 72 hours to accept in San Francisco and Evanston, 24 in
    Berkeley; Chicago's deadline is the owner's to set). The offer is an
    open shift (shift_requests) with its posting window on record; hours
    added straight to a person's week, or a new hire put on the roster,
    while no offer ran are named to the owner.

Tables (created at boot, kept three years with the scheduling records):
good_faith_estimates, fww_requests, fww_access_offers. The law that holds
the restaurant comes from compliance_packs; a restaurant none covers gets
these screens as optional good practice, never as a breach.
"""
import json
import statistics
from datetime import date, datetime, timedelta

from models import DB_PATH
import models as _models


class FairWorkweekError(ValueError):
    pass


def get_conn(db_path=None):
    return _models.get_conn(db_path or DB_PATH)


def init_fair_workweek(db_path=None):
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS good_faith_estimates (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id    INTEGER NOT NULL REFERENCES restaurants(id),
            person_id        INTEGER,
            employee_name    TEXT    NOT NULL,
            employee_key     TEXT    NOT NULL,
            role             TEXT,
            hours_low        REAL,
            hours_high       REAL,
            average_hours    REAL,
            median_monthly_hours REAL,
            min_shifts_month INTEGER,
            days_json        TEXT,
            on_call          TEXT,
            locations        TEXT,
            period_days      INTEGER,
            text             TEXT    NOT NULL,
            basis_json       TEXT,
            jurisdiction     TEXT,
            issued_at        TEXT    NOT NULL DEFAULT (datetime('now')),
            issued_by        TEXT,
            acknowledged_at  TEXT,
            superseded_at    TEXT,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gfe_person ON good_faith_estimates(restaurant_id, employee_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gfe_created ON good_faith_estimates(created_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS fww_requests (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id    INTEGER NOT NULL REFERENCES restaurants(id),
            employee_name    TEXT    NOT NULL,
            employee_key     TEXT    NOT NULL,
            kind             TEXT    NOT NULL,            -- estimate_change | schedule_preference
            estimate_id      INTEGER,
            body             TEXT    NOT NULL,
            due_at           TEXT,
            jurisdiction     TEXT,
            status           TEXT    NOT NULL DEFAULT 'open',   -- open | granted | modified | declined | withdrawn
            answer           TEXT,
            answered_by      TEXT,
            answered_at      TEXT,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fww_requests_open ON fww_requests(restaurant_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fww_requests_created ON fww_requests(created_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS fww_access_offers (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id    INTEGER NOT NULL REFERENCES restaurants(id),
            request_id       INTEGER,
            shift_date       TEXT    NOT NULL,
            shift_start      TEXT,
            shift_end        TEXT,
            role             TEXT,
            hours            REAL,
            offered_to_json  TEXT,
            posted_at        TEXT    NOT NULL DEFAULT (datetime('now')),
            closes_at        TEXT    NOT NULL,
            jurisdiction     TEXT,
            status           TEXT    NOT NULL DEFAULT 'open',   -- open | filled | closed_unfilled | cancelled
            filled_by        TEXT,
            filled_at        TEXT,
            created_by       TEXT,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fww_access_open ON fww_access_offers(restaurant_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fww_access_created ON fww_access_offers(created_at)")
        conn.commit()
    finally:
        conn.close()


def _key(n):
    return " ".join(str(n or "").lower().split())


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _law(restaurant, key, on=None):
    """(code, label, value dict) of the fact `key` from the predictive law
    that holds the restaurant, else (None, None, None)."""
    import compliance_packs as cp
    code, e = cp.predictive_entry(restaurant, on)
    if not e:
        return None, None, None
    v = cp.value_on(e.get(key), on)
    return code, e.get("label"), ((v or {}).get("value") if v else None)


# ── good-faith estimates ───────────────────────────────────────────────────

def _published_rows(restaurant_id, weeks=12, db_path=None):
    """[(week_start, rows)] of the weeks staff were SENT — each week's live
    copy's PUBLISHED rows (models.live_where / live_csv_sql; schedule
    re-audit 10/11/26 BUGSLIFE-17). It read the working copy of any
    published row nothing superseded, so an estimate given to a new hire
    could count a manager's unsent edits as the published schedule."""
    import schedule_shifts
    since = (date.today() - timedelta(days=7 * weeks)).isoformat()
    conn = get_conn(db_path)
    try:
        hist = conn.execute(f"SELECT h.id, h.week_start FROM schedule_history h "
                            f"WHERE h.restaurant_id=? AND {_models.live_where('h')} AND h.week_start>=? "
                            "ORDER BY h.week_start", (restaurant_id, since)).fetchall()
        # Their published rows from the shift table (schedule_shifts —
        # schedule re-audit 10/11/26 ARCH-7).
        by_week = schedule_shifts.rows_by_week([h["id"] for h in hist], "live", conn=conn)
    finally:
        conn.close()
    return [(h["week_start"], by_week.get(int(h["id"])) or []) for h in hist]


def draft_estimate(restaurant_id, employee_name, db_path=None) -> dict:
    """The estimate as the product would write it, from what it knows —
    for the owner to edit and issue. {"employee", "role", "hours_low",
    "hours_high", "average_hours", "median_monthly_hours",
    "min_shifts_month", "days": [{"day", "start", "end"}], "on_call",
    "period_days", "basis": [...], "law", "text"}."""
    from schedule_versions import rows_from_csv  # noqa: F401 (keeps the reader's import local)
    r = _models.get_restaurant(restaurant_id, db_path or DB_PATH)
    if not r:
        raise FairWorkweekError("restaurant not found")
    name = " ".join(str(employee_name or "").split())
    if not name:
        raise FairWorkweekError("who is the estimate for?")
    key = _key(name)
    weeks = _published_rows(restaurant_id, db_path=db_path)
    per_week, shifts_week, slots, roles = [], [], {}, {}
    for ws, rows in weeks:
        mine = [x for x in rows if _key(x.get("employee")) == key]
        if not mine:
            continue
        per_week.append(sum(_hours(x) for x in mine))
        shifts_week.append(len(mine))
        for x in mine:
            try:
                day = datetime.strptime(x.get("date"), "%Y-%m-%d").strftime("%A")
            except (TypeError, ValueError):
                continue
            slots.setdefault((day, x.get("shift_start"), x.get("shift_end")), 0)
            slots[(day, x.get("shift_start"), x.get("shift_end"))] += 1
            roles[x.get("role") or ""] = roles.get(x.get("role") or "", 0) + 1
    basis = []
    desired = None
    try:
        import staff_settings as _ss
        st = (_ss.get_all(restaurant_id) or {}).get(name) or {}
        desired = st.get("desired_hours") or st.get("max_hours")
    except Exception:
        st = {}
    if per_week:
        srt = sorted(per_week)
        lo = srt[max(0, int(round(0.25 * (len(srt) - 1))))]
        hi = srt[min(len(srt) - 1, int(round(0.75 * (len(srt) - 1))))]
        avg = round(statistics.mean(per_week), 1)
        basis.append(f"{len(per_week)} published week{'s' if len(per_week) != 1 else ''} they worked")
    elif desired:
        lo = hi = avg = float(desired)
        basis.append("the hours they asked for")
    else:
        lo = hi = avg = None
        basis.append("no weeks on file yet — set the hours yourself")
    n_weeks = max(1, len(per_week))
    days = []
    by_day = {}
    for (day, s, e), n in slots.items():
        if n / n_weeks >= 0.4 and (day not in by_day or n > by_day[day][2]):
            by_day[day] = (s, e, n)
    for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
        if day in by_day:
            days.append({"day": day, "start": by_day[day][0], "end": by_day[day][1]})
    role = max(roles, key=roles.get) if roles else (st.get("role") if isinstance(st, dict) else None)
    code, label, rule = _law(r, "good_faith_estimate")
    period = (rule or {}).get("period_days")
    out = {"employee": name, "role": role or None,
           "hours_low": round(lo, 1) if lo is not None else None, "hours_high": round(hi, 1) if hi is not None else None,
           "average_hours": avg, "median_monthly_hours": round(statistics.median(per_week) * 52 / 12, 1) if per_week else None,
           # the fewest shifts a month they can expect: their quieter weeks
           # (lower quartile of shifts a week) over four weeks
           "min_shifts_month": (int(sorted(shifts_week)[max(0, int(round(0.25 * (len(shifts_week) - 1))))] * 4)
                                if shifts_week else None),
           "days": days, "on_call": "No on-call shifts are expected.", "period_days": period,
           "basis": basis, "law": label, "jurisdiction": code,
           "contents": (rule or {}).get("contents") or ["average_weekly_hours", "days_times", "on_call"]}
    out["text"] = estimate_text(out, restaurant_name=getattr(r, "name", ""))
    return out


def _hours(x):
    from schedule_rules import parse_minutes
    s, e = parse_minutes(x.get("shift_start") or ""), parse_minutes(x.get("shift_end") or "")
    if s is None or e is None:
        return 0.0
    if e <= s:
        e += 24 * 60
    return (e - s) / 60.0


def estimate_text(est: dict, restaurant_name="") -> str:
    """The written estimate in plain words (what the person acknowledges)."""
    parts = []
    period = est.get("period_days")
    lead = f"{restaurant_name}: your good-faith schedule estimate" if restaurant_name else "Your good-faith schedule estimate"
    if period:
        lead += f" for your first {int(period)} days"
    parts.append(lead + ".")
    if est.get("role"):
        parts.append(f"Role: {est['role']}.")
    lo, hi = est.get("hours_low"), est.get("hours_high")
    if lo is not None and hi is not None:
        parts.append(f"Expected hours: {lo:g}" + (f" to {hi:g}" if hi != lo else "") + " a week"
                     + (f" (about {est['average_hours']:g} on average)" if est.get("average_hours") is not None else "")
                     + ".")
    if est.get("median_monthly_hours") is not None and "median_monthly_hours" in (est.get("contents") or []):
        parts.append(f"Median hours a month: {est['median_monthly_hours']:g}.")
    if est.get("min_shifts_month") and "min_shifts_month" in (est.get("contents") or []):
        parts.append(f"At least {int(est['min_shifts_month'])} shifts a month.")
    if est.get("days"):
        parts.append("Usual days and times: " + "; ".join(f"{d['day']} {d.get('start')}–{d.get('end')}"
                                                         for d in est["days"]) + ".")
    if est.get("on_call"):
        parts.append(str(est["on_call"]))
    parts.append("This is an estimate, not a guarantee of hours; you may ask for a change in writing.")
    return " ".join(parts)


def issue_estimate(restaurant_id, estimate: dict, issued_by=None, db_path=None) -> dict:
    """Store an estimate the owner reviewed (the draft, edited), superseding
    the person's earlier one."""
    if not isinstance(estimate, dict):
        raise FairWorkweekError("send the estimate")
    name = " ".join(str(estimate.get("employee") or "").split())
    if not name:
        raise FairWorkweekError("who is the estimate for?")
    r = _models.get_restaurant(restaurant_id, db_path or DB_PATH)
    def num(k):
        v = estimate.get(k)
        if v in (None, ""):
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise FairWorkweekError(f"{k.replace('_', ' ')} must be a number")
        if f < 0 or f > 100:
            raise FairWorkweekError(f"{k.replace('_', ' ')} must be between 0 and 100")
        return round(f, 1)
    lo, hi = num("hours_low"), num("hours_high")
    if lo is not None and hi is not None and hi < lo:
        raise FairWorkweekError("the high end of the hours can't be under the low end")
    days = [d for d in (estimate.get("days") or []) if isinstance(d, dict) and d.get("day")][:14]
    est = dict(estimate, hours_low=lo, hours_high=hi, average_hours=num("average_hours"), days=days)
    text = " ".join(str(estimate.get("text") or "").split())[:2000] or estimate_text(est, getattr(r, "name", ""))
    code, _label, rule = _law(r, "good_faith_estimate")
    key = _key(name)
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE good_faith_estimates SET superseded_at=? WHERE restaurant_id=? AND employee_key=? AND "
                     "superseded_at IS NULL", (_now(), restaurant_id, key))
        try:
            import people
            pid = people.person_id_for(restaurant_id, name, db_path=db_path, create=False)
        except Exception:
            pid = None
        cur = conn.execute(
            "INSERT INTO good_faith_estimates (restaurant_id, person_id, employee_name, employee_key, role, hours_low, "
            "hours_high, average_hours, median_monthly_hours, min_shifts_month, days_json, on_call, locations, "
            "period_days, text, basis_json, jurisdiction, issued_at, issued_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (restaurant_id, pid, name, key, (estimate.get("role") or None), lo, hi, est["average_hours"],
             num("median_monthly_hours"), int(estimate["min_shifts_month"]) if estimate.get("min_shifts_month") else None,
             json.dumps(days), (estimate.get("on_call") or None), (estimate.get("locations") or getattr(r, "name", None)),
             (rule or {}).get("period_days"), text, json.dumps(estimate.get("basis") or []), code, _now(),
             (issued_by or "")[:120] or None))
        conn.commit()
        return _estimate_row(conn.execute("SELECT * FROM good_faith_estimates WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def _estimate_row(r) -> dict:
    d = dict(r)
    try:
        d["days"] = json.loads(d.pop("days_json") or "[]")
    except (TypeError, ValueError):
        d["days"] = []
    try:
        d["basis"] = json.loads(d.pop("basis_json") or "[]")
    except (TypeError, ValueError):
        d["basis"] = []
    return d


def current_estimate(restaurant_id, employee_name, db_path=None):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM good_faith_estimates WHERE restaurant_id=? AND employee_key=? AND "
                         "superseded_at IS NULL ORDER BY id DESC LIMIT 1", (restaurant_id, _key(employee_name))).fetchone()
        return _estimate_row(r) if r else None
    finally:
        conn.close()


def acknowledge_estimate(restaurant_id, estimate_id, employee_name, db_path=None) -> dict:
    """The person signs for their estimate (time-stamped)."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM good_faith_estimates WHERE id=? AND restaurant_id=?",
                         (int(estimate_id), restaurant_id)).fetchone()
        if not r or r["employee_key"] != _key(employee_name):
            raise FairWorkweekError("that estimate isn't yours")
        if not r["acknowledged_at"]:
            conn.execute("UPDATE good_faith_estimates SET acknowledged_at=? WHERE id=?", (_now(), r["id"]))
            conn.commit()
        return _estimate_row(conn.execute("SELECT * FROM good_faith_estimates WHERE id=?", (r["id"],)).fetchone())
    finally:
        conn.close()


def estimates_status(restaurant_id, db_path=None, today=None) -> dict:
    """{"law", "required": bool, "people": [{"employee", "first_seen",
    "estimate": row|None, "new": bool}]} — every active person, with their
    estimate; `new` marks someone first seen in the last 90 days with none
    (the law asks for it at hire)."""
    r = _models.get_restaurant(restaurant_id, db_path or DB_PATH)
    code, label, rule = _law(r, "good_faith_estimate")
    try:
        import staff_settings as _ss
        roster = [p for p in _ss.roster(restaurant_id) if p.get("active", True)]
    except Exception:
        roster = []
    conn = get_conn(db_path)
    try:
        ests = {row["employee_key"]: _estimate_row(row) for row in conn.execute(
            "SELECT * FROM good_faith_estimates WHERE restaurant_id=? AND superseded_at IS NULL", (restaurant_id,))}
        seen = {row["name_key"]: row["created_at"] for row in conn.execute(
            "SELECT name_key, MIN(created_at) AS created_at FROM people WHERE restaurant_id=? GROUP BY name_key",
            (restaurant_id,)).fetchall()} if _has_table(conn, "people") else {}
    finally:
        conn.close()
    t = today or date.today()
    cutoff = (t - timedelta(days=90)).isoformat()
    people = []
    for p in roster:
        k = _key(p.get("name"))
        first = (seen.get(k) or p.get("added_at") or "")[:10] or None
        est = ests.get(k)
        people.append({"employee": p.get("name"), "role": p.get("role"), "first_seen": first, "estimate": est,
                       "new": bool(first and first >= cutoff and not est)})
    return {"law": label, "jurisdiction": code, "required": bool(rule), "rule": rule, "people": people}


def _has_table(conn, name) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


# ── written answers to change requests ─────────────────────────────────────

def request_change(restaurant_id, employee_name, body, kind="estimate_change", db_path=None) -> dict:
    """A person asks in writing to change their estimate (or their usual
    schedule). Its answer is due in the law's days, where it sets them."""
    name = " ".join(str(employee_name or "").split())
    body = " ".join(str(body or "").split())[:1000]
    if not name or not body:
        raise FairWorkweekError("say what you'd like changed")
    if kind not in ("estimate_change", "schedule_preference"):
        raise FairWorkweekError("unknown request")
    r = _models.get_restaurant(restaurant_id, db_path or DB_PATH)
    code, label, rule = _law(r, "change_requests")
    due = None
    if rule and rule.get("answer_days") and (rule.get("applies_to") in (None, "estimate") and kind == "estimate_change"
                                             or rule.get("applies_to") == "any"):
        due = (datetime.utcnow() + timedelta(days=int(rule["answer_days"]))).strftime("%Y-%m-%d %H:%M:%S")
    est = current_estimate(restaurant_id, name, db_path)
    conn = get_conn(db_path)
    try:
        cur = conn.execute("INSERT INTO fww_requests (restaurant_id, employee_name, employee_key, kind, estimate_id, "
                           "body, due_at, jurisdiction) VALUES (?,?,?,?,?,?,?,?)",
                           (restaurant_id, name, _key(name), kind, (est or {}).get("id"), body, due, code))
        conn.commit()
        return dict(conn.execute("SELECT * FROM fww_requests WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def answer_request(restaurant_id, request_id, decision, answer, answered_by=None, db_path=None) -> dict:
    """The owner's written answer: granted, modified or declined, in words."""
    if decision not in ("granted", "modified", "declined"):
        raise FairWorkweekError("answer granted, modified or declined")
    text = " ".join(str(answer or "").split())[:1000]
    if not text:
        raise FairWorkweekError("write the answer — the law asks for it in writing")
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE fww_requests SET status=?, answer=?, answered_by=?, answered_at=? WHERE id=? AND "
                           "restaurant_id=? AND status='open'",
                           (decision, text, (answered_by or "")[:120] or None, _now(), int(request_id), restaurant_id))
        if cur.rowcount != 1:
            raise FairWorkweekError("that request was already answered")
        conn.commit()
        return dict(conn.execute("SELECT * FROM fww_requests WHERE id=?", (int(request_id),)).fetchone())
    finally:
        conn.close()


def requests(restaurant_id, employee_name=None, db_path=None, now=None) -> list:
    """Requests, newest first, each with `overdue` (open past its due)."""
    now_s = (now or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    try:
        q, args = "SELECT * FROM fww_requests WHERE restaurant_id=?", [restaurant_id]
        if employee_name:
            q += " AND employee_key=?"
            args.append(_key(employee_name))
        rows = [dict(r) for r in conn.execute(q + " ORDER BY id DESC LIMIT 200", args).fetchall()]
    finally:
        conn.close()
    for r in rows:
        r["overdue"] = bool(r["status"] == "open" and r.get("due_at") and r["due_at"] < now_s)
    return rows


# ── access to hours ────────────────────────────────────────────────────────

def access_rule(restaurant, on=None):
    """(code, label, rule) — the access-to-hours rule that holds, or Nones."""
    return _law(restaurant, "access_to_hours", on)


def offer_hours(restaurant_id, day, shift_start, shift_end, role=None, hours_window=None, actor=None,
                note=None, db_path=None, now=None) -> dict:
    """Offer additional hours to the current staff first: an open shift
    (shift_requests.post_open_shift — broadcast to who can take it) with its
    posting window on the record. `hours_window` is how long it stays
    offered: the law's (72 in New York City, Seattle, Philadelphia, San
    Francisco, Evanston; 24 in Berkeley) unless the owner sets a longer one;
    Chicago states no fixed period, so the owner must set it."""
    import shift_requests
    r = _models.get_restaurant(restaurant_id, db_path or DB_PATH)
    code, label, rule = access_rule(r)
    law_h = None
    if rule:
        law_h = rule.get("post_hours") or rule.get("accept_hours")
    try:
        hw = float(hours_window) if hours_window not in (None, "") else None
    except (TypeError, ValueError):
        raise FairWorkweekError("how long the offer stays open is a number of hours")
    if hw is None:
        hw = float(law_h) if law_h else None
    if hw is None:
        raise FairWorkweekError("say how long the offer stays open"
                                + (f" — {label}'s law doesn't fix a period" if label else ""))
    if law_h and hw < float(law_h):
        raise FairWorkweekError(f"{label} asks for at least {int(law_h)} hours")
    try:
        posted_req = shift_requests.post_open_shift(restaurant_id, day, shift_start, shift_end=shift_end, role=role,
                                                    actor=actor, note=note, db_path=db_path or DB_PATH, now=now)
    except shift_requests.ShiftRequestError as e:
        raise FairWorkweekError(str(e))
    req = (posted_req or {}).get("request") or {}
    posted = now or datetime.utcnow()
    closes = posted + timedelta(hours=hw)
    from schedule_rules import parse_minutes
    s, e = parse_minutes(shift_start or ""), parse_minutes(shift_end or "")
    hours = None
    if s is not None and e is not None:
        hours = round(((e + (24 * 60 if e <= s else 0)) - s) / 60.0, 2)
    conn = get_conn(db_path)
    try:
        cur = conn.execute("INSERT INTO fww_access_offers (restaurant_id, request_id, shift_date, shift_start, shift_end, "
                           "role, hours, offered_to_json, posted_at, closes_at, jurisdiction, created_by) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (restaurant_id, req.get("id"), str(day)[:10], shift_start, shift_end, role, hours,
                            json.dumps(req.get("told") or []), posted.strftime("%Y-%m-%d %H:%M:%S"),
                            closes.strftime("%Y-%m-%d %H:%M:%S"), code, ((actor or "") if isinstance(actor, str)
                                                                          else "")[:120] or None))
        conn.commit()
        return dict(conn.execute("SELECT * FROM fww_access_offers WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def sync_access_offers(restaurant_id, db_path=None, now=None) -> int:
    """Close each offer whose open shift was taken (filled) or whose window
    ran out with nobody (closed_unfilled — the hours may now go to someone
    new). Returns the number changed."""
    now_s = (now or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    n = 0
    try:
        rows = conn.execute("SELECT o.id, o.closes_at, r.status AS rstatus, r.replacement_name, r.decided_at "
                            "FROM fww_access_offers o LEFT JOIN shift_change_requests r ON r.id=o.request_id "
                            "WHERE o.restaurant_id=? AND o.status='open'", (restaurant_id,)).fetchall()
        for o in rows:
            if o["rstatus"] == "covered":
                conn.execute("UPDATE fww_access_offers SET status='filled', filled_by=?, filled_at=? WHERE id=?",
                             (o["replacement_name"], o["decided_at"] or now_s, o["id"]))
                n += 1
            elif o["rstatus"] in ("cancelled", "withdrawn", "expired"):
                conn.execute("UPDATE fww_access_offers SET status='cancelled' WHERE id=?", (o["id"],))
                n += 1
            elif o["closes_at"] <= now_s:
                conn.execute("UPDATE fww_access_offers SET status='closed_unfilled' WHERE id=?", (o["id"],))
                n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def access_offers(restaurant_id, db_path=None, since=None) -> list:
    sync_access_offers(restaurant_id, db_path)
    conn = get_conn(db_path)
    try:
        q, args = "SELECT * FROM fww_access_offers WHERE restaurant_id=?", [restaurant_id]
        if since:
            q += " AND shift_date>=?"
            args.append(str(since)[:10])
        return [dict(r) for r in conn.execute(q + " ORDER BY id DESC LIMIT 200", args).fetchall()]
    finally:
        conn.close()


def added_hours_without_offer(restaurant_id, changes, db_path=None) -> list:
    """The hours a change ADDS straight to someone's week (predictability
    pay's `add` and `extend` changes) while the law asks for them to be
    offered to the current staff first and no offer for that date and role
    ran — named to the owner before Send. [{employee, date, hours, text}]."""
    r = _models.get_restaurant(restaurant_id, db_path or DB_PATH)
    code, label, rule = access_rule(r)
    if not rule:
        return []
    offers = access_offers(restaurant_id, db_path)
    out = []
    for c in changes or []:
        if c.get("kind") not in ("add", "extend") or not c.get("hours_added"):
            continue
        role = ((c.get("after") or {}).get("role") or "").strip().lower()
        ran = [o for o in offers if o["shift_date"] == c.get("date") and (not role or not o.get("role")
                                                                          or o["role"].strip().lower() == role)
               and o["status"] in ("filled", "closed_unfilled")]
        if ran:
            continue
        from time_utils import mdy
        out.append({"employee": c["employee"], "date": c.get("date"), "hours": c.get("hours_added"),
                    "text": f"{c['employee']} gets {float(c.get('hours_added') or 0):g} added hours on "
                            f"{mdy(c.get('date'))} — under {label}'s law, offer added hours to your current staff "
                            f"first (Offer to current staff)."})
    return out


def hire_check(restaurant_id, db_path=None) -> dict:
    """Before someone new joins: the law's access-to-hours rule and the
    offers that ran in the last 30 days. {"rule", "law", "offers_run",
    "open", "text"} — `text` the owner's sentence, None when no law asks."""
    r = _models.get_restaurant(restaurant_id, db_path or DB_PATH)
    code, label, rule = access_rule(r)
    if not rule:
        return {"rule": None, "law": None, "offers_run": 0, "open": 0, "text": None}
    since = (date.today() - timedelta(days=30)).isoformat()
    offers = access_offers(restaurant_id, db_path, since=since)
    ran = [o for o in offers if o["status"] in ("filled", "closed_unfilled")]
    open_ = [o for o in offers if o["status"] == "open"]
    text = (f"{label}'s Fair Workweek law asks you to offer additional hours to your current staff before hiring. "
            + (f"{len(ran)} offer{'s' if len(ran) != 1 else ''} ran in the last 30 days"
               + (f", {len(open_)} still open." if open_ else ".") if ran or open_
               else "No offer to current staff ran in the last 30 days — post the hours first (Offer to current staff)."))
    return {"rule": rule, "law": label, "offers_run": len(ran), "open": len(open_), "text": text}
