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
no_show, called_out, left_early, covered, excused — minutes late, who
covered, how much notice a call-out gave (notice_minutes), and where it was
seen. `excused` is a drop a manager approved that nobody claimed (schedule
audit 10/3/26 E-5): the person was let off the shift before it started, so
it is not a shift they owed. A call-out carries the notice it gave when the
shift requests show it — the person asked to drop the shift at a known time
and it was never approved (L-18). Its writers, strongest first (a weaker
one never overwrites a stronger one's outcome):

  manual               the owner or a manager correcting it
  closeout_confirmed   a closer's "who didn't make it", matched exactly to
                       someone on that night's published schedule
  coverage_check       the live clock-in check (strategy_jobs.run_coverage_
                       check): arrived after the grace → late; still missing
                       when the night is over → no_show
  schedule_vs_punch_join  the published week against the POS punches, the
                       night after, for a night whose POS day is final — the
                       only source an RPOWER restaurant can have
  self_report          the employee's own "running late" in the staff app
                       (staff_comms.report_late, COM-05): late, minutes =
                       their ETA. The weakest: every clock-in reading above
                       overwrites it, and it never overwrites one — nor
                       fills in a measured row's minutes (an ETA is a guess)

Readers — reliability (staff_settings.reliability), attendance by weekday
and standby days (schedule_learning), the schedule prompt's no-show block
and the Shift Quality reliability dimension — all go through ONE weighted
reader, staff_settings.attendance_events / weighted_attendance (schedule
audit 10/3/26 L-17: they used three windows and two decays), which reads
`reliability_events`: these outcomes, plus the shifts from a source that
carried a real schedule (an upload with scheduled and actual hours). A
person nobody watched is absent — unknown — never "reliable". An excused or
covered shift is not one the person owed, so no reader counts it.

Retention: raw events 2 years (ops._RETENTION_DAYS "attendance_events"),
then quarterly per person in person_quarters (shift_facts.rollup_quarters),
kept forever.
"""
import json
import logging
from datetime import date, datetime, timedelta

import models as _models_mod

log = logging.getLogger(__name__)

OUTCOMES = ("on_time", "late", "no_show", "called_out", "left_early", "covered", "excused")
MISSES = ("no_show", "called_out")
# Not a shift the person owed: somebody else took it, or a manager let them
# off it before it started (E-5). Every reader leaves these out of the shifts
# it counts — worked, missed or late.
NOT_OWED = ("covered", "excused")
# Outcomes read off a clock-in time, so they say whether the person was
# late (D-44: lateness is measured only on these).
TIMED = ("on_time", "late", "left_early")
# Who may overwrite whom: a stronger source's outcome stands.
SOURCE_RANK = {"manual": 4, "closeout_confirmed": 3, "coverage_check": 2, "schedule_vs_punch_join": 1,
               # The person's own word before anyone saw them arrive.
               "self_report": 0}
# Past this many minutes after the expected clock-in a clock-in is late.
# The expected clock-in is the scheduled start less the role's lead
# (clock_in_leads — "servers clock in 5 minutes before their shift").
LATE_AFTER_MINUTES = 10
# The furthest ahead of a shift a role may be asked to clock in.
CLOCK_IN_LEAD_MAX = 120
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
            notice_minutes  INTEGER,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, employee_key, business_date, shift_start)
        )""")
        # How far ahead a call-out told the restaurant (schedule audit
        # 10/3/26 L-18): added to a table created before it, here at boot.
        have = {r[1] for r in conn.execute("PRAGMA table_info(attendance_events)")}
        if "notice_minutes" not in have:
            conn.execute("ALTER TABLE attendance_events ADD COLUMN notice_minutes INTEGER")
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
           covered_by=None, role=None, history_id=None, note=None, person_id=None, notice_minutes=None,
           db_path=None) -> bool:
    """One watched shift's outcome. A weaker source never overwrites a
    stronger one's outcome (SOURCE_RANK); agreeing, it may fill in what the
    stronger one did not know (minutes late, a call-out's notice). Returns
    whether it wrote."""
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
        notice = None if notice_minutes is None else max(0, int(notice_minutes))
        if cur is not None:
            mine, theirs = SOURCE_RANK[source], SOURCE_RANK.get(cur["source"], 0)
            if mine < theirs:
                fill = {}
                if (cur["outcome"] == outcome and cur["minutes_late"] is None and minutes_late is not None
                        and source != "self_report"):
                    fill["minutes_late"] = int(minutes_late)
                if cur["outcome"] == outcome and cur["notice_minutes"] is None and notice is not None:
                    fill["notice_minutes"] = notice
                if fill:
                    conn.execute(f"UPDATE attendance_events SET {', '.join(k + '=?' for k in fill)}, "
                                 "updated_at=datetime('now') WHERE id=?", (*fill.values(), cur["id"]))
                    conn.commit()
                    return True
                conn.rollback()
                return False
            conn.execute("UPDATE attendance_events SET outcome=?, source=?, minutes_late=COALESCE(?, minutes_late), "
                         "covered_by=COALESCE(?, covered_by), role=COALESCE(?, role), history_id=COALESCE(?, history_id), "
                         "note=COALESCE(?, note), person_id=COALESCE(?, person_id), "
                         "notice_minutes=COALESCE(?, notice_minutes), shift_start=CASE WHEN "
                         "shift_start='' THEN ? ELSE shift_start END, employee_name=?, updated_at=datetime('now') "
                         "WHERE id=?",
                         (outcome, source, minutes_late, covered_by, role, history_id, (note or "")[:300] or None,
                          person_id, notice, start, " ".join(str(name).split()), cur["id"]))
        else:
            conn.execute("INSERT INTO attendance_events (restaurant_id, person_id, employee_name, employee_key, "
                         "business_date, shift_start, role, outcome, minutes_late, covered_by, source, history_id, note, "
                         "notice_minutes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (restaurant_id, person_id, " ".join(str(name).split()), key, day, start, role, outcome,
                          minutes_late, covered_by, source, history_id, (note or "")[:300] or None, notice))
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


