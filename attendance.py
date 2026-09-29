"""
attendance.py — who turned up, shift by shift, on the nights somebody could
see (memory audit 9/29/26, attendance).

A no-show used to be "scheduled > 0 and actual 0" in the shifts file, and
nothing else. RPOWER's timeclock carries no schedule (scheduled was copied
from actual) and Toast's falls back the same way, and a no-show has no
punch at all — so at a POS restaurant no row could ever say someone missed
a shift, and the engine reported everyone as reliable. The real no-shows
became coverage issues that nothing fed back to the person: Maria missed
three Saturdays and next week's draft put her alone on Saturday bar.

`attendance_events` is one row per scheduled shift somebody watched: the
person, the date, the scheduled start, the OUTCOME — on_time, late,
no_show, called_out, left_early, covered — minutes late, who covered, and
where it was seen. Its writers, strongest first (a weaker one never
overwrites a stronger one's outcome):

  manual               the owner or a manager correcting it
  closeout_confirmed   a closer's "who didn't make it", matched exactly to
                       someone on that night's published schedule
  coverage_check       the live clock-in check (strategy_jobs.run_coverage_
                       check): arrived after the grace → late; still missing
                       when the night is over → no_show
  schedule_vs_punch_join  the published week against the POS punches, the
                       night after, for a night whose POS day is final — the
                       only source an RPOWER restaurant can have

Readers — reliability (staff_settings.reliability), attendance by weekday
and standby days (schedule_learning), the schedule prompt's no-show block
and the Shift Quality reliability dimension — read `reliability_events`:
these outcomes, plus the shifts from a source that carried a real schedule
(an upload with scheduled and actual hours). A person nobody watched is
absent — unknown — never "reliable".

Retention: raw events 2 years (ops._RETENTION_DAYS "attendance_events"),
then quarterly per person in person_quarters (shift_facts.rollup_quarters),
kept forever.
"""
import json
import logging
from datetime import date, datetime, timedelta

import models as _models_mod

log = logging.getLogger(__name__)

OUTCOMES = ("on_time", "late", "no_show", "called_out", "left_early", "covered")
MISSES = ("no_show", "called_out")
# Who may overwrite whom: a stronger source's outcome stands.
SOURCE_RANK = {"manual": 4, "closeout_confirmed": 3, "coverage_check": 2, "schedule_vs_punch_join": 1}
# Past this many minutes after the scheduled start a clock-in is late (the
# live check's own grace, intraday.COVERAGE_GRACE_MINUTES).
LATE_AFTER_MINUTES = 10
# Worked this much less than scheduled: left early (staff_settings'
# short-shift rule).
LEFT_EARLY_HOURS = 1.5
# The nights the nightly join looks back over (a POS day goes final the
# morning after; a slow sync catches up within the week).
JOIN_DAYS = 7


def get_conn(db_path=None):
    """models.get_conn at call time (CLAUDE.md, bound imports)."""
    return _models_mod.get_conn(db_path) if db_path else _models_mod.get_conn()


