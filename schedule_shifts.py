"""
schedule_shifts.py — one row per shift of each copy of a stored week, in the
primary database, and THE read of a stored week's rows (schedule re-audit
10/11/26 ARCH-7, contract #5).

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
disagree with the text it was written beside.

READING A WEEK. Every reader of a stored week's rows reads them here —
`rows_of` (one week, by history id and copy), `rows_by_week` (many weeks in
two queries), `live_week_rows` (the week in force on a date), `revision_rows`
(one schedule_versions revision: the projection when it is the revision the
table holds) — never by parsing the text itself
(tests/test_sf11_w_shift_reads.py holds the code to it: a parse of a week's
stored text outside this module and the storage modules is a ratchet that
may only shrink). A reader gets the rows exactly as the text parses
(schedule_rows.parse's shape and order) from the table; it gets the text's
parse instead — the fallback, inside this module — only for a week the
table cannot serve exactly:

  * a week not projected yet (written before the table existed, and the
    boot backfill has not reached it);
  * a week whose text changed around store_week since (`source_sig`);
  * a week whose text the table cannot hold exactly (`exact` 0: rows from
    before shift ids, a duplicated id, a field the projection normalises).

Rows (`schedule_shifts`), one per shift per copy of a week:
  history_id, copy ('working' | 'published'), revision (the
  schedule_versions version the copy was written at; 0 before versions),
  seq (the row's place in the copy's text), shift_id (schedule_rows' id,
  kept by a shift across edits, versions, swaps and covers), person_id
  (people.id when the name means exactly one person), person_ref ("p:<id>",
  else "n:<folded name>" — who the double-booking guard compares),
  employee_name / employee_key, role, date, day, shift_start / shift_end as
  written, starts_at / ends_at on the business date's wall clock (an
  overnight close past midnight), hours, locked, week_start, and
  overlap_ok_with (the shift ids this one may overlap — see below).

One revision of each copy is kept — the current one. Every older state is
schedule_versions' (a full or delta snapshot); a row per shift per version
would have multiplied the table by every save of every week, the cost the
version deltas were built to stop (SCALE-34).

THE DOUBLE-BOOKING GUARD. One person on two overlapping shifts in the
PUBLISHED copy of a week is refused by the database: a trigger
(schedule_shifts_no_double_booking — SQLite's in init_schedule_shifts, its
plpgsql form in pg_copy.trigger_statements) aborts the insert, so the week's
whole write rolls back with it. `project` checks first and raises
DoubleBooking naming the people, so a caller gets a readable refusal and the
trigger is the backstop for any write that goes around it. An overlap is
let through only pair by pair (`overlap_ok_with`) when:
  * the publish gate cleared the week (schedule_publish._stamp passes
    allow_overlaps — an overlap is a must-fix a Send may go past only with a
    reason), or
  * the pair was already in the copy before this write (a cover on a week
    sent past an overlap must not be refused for it), or
  * the week is projected for the first time (the boot backfill: a week
    published before the guard existed is what staff already have).
The working copy (a draft, an unsent edit) is never refused: the sweep and
the publish gate say what it holds. On Postgres the same trigger runs; every
week write there holds the write lock (BEGIN IMMEDIATE is
pg_advisory_xact_lock — db_dialect), so two writers cannot each pass the
check. (The shadow's sched.shifts carries the same rule as an exclusion
constraint over a tstzrange — migrations/postgres/0001; the copied table
keeps its times as text, which no immutable index expression can cast.)

`schedule_shift_weeks` marks a week projected (history_id, revision,
source_sig, exact, fmt, projected_at). The boot backfill is bounded and
resumable: newest weeks first, at most BACKFILL_MAX_WEEKS weeks and
BACKFILL_SECONDS per boot, with its place kept in job_cursors
(CURSOR_KEY), wrapping to the newest week once it reaches the oldest — and
it re-projects a week whose marker is missing, of an older format, or stale.

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
# Weeks the boot backfill examines per boot at most (with the time bound):
# measured 10/14/26 on a synthetic database of 2,000 weeks x 150 shifts
# (tests/test_sf11_w_shift_reads.py::test_the_boot_backfill_is_bounded_and_resumes, and the
# report's figure) — well inside Railway's 120-second healthcheck.
BACKFILL_MAX_WEEKS = 600
CURSOR_KEY = "schedule_shifts:backfill"
# The projection's format: a marker of an older one is re-projected by the
# backfill, and never served meanwhile (its `exact` is 0).
FORMAT = 2
TRIGGER = "schedule_shifts_no_double_booking"


class DoubleBooking(Exception):
    """A write would put one person on two overlapping shifts in a week's
    published copy. `pairs` [(shift_id, shift_id, employee, date)]."""

    def __init__(self, pairs):
        self.pairs = list(pairs or [])
        first = self.pairs[0] if self.pairs else ("", "", "someone", "")
        super().__init__(f"{first[2]} would be on two overlapping shifts on {first[3]}")


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == _models_mod.DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


# The trigger's condition, SQLite's form (pg_copy.trigger_statements holds
# the plpgsql twin; tests/test_sf11_w_shift_reads.py holds the two to one rule).
_TRIGGER_SQL = (
    f"CREATE TRIGGER IF NOT EXISTS {TRIGGER} BEFORE INSERT ON schedule_shifts "
    "WHEN NEW.copy='published' AND NEW.person_ref IS NOT NULL AND NEW.starts_at IS NOT NULL "
    "AND NEW.ends_at IS NOT NULL AND EXISTS (SELECT 1 FROM schedule_shifts o WHERE o.history_id=NEW.history_id "
    "AND o.copy=NEW.copy AND o.person_ref=NEW.person_ref AND o.starts_at < NEW.ends_at "
    "AND NEW.starts_at < o.ends_at "
    "AND instr(' ' || COALESCE(NEW.overlap_ok_with, '') || ' ', ' ' || o.shift_id || ' ')=0 "
    "AND instr(' ' || COALESCE(o.overlap_ok_with, '') || ' ', ' ' || NEW.shift_id || ' ')=0) "
    "BEGIN SELECT RAISE(ABORT, 'schedule_shifts: double booking'); END")


def init_schedule_shifts(db_path=None):
    """The projection's tables and its guard, at boot (init_db). history_id
    carries no foreign key on purpose: the projection is derived, every
    deleter of a week removes its rows (models.delete_schedule_history, the
    drafts prune), and a stray row for a week that is gone is never read
    (readers join schedule_history)."""
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
            seq            INTEGER,
            day            TEXT,
            person_ref     TEXT,
            overlap_ok_with TEXT,
            UNIQUE(history_id, copy, shift_id)
        )""")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_shifts)").fetchall()}
        for col, typ in (("seq", "INTEGER"), ("day", "TEXT"), ("person_ref", "TEXT"),
                         ("overlap_ok_with", "TEXT")):
            if col not in cols:
                conn.execute(f"ALTER TABLE schedule_shifts ADD COLUMN {col} {typ}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_shifts_date ON schedule_shifts(restaurant_id, date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_shifts_sid ON schedule_shifts(restaurant_id, shift_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_shifts_person "
                     "ON schedule_shifts(restaurant_id, person_id, date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_shifts_key "
                     "ON schedule_shifts(restaurant_id, employee_key, date)")
        # The guard's lookup: one person's shifts in one copy of one week.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_shifts_ref "
                     "ON schedule_shifts(history_id, copy, person_ref)")
        conn.execute("""CREATE TABLE IF NOT EXISTS schedule_shift_weeks (
            history_id     INTEGER PRIMARY KEY,
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            revision       INTEGER NOT NULL DEFAULT 0,
            source_sig     TEXT,
            exact          INTEGER NOT NULL DEFAULT 0,
            fmt            INTEGER NOT NULL DEFAULT 0,
            projected_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        wcols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_shift_weeks)").fetchall()}
        for col in ("exact", "fmt"):
            if col not in wcols:
                conn.execute(f"ALTER TABLE schedule_shift_weeks ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_shift_weeks_rid "
                     "ON schedule_shift_weeks(restaurant_id)")
        conn.execute(_TRIGGER_SQL)
        conn.commit()
    finally:
        conn.close()


def _fold(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def _stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M") if dt is not None else None


def person_ref(person_id, name) -> str:
    """Who the guard compares: the person's id, else their folded name."""
    return f"p:{int(person_id)}" if person_id is not None else f"n:{_fold(name)}"


def shift_values(row) -> dict:
    """One stored row as the projection holds it (pure)."""
    s, e = _stime.shift_span(row)
    hours = round((e - s).total_seconds() / 3600, 2) if s is not None and e is not None else None
    try:
        pid = int(row.get("person_id")) if str(row.get("person_id") or "").strip() else None
    except (TypeError, ValueError):
        pid = None
    name = str(row.get("employee") or "").strip()
    return {"shift_id": str(row.get("shift_id") or "").lower(), "person_id": pid, "employee_name": name,
            "employee_key": _fold(name), "role": (row.get("role") or "").strip() or None,
            "date": str(row.get("date") or "").strip()[:10], "day": str(row.get("day") or "").strip(),
            "shift_start": (row.get("shift_start") or "").strip(),
            "shift_end": (row.get("shift_end") or "").strip(), "starts_at": _stamp(s), "ends_at": _stamp(e),
            "hours": hours, "scheduled_hours": (row.get("scheduled_hours") or "").strip(),
            "notes": (row.get("notes") or "").strip(), "locked": 1 if _srows.is_locked(row) else 0,
            "person_ref": person_ref(pid, name)}


_COLS = ("shift_id", "person_id", "employee_name", "employee_key", "role", "date", "day", "shift_start",
         "shift_end", "starts_at", "ends_at", "hours", "scheduled_hours", "notes", "locked", "person_ref")


def stored_row(d) -> dict:
    """A projected shift (a schedule_shifts row, or shift_values' dict) in
    schedule_rows.parse's shape — every STORED column a string."""
    pid = d.get("person_id")
    return {"date": d.get("date") or "", "day": d.get("day") or "", "employee": d.get("employee_name") or "",
            "role": d.get("role") or "", "shift_start": d.get("shift_start") or "",
            "shift_end": d.get("shift_end") or "", "scheduled_hours": d.get("scheduled_hours") or "",
            "notes": d.get("notes") or "", "shift_id": d.get("shift_id") or "",
            "person_id": "" if pid is None or pid == "" else str(int(pid)),
            "locked": "1" if d.get("locked") else ""}


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
    projection only, and the week is marked inexact: readers read its
    text until its next write stamps ids)."""
    out, seen = [], set()
    for r in _srows.carry_ids(rows, rows, known_ids=[x.get("shift_id") for x in rows]):
        sid = str(r.get("shift_id") or "").lower()
        if not (r.get("employee") or "").strip() or sid in seen:
            continue
        seen.add(sid)
        out.append(r)
    return out


def _overlaps(values) -> list:
    """[(i, j)] — index pairs of one copy's shift values whose person (by
    person_ref) is on both and whose times overlap. Pure."""
    by = {}
    for i, v in enumerate(values):
        if v.get("starts_at") and v.get("ends_at") and v.get("person_ref"):
            by.setdefault(v["person_ref"], []).append(i)
    out = []
    for idx in by.values():
        if len(idx) < 2:
            continue
        idx.sort(key=lambda k: values[k]["starts_at"])
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                va, vb = values[idx[a]], values[idx[b]]
                if vb["starts_at"] >= va["ends_at"]:
                    break
                out.append((idx[a], idx[b]))
    return out


def _prior_pairs(conn, history_id) -> set:
    """The overlapping pairs (frozenset of two shift ids) the week's
    published copy already held before this write."""
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT shift_id, person_id, employee_key, person_ref, starts_at, ends_at FROM schedule_shifts "
            "WHERE history_id=? AND copy='published'", (history_id,)).fetchall()]
    except Exception as e:
        if "no such" in str(e).lower():
            return set()
        raise
    for r in rows:
        if not r.get("person_ref"):
            r["person_ref"] = f"p:{int(r['person_id'])}" if r.get("person_id") is not None \
                else f"n:{r.get('employee_key') or ''}"
    return {frozenset((rows[i]["shift_id"], rows[j]["shift_id"])) for i, j in _overlaps(rows)}


def project(conn, restaurant_id, history_id, allow_overlaps=False) -> int:
    """Rewrite the projection of one stored week from its text, on the
    caller's connection inside its write transaction — the one commit
    point's last step. Returns the rows written. A week that is gone has
    its rows removed. Raises DoubleBooking when the write would put one
    person on two overlapping shifts in the published copy that were not
    already there (module doc; `allow_overlaps`: the publish gate cleared
    the week), and on a database error — so the caller's write rolls back
    with it: the table and the text never disagree."""
    row = conn.execute("SELECT id, restaurant_id, week_start, schedule_csv, published_csv, published_at "
                       "FROM schedule_history WHERE id=? AND restaurant_id=?",
                       (history_id, restaurant_id)).fetchone()
    try:
        had = conn.execute("SELECT 1 FROM schedule_shift_weeks WHERE history_id=?", (history_id,)).fetchone()
    except Exception as e:
        # A database boot has not reached yet (init_db creates the table):
        # the week has no projection, and the boot backfill writes it.
        if "no such table" in str(e).lower():
            return 0
        raise
    prior = _prior_pairs(conn, history_id) if had else set()
    conn.execute("DELETE FROM schedule_shifts WHERE history_id=?", (history_id,))
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
    exact = True
    values = []
    for copy, text in copies:
        parsed = _srows.parse(text or "")
        kept = _copy_rows(parsed)
        vals = [shift_values(r) for r in kept]
        # Exactly what the text parses to, in its order — or readers read
        # the text (module doc).
        if len(kept) != len(parsed) or any(stored_row(v) != p for v, p in zip(vals, parsed)):
            exact = False
        if copy == "published":
            bad = []
            for i, j in _overlaps(vals):
                pair = frozenset((vals[i]["shift_id"], vals[j]["shift_id"]))
                if allow_overlaps or not had or pair in prior:
                    vals[i].setdefault("_ok", set()).add(vals[j]["shift_id"])
                    vals[j].setdefault("_ok", set()).add(vals[i]["shift_id"])
                else:
                    bad.append((vals[i]["shift_id"], vals[j]["shift_id"], vals[i]["employee_name"], vals[i]["date"]))
            if bad:
                raise DoubleBooking(bad)
        for seq, v in enumerate(vals):
            values.append((restaurant_id, history_id, copy, int(rev), row["week_start"], seq,
                           " ".join(sorted(v["_ok"])) if v.get("_ok") else None)
                          + tuple(v[c] for c in _COLS))
    if values:
        conn.executemany(
            "INSERT INTO schedule_shifts (restaurant_id, history_id, copy, revision, week_start, seq, "
            "overlap_ok_with, " + ", ".join(_COLS) + ") VALUES (" + ",".join("?" * (7 + len(_COLS))) + ")", values)
    conn.execute("INSERT INTO schedule_shift_weeks (history_id, restaurant_id, revision, source_sig, exact, fmt, "
                 "projected_at) VALUES (?,?,?,?,?,?,datetime('now')) ON CONFLICT(history_id) DO UPDATE SET "
                 "revision=excluded.revision, source_sig=excluded.source_sig, exact=excluded.exact, "
                 "fmt=excluded.fmt, projected_at=excluded.projected_at",
                 (history_id, restaurant_id, int(rev), source_sig(row), 1 if exact else 0, FORMAT))
    return len(values)


def projected(conn, history_id) -> bool:
    """Whether one stored week's projection has been written."""
    try:
        return conn.execute("SELECT 1 FROM schedule_shift_weeks WHERE history_id=?",
                            (history_id,)).fetchone() is not None
    except Exception as e:
        if "no such table" in str(e).lower():
            return False
        raise