def _releases(restaurant_id, day, restaurant=None, db_path=None) -> list:
    """shift_requests.shift_release for `day`: what the requests say about a
    shift its holder did not work (covered, excused, called out with notice).
    [] when they cannot be read — the shift is then judged on the punches
    alone, as before."""
    try:
        import shift_requests
        return shift_requests.shift_release(restaurant_id, str(day)[:10], restaurant=restaurant,
                                            db_path=db_path or _models_mod.DB_PATH)
    except Exception as e:
        log.warning("attendance: shift requests unreadable rid=%s day=%s: %s", restaurant_id, day, e)
        return []


def _unworked(releases, name, start):
    """(outcome, covered_by, notice_minutes) for a published shift with no
    punch: covered when somebody took it, excused when a manager let them
    off it (an approved drop nobody claimed — schedule audit 10/3/26 E-5),
    called_out when they asked off and it was never approved (with the
    notice they gave — L-18), else a no-show."""
    import shift_requests
    hit = shift_requests.released_for(releases, name, start)
    if not hit:
        return "no_show", None, None
    if hit["outcome"] == "covered":
        return "covered", hit.get("replacement") or "someone", None
    return hit["outcome"], None, hit.get("notice_minutes")


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


def clock_in_leads(restaurant) -> dict:
    """{role lowercased: minutes} — how long before their OWN shift start
    someone in that role is expected to clock in (role_arrival_json; owner,
    9/30/26: "5 minutes before each employee's starting shift, not before
    open"). Never moves a shift; it only sets when a clock-in counts as on
    time. A role left out clocks in at its start."""
    raw = getattr(restaurant, "role_arrival_json", None) if restaurant is not None else None
    try:
        rows = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    out = {}
    for k, v in (rows.items() if isinstance(rows, dict) else ()):
        try:
            # Stored positive; an older negative "before open" value reads
            # as the same number of minutes.
            m = min(CLOCK_IN_LEAD_MAX, abs(int(v)))
        except (TypeError, ValueError):
            continue
        if str(k).strip():
            out[str(k).strip().lower()] = m
    return out


def salaried_keys(restaurant) -> set:
    """salaried_name_key()s of the salaried staff — they don't clock in
    (owner, 9/30/26: "managers don't clock in"), so a missing punch is
    never theirs to answer for. Every spelling of each of them
    (models.salaried_keys, people's identity — schedule audit 10/3/26
    D-7): "Gabriel Huerta" salaried and punching as "Gabe Huerta" is
    salaried here too."""
    try:
        from models import salaried_keys as _salaried_keys
        return set(_salaried_keys(restaurant))
    except Exception:
        return set()


def roles_without_clock_in(restaurant_id, restaurant=None, db_path=None) -> list:
    """The roles whose every active person is salaried (Manager FOH at
    Simple EJ's) — the rules screen asks no clock-in lead for them."""
    sal = salaried_keys(restaurant or _models_mod.get_restaurant(restaurant_id, db_path or _models_mod.DB_PATH))
    if not sal:
        return []
    try:
        import staff_settings
        from models import salaried_name_key
        people = staff_settings.roster(restaurant_id, db_path=db_path or _models_mod.DB_PATH)
    except Exception:
        return []
    by_role = {}
    for p in people:
        role = (p.get("role") or "").strip()
        if role:
            by_role.setdefault(role, []).append(salaried_name_key(p.get("name")) in sal)
    return sorted(r for r, flags in by_role.items() if flags and all(flags))