def init_attendance(db_path=None):
    """Boot DDL (models.init_db), never on a request path."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS attendance_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            person_id       INTEGER,
            employee_name   TEXT    NOT NULL,
            employee_key    TEXT    NOT NULL,
            business_date   TEXT    NOT NULL,
            shift_start     TEXT    NOT NULL DEFAULT '',
            role            TEXT,
            outcome         TEXT    NOT NULL,
            minutes_late    INTEGER,
            covered_by      TEXT,
            source          TEXT    NOT NULL,
            history_id      INTEGER,
            note            TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, employee_key, business_date, shift_start)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attendance_person ON attendance_events"
                     "(restaurant_id, employee_key, business_date)")
        # ops.prune_ledgers deletes by business_date (the retention registry).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance_events(business_date)")
        conn.commit()
    finally:
        conn.close()


def _nk(name):
    import staff_settings
    return staff_settings.name_key(name)


def _minutes(value):
    import schedule_requirements
    return schedule_requirements._minutes(value)


def record(restaurant_id, name, business_date, outcome, source, shift_start="", minutes_late=None,
           covered_by=None, role=None, history_id=None, note=None, person_id=None, db_path=None) -> bool:
    """One watched shift's outcome. A weaker source never overwrites a
    stronger one's outcome (SOURCE_RANK); agreeing, it may fill in what the
    stronger one did not know (minutes late). Returns whether it wrote."""
    if outcome not in OUTCOMES or source not in SOURCE_RANK:
        raise ValueError(f"attendance: {outcome!r} from {source!r}")
    key = _nk(name)
    day = str(business_date)[:10]
    if not key or len(day) != 10:
        return False
    start = str(shift_start or "").strip()
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("SELECT * FROM attendance_events WHERE restaurant_id=? AND employee_key=? AND "
                           "business_date=? AND shift_start=?", (restaurant_id, key, day, start)).fetchone()
        if cur is None and start:
            # The same shift recorded without its start (a callout names only
            # the person) — one row for the night, not two.
            cur = conn.execute("SELECT * FROM attendance_events WHERE restaurant_id=? AND employee_key=? AND "
                               "business_date=? AND shift_start=''", (restaurant_id, key, day)).fetchone()
        if cur is not None:
            mine, theirs = SOURCE_RANK[source], SOURCE_RANK.get(cur["source"], 0)
            if mine < theirs:
                if cur["outcome"] == outcome and cur["minutes_late"] is None and minutes_late is not None:
                    conn.execute("UPDATE attendance_events SET minutes_late=?, updated_at=datetime('now') WHERE id=?",
                                 (int(minutes_late), cur["id"]))
                    conn.commit()
                    return True
                conn.rollback()
                return False
            conn.execute("UPDATE attendance_events SET outcome=?, source=?, minutes_late=COALESCE(?, minutes_late), "
                         "covered_by=COALESCE(?, covered_by), role=COALESCE(?, role), history_id=COALESCE(?, history_id), "
                         "note=COALESCE(?, note), person_id=COALESCE(?, person_id), shift_start=CASE WHEN "
                         "shift_start='' THEN ? ELSE shift_start END, employee_name=?, updated_at=datetime('now') "
                         "WHERE id=?",
                         (outcome, source, minutes_late, covered_by, role, history_id, (note or "")[:300] or None,
                          person_id, start, " ".join(str(name).split()), cur["id"]))
        else:
            conn.execute("INSERT INTO attendance_events (restaurant_id, person_id, employee_name, employee_key, "
                         "business_date, shift_start, role, outcome, minutes_late, covered_by, source, history_id, note) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (restaurant_id, person_id, " ".join(str(name).split()), key, day, start, role, outcome,
                          minutes_late, covered_by, source, history_id, (note or "")[:300] or None))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── writers ─────────────────────────────────────────────────────────────────

def _published_rows(restaurant_id, day, db_path=None):
    """(rows, history_id) of the published week covering `day`, each row's
    name read as the person it means now (people.canonical_names)."""
    try:
        import intraday
        from schedule_versions import rows_from_csv
        text = intraday._published_csv(restaurant_id, date.fromisoformat(str(day)[:10]), db_path=db_path or
                                       _models_mod.DB_PATH)
        rows = [r for r in rows_from_csv(text) if (r.get("date") or "")[:10] == str(day)[:10]]
    except Exception:
        return [], None
    hid = None
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT id FROM schedule_history WHERE restaurant_id=? AND week_start<=? AND "
                               "week_end>=? AND published_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                               (restaurant_id, str(day)[:10], str(day)[:10])).fetchone()
            hid = row["id"] if row else None
        finally:
            conn.close()
    except Exception:
        hid = None
    try:
        import people
        canon = people.canonical_names(restaurant_id, [r["employee"] for r in rows], db_path=db_path)
        rows = [dict(r, employee=canon.get(r["employee"], r["employee"])) for r in rows]
    except Exception:
        pass
    return rows, hid


def _person_ids(restaurant_id, names, db_path=None) -> dict:
    try:
        conn = get_conn(db_path)
        try:
            got = {r["name_key"]: r["id"] for r in conn.execute(
                "SELECT id, name_key FROM people WHERE restaurant_id=? AND merged_into IS NULL", (restaurant_id,))}
        finally:
            conn.close()
    except Exception:
        return {}
    return {n: got.get(_nk(n)) for n in names}


def _covered_by(restaurant_id, name, day, start, db_path=None):
    """Who took the shift, when a drop or swap was covered, else None."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT employee_name, replacement_name, shift_start FROM shift_change_requests "
                            "WHERE restaurant_id=? AND date=? AND status='covered'",
                            (restaurant_id, str(day)[:10])).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    for r in rows:
        if _nk(r["employee_name"]) == _nk(name) and (not start or not r["shift_start"]
                                                    or _minutes(r["shift_start"]) == _minutes(start)):
            return r["replacement_name"] or "someone"
    return None