def _cursor(conn, value=None):
    """The backfill's place (job_cursors CURSOR_KEY): the history id it
    resumes BELOW, None to start from the newest. Read, or written when
    `value` is given ("" clears it)."""
    if value is None:
        try:
            r = conn.execute("SELECT value FROM job_cursors WHERE key=?", (CURSOR_KEY,)).fetchone()
        except Exception as e:
            if "no such table" in str(e).lower():
                return None
            raise
        try:
            return int(r[0]) if r and str(r[0] or "").strip() else None
        except (TypeError, ValueError):
            return None
    conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                 (CURSOR_KEY, str(value)))
    return value


def _needs(m) -> bool:
    """Whether a week (its text and marker, one row) needs projecting."""
    return m["fmt"] is None or int(m["fmt"] or 0) < FORMAT or m["source_sig"] != source_sig(m)


def backfill(db_path=None, max_seconds: float = BACKFILL_SECONDS, max_weeks: int = BACKFILL_MAX_WEEKS) -> dict:
    """At boot (init_db): walk the stored weeks from the cursor down —
    newest first — projecting every one whose marker is missing, of an older
    format or stale, each in its own write. Bounded twice: `max_weeks` weeks
    examined and `max_seconds` per pass; resumable: the cursor (job_cursors
    CURSOR_KEY) keeps where it stopped, and the next pass starts there. A
    walk that reaches the oldest week clears the cursor, so the next pass
    starts from the newest again (a week a reader found stale meanwhile is
    met again). A reader uses the text for a week the walk has not reached.
    Returns {projected, examined, failed, hit_bound, cursor}."""
    out = {"projected": 0, "examined": 0, "failed": 0, "hit_bound": False, "cursor": None}
    stop = time.monotonic() + float(max_seconds)
    conn = get_conn(db_path)
    try:
        try:
            below = _cursor(conn)
            q = ("SELECT h.id, h.restaurant_id, h.schedule_csv, h.published_csv, h.published_at, "
                 "w.source_sig, w.fmt FROM schedule_history h LEFT JOIN schedule_shift_weeks w "
                 "ON w.history_id=h.id" + (" WHERE h.id<?" if below is not None else "") +
                 " ORDER BY h.id DESC LIMIT ?")
            batch = conn.execute(q, ((below,) if below is not None else ()) + (int(max_weeks),)).fetchall()
        except Exception as e:
            if "no such table" in str(e).lower():
                return out
            raise
        last = None
        for m in batch:
            if time.monotonic() > stop:
                out["hit_bound"] = True
                break
            out["examined"] += 1
            last = m["id"]
            if not _needs(m):
                continue
            try:
                conn.execute("BEGIN IMMEDIATE")
                project(conn, m["restaurant_id"], m["id"])
                conn.commit()
                out["projected"] += 1
            except Exception as e:
                conn.rollback()
                out["failed"] += 1
                log.warning("shifts of week %s not projected: %s", m["id"], e)
        if out["hit_bound"] or len(batch) >= int(max_weeks):
            out["hit_bound"] = True
            out["cursor"] = last
            _cursor(conn, "" if last is None else last)
        else:
            _cursor(conn, "")
        conn.commit()
    finally:
        conn.close()
    return out


