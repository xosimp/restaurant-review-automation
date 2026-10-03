"""
shift_facts.py — every shift every person worked, one row each, kept
(memory audit 9/29/26, shift_facts).

Per-shift, per-person history used to be ONE CSV blob per restaurant
(client_data.shifts_csv), and a manual upload replaced it whole: a
restaurant with a year of history uploaded a two-week export and from then
on each person's no-show rate rested on two weeks, "usual days" reflected
two weeks, and the could-hold and mentoring evidence was gone. A POS
restaurant that uploaded one CSV to fix a week lost its accumulated POS
history outside that file. Only the POS path merged.

Now there is ONE ingest (`ingest`) for a POS sync, an owner's upload and an
admin's upload, with the POS rule for all of them: inside the new rows'
date range the new rows are the record, outside it everything is kept.
Before anything is stored each row is given its person
(people.resolve_rows — the POS id first, then the exact name), so the file
and this table carry one spelling per person. The blob is still written (the
labor analysis and every older reader parse it), and `shift_facts` holds
the same shifts with a person_id, the source, whether the source carried a
real schedule (`scheduled_hours` NULL when it did not: RPOWER, Square,
Clover and Toast without one — a copied actual is not a schedule), pay and
when it arrived. Readers that ask about a PERSON over time — reliability,
tenure, usual days, mentoring — read this table with an explicit window.

Retention: raw rows 3 years (ops._RETENTION_DAYS "shift_facts"), then the
per-person quarterly summaries in people's `person_quarters`, kept forever
(`rollup_quarters`, run nightly before the prune can reach a quarter).
"""
import logging
from datetime import date, datetime, timedelta

import models as _models_mod

log = logging.getLogger(__name__)

# Raw per-shift rows kept this long; a quarter is summarised while all of it
# is still here (QUARTER_SAFETY_DAYS before its first day falls out).
RETAIN_DAYS = 3 * 365
QUARTER_SAFETY_DAYS = 45


def get_conn(db_path=None):
    """models.get_conn at call time (CLAUDE.md, bound imports)."""
    return _models_mod.get_conn(db_path) if db_path else _models_mod.get_conn()


def init_shift_facts(db_path=None):
    """Boot DDL (models.init_db), never on a request path."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS shift_facts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id    INTEGER NOT NULL REFERENCES restaurants(id),
            business_date    TEXT    NOT NULL,
            person_id        INTEGER,
            employee_name    TEXT    NOT NULL,
            employee_key     TEXT    NOT NULL,
            role             TEXT,
            shift_start      TEXT    NOT NULL DEFAULT '',
            shift_end        TEXT,
            scheduled_hours  REAL,
            actual_hours     REAL,
            pay_rate         REAL,
            source           TEXT    NOT NULL,
            external_id      TEXT,
            ingested_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, business_date, employee_key, shift_start)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_shift_facts_person ON shift_facts(restaurant_id, person_id, business_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_shift_facts_key ON shift_facts(restaurant_id, employee_key, business_date)")
        # ops.prune_ledgers deletes by business_date (the retention registry).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_shift_facts_date ON shift_facts(business_date)")
        conn.commit()
    finally:
        conn.close()


def _num(v):
    try:
        if v in (None, ""):
            return None
        n = float(v)
        return n if n == n else None
    except (TypeError, ValueError):
        return None


def _fact(row, source):
    """One stored shift from one normalised shift row, or None."""
    import staff_settings
    day = str(row.get("date") or "")[:10]
    name = " ".join(str(row.get("employee") or "").split())
    if len(day) != 10 or not name:
        return None
    known = str(row.get("schedule_known", "")).strip()
    sched = _num(row.get("scheduled_hours"))
    # A POS row whose "scheduled" hours are its actual hours copied is no
    # schedule at all (RPOWER, Square, Clover; Toast without one).
    if known == "0":
        sched = None
    return {"business_date": day, "person_id": row.get("person_id"), "employee_name": name,
            "employee_key": staff_settings.name_key(name), "role": (str(row.get("role") or "").strip() or None),
            "shift_start": str(row.get("shift_start") or "").strip(), "shift_end": (str(row.get("shift_end") or "").strip() or None),
            "scheduled_hours": sched, "actual_hours": _num(row.get("actual_hours")),
            "pay_rate": _num(row.get("pay_rate")), "source": source,
            "external_id": (str(row.get("employee_ext_id") or "").strip() or None)}


def _write_facts(conn, restaurant_id, facts, lo, hi):
    """Replace this restaurant's facts inside [lo, hi] with `facts`."""
    conn.execute("DELETE FROM shift_facts WHERE restaurant_id=? AND business_date BETWEEN ? AND ?",
                 (restaurant_id, lo, hi))
    conn.executemany(
        "INSERT OR REPLACE INTO shift_facts (restaurant_id, business_date, person_id, employee_name, employee_key, role, "
        "shift_start, shift_end, scheduled_hours, actual_hours, pay_rate, source, external_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(restaurant_id, f["business_date"], f["person_id"], f["employee_name"], f["employee_key"], f["role"],
          f["shift_start"], f["shift_end"], f["scheduled_hours"], f["actual_hours"], f["pay_rate"], f["source"],
          f["external_id"]) for f in facts])


