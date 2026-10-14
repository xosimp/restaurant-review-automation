"""
schedule_shifts.py — one row per shift of each copy of a stored week, in the
primary database (schedule re-audit 10/11/26 ARCH-7, contract #5).

The week is stored as text: schedule_history.schedule_csv (the WORKING copy
— the draft, or a sent week with the manager's unsent edits) and
published_csv (the PUBLISHED copy — what staff were told), with every saved
state in schedule_versions. Every question about a shift — who works Friday,
the shift a request is about, one person's week — parsed whole weeks, and
the normalised model (shifts keyed by shift_id on real time) lived only in
the Postgres shadow behind PG_SHADOW_WRITE.

This module is that model in the primary database, as a projection written
at the one commit point: every write of a week's text goes through
schedule_versions.store_week (or models.save_schedule_history's insert),
which calls `project` inside the same transaction — so the table can never
disagree with the text it was written beside. The CSV stays the stored
revision until every reader has moved (the readers still on it are listed
in MODULE_OVERVIEW.md); the projection is what per-shift queries read.

Rows (`schedule_shifts`), one per shift per copy of a week:
  history_id, copy ('working' | 'published'), revision (the
  schedule_versions version the copy was written at; 0 before versions),
  shift_id (schedule_rows' id, kept by a shift across edits, versions,
  swaps and covers), person_id (people.id when the name means exactly one
  person), employee_name / employee_key, role, date, shift_start /
  shift_end as written, starts_at / ends_at on the business date's wall
  clock (an overnight close past midnight), hours, locked, week_start.

One revision of each copy is kept — the current one. Every older state is
schedule_versions' (a full or delta snapshot); a row per shift per version
would have multiplied the table by every save of every week, the cost the
version deltas were built to stop (SCALE-34).

`schedule_shift_weeks` marks a week projected (history_id, revision,
projected_at): a reader uses the table for a projected week and the text
otherwise, so the boot backfill (bounded, newest weeks first, resumable by
the marker itself) never leaves a reader with half a week.

L2: reads people only through the rows' stamped person ids.
"""
import logging
import time
import zlib

import models as _models_mod
import schedule_rows as _srows
import schedule_time as _stime

log = logging.getLogger(__name__)

COPIES = ("working", "published")
BACKFILL_SECONDS = 10.0


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == _models_mod.DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def init_schedule_shifts(db_path=None):
    """The projection's tables, at boot (init_db). history_id carries no
    foreign key on purpose: the projection is derived, every deleter of a
    week removes its rows (models.delete_schedule_history, the drafts
    prune), and a stray row for a week that is gone is never read (readers
    join schedule_history)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS schedule_shifts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            history_id     INTEGER NOT NULL,
            copy           TEXT    NOT NULL,
            revision       INTEGER NOT NULL DEFAULT 0,
            shift_id       TEXT    NOT NULL,
            person_id      INTEGER,
            employee_name  TEXT    NOT NULL,
            employee_key   TEXT    NOT NULL,
            role           TEXT,
            date           TEXT    NOT NULL,
            shift_start    TEXT,
            shift_end      TEXT,
            starts_at      TEXT,
            ends_at        TEXT,
            hours          REAL,
            scheduled_hours TEXT,
            notes          TEXT,
            locked         INTEGER NOT NULL DEFAULT 0,
            week_start     TEXT,
            UNIQUE(history_id, copy, shift_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_shifts_date ON schedule_shifts(restaurant_id, date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_shifts_sid ON schedule_shifts(restaurant_id, shift_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_shifts_person "
                     "ON schedule_shifts(restaurant_id, person_id, date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_shifts_key "
                     "ON schedule_shifts(restaurant_id, employee_key, date)")
        conn.execute("""CREATE TABLE IF NOT EXISTS schedule_shift_weeks (
            history_id     INTEGER PRIMARY KEY,
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            revision       INTEGER NOT NULL DEFAULT 0,
            source_sig     TEXT,
            projected_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_shift_weeks_rid "
                     "ON schedule_shift_weeks(restaurant_id)")
        conn.commit()
    finally:
        conn.close()