# ── readers ──────────────────────────────────────────────────────────────────

def _as_row(r) -> dict:
    """A projected shift in the schedule_rows shape plus its projection
    fields (history_id, copy, starts_at, ends_at, hours)."""
    d = dict(r)
    out = stored_row(d)
    if not out["day"]:
        try:
            from datetime import date as _date
            out["day"] = _date.fromisoformat(d["date"]).strftime("%A")
        except (TypeError, ValueError):
            pass
    if not out["scheduled_hours"] and d.get("hours") is not None:
        out["scheduled_hours"] = f"{d['hours']:g}"
    out.update(history_id=d["history_id"], copy=d["copy"], starts_at=d.get("starts_at"),
               ends_at=d.get("ends_at"), hours=d.get("hours"))
    return out


def _servable(m) -> bool:
    """Whether a week's projection may be served for it: written from the
    week as it stands (source_sig), of the current format, and exact."""
    return (m["source_sig"] is not None and m["source_sig"] == source_sig(m)
            and int(m["fmt"] or 0) >= FORMAT and bool(m["exact"]))


def current(conn, history_id) -> bool:
    """Whether one week's projection was written from the week as it stands
    now (its marker's source_sig matches the text and the stamp) and holds
    the text exactly."""
    try:
        m = conn.execute("SELECT w.source_sig, w.exact, w.fmt, h.schedule_csv, h.published_csv, h.published_at "
                         "FROM schedule_shift_weeks w JOIN schedule_history h ON h.id=w.history_id "
                         "WHERE w.history_id=?", (history_id,)).fetchone()
    except Exception as e:
        if "no such" in str(e).lower():
            return False
        raise
    return m is not None and _servable(m)