def pos_day_final(restaurant_id, day, db_path=None) -> bool:
    """Whether the POS's record of `day` is complete: a final daily-history
    row for it from a POS pull, and punches for it in the per-shift history.
    A night the POS has not finished reporting is not a night anyone was
    absent from."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT final, provider FROM labor_daily_history WHERE restaurant_id=? AND date=?",
                           (restaurant_id, str(day)[:10])).fetchone()
        punches = conn.execute("SELECT 1 FROM shift_facts WHERE restaurant_id=? AND business_date=? AND source IN "
                               "('rpower','toast','square','clover') LIMIT 1", (restaurant_id, str(day)[:10])).fetchone()
    except Exception:
        return False
    finally:
        conn.close()
    return bool(row and row["provider"] and (row["final"] is None or int(row["final"]) == 1) and punches)


def join_published(restaurant_id, day, db_path=None) -> dict:
    """The published week's `day` against the POS punches for it — the
    night after, and only for a night whose POS day is final
    (pos_day_final). Each scheduled shift: a punch for its person that day
    → on_time, late (past LATE_AFTER_MINUTES) or left_early (LEFT_EARLY_HOURS
    short); none → covered when a drop or swap was covered, else no_show.
    Source schedule_vs_punch_join, the weakest: the live check and a
    closer's word stand over it. Returns {watched, recorded}."""
    rows, hid = _published_rows(restaurant_id, day, db_path)
    if not rows or not pos_day_final(restaurant_id, day, db_path):
        return {"watched": False, "recorded": 0}
    import shift_facts
    punches = {}
    for p in shift_facts.rows(restaurant_id, since=day, until=day, db_path=db_path):
        if p.get("source") in ("rpower", "toast", "square", "clover"):
            punches.setdefault(_nk(p["employee"]), []).append(p)
    pids = _person_ids(restaurant_id, [r["employee"] for r in rows], db_path)
    n = 0
    for r in rows:
        name, start = r["employee"], (r.get("shift_start") or "").strip()
        mine = punches.get(_nk(name)) or []
        sched_start, sched_end = _minutes(start), _minutes(r.get("shift_end"))
        outcome, late, covered = None, None, None
        if mine:
            # The punch nearest the scheduled start is this shift's.
            best = min(mine, key=lambda p: abs((_minutes(p.get("shift_start")) or 0) - (sched_start or 0)))
            in_m, out_m = _minutes(best.get("shift_start")), _minutes(best.get("shift_end"))
            outcome = "on_time"
            if in_m is not None and sched_start is not None and in_m - sched_start > LATE_AFTER_MINUTES:
                outcome, late = "late", int(in_m - sched_start)
            elif (out_m is not None and sched_end is not None and sched_end - out_m >= LEFT_EARLY_HOURS * 60
                  and out_m > (in_m or 0)):
                outcome = "left_early"
        else:
            covered = _covered_by(restaurant_id, name, day, start, db_path)
            outcome = "covered" if covered else "no_show"
        if record(restaurant_id, name, day, outcome, "schedule_vs_punch_join", shift_start=start, minutes_late=late,
                  covered_by=covered, role=r.get("role"), history_id=hid, person_id=pids.get(name), db_path=db_path):
            n += 1
    return {"watched": True, "recorded": n}


def from_coverage_issues(restaurant_id, today=None, days=JOIN_DAYS, db_path=None) -> int:
    """What the live clock-in check saw, recorded per person: an issue it
    closed because they clocked in → late; one still open once the night is
    over → no_show. A manager closing the issue says nothing about whether
    they came (dsr.block_labor's rule), so that is left to the join and the
    closer. Returns outcomes written."""
    from time_utils import restaurant_now_by_id
    try:
        now_local = restaurant_now_by_id(restaurant_id, naive=True)
    except Exception:
        now_local = datetime.now()
    today = today or now_local.date()
    since = (today - timedelta(days=days)).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT source_key, status, resolution_note, resolved_at, meta_json FROM ops_issues "
                            "WHERE restaurant_id=? AND kind='coverage' AND source_key >= ?",
                            (restaurant_id, f"coverage:{since}")).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    n = 0
    for r in rows:
        parts = (r["source_key"] or "").split(":", 2)
        if len(parts) < 3:
            continue
        day = parts[1]
        try:
            meta = json.loads(r["meta_json"] or "null") or {}
        except (TypeError, ValueError):
            meta = {}
        name = meta.get("missing") or parts[2]
        start = meta.get("shift_start") or ""
        if (r["resolution_note"] or "").startswith("Closed automatically: they clocked in"):
            outcome = "late"
        elif r["status"] != "resolved" and day < today.isoformat():
            outcome = "no_show"
        else:
            continue
        if record(restaurant_id, name, day, outcome, "coverage_check", shift_start=start, role=meta.get("role"),
                  note="seen by the live clock-in check", db_path=db_path):
            n += 1
    return n