def join_published(restaurant_id, day, db_path=None) -> dict:
    """The published week's `day` against the POS punches for it — the
    night after, and only for a night whose POS day is final
    (pos_day_final). Each scheduled shift: a punch for its person that day
    → on_time, late (LATE_AFTER_MINUTES past the expected clock-in, the
    start less the role's clock_in_leads) or left_early (LEFT_EARLY_HOURS
    short); none → what the shift requests say (_unworked: covered,
    excused for a drop a manager approved, called_out with its notice for
    one asked and never approved), else no_show. A salaried person doesn't
    clock in and is not judged.
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
    restaurant = _models_mod.get_restaurant(restaurant_id, db_path or _models_mod.DB_PATH)
    sal, leads = salaried_keys(restaurant), clock_in_leads(restaurant)
    if sal:
        from models import salaried_name_key
        rows = [r for r in rows if salaried_name_key(r["employee"]) not in sal]
    pids = _person_ids(restaurant_id, [r["employee"] for r in rows], db_path)
    releases = None
    n = 0
    for r in rows:
        name, start = r["employee"], (r.get("shift_start") or "").strip()
        mine = punches.get(_nk(name)) or []
        sched_start, sched_end = _minutes(start), _minutes(r.get("shift_end"))
        # When they were due to clock in: the start less their role's lead.
        due_in = None if sched_start is None else sched_start - leads.get((r.get("role") or "").strip().lower(), 0)
        outcome, late, covered, notice = None, None, None, None
        if mine:
            # The punch nearest the scheduled start is this shift's.
            best = min(mine, key=lambda p: abs((_minutes(p.get("shift_start")) or 0) - (sched_start or 0)))
            in_m, out_m = _minutes(best.get("shift_start")), _minutes(best.get("shift_end"))
            outcome = "on_time"
            if in_m is not None and due_in is not None and in_m - due_in > LATE_AFTER_MINUTES:
                outcome, late = "late", int(in_m - due_in)
            elif (out_m is not None and sched_end is not None and sched_end - out_m >= LEFT_EARLY_HOURS * 60
                  and out_m > (in_m or 0)):
                outcome = "left_early"
        else:
            if releases is None:
                releases = _releases(restaurant_id, day, restaurant=restaurant, db_path=db_path)
            outcome, covered, notice = _unworked(releases, name, start)
        if record(restaurant_id, name, day, outcome, "schedule_vs_punch_join", shift_start=start, minutes_late=late,
                  covered_by=covered, role=r.get("role"), history_id=hid, person_id=pids.get(name),
                  notice_minutes=notice, db_path=db_path):
            n += 1
    return {"watched": True, "recorded": n}


def from_coverage_issues(restaurant_id, today=None, days=JOIN_DAYS, db_path=None) -> int:
    """What the live clock-in check saw, recorded per person: somebody it
    saw clock in after their issue opened → late; somebody still missing on
    an issue still open once the night is over → no_show — or called_out
    with the notice they gave, when they had asked to drop the shift and
    nobody approved it (L-18). A manager closing the issue, or a teammate
    taking the shift from it, says nothing about whether they came
    (dsr.block_labor's rule), so that is left to the join and the closer.

    "Today" is the restaurant's business date (schedule audit 10/3/26 E-4):
    at 1am on a 2am close, tonight's issue is not over yet. A role issue
    (issues.coverage_people) is read person by person. Returns outcomes
    written."""
    import issues
    from time_utils import business_date
    if today is None:
        try:
            r_ = _models_mod.get_restaurant(restaurant_id, db_path or _models_mod.DB_PATH)
            from time_utils import restaurant_now
            today = business_date(r_, restaurant_now(r_, naive=True))
        except Exception:
            today = date.today()
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
        for p in issues.coverage_people(r):
            day = p.get("business_date") or parts[1]
            notice = None
            if p.get("status") == "arrived":
                outcome = "late"
            elif p.get("status") == "missing" and r["status"] != "resolved" and day < today.isoformat():
                outcome = "called_out" if p.get("asked_off") else "no_show"
                notice = p.get("notice_minutes") if p.get("asked_off") else None
            else:
                continue
            if record(restaurant_id, p["employee"], day, outcome, "coverage_check",
                      shift_start=p.get("shift_start") or "", role=p.get("role"),
                      note="seen by the live clock-in check", notice_minutes=notice, db_path=db_path):
                n += 1
    return n


def from_closeout(restaurant_id, business_date, callouts_text, db_path=None) -> list:
    """A closer's "who didn't make it", CONFIRMED: each name in it that is
    exactly someone on that night's published schedule (their spelling or
    an alias — never a guess from a first name) → called_out, with the
    notice they gave when the shift requests show they asked off ahead
    (L-18); somebody a manager had already let off the shift (an approved
    drop nobody claimed) → excused, never a call-out (E-5). Returns the
    names recorded."""
    import re
    text = str(callouts_text or "").strip()
    if not text:
        return []
    rows, hid = _published_rows(restaurant_id, business_date, db_path)
    if not rows:
        return []
    releases = _releases(restaurant_id, business_date, db_path=db_path)
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
        start = (row.get("shift_start") or "").strip()
        outcome, covered, notice = _unworked(releases, row["employee"], start)
        if outcome == "covered":
            continue                       # their shift was somebody else's tonight
        if outcome == "no_show":
            outcome = "called_out"         # the closer's word: they didn't make it
        if record(restaurant_id, row["employee"], business_date, outcome, "closeout_confirmed",
                  shift_start=start, role=row.get("role"), history_id=hid,
                  note=f"the close-out: {phrase[:120]}", notice_minutes=notice, db_path=db_path):
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


def reliability_events(restaurant_id, since=None, db_path=None, detail=False) -> list:
    """[(name, iso_date, outcome)] — every shift somebody watched: the
    recorded outcomes, and the shifts from a source that carried a REAL
    schedule (an upload with scheduled and actual hours: actual 0 is a
    no-show, 1.5h short a short shift). A POS row whose schedule was its
    actual hours copied says nothing about attendance and is never read.

    `detail`: [(name, iso_date, outcome, extra)] instead, `extra` being
    {"notice_minutes": n or None, "timed": bool} — how far ahead a call-out
    told the restaurant (L-18) and whether the outcome was read off a
    clock-in time, so lateness can be judged on it (D-44). The one weighted
    reader (staff_settings.attendance_events) reads this form."""
    import shift_facts
    out, seen = [], set()
    for e in events(restaurant_id, since=since, db_path=db_path):
        outcome = "short" if e["outcome"] == "left_early" else e["outcome"]
        if detail:
            out.append((e["employee_name"], e["business_date"], outcome,
                        {"notice_minutes": e.get("notice_minutes"), "timed": e["outcome"] in TIMED}))
        else:
            out.append((e["employee_name"], e["business_date"], outcome))
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
        outcome = "no_show" if actual == 0 else ("short" if sched - actual >= LEFT_EARLY_HOURS else "worked")
        out.append((name, day, outcome, {"notice_minutes": None, "timed": False}) if detail else (name, day, outcome))
    return out


def watched(restaurant_id, days=90, db_path=None) -> bool:
    """Whether anybody's attendance has been watched here lately."""
    since = (date.today() - timedelta(days=days)).isoformat()
    return bool(reliability_events(restaurant_id, since=since, db_path=db_path))