def copy_rows(conn, history_id, copy="published"):
    """The projected rows of one copy of one week (with the projection
    fields), or None when the week has no servable projection (the caller
    reads rows_of instead)."""
    if copy not in COPIES:
        raise ValueError(copy)
    if not current(conn, history_id):
        return None
    return [_as_row(r) for r in conn.execute(
        "SELECT * FROM schedule_shifts WHERE history_id=? AND copy=? ORDER BY seq, id",
        (history_id, copy)).fetchall()]


def _resolve(m, copy):
    """(projection copy or None, the text that copy is) for one week's
    marker row: "working" is schedule_csv; "live" (or "published") is what
    staff have — models.live_csv_sql, COALESCE(published_csv,
    schedule_csv)."""
    if copy == "working":
        return "working", m["schedule_csv"] or ""
    text = m["published_csv"] if m["published_csv"] is not None else (m["schedule_csv"] or "")
    if m["published_at"]:
        return "published", text
    # Shared before the publish stamp existed: the working copy is the live
    # one — unless a published copy sits on a week with no stamp, which the
    # projection does not hold.
    return ("working" if m["published_csv"] is None else None), text


_MARK_SQL = ("SELECT h.id, h.schedule_csv, h.published_csv, h.published_at, w.source_sig, w.exact, w.fmt "
             "FROM schedule_history h LEFT JOIN schedule_shift_weeks w ON w.history_id=h.id WHERE h.id IN ")