def from_closeout(restaurant_id, business_date, callouts_text, db_path=None) -> list:
    """A closer's "who didn't make it", CONFIRMED: each name in it that is
    exactly someone on that night's published schedule (their spelling or
    an alias — never a guess from a first name) → called_out. Returns the
    names recorded."""
    import re
    text = str(callouts_text or "").strip()
    if not text:
        return []
    rows, hid = _published_rows(restaurant_id, business_date, db_path)
    if not rows:
        return []
    by_key = {}
    for r in rows:
        by_key.setdefault(_nk(r["employee"]), r)
    try:
        import people
        canon = people.canonical_names
    except Exception:
        canon = None
    said = [p.strip(" .") for p in re.split(r"[,;\n]+|\band\b|&", text) if p.strip(" .")]
    done = []
    for phrase in said:
        # "Maria (sick)", "Tom - no call": the name is what comes before.
        who = re.split(r"\s*[\(\-–—:]\s*", phrase, maxsplit=1)[0].strip()
        key = _nk(who)
        if canon is not None and key not in by_key:
            try:
                key = _nk(canon(restaurant_id, [who], db_path=db_path).get(who) or who)
            except Exception:
                pass
        row = by_key.get(key)
        if not row:
            continue
        if record(restaurant_id, row["employee"], business_date, "called_out", "closeout_confirmed",
                  shift_start=(row.get("shift_start") or "").strip(), role=row.get("role"), history_id=hid,
                  note=f"the close-out: {phrase[:120]}", db_path=db_path):
            done.append(row["employee"])
    return done


# ── readers ─────────────────────────────────────────────────────────────────

def events(restaurant_id, since=None, until=None, db_path=None) -> list:
    sql = "SELECT * FROM attendance_events WHERE restaurant_id=?"
    args = [restaurant_id]
    if since:
        sql += " AND business_date >= ?"
        args.append(str(since)[:10])
    if until:
        sql += " AND business_date <= ?"
        args.append(str(until)[:10])
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql + " ORDER BY business_date, shift_start", args).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def reliability_events(restaurant_id, since=None, db_path=None) -> list:
    """[(name, iso_date, outcome)] — every shift somebody watched: the
    recorded outcomes, and the shifts from a source that carried a REAL
    schedule (an upload with scheduled and actual hours: actual 0 is a
    no-show, 1.5h short a short shift). A POS row whose schedule was its
    actual hours copied says nothing about attendance and is never read."""
    import shift_facts
    out, seen = [], set()
    for e in events(restaurant_id, since=since, db_path=db_path):
        out.append((e["employee_name"], e["business_date"],
                    "short" if e["outcome"] == "left_early" else e["outcome"]))
        seen.add((e["employee_key"], e["business_date"]))
    for r in shift_facts.person_rows(restaurant_id, since=since, db_path=db_path):
        if not r.get("schedule_known"):
            continue
        name, day = (r.get("employee") or "").strip(), str(r.get("date") or "")[:10]
        if not name or (_nk(name), day) in seen:
            continue
        try:
            sched = float(r.get("scheduled_hours") or 0)
            actual = r.get("actual_hours")
            if actual in (None, ""):
                continue                      # no clock-in reading: says nothing
            actual = float(actual)
        except (TypeError, ValueError):
            continue
        if sched <= 0:
            continue
        out.append((name, day, "no_show" if actual == 0 else ("short" if sched - actual >= LEFT_EARLY_HOURS
                                                              else "worked")))
    return out


def watched(restaurant_id, days=90, db_path=None) -> bool:
    """Whether anybody's attendance has been watched here lately."""
    since = (date.today() - timedelta(days=days)).isoformat()
    return bool(reliability_events(restaurant_id, since=since, db_path=db_path))


def summary_lines(restaurant_id, today=None, db_path=None, limit=6) -> list:
    """Plain sentences about attendance over the last 12 weeks, for the
    people memory: who missed shifts, on which weekdays, and whether the
    nights were watched at all."""
    today = today or date.today()
    since = (today - timedelta(weeks=12)).isoformat()
    evs = events(restaurant_id, since=since, db_path=db_path)
    if not evs:
        return []
    by = {}
    for e in evs:
        t = by.setdefault(e["employee_name"], {"n": 0, "miss": 0, "late": 0, "days": {}, "last": e["business_date"]})
        t["n"] += 1
        t["last"] = max(t["last"], e["business_date"])
        if e["outcome"] in MISSES:
            t["miss"] += 1
            wd = date.fromisoformat(e["business_date"]).strftime("%A")
            t["days"][wd] = t["days"].get(wd, 0) + 1
        elif e["outcome"] == "late":
            t["late"] += 1
    lines = []
    for name, t in sorted(by.items(), key=lambda kv: (-kv[1]["miss"], -kv[1]["late"], kv[0])):
        if not t["miss"] and t["late"] < 2:
            continue
        bits = []
        if t["miss"]:
            top = max(t["days"].items(), key=lambda kv: kv[1])
            bits.append(f"missed {t['miss']} of {t['n']} watched shifts"
                        + (f", {top[1]} of them {top[0]}s" if top[1] >= 2 else ""))
        if t["late"] >= 2:
            bits.append(f"late {t['late']} times")
        lines.append({"text": f"{name}: " + "; ".join(bits) + " (last 12 weeks)", "date": t["last"],
                      "name": name, "weekdays": sorted(t["days"])})
        if len(lines) >= limit:
            break
    return lines