def summary_lines(restaurant_id, today=None, db_path=None, limit=6) -> list:
    """Plain sentences about attendance over the last 12 weeks, for the
    people memory: who missed shifts or came late, and on which weekday.
    Every watched shift counts — the recorded outcomes and an upload's real
    schedule against its punches (reliability_events), the same shifts the
    reliability figure reads — so the memory never says less than the
    schedule's own RELIABILITY block."""
    today = today or date.today()
    since = (today - timedelta(weeks=12)).isoformat()
    # A shift somebody else took, or one a manager let them off (E-5), was
    # not theirs to work or miss.
    evs = [e for e in reliability_events(restaurant_id, since=since, db_path=db_path)
           if str(e[1])[:10] <= today.isoformat() and e[2] not in NOT_OWED]
    if not evs:
        return []
    by = {}
    for name, day, outcome in evs:
        t = by.setdefault(_nk(name), {"name": name, "n": 0, "miss": 0, "late": 0, "days": {}, "last": day})
        t["n"] += 1
        t["last"] = max(t["last"], day)
        if outcome in MISSES:
            t["miss"] += 1
            wd = date.fromisoformat(str(day)[:10]).strftime("%A")
            t["days"][wd] = t["days"].get(wd, 0) + 1
        elif outcome == "late":
            t["late"] += 1
    lines = []
    for t in sorted(by.values(), key=lambda t: (-t["miss"], -t["late"], t["name"])):
        if not t["miss"] and t["late"] < 2:
            continue
        bits = []
        top_day = max(t["days"].items(), key=lambda kv: kv[1]) if t["days"] else None
        if t["miss"]:
            bits.append(f"missed {t['miss']} of {t['n']} watched shifts"
                        + (f", {top_day[1]} of them {top_day[0]}s" if top_day[1] >= 2 else ""))
        if t["late"] >= 2:
            bits.append(f"late {t['late']} times")
        lines.append({"text": f"{t['name']}: " + "; ".join(bits) + " (last 12 weeks)", "date": t["last"],
                      "name": t["name"], "weekdays": sorted(t["days"]), "misses": t["miss"], "late": t["late"],
                      "top_day": top_day[0] if top_day and top_day[1] >= 2 else None})
        if len(lines) >= limit:
            break
    return lines