def rows_by_week(history_ids, copy="live", conn=None, db_path=None, restaurant_id=None) -> dict:
    """{history_id: rows} — each week's rows in schedule_rows.parse's shape
    and order: the WORKING copy (`copy` "working") or the copy staff have
    ("live"). From the projection for every week it can serve exactly, in
    one query per copy; the text's parse for the rest (module doc). A week
    that is gone (or another restaurant's, with `restaurant_id`) reads [].
    `conn`: read inside the caller's transaction."""
    if copy not in ("working", "live", "published"):
        raise ValueError(copy)
    ids = []
    for h in history_ids or []:
        try:
            h = int(h)
        except (TypeError, ValueError):
            continue
        if h not in ids:
            ids.append(h)
    out = {h: [] for h in ids}
    if not ids:
        return out
    own = conn is None
    conn = conn or get_conn(db_path)
    try:
        serve = {"working": [], "published": []}
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            q = _MARK_SQL + "(" + ",".join("?" * len(chunk)) + ")"
            args = list(chunk)
            if restaurant_id is not None:
                q += " AND h.restaurant_id=?"
                args.append(restaurant_id)
            try:
                marks = conn.execute(q, args).fetchall()
            except Exception as e:
                if "no such" not in str(e).lower():
                    raise
                marks = conn.execute(
                    "SELECT id, schedule_csv, published_csv, published_at, NULL AS source_sig, 0 AS exact, "
                    "0 AS fmt FROM schedule_history WHERE id IN (" + ",".join("?" * len(chunk)) + ")"
                    + (" AND restaurant_id=?" if restaurant_id is not None else ""), args).fetchall()
            for m in marks:
                pcopy, text = _resolve(m, copy)
                if pcopy is not None and _servable(m):
                    serve[pcopy].append(m["id"])
                else:
                    # The fallback (module doc): this week's text, parsed here.
                    out[m["id"]] = _srows.parse(text)
        for pcopy, hids in serve.items():
            for i in range(0, len(hids), 400):
                chunk = hids[i:i + 400]
                for r in conn.execute(
                        "SELECT * FROM schedule_shifts WHERE copy=? AND history_id IN ("
                        + ",".join("?" * len(chunk)) + ") ORDER BY history_id, seq, id",
                        [pcopy] + chunk).fetchall():
                    out[r["history_id"]].append(stored_row(dict(r)))
    finally:
        if own:
            conn.close()
    return out