def _fold(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def _stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M") if dt is not None else None


def shift_values(row) -> dict:
    """One stored row as the projection holds it (pure)."""
    s, e = _stime.shift_span(row)
    hours = round((e - s).total_seconds() / 3600, 2) if s is not None and e is not None else None
    try:
        pid = int(row.get("person_id")) if str(row.get("person_id") or "").strip() else None
    except (TypeError, ValueError):
        pid = None
    name = " ".join(str(row.get("employee") or "").split())
    return {"shift_id": str(row.get("shift_id") or "").lower(), "person_id": pid, "employee_name": name,
            "employee_key": _fold(name), "role": (row.get("role") or "").strip() or None,
            "date": str(row.get("date") or "")[:10], "shift_start": (row.get("shift_start") or "").strip(),
            "shift_end": (row.get("shift_end") or "").strip(), "starts_at": _stamp(s), "ends_at": _stamp(e),
            "hours": hours, "scheduled_hours": (row.get("scheduled_hours") or "").strip(),
            "notes": (row.get("notes") or "").strip(), "locked": 1 if _srows.is_locked(row) else 0}


_COLS = ("shift_id", "person_id", "employee_name", "employee_key", "role", "date", "shift_start", "shift_end",
         "starts_at", "ends_at", "hours", "scheduled_hours", "notes", "locked")


def source_sig(row) -> str:
    """A short signature of what a week's projection was written from — its
    two copies' text and its publish stamp. A reader compares it with the
    week as it stands and reads the text instead when they differ (a write
    that went around store_week: an old script, a test's own UPDATE), so a
    stale projection is never served."""
    blob = "\x00".join(str(row[k] or "") for k in ("schedule_csv", "published_csv", "published_at"))
    return f"{len(blob)}:{zlib.crc32(blob.encode('utf-8')):08x}"


def _copy_rows(rows) -> list:
    """The rows of one copy that are shifts with an id, each id once (a row
    without one — text written before ids — gets one here for the
    projection only; the stored text keeps its own until its next write)."""
    out, seen = [], set()
    for r in _srows.carry_ids(rows, rows, known_ids=[x.get("shift_id") for x in rows]):
        sid = str(r.get("shift_id") or "").lower()
        if not (r.get("employee") or "").strip() or sid in seen:
            continue
        seen.add(sid)
        out.append(r)
    return out


def project(conn, restaurant_id, history_id) -> int:
    """Rewrite the projection of one stored week from its text, on the
    caller's connection inside its write transaction — the one commit
    point's last step. Returns the rows written. A week that is gone has
    its rows removed. Raises on a database error, so the caller's write
    rolls back with it: the table and the text never disagree."""
    row = conn.execute("SELECT id, restaurant_id, week_start, schedule_csv, published_csv, published_at "
                       "FROM schedule_history WHERE id=? AND restaurant_id=?",
                       (history_id, restaurant_id)).fetchone()
    try:
        conn.execute("DELETE FROM schedule_shifts WHERE history_id=?", (history_id,))
    except Exception as e:
        # A database boot has not reached yet (init_db creates the table):
        # the week has no projection, and the boot backfill writes it.
        if "no such table" in str(e).lower():
            return 0
        raise
    if row is None:
        conn.execute("DELETE FROM schedule_shift_weeks WHERE history_id=?", (history_id,))
        return 0
    try:
        rev = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schedule_versions WHERE history_id=?",
                           (history_id,)).fetchone()[0] or 0
    except Exception as e:
        if "no such table" not in str(e).lower():
            raise
        rev = 0
    copies = [("working", row["schedule_csv"])]
    if row["published_at"]:
        copies.append(("published", row["published_csv"] if row["published_csv"] is not None
                       else row["schedule_csv"]))
    n = 0
    values = []
    for copy, text in copies:
        for r in _copy_rows(_srows.parse(text or "")):
            v = shift_values(r)
            values.append((restaurant_id, history_id, copy, int(rev), row["week_start"])
                          + tuple(v[c] for c in _COLS))
            n += 1
    if values:
        conn.executemany(
            "INSERT INTO schedule_shifts (restaurant_id, history_id, copy, revision, week_start, "
            + ", ".join(_COLS) + ") VALUES (" + ",".join("?" * (5 + len(_COLS))) + ")", values)
    conn.execute("INSERT INTO schedule_shift_weeks (history_id, restaurant_id, revision, source_sig, projected_at) "
                 "VALUES (?,?,?,?,datetime('now')) ON CONFLICT(history_id) DO UPDATE SET "
                 "revision=excluded.revision, source_sig=excluded.source_sig, projected_at=excluded.projected_at",
                 (history_id, restaurant_id, int(rev), source_sig(row)))
    return n