def ingest(restaurant_id, rows, source, db_path=None) -> dict:
    """The one way shifts come in — a POS sync, an owner's upload, an admin's
    upload. `rows` are normalised shift rows (labor.load_shifts). Returns
    {rows, window: (lo, hi) | None, people: {...}} after:

      1. rows dated after the restaurant's today dropped (a typo, never a
         shift — re-audit B3#4);
      2. each row given its person (people.resolve_rows): POS id first, then
         the exact name — the file and this table then spell each person
         one way;
      3. the stored file merged by the POS rule: inside the new rows' dates
         they are the record, outside them everything already stored stays
         (an upload used to replace the whole file — MOD-LAB-18 was only
         fixed for the POS path); kept rows are respelled to their person
         (by POS id, and by an alias) so one person is one name throughout;
      4. shift_facts replaced inside the same range, kept outside it;
      5. tenure memory (models.save_client_data remembers it)."""
    import csv as _csv
    import io as _io
    from labor import drop_future_shifts, load_shifts
    import people
    src = str(source or "upload").strip().lower() or "upload"
    new_rows = [dict(r) for r in drop_future_shifts(rows or [], restaurant_id=restaurant_id)]
    try:
        who = people.resolve_rows(restaurant_id, new_rows, src, db_path=db_path)
    except Exception as e:
        # Identity must never lose a sync: the rows go in under the names
        # they came with, as they always did, and the failure is recorded.
        import ops
        ops.capture(e, job="people_resolve", context=f"restaurant_id={restaurant_id}")
        who = {"error": str(e)[:200]}
    dates = sorted({r["date"] for r in new_rows if r.get("date")})
    merged = list(new_rows)
    conn = get_conn(db_path)
    try:
        prior_row = conn.execute("SELECT shifts_csv FROM client_data WHERE restaurant_id=?", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    prior = ((prior_row["shifts_csv"] if prior_row else "") or "")
    if dates and prior.strip():
        lo, hi = dates[0], dates[-1]
        kept = [r for r in load_shifts(csv_string=prior) if not (lo <= (r.get("date") or "") <= hi)]
        _respell(restaurant_id, kept, new_rows, db_path)
        merged = kept + merged
    # The file keeps no more than shift_facts does (memory re-audit 9/29/26,
    # FORGET-12): every ingest kept all stored rows outside the new window,
    # so the three-year window bounded the table and nothing else — a
    # person who left in 2026 was in the file, every backup of it and every
    # labor parse in 2031. The quarterly summaries are written from
    # shift_facts, never from this file, so trimming it loses nothing kept.
    edge = blob_edge()
    merged = [r for r in merged if not (str(r.get("date") or "")[:10] and str(r.get("date") or "")[:10] < edge)]
    merged.sort(key=lambda r: (r.get("date") or "", str(r.get("shift_start") or "")))
    fields = []
    for r in merged:
        for k in r:
            if k is not None and k not in fields and k != "person_id":
                fields.append(k)
    buf = _io.StringIO()
    # "\n" like the files people upload: a clean file stores exactly as sent.
    w = _csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(merged)
    from models import save_client_data
    save_client_data(restaurant_id, "shifts", buf.getvalue(), source=src, db_path=db_path or _models_mod.DB_PATH)
    if dates:
        facts = [f for f in (_fact(r, src) for r in new_rows) if f]
        conn = get_conn(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _write_facts(conn, restaurant_id, facts, dates[0], dates[-1])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {"rows": len(new_rows), "window": (dates[0], dates[-1]) if dates else None, "people": who,
            "resolved": new_rows}


def blob_edge(today=None) -> str:
    """The oldest date the stored shifts file keeps: shift_facts' own raw
    window (the retention registry's, else RETAIN_DAYS)."""
    try:
        import ops
        days = int(ops._RETENTION_DAYS.get("shift_facts", RETAIN_DAYS))
    except Exception:
        days = RETAIN_DAYS
    return ((today or date.today()) - timedelta(days=days)).isoformat()


def rewrite_window_from_csv(restaurant_id, csv_text, lo, hi, source="upload", db_path=None) -> int:
    """The facts inside [lo, hi] put back from a stored file — the undo of an
    ingest whose later step failed (the upload restores its previous file,
    and the facts follow it). Returns the facts written."""
    from labor import load_shifts
    import staff_settings
    rows = [r for r in (load_shifts(csv_string=csv_text) if (csv_text or "").strip() else [])
            if lo <= str(r.get("date") or "")[:10] <= hi]
    conn = get_conn(db_path)
    try:
        pid_of = {p["name_key"]: p["id"] for p in conn.execute(
            "SELECT id, name_key FROM people WHERE restaurant_id=? AND merged_into IS NULL", (restaurant_id,)).fetchall()}
    except Exception:
        pid_of = {}
    facts = []
    for r in rows:
        f = _fact(r, str(source or "upload").lower())
        if f:
            f["person_id"] = pid_of.get(staff_settings.name_key(f["employee_name"]))
            facts.append(f)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _write_facts(conn, restaurant_id, facts, lo, hi)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return len(facts)


def replace_all(restaurant_id, csv_text, source="seed", db_path=None) -> int:
    """Every fact of a restaurant replaced by one file — a seed (the demo
    account), which IS the whole record, never an upload."""
    from labor import load_shifts
    rows = load_shifts(csv_string=csv_text) if (csv_text or "").strip() else []
    dates = sorted({str(r.get("date") or "")[:10] for r in rows if r.get("date")})
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM shift_facts WHERE restaurant_id=?", (restaurant_id,))
        conn.commit()
    finally:
        conn.close()
    if not dates:
        return 0
    return rewrite_window_from_csv(restaurant_id, csv_text, dates[0], dates[-1], source, db_path=db_path)


def _respell(restaurant_id, kept, new_rows, db_path):
    """Kept rows spelled the way their person is spelled now: a row carrying
    a POS id this sync resolved takes that person's name; any other takes
    the display name of the one person its spelling means (people.
    canonical_names). A name that means nobody, or two people, is left."""
    import people
    by_ext = {}
    for r in new_rows:
        ext = str(r.get("employee_ext_id") or "").strip()
        if ext and r.get("person_id"):
            by_ext[ext] = r.get("employee")
    names = sorted({r.get("employee") for r in kept if r.get("employee")})
    try:
        canon = people.canonical_names(restaurant_id, names, db_path=db_path)
    except Exception:
        canon = {}
    for r in kept:
        ext = str(r.get("employee_ext_id") or "").strip()
        if ext and ext in by_ext:
            r["employee"] = by_ext[ext]
        elif r.get("employee") in canon:
            r["employee"] = canon[r["employee"]]


def backfill_from_csv(db_path=None, max_seconds=15.0) -> int:
    """Once per restaurant whose facts are empty: its stored shifts file as
    facts (the history it had before this table existed). Bounded; the
    nightly people job reaches the rest. Idempotent."""
    import time as _t
    from labor import load_shifts
    import staff_settings
    conn = get_conn(db_path)
    try:
        rids = [r[0] for r in conn.execute(
            "SELECT restaurant_id FROM client_data WHERE shifts_csv IS NOT NULL AND shifts_csv != '' "
            "AND restaurant_id NOT IN (SELECT DISTINCT restaurant_id FROM shift_facts)").fetchall()]
    finally:
        conn.close()
    stop = _t.monotonic() + float(max_seconds)
    done = 0
    for rid in rids:
        if _t.monotonic() > stop:
            break
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT shifts_csv, shifts_source FROM client_data WHERE restaurant_id=?", (rid,)).fetchone()
            text = (row["shifts_csv"] if row else "") or ""
            src = str((row["shifts_source"] if row else "") or "upload").lower() or "upload"
            rows = load_shifts(csv_string=text) if text.strip() else []
            pid_of = {}
            try:
                for p in conn.execute("SELECT id, name_key FROM people WHERE restaurant_id=? AND merged_into IS NULL",
                                      (rid,)).fetchall():
                    pid_of[p["name_key"]] = p["id"]
            except Exception:
                pid_of = {}
            facts = []
            for r in rows:
                f = _fact(r, src)
                if f:
                    f["person_id"] = pid_of.get(staff_settings.name_key(f["employee_name"]))
                    facts.append(f)
            if facts:
                conn.execute("BEGIN IMMEDIATE")
                lo = min(f["business_date"] for f in facts)
                hi = max(f["business_date"] for f in facts)
                _write_facts(conn, rid, facts, lo, hi)
                conn.commit()
            done += 1
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            log.warning("[shift_facts] backfill skipped restaurant %s: %s", rid, e)
        finally:
            conn.close()
    return done


# ── readers: a person over time, with an explicit window ─────────────────────

def rows(restaurant_id, since=None, until=None, db_path=None) -> list:
    """Stored shifts as dicts shaped like the file's rows ({date, employee,
    role, shift_start, shift_end, scheduled_hours, actual_hours, person_id,
    source, schedule_known, pay_rate}), oldest first, inside [since, until].
    `pay_rate` is what the POS paid for the punch (labor._shift_rate's first
    link) — the daypart labor cost prices each punch with it (schedule audit
    10/3/26 L-13)."""
    sql = "SELECT * FROM shift_facts WHERE restaurant_id=?"
    args = [restaurant_id]
    if since:
        sql += " AND business_date >= ?"
        args.append(str(since)[:10])
    if until:
        sql += " AND business_date <= ?"
        args.append(str(until)[:10])
    conn = get_conn(db_path)
    try:
        got = conn.execute(sql + " ORDER BY business_date, shift_start", args).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    out = []
    for r in got:
        out.append({"date": r["business_date"], "employee": r["employee_name"], "role": r["role"] or "",
                    "shift_start": r["shift_start"] or "", "shift_end": r["shift_end"] or "",
                    "scheduled_hours": r["scheduled_hours"], "actual_hours": r["actual_hours"],
                    "person_id": r["person_id"], "source": r["source"],
                    "schedule_known": r["scheduled_hours"] is not None, "pay_rate": r["pay_rate"]})
    return out


def has_facts(restaurant_id, db_path=None) -> bool:
    conn = get_conn(db_path)
    try:
        return conn.execute("SELECT 1 FROM shift_facts WHERE restaurant_id=? LIMIT 1", (restaurant_id,)).fetchone() is not None
    except Exception:
        return False
    finally:
        conn.close()


def person_rows(restaurant_id, since=None, until=None, db_path=None) -> list:
    """rows() when this restaurant has facts, else the stored file's rows —
    the one source every per-person reader reads, so a restaurant whose
    history predates the table (before its backfill) still reads its own
    shifts."""
    if has_facts(restaurant_id, db_path):
        return rows(restaurant_id, since=since, until=until, db_path=db_path)
    try:
        from models import _cached_shifts
        out = []
        for r in _cached_shifts(restaurant_id) or []:
            d = str(r.get("date") or "")[:10]
            if (since and d and d < str(since)[:10]) or (until and d and d > str(until)[:10]):
                continue
            out.append(dict(r, schedule_known=str(r.get("schedule_known", "")).strip() != "0"))
        return out
    except Exception:
        return []


def tenure(restaurant_id, db_path=None) -> dict:
    """{employee_name: {"shifts": n, "first": iso, "last": iso}} over a
    person's whole history — the cumulative count the rolling file could
    never give (staff_first_seen kept only the most shifts any one upload
    showed). A lifetime reader: the raw shifts are kept three years
    (ops._RETENTION_DAYS), so each quarter is counted from its raw rows or
    from its person_quarters summary, whichever holds more of it — a quarter
    the prune has started on is read from the summary written while it was
    whole — and the first day is the earliest either has."""
    conn = get_conn(db_path)
    try:
        raw = conn.execute("SELECT employee_key, employee_name, business_date FROM shift_facts WHERE restaurant_id=?",
                           (restaurant_id,)).fetchall()
        try:
            summ = conn.execute("SELECT employee_key, employee_name, quarter, shifts, first_date, last_date FROM "
                                "person_quarters WHERE restaurant_id=? AND shifts > 0", (restaurant_id,)).fetchall()
        except Exception:
            summ = []
    except Exception:
        return {}
    finally:
        conn.close()
    per = {}      # key -> {"name", "q": {quarter: [raw_n, summary_n]}, "first", "last"}
    for r in raw:
        try:
            q = _quarter(date.fromisoformat(r["business_date"]))
        except ValueError:
            continue
        p = per.setdefault(r["employee_key"], {"name": r["employee_name"], "q": {}, "first": None, "last": None})
        p["q"].setdefault(q, [0, 0])[0] += 1
        d = r["business_date"]
        p["first"] = d if p["first"] is None or d < p["first"] else p["first"]
        p["last"] = d if p["last"] is None or d > p["last"] else p["last"]
    for r in summ:
        p = per.setdefault(r["employee_key"], {"name": r["employee_name"], "q": {}, "first": None, "last": None})
        p["q"].setdefault(r["quarter"], [0, 0])[1] = int(r["shifts"] or 0)
        for d, pick in ((r["first_date"], min), (r["last_date"], max)):
            if d:
                cur = p["first"] if pick is min else p["last"]
                val = d if cur is None else pick(cur, d)
                if pick is min:
                    p["first"] = val
                else:
                    p["last"] = val
    return {p["name"]: {"shifts": sum(max(a, b) for a, b in p["q"].values()), "first": p["first"], "last": p["last"]}
            for p in per.values() if p["q"]}


# ── quarterly summaries, kept forever ───────────────────────────────────────

def _quarter(day) -> str:
    return f"{day.year}-Q{(day.month - 1) // 3 + 1}"


def _quarter_start(q) -> date:
    y, n = q.split("-Q")
    return date(int(y), (int(n) - 1) * 3 + 1, 1)


def _whole_since(table, default_days, today) -> date:
    """The first day of the oldest quarter whose raw `table` rows are all
    still here (the retention registry's window, less a safety margin)."""
    try:
        import ops
        days = int(ops._RETENTION_DAYS.get(table, default_days))
    except Exception:
        days = default_days
    edge = today - timedelta(days=max(0, days - QUARTER_SAFETY_DAYS))
    first = _quarter_start(_quarter(edge))
    if first < edge:                               # that quarter is already partly gone
        first = _quarter_start(_quarter(first + timedelta(days=100)))
    return first


def rollup_quarters(restaurant_id, db_path=None, today=None) -> int:
    """Per person per quarter into people's person_quarters, kept forever:
    shifts, hours, scheduled shifts, roles, dayparts and weekdays from
    shift_facts, and watched shifts and each outcome from attendance_events
    — each half written only for quarters whose raw rows are ALL still here
    (their own retention windows differ), so a quarter summarised while
    whole is never rewritten from what is left of it. Returns the
    (person, quarter) rows touched."""
    import json
    from shift_quality import daypart_of
    today = today or date.today()
    shifts_since = _whole_since("shift_facts", RETAIN_DAYS, today).isoformat()
    att_since = _whole_since("attendance_events", 730, today).isoformat()
    sig_since = _whole_since("person_signals", 730, today).isoformat()
    conn = get_conn(db_path)
    try:
        got = conn.execute("SELECT * FROM shift_facts WHERE restaurant_id=? AND business_date >= ?",
                           (restaurant_id, shifts_since)).fetchall()
        try:
            att = conn.execute("SELECT employee_key, employee_name, person_id, business_date, outcome "
                               "FROM attendance_events WHERE restaurant_id=? AND business_date >= ?",
                               (restaurant_id, att_since)).fetchall()
        except Exception:
            att = []
        try:
            sig = conn.execute("SELECT employee_key, employee_name, person_id, signal_date, kind, polarity "
                               "FROM person_signals WHERE restaurant_id=? AND signal_date >= ? AND status='confirmed'",
                               (restaurant_id, sig_since)).fetchall()
        except Exception:
            sig = []
    finally:
        conn.close()
    shifts, outcomes = {}, {}
    for r in got:
        try:
            d = date.fromisoformat(r["business_date"])
        except ValueError:
            continue
        a = shifts.setdefault((r["employee_key"], _quarter(d)), {
            "name": r["employee_name"], "pid": r["person_id"], "shifts": 0, "hours": 0.0, "scheduled": 0,
            "roles": {}, "dayparts": {}, "weekdays": {}, "first": r["business_date"], "last": r["business_date"]})
        a["shifts"] += 1
        a["hours"] += float(r["actual_hours"] if r["actual_hours"] is not None else (r["scheduled_hours"] or 0))
        if r["scheduled_hours"] is not None:
            a["scheduled"] += 1
        if r["role"]:
            a["roles"][r["role"]] = a["roles"].get(r["role"], 0) + 1
        part = daypart_of(r["shift_start"] or "")
        if part != "unknown":
            a["dayparts"][part] = a["dayparts"].get(part, 0) + 1
        wd = d.strftime("%A")
        a["weekdays"][wd] = a["weekdays"].get(wd, 0) + 1
        a["first"], a["last"] = min(a["first"], r["business_date"]), max(a["last"], r["business_date"])
    for r in att:
        try:
            q = _quarter(date.fromisoformat(r["business_date"]))
        except ValueError:
            continue
        o = outcomes.setdefault((r["employee_key"], q), {"name": r["employee_name"], "pid": r["person_id"], "n": {}})
        o["n"][r["outcome"]] = o["n"].get(r["outcome"], 0) + 1
    signals = {}
    for r in sig:
        try:
            q = _quarter(date.fromisoformat(str(r["signal_date"])[:10]))
        except ValueError:
            continue
        g = signals.setdefault((r["employee_key"], q), {"name": r["employee_name"], "pid": r["person_id"],
                                                        "taken": 0, "declined": 0, "pos": 0, "neg": 0})
        if r["kind"] == "cover_accepted":
            g["taken"] += 1
        elif r["kind"] == "cover_declined":
            g["declined"] += 1
        elif r["kind"] == "review_mention":
            if (r["polarity"] or 0) > 0:
                g["pos"] += 1
            elif (r["polarity"] or 0) < 0:
                g["neg"] += 1
    if not shifts and not outcomes and not signals:
        return 0
    conn = get_conn(db_path)
    try:
        for (key, q), a in shifts.items():
            conn.execute(
                "INSERT INTO person_quarters (restaurant_id, person_id, employee_name, employee_key, quarter, shifts, "
                "hours, scheduled_shifts, roles_json, dayparts_json, weekdays_json, first_date, last_date, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id, employee_key, quarter) "
                "DO UPDATE SET person_id=excluded.person_id, employee_name=excluded.employee_name, "
                "shifts=excluded.shifts, hours=excluded.hours, scheduled_shifts=excluded.scheduled_shifts, "
                "roles_json=excluded.roles_json, dayparts_json=excluded.dayparts_json, "
                "weekdays_json=excluded.weekdays_json, first_date=excluded.first_date, last_date=excluded.last_date, "
                "updated_at=excluded.updated_at",
                (restaurant_id, a["pid"], a["name"], key, q, a["shifts"], round(a["hours"], 2), a["scheduled"],
                 json.dumps(a["roles"]), json.dumps(a["dayparts"]), json.dumps(a["weekdays"]), a["first"], a["last"]))
        for (key, q), o in outcomes.items():
            n = o["n"]
            conn.execute(
                "INSERT INTO person_quarters (restaurant_id, person_id, employee_name, employee_key, quarter, watched, "
                "no_shows, called_out, late, left_early, covered, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,"
                "datetime('now')) ON CONFLICT(restaurant_id, employee_key, quarter) DO UPDATE SET "
                "watched=excluded.watched, no_shows=excluded.no_shows, called_out=excluded.called_out, "
                "late=excluded.late, left_early=excluded.left_early, covered=excluded.covered, "
                "updated_at=excluded.updated_at",
                (restaurant_id, o["pid"], o["name"], key, q, sum(n.values()), n.get("no_show", 0), n.get("called_out", 0),
                 n.get("late", 0), n.get("left_early", 0), n.get("covered", 0)))
        for (key, q), g in signals.items():
            conn.execute(
                "INSERT INTO person_quarters (restaurant_id, person_id, employee_name, employee_key, quarter, "
                "covers_taken, covers_declined, mentions_positive, mentions_negative, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id, employee_key, quarter) DO UPDATE "
                "SET covers_taken=excluded.covers_taken, covers_declined=excluded.covers_declined, "
                "mentions_positive=excluded.mentions_positive, mentions_negative=excluded.mentions_negative, "
                "updated_at=excluded.updated_at",
                (restaurant_id, g["pid"], g["name"], key, q, g["taken"], g["declined"], g["pos"], g["neg"]))
        conn.commit()
    finally:
        conn.close()
    return len(set(shifts) | set(outcomes) | set(signals))


def roll_all_quarters(db_path=None, now=None) -> dict:
    """ops.prune_ledgers' rollup for shift_facts, attendance_events and
    person_signals (ops._RETENTION_ROLLUP): every restaurant's quarterly
    summaries brought up to date BEFORE any of their raw rows go, so a
    quarter is summarised while it is whole. Raises on a failure — the
    registry then keeps those tables' rows that night."""
    today = (now.date() if isinstance(now, datetime) else now) or date.today()
    conn = get_conn(db_path)
    try:
        rids = set()
        for table in ("shift_facts", "attendance_events", "person_signals"):
            try:
                rids |= {r[0] for r in conn.execute(f"SELECT DISTINCT restaurant_id FROM {table}").fetchall()}
            except Exception as e:
                if "no such table" not in str(e).lower():
                    raise
    finally:
        conn.close()
    rows = 0
    for rid in sorted(rids):
        rows += rollup_quarters(rid, db_path=db_path, today=today)
    return {"restaurants": len(rids), "rows": rows}