def rows_of(history_id, copy="live", conn=None, db_path=None, restaurant_id=None) -> list:
    """One week's rows (rows_by_week for one week): the copy staff have
    ("live", the default) or the WORKING copy ("working")."""
    if history_id is None:
        return []
    return rows_by_week([history_id], copy, conn=conn, db_path=db_path,
                        restaurant_id=restaurant_id).get(int(history_id), [])


def live_week_rows(restaurant_id, on_date, db_path=None) -> list:
    """The rows of the week in force for staff on `on_date` — its published
    copy (models.live_week_for, the one rule) — [] when no live week covers
    the date."""
    w = _models_mod.live_week_for(restaurant_id, on_date, db_path=db_path or _models_mod.DB_PATH)
    if not w:
        return []
    return rows_of(w["id"], "live", db_path=db_path)


def revision_rows(conn, history_id, version, text) -> list:
    """The rows of one schedule_versions revision whose text is `text`: the
    projection's when the table holds exactly that revision of a copy (its
    revision number and the copy's text both match), else the revision's
    text parsed — an older revision, which the table does not keep (one
    revision per copy, module doc)."""
    try:
        m = conn.execute(_MARK_SQL + "(?)", (history_id,)).fetchone()
        rev = conn.execute("SELECT MAX(revision) FROM schedule_shifts WHERE history_id=?",
                           (history_id,)).fetchone()
    except Exception as e:
        if "no such" not in str(e).lower():
            raise
        m = rev = None
    if m is not None and rev is not None and rev[0] is not None and int(rev[0]) == int(version or 0) \
            and _servable(m):
        for copy in ("working", "live"):
            pcopy, ctext = _resolve(m, copy)
            if pcopy is not None and (ctext or "") == (text or ""):
                return [stored_row(dict(r)) for r in conn.execute(
                    "SELECT * FROM schedule_shifts WHERE history_id=? AND copy=? ORDER BY seq, id",
                    (history_id, pcopy)).fetchall()]
    return _srows.parse(text or "")