def projected(conn, history_id) -> bool:
    """Whether one stored week's projection has been written."""
    try:
        return conn.execute("SELECT 1 FROM schedule_shift_weeks WHERE history_id=?",
                            (history_id,)).fetchone() is not None
    except Exception as e:
        if "no such table" in str(e).lower():
            return False
        raise


def backfill(db_path=None, max_seconds: float = BACKFILL_SECONDS) -> int:
    """Project every stored week not projected yet — newest first, each in
    its own write, bounded by `max_seconds`. Resumable by its own marker:
    whatever it did not reach, the next pass does (a reader uses the text
    for a week with no marker meanwhile). At boot (init_db). Returns the
    weeks projected."""
    conn = get_conn(db_path)
    try:
        try:
            todo = [(r["id"], r["restaurant_id"]) for r in conn.execute(
                "SELECT h.id, h.restaurant_id FROM schedule_history h WHERE NOT EXISTS "
                "(SELECT 1 FROM schedule_shift_weeks w WHERE w.history_id=h.id) ORDER BY h.id DESC").fetchall()]
        except Exception as e:
            if "no such table" in str(e).lower():
                return 0
            raise
    finally:
        conn.close()
    stop = time.monotonic() + float(max_seconds)
    done = 0
    for hid, rid in todo:
        if time.monotonic() > stop:
            break
        conn = get_conn(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not projected(conn, hid):
                project(conn, rid, hid)
            conn.commit()
            done += 1
        except Exception as e:
            conn.rollback()
            log.warning("shifts of week %s not projected: %s", hid, e)
        finally:
            conn.close()
    return done


# ── readers ──────────────────────────────────────────────────────────────────

def _as_row(r) -> dict:
    """A projected shift in the schedule_rows shape (plus its projection
    fields), so a reader moved onto the table reads what it read before."""
    d = dict(r)
    try:
        from datetime import date as _date
        wd = _date.fromisoformat(d["date"]).strftime("%A")
    except (TypeError, ValueError):
        wd = ""
    return {"date": d["date"], "day": wd, "employee": d["employee_name"], "role": d.get("role") or "",
            "shift_start": d.get("shift_start") or "", "shift_end": d.get("shift_end") or "",
            "scheduled_hours": d.get("scheduled_hours") if d.get("scheduled_hours") is not None
            else ("" if d.get("hours") is None else f"{d['hours']:g}"), "notes": d.get("notes") or "",
            "shift_id": d["shift_id"], "person_id": "" if d.get("person_id") is None else str(d["person_id"]),
            "locked": "1" if d.get("locked") else "", "history_id": d["history_id"], "copy": d["copy"],
            "starts_at": d.get("starts_at"), "ends_at": d.get("ends_at"), "hours": d.get("hours")}


def current(conn, history_id) -> bool:
    """Whether one week's projection was written from the week as it stands
    now (its marker's source_sig matches the text and the stamp)."""
    try:
        m = conn.execute("SELECT w.source_sig, h.schedule_csv, h.published_csv, h.published_at "
                         "FROM schedule_shift_weeks w JOIN schedule_history h ON h.id=w.history_id "
                         "WHERE w.history_id=?", (history_id,)).fetchone()
    except Exception as e:
        if "no such table" in str(e).lower():
            return False
        raise
    return m is not None and m["source_sig"] == source_sig(m)


def copy_rows(conn, history_id, copy="published"):
    """The projected rows of one copy of one week, or None when the week has
    no current projection (the caller reads the text)."""
    if copy not in COPIES:
        raise ValueError(copy)
    if not current(conn, history_id):
        return None
    return [_as_row(r) for r in conn.execute(
        "SELECT * FROM schedule_shifts WHERE history_id=? AND copy=? ORDER BY date, starts_at, id",
        (history_id, copy)).fetchall()]


def live_shifts_on(restaurant_id, day, db_path=None) -> tuple:
    """(rows, history_id) — the shifts staff have on `day`: the published
    copy of the live week covering it (models.live_week_for, the one rule),
    read from the projection; from the week's text when it has none yet.
    ([], None) when no live week covers the day."""
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day or "")[:10]
    w = _models_mod.live_week_for(restaurant_id, iso, db_path=db_path or _models_mod.DB_PATH)
    if not w:
        return [], None
    conn = get_conn(db_path)
    try:
        got = None
        # A week shared before the publish stamp existed has no published
        # copy in the projection: its text is the live copy.
        if w.get("published_at"):
            got = copy_rows(conn, w["id"], "published")
    finally:
        conn.close()
    if got is None:
        got = _srows.parse(w.get("schedule_csv") or "")
    return [r for r in got if str(r.get("date") or "")[:10] == iso], w["id"]