def live_shifts_on(restaurant_id, day, db_path=None) -> tuple:
    """(rows, history_id) — the shifts staff have on `day`: the published
    copy of the live week covering it (models.live_week_for, the one rule),
    read from the projection (with its fields); from the week's text when it
    has no servable one. ([], None) when no live week covers the day."""
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day or "")[:10]
    w = _models_mod.live_week_for(restaurant_id, iso, db_path=db_path or _models_mod.DB_PATH)
    if not w:
        return [], None
    conn = get_conn(db_path)
    try:
        got = None
        # A week shared before the publish stamp existed has no published
        # copy in the projection: rows_of reads its live copy.
        if w.get("published_at"):
            got = copy_rows(conn, w["id"], "published")
        if got is None:
            got = rows_of(w["id"], "live", conn=conn)
    finally:
        conn.close()
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
    """[(shift_id, shift_id)] — two shifts of one person (by person_ref) in
    one copy of a week whose times overlap, the guard's own rule. The
    published copy holds only the pairs the guard let through (module doc);
    the working copy may hold any — the sweep and the publish gate say so."""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT a.shift_id, b.shift_id FROM schedule_shifts a JOIN schedule_shifts b "
        "ON a.history_id=b.history_id AND a.copy=b.copy AND a.id<b.id AND a.person_ref=b.person_ref "
        "AND a.starts_at < b.ends_at AND b.starts_at < a.ends_at "
        "WHERE a.history_id=? AND a.copy=?", (history_id, copy)).fetchall()]