def find(restaurant_id, shift_id, copy="published", db_path=None):
    """The newest projected row of one shift by its id (the published copy
    by default), or None."""
    if not _srows.valid_id(shift_id):
        return None
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT s.* FROM schedule_shifts s JOIN schedule_history h ON h.id=s.history_id "
                         "WHERE s.restaurant_id=? AND s.shift_id=? AND s.copy=? "
                         "ORDER BY s.history_id DESC LIMIT 1",
                         (restaurant_id, str(shift_id).lower(), copy)).fetchone()
    finally:
        conn.close()
    return _as_row(r) if r else None


def person_shifts(restaurant_id, person_id=None, name=None, lo=None, hi=None, copy="published",
                  db_path=None) -> list:
    """One person's shifts on the LIVE copies (by person id, else by their
    folded name) between `lo` and `hi` (ISO dates, either open) — one
    indexed query, no week parsed."""
    from models import live_where
    if person_id is None and not name:
        return []
    q = ("SELECT s.* FROM schedule_shifts s JOIN schedule_history h ON h.id=s.history_id "
         "WHERE s.restaurant_id=? AND s.copy=? AND " + live_where("h"))
    args = [restaurant_id, copy]
    if person_id is not None:
        q += " AND s.person_id=?"
        args.append(int(person_id))
    else:
        q += " AND s.employee_key=?"
        args.append(_fold(name))
    if lo:
        q += " AND s.date>=?"
        args.append(str(lo)[:10])
    if hi:
        q += " AND s.date<=?"
        args.append(str(hi)[:10])
    q += " ORDER BY s.date, s.starts_at"
    conn = get_conn(db_path)
    try:
        return [_as_row(r) for r in conn.execute(q, args).fetchall()]
    finally:
        conn.close()


def double_bookings(conn, history_id, copy="published") -> list:
    """[(shift_id, shift_id)] — two shifts of one person (by person id, else
    folded name) in one copy of a week whose times overlap. The publish gate
    and the sweep refuse them; this is the database's own check of what a
    copy holds (ARCH-7)."""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT a.shift_id, b.shift_id FROM schedule_shifts a JOIN schedule_shifts b "
        "ON a.history_id=b.history_id AND a.copy=b.copy AND a.id<b.id "
        "AND ((a.person_id IS NOT NULL AND a.person_id=b.person_id) OR "
        "     (a.person_id IS NULL AND b.person_id IS NULL AND a.employee_key=b.employee_key)) "
        "AND a.starts_at < b.ends_at AND b.starts_at < a.ends_at "
        "WHERE a.history_id=? AND a.copy=?", (history_id, copy)).fetchall()]
