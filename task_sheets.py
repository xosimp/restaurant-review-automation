"""
task_sheets.py — the opening and closing sheets by job code, who owes them
today, and what happened (docs/plans/TASK_SHEETS_PLAN.md, built 9/30/26).

Erik's problem, in his words: the other managers do not do their opening and
closing duties consistently, so he ends up doing them himself. A flat
checklist everyone could see and nobody owned got done by Erik. So:

- A SHEET belongs to a job code and a shift kind (opening, closing, mid, or
  any), in the owner's words, with ordered lines. A line may be due a number
  of minutes after the shift starts (before it ends, for a closing sheet),
  may need proof (a photo, a number with an allowed range, a note) and may
  be critical.
- An ASSIGNMENT is one sheet for one business day, owned by the people the
  PUBLISHED schedule puts on that job code and shift — the opener is the
  earliest start, the closer the latest end. It snapshots the lines and their
  due times when it is issued, so a line added at noon does not make this
  morning's opener late. With no published schedule for the day it is issued
  unassigned: whoever signs in on that job code sees it, and the owner's day
  view says "no schedule published" — itself a finding. While it is open its
  PEOPLE follow the live published week: a same-day cover or swap hands it
  to whoever now works the shift (refresh_assignees; every read checks the
  week's signature, and re-reads the CSV only when it changed). A sheet
  nobody works that day is recorded status='none' and never shown.
- A COMPLETION is immutable: a tick writes a row, an un-tick writes another
  (undone=1). The newest row per line is its state. Late (after its due
  time) and flagged (a number outside its range) are stamped on the row.
- Owners do not tick. They read every sheet for the day (`day_view`), edit
  the sheets, and read the consistency report (`report`) — the managers side
  by side. Staff tick their own sheets; a manager also sees the floor and
  signs the shift off.
- Misses reach someone (`run_job`, every 20 minutes): a critical line past
  due opens a texted issue for the routed manager (and a critical reading
  out of its range does the moment it is ticked); a sheet left unfinished
  at shift end opens a quiet issue (Home and the issue list); three misses of
  one line by one person in 14 days is a pattern the owner sees once.

Nothing here calls a model except `starter_lines`, which drafts a sheet the
owner accepts line by line (never saved on its own).
"""
import json
import re
import secrets
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH

SHIFT_KINDS = ("opening", "closing", "mid", "any")
SHIFT_KIND_LABEL = {"opening": "Opening", "closing": "Closing", "mid": "Mid", "any": "All day"}
PROOF_KINDS = ("none", "photo", "number", "note")
# An assignment closes this long after its shift ends: a closer ticking the
# last line as they walk out is not a miss.
CLOSE_GRACE_MINUTES = 30
PATTERN_MISSES = 3
PATTERN_DAYS = 14
# evaluate() sweeps every open sheet; one older than this closes quietly.
STALE_DAYS = 3
MAX_LINES = 80
MAX_SHEETS = 60
LABEL_MAX = 240
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# The job codes a manager holds (the floor view, sign-off, and the report's
# managers side by side).
_MANAGER_WORDS = re.compile(r"\b(manager|mgr|gm|supervisor|shift lead|foh lead|boh lead|kitchen lead)\b", re.I)


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


class TaskSheetError(ValueError):
    """Something the owner or employee is told in words they can act on."""


def is_manager_code(job_code) -> bool:
    return bool(_MANAGER_WORDS.search(str(job_code or "")))


def _key(s) -> str:
    return " ".join(str(s or "").split()).casefold()


def _now_utc():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


# ── schema (boot only: models.init_task_management calls this) ─────────────

def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS task_sheets (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        job_code       TEXT    NOT NULL,
        shift_kind     TEXT    NOT NULL DEFAULT 'any',
        title          TEXT,
        days_of_week   TEXT,                   -- "0,4" = Monday and Friday only; NULL = every day
        requires_signoff INTEGER NOT NULL DEFAULT 0,
        active         INTEGER NOT NULL DEFAULT 1,
        version        INTEGER NOT NULL DEFAULT 1,
        migrated_from  TEXT,                   -- 'task_templates' for a list carried over
        updated_by     TEXT,
        updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_sheets_r ON task_sheets(restaurant_id, active)")
    conn.execute("""CREATE TABLE IF NOT EXISTS task_sheet_lines (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        sheet_id       INTEGER NOT NULL REFERENCES task_sheets(id),
        label          TEXT    NOT NULL,
        section        TEXT,
        sort_order     INTEGER NOT NULL DEFAULT 0,
        due_offset_min INTEGER,                -- after shift start (before end, for closing); NULL = any time
        proof          TEXT    NOT NULL DEFAULT 'none',
        proof_label    TEXT,
        min_value      REAL,
        max_value      REAL,
        critical       INTEGER NOT NULL DEFAULT 0,
        active         INTEGER NOT NULL DEFAULT 1,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        updated_at     TEXT    NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_lines_sheet ON task_sheet_lines(sheet_id, active, sort_order)")
    conn.execute("""CREATE TABLE IF NOT EXISTS task_assignments (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        sheet_id       INTEGER NOT NULL REFERENCES task_sheets(id),
        task_date      TEXT    NOT NULL,       -- the business date
        job_code       TEXT    NOT NULL,
        shift_kind     TEXT    NOT NULL,
        title          TEXT,
        assignees_json TEXT,                   -- names from the published schedule
        unassigned     INTEGER NOT NULL DEFAULT 0,
        shift_start    TEXT,                   -- local wall clock, YYYY-MM-DDTHH:MM:SS
        shift_end      TEXT,
        sheet_version  INTEGER NOT NULL,
        lines_json     TEXT    NOT NULL,       -- the lines as issued, with their due times
        status         TEXT    NOT NULL DEFAULT 'open',   -- open | done | partial | missed | none (nobody on it today)
        done_count     INTEGER,
        line_count     INTEGER,
        closed_at      TEXT,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(sheet_id, task_date)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_asg_day ON task_assignments(restaurant_id, task_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_asg_date ON task_assignments(task_date)")
    conn.execute("""CREATE TABLE IF NOT EXISTS task_line_completions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        assignment_id  INTEGER NOT NULL REFERENCES task_assignments(id),
        line_id        INTEGER NOT NULL,
        completed_by   TEXT,
        completed_by_user_id INTEGER,
        completed_at   TEXT    NOT NULL DEFAULT (datetime('now')),   -- UTC
        completed_local TEXT,                  -- the restaurant's wall clock
        proof_value    TEXT,
        proof_media_id INTEGER,
        late           INTEGER NOT NULL DEFAULT 0,
        flagged        INTEGER NOT NULL DEFAULT 0,
        undone         INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_done_asg ON task_line_completions(assignment_id, line_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_done_at ON task_line_completions(completed_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS task_proof_media (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        token          TEXT    NOT NULL UNIQUE,
        mime           TEXT    NOT NULL,
        data           BLOB    NOT NULL,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_media_at ON task_proof_media(created_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS task_signoffs (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        task_date      TEXT    NOT NULL,
        shift_kind     TEXT    NOT NULL,
        signed_by      TEXT,
        signed_by_user_id INTEGER,
        note           TEXT,
        summary_json   TEXT,
        signed_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(restaurant_id, task_date, shift_kind)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_signoff_date ON task_signoffs(task_date)")
    _add_columns(conn)


# Columns added after the tables first shipped (employee audit B4, 10/2/26),
# added here at boot only:
#   task_assignments.schedule_sig — the published week (id, version, stamps)
#     the row's people were resolved from. A sheet re-resolves its people
#     when the live week changes under it (a same-day cover or swap, LG-15),
#     and a day whose rows all carry the live signature reads no CSV at all
#     (PERF-08). status='none' is a sheet whose job code nobody works that
#     shift today: recorded, so the day settles, and never shown.
#   task_proof_media.uploaded_by_user_id — whose upload, for the per-login
#     photo cap (SEC-13).
_LATE_COLUMNS = (("task_assignments", "schedule_sig", "TEXT"),
                 ("task_proof_media", "uploaded_by_user_id", "INTEGER"))


def _add_columns(conn):
    for table, col, decl in _LATE_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_media_uploader ON task_proof_media(uploaded_by_user_id, created_at)")


def migrate_templates(conn):
    """Carry every restaurant's flat task_templates lists over as All day
    sheets, once, so nothing an owner already typed is lost (plan, phase 1).
    A restaurant that already has sheets is left alone."""
    rows = conn.execute(
        "SELECT t.restaurant_id, t.role, t.label, t.sort_order, t.id FROM task_templates t "
        "WHERE t.is_active=1 AND NOT EXISTS (SELECT 1 FROM task_sheets s WHERE s.restaurant_id=t.restaurant_id) "
        "ORDER BY t.restaurant_id, t.role, t.sort_order, t.id").fetchall()
    by = {}
    for r in rows:
        by.setdefault((r[0], " ".join(str(r[1]).split())), []).append(r[2])
    for (rid, role), labels in by.items():
        cur = conn.execute("INSERT INTO task_sheets (restaurant_id, job_code, shift_kind, title, migrated_from, updated_by) "
                           "VALUES (?,?,?,?,?,?)", (rid, role, "any", None, "task_templates", "carried over"))
        for i, label in enumerate(labels[:MAX_LINES]):
            conn.execute("INSERT INTO task_sheet_lines (restaurant_id, sheet_id, label, sort_order) VALUES (?,?,?,?)",
                         (rid, cur.lastrowid, str(label)[:LABEL_MAX], i))
    return len(by)


# ── the owner's editor ──────────────────────────────────────────────────────

def _clean_days(days):
    if days in (None, "", [], "all"):
        return None
    if isinstance(days, str):
        days = [d for d in days.split(",") if d.strip() != ""]
    out = sorted({int(d) for d in days if str(d).strip().lstrip("-").isdigit() and 0 <= int(d) <= 6})
    return ",".join(str(d) for d in out) if out and len(out) < 7 else None


def _clean_line(fields, partial=False):
    out = {}
    if "label" in fields or not partial:
        label = " ".join(str(fields.get("label") or "").split())[:LABEL_MAX]
        if not label:
            raise TaskSheetError("Write what needs doing.")
        out["label"] = label
    if "section" in fields:
        out["section"] = " ".join(str(fields.get("section") or "").split())[:60] or None
    if "due_offset_min" in fields:
        v = fields.get("due_offset_min")
        if v in (None, ""):
            out["due_offset_min"] = None
        else:
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise TaskSheetError("The due time is a number of minutes.")
            if v < 0 or v > 18 * 60:
                raise TaskSheetError("A due time is between 0 and 18 hours into the shift.")
            out["due_offset_min"] = v
    if "proof" in fields:
        proof = str(fields.get("proof") or "none").lower()
        if proof not in PROOF_KINDS:
            raise TaskSheetError("Proof is a photo, a number, a note, or nothing.")
        out["proof"] = proof
    if "proof_label" in fields:
        out["proof_label"] = " ".join(str(fields.get("proof_label") or "").split())[:60] or None
    for k in ("min_value", "max_value"):
        if k in fields:
            v = fields.get(k)
            if v in (None, ""):
                out[k] = None
            else:
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    raise TaskSheetError("The allowed range is two numbers.")
    if out.get("min_value") is not None and out.get("max_value") is not None and out["min_value"] > out["max_value"]:
        raise TaskSheetError("The lowest allowed number is above the highest.")
    if "critical" in fields:
        out["critical"] = 1 if fields.get("critical") in (True, 1, "1", "true", "on") else 0
    return out


def _bump(conn, sheet_id, who):
    conn.execute("UPDATE task_sheets SET version=version+1, updated_at=datetime('now'), updated_by=? WHERE id=?",
                 ((who or "")[:120] or None, sheet_id))


def _sheet(conn, restaurant_id, sheet_id):
    row = conn.execute("SELECT * FROM task_sheets WHERE id=? AND restaurant_id=?", (int(sheet_id), restaurant_id)).fetchone()
    if not row:
        raise TaskSheetError("That sheet isn't at this restaurant.")
    return row


def create_sheet(restaurant_id, job_code, shift_kind="any", title=None, days_of_week=None,
                 requires_signoff=False, who=None, db_path=DB_PATH) -> dict:
    job_code = " ".join(str(job_code or "").split())[:60]
    if not job_code:
        raise TaskSheetError("Pick the job code this sheet is for.")
    if shift_kind not in SHIFT_KINDS:
        raise TaskSheetError("A sheet is for opening, closing, mid or all day.")
    days = _clean_days(days_of_week)
    conn = get_conn(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM task_sheets WHERE restaurant_id=? AND active=1", (restaurant_id,)).fetchone()[0]
        if n >= MAX_SHEETS:
            raise TaskSheetError(f"A restaurant keeps up to {MAX_SHEETS} sheets.")
        dup = conn.execute("SELECT id FROM task_sheets WHERE restaurant_id=? AND active=1 AND lower(job_code)=lower(?) "
                           "AND shift_kind=? AND COALESCE(days_of_week,'')=COALESCE(?,'')",
                           (restaurant_id, job_code, shift_kind, days)).fetchone()
        if dup:
            raise TaskSheetError(f"There is already a {SHIFT_KIND_LABEL[shift_kind].lower()} sheet for {job_code}.")
        cur = conn.execute("INSERT INTO task_sheets (restaurant_id, job_code, shift_kind, title, days_of_week, "
                           "requires_signoff, updated_by) VALUES (?,?,?,?,?,?,?)",
                           (restaurant_id, job_code, shift_kind, (str(title or "").strip()[:80] or None), days,
                            1 if requires_signoff else 0, (who or "")[:120] or None))
        conn.commit()
        return get_sheet(restaurant_id, cur.lastrowid, db_path=db_path)
    finally:
        conn.close()


def update_sheet(restaurant_id, sheet_id, fields, who=None, db_path=DB_PATH) -> dict:
    conn = get_conn(db_path)
    try:
        cur = _sheet(conn, restaurant_id, sheet_id)
        sets, args = [], []
        if "job_code" in fields:
            jc = " ".join(str(fields.get("job_code") or "").split())[:60]
            if not jc:
                raise TaskSheetError("Pick the job code this sheet is for.")
            sets.append("job_code=?"); args.append(jc)
        if "shift_kind" in fields:
            if fields["shift_kind"] not in SHIFT_KINDS:
                raise TaskSheetError("A sheet is for opening, closing, mid or all day.")
            sets.append("shift_kind=?"); args.append(fields["shift_kind"])
        if "title" in fields:
            sets.append("title=?"); args.append(str(fields.get("title") or "").strip()[:80] or None)
        if "days_of_week" in fields:
            sets.append("days_of_week=?"); args.append(_clean_days(fields.get("days_of_week")))
        if "requires_signoff" in fields:
            sets.append("requires_signoff=?"); args.append(1 if fields.get("requires_signoff") else 0)
        if "active" in fields:
            sets.append("active=?"); args.append(1 if fields.get("active") else 0)
        if sets:
            conn.execute(f"UPDATE task_sheets SET {', '.join(sets)} WHERE id=?", (*args, cur["id"]))
            _bump(conn, cur["id"], who)
            conn.commit()
        return get_sheet(restaurant_id, cur["id"], db_path=db_path)
    finally:
        conn.close()


def add_line(restaurant_id, sheet_id, fields, who=None, db_path=DB_PATH) -> dict:
    vals = _clean_line(fields)
    conn = get_conn(db_path)
    try:
        s = _sheet(conn, restaurant_id, sheet_id)
        n = conn.execute("SELECT COUNT(*) FROM task_sheet_lines WHERE sheet_id=? AND active=1", (s["id"],)).fetchone()[0]
        if n >= MAX_LINES:
            raise TaskSheetError(f"A sheet holds up to {MAX_LINES} lines — split it by section or shift.")
        nxt = conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM task_sheet_lines WHERE sheet_id=?", (s["id"],)).fetchone()[0]
        cols = ["restaurant_id", "sheet_id", "sort_order"] + list(vals)
        cur = conn.execute(f"INSERT INTO task_sheet_lines ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                           (restaurant_id, s["id"], nxt, *vals.values()))
        _bump(conn, s["id"], who)
        conn.commit()
        line_id = cur.lastrowid
    finally:
        conn.close()
    sheet = get_sheet(restaurant_id, sheet_id, db_path=db_path)
    line = next(l for l in sheet["lines"] if l["id"] == line_id)
    return {"line": line, "sheet": sheet, "similar": similar_lines(sheet, line)}


def update_line(restaurant_id, line_id, fields, who=None, db_path=DB_PATH) -> dict:
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM task_sheet_lines WHERE id=? AND restaurant_id=?",
                           (int(line_id), restaurant_id)).fetchone()
        if not row:
            raise TaskSheetError("That line isn't on a sheet here.")
        if "active" in fields and not fields.get("active"):
            conn.execute("UPDATE task_sheet_lines SET active=0, updated_at=datetime('now') WHERE id=?", (row["id"],))
        else:
            vals = _clean_line(fields, partial=True)
            if vals:
                conn.execute(f"UPDATE task_sheet_lines SET {', '.join(k + '=?' for k in vals)}, updated_at=datetime('now') "
                             f"WHERE id=?", (*vals.values(), row["id"]))
        _bump(conn, row["sheet_id"], who)
        conn.commit()
        sheet_id = row["sheet_id"]
    finally:
        conn.close()
    sheet = get_sheet(restaurant_id, sheet_id, db_path=db_path)
    line = next((l for l in sheet["lines"] if l["id"] == int(line_id)), None)
    return {"line": line, "sheet": sheet, "similar": similar_lines(sheet, line) if line else []}


def reorder_lines(restaurant_id, sheet_id, line_ids, who=None, db_path=DB_PATH) -> dict:
    conn = get_conn(db_path)
    try:
        s = _sheet(conn, restaurant_id, sheet_id)
        have = {r[0] for r in conn.execute("SELECT id FROM task_sheet_lines WHERE sheet_id=? AND active=1", (s["id"],))}
        order = [int(i) for i in (line_ids or []) if str(i).isdigit() and int(i) in have]
        if set(order) != have:
            raise TaskSheetError("The new order has to list every line on the sheet once.")
        for i, lid in enumerate(order):
            conn.execute("UPDATE task_sheet_lines SET sort_order=? WHERE id=?", (i, lid))
        _bump(conn, s["id"], who)
        conn.commit()
    finally:
        conn.close()
    return get_sheet(restaurant_id, sheet_id, db_path=db_path)


def _line_public(r) -> dict:
    return {"id": r["id"], "label": r["label"], "section": r["section"], "sort_order": r["sort_order"],
            "due_offset_min": r["due_offset_min"], "proof": r["proof"] or "none", "proof_label": r["proof_label"],
            "min_value": r["min_value"], "max_value": r["max_value"], "critical": bool(r["critical"])}


def _sheet_public(s, lines) -> dict:
    days = [int(d) for d in (s["days_of_week"] or "").split(",") if d.strip().isdigit()]
    return {"id": s["id"], "job_code": s["job_code"], "shift_kind": s["shift_kind"],
            "shift_label": SHIFT_KIND_LABEL.get(s["shift_kind"], s["shift_kind"]),
            "title": s["title"] or f"{SHIFT_KIND_LABEL.get(s['shift_kind'], '')} — {s['job_code']}".strip(" —"),
            "days_of_week": days, "days_label": ", ".join(DAY_NAMES[d][:3] for d in days) if days else "Every day",
            "requires_signoff": bool(s["requires_signoff"]), "active": bool(s["active"]), "version": s["version"],
            "updated_by": s["updated_by"], "updated_at": s["updated_at"], "carried_over": s["migrated_from"] == "task_templates",
            "manager": is_manager_code(s["job_code"]), "lines": lines}


def get_sheet(restaurant_id, sheet_id, db_path=DB_PATH) -> dict:
    conn = get_conn(db_path)
    try:
        s = _sheet(conn, restaurant_id, sheet_id)
        lines = [_line_public(r) for r in conn.execute(
            "SELECT * FROM task_sheet_lines WHERE sheet_id=? AND active=1 ORDER BY sort_order, id", (s["id"],))]
        return _sheet_public(s, lines)
    finally:
        conn.close()


def list_sheets(restaurant_id, include_inactive=False, db_path=DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        sheets = conn.execute("SELECT * FROM task_sheets WHERE restaurant_id=?" + ("" if include_inactive else " AND active=1") +
                              " ORDER BY lower(job_code), CASE shift_kind WHEN 'opening' THEN 0 WHEN 'mid' THEN 1 "
                              "WHEN 'closing' THEN 2 ELSE 3 END, id", (restaurant_id,)).fetchall()
        lines = {}
        for r in conn.execute("SELECT * FROM task_sheet_lines WHERE restaurant_id=? AND active=1 ORDER BY sort_order, id",
                              (restaurant_id,)):
            lines.setdefault(r["sheet_id"], []).append(_line_public(r))
        return [_sheet_public(s, lines.get(s["id"], [])) for s in sheets]
    finally:
        conn.close()


_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "at", "with", "your", "all", "up", "down", "make", "sure"}


def _words(s):
    return {w for w in _WORD.findall(str(s or "").lower()) if w not in _STOP and len(w) > 1}


def similar_lines(sheet, line) -> list:
    """Lines on the same sheet that say nearly the same thing as `line`
    ("two lines say 'wipe down the line'") — deterministic word overlap,
    flagged when the owner edits, never merged for him."""
    if not line:
        return []
    mine = _words(line["label"])
    out = []
    for other in sheet.get("lines") or []:
        if other["id"] == line["id"]:
            continue
        theirs = _words(other["label"])
        if not mine or not theirs:
            continue
        overlap = len(mine & theirs) / max(1, min(len(mine), len(theirs)))
        if overlap >= 0.8 and len(mine & theirs) >= 2:
            out.append({"id": other["id"], "label": other["label"]})
    return out


def job_codes(restaurant_id, db_path=DB_PATH) -> list:
    """Every job code the restaurant uses, as its schedule spells them — the
    published schedules' roles, the POS roles, and the sheets' own — so a
    sheet matches the schedule exactly."""
    seen = {}
    conn = get_conn(db_path)
    try:
        for (jc,) in conn.execute("SELECT job_code FROM task_sheets WHERE restaurant_id=?", (restaurant_id,)):
            seen.setdefault(_key(jc), jc)
        try:
            for (role,) in conn.execute("SELECT DISTINCT role FROM person_roles WHERE restaurant_id=? "
                                        "AND removed_at IS NULL AND COALESCE(role,'')<>''", (restaurant_id,)):
                seen.setdefault(_key(role), role)
        except Exception:
            pass
    finally:
        conn.close()
    try:
        for s in _recent_schedule_roles(restaurant_id):
            seen.setdefault(_key(s), s)
    except Exception:
        pass
    return sorted(seen.values(), key=lambda s: s.lower())


def _recent_schedule_roles(restaurant_id):
    import csv, io
    conn = get_conn()
    try:
        row = conn.execute("SELECT schedule_csv FROM schedule_history WHERE restaurant_id=? AND schedule_csv IS NOT NULL "
                           "ORDER BY id DESC LIMIT 1", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    roles = set()
    if row and row[0]:
        for r in csv.DictReader(io.StringIO(row[0])):
            if (r.get("role") or "").strip():
                roles.add(" ".join(r["role"].split()))
    return roles


# ── today: which day, and who owes which sheet ──────────────────────────────

def _restaurant(restaurant_id):
    from models import get_restaurant
    return get_restaurant(restaurant_id)


def local_now(restaurant_id, restaurant=None):
    from time_utils import restaurant_now
    return restaurant_now(restaurant or _restaurant(restaurant_id), naive=True)


def business_day(restaurant_id, restaurant=None, now_local=None) -> date:
    """The service date a sheet belongs to: a closer ticking at 1am is still
    on last night's sheet (time_utils.business_date)."""
    from time_utils import business_date
    r = restaurant or _restaurant(restaurant_id)
    return business_date(r, now_local or local_now(restaurant_id, r))


def _published_owner(restaurant_id, day):
    """The id of the newest PUBLISHED week covering `day`, or None."""
    from staff_schedule import _published_weeks, _owner_of
    return _owner_of(_published_weeks(restaurant_id, day), day)


def published_day_shifts(restaurant_id, day, owner=None) -> tuple:
    """(shifts, published) for one business date: every shift on the
    newest PUBLISHED week that covers it, as [{employee, role, start, end}]
    with local datetimes (labor.timed_shifts_from_csv). A draft never
    assigns anyone a sheet — staff_schedule's own rule. Reads only the
    week's CSV (models.get_published_week_csv, PERF-12)."""
    import labor
    if owner is None:
        owner = _published_owner(restaurant_id, day)
    if owner is None:
        return [], False
    week = _models_mod.get_published_week_csv(owner, restaurant_id) or {}
    iso = day.isoformat()
    shifts = [s for s in labor.timed_shifts_from_csv(week.get("schedule_csv") or "") if s["start"][:10] == iso]
    return shifts, True


def _schedule_sig(restaurant_id, day, db_path=DB_PATH):
    """(signature, owner id) of the live published week for `day`, without
    reading its CSV: the week's id, its newest version (every cover, swap and
    edit appends one — schedule_versions.write_on), and its edit and publish
    stamps. "none" when no published week covers the day."""
    owner = _published_owner(restaurant_id, day)
    if owner is None:
        return "none", None
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT edited_at, published_at FROM schedule_history WHERE id=? AND restaurant_id=?",
                           (owner, restaurant_id)).fetchone()
        try:
            ver = conn.execute("SELECT MAX(version) FROM schedule_versions WHERE history_id=?", (owner,)).fetchone()[0]
        except Exception:
            ver = None
    finally:
        conn.close()
    if not row:
        return "none", None
    return f"{owner}:{ver or 0}:{row['edited_at'] or ''}:{row['published_at'] or ''}", owner


def _pick(sheet, shifts):
    """(assignees, start, end) for this sheet from the day's shifts on its
    job code, or None when nobody on that code works that shift."""
    mine = [s for s in shifts if _key(s["role"]) == _key(sheet["job_code"])]
    if not mine:
        return None
    kind = sheet["shift_kind"]
    first = min(s["start"] for s in mine)
    last = max(s["end"] for s in mine)
    if kind == "opening":
        chosen = [s for s in mine if s["start"] == first]
    elif kind == "closing":
        chosen = [s for s in mine if s["end"] == last]
    elif kind == "mid":
        chosen = [s for s in mine if s["start"] != first and s["end"] != last]
    else:
        chosen = mine
    if not chosen:
        return None
    names = list(dict.fromkeys(s["employee"] for s in chosen))
    return names, min(s["start"] for s in chosen), max(s["end"] for s in chosen)


def _hours_window(restaurant, day):
    """The day's service hours, for a sheet issued with no schedule."""
    try:
        from time_utils import service_window
        w = service_window(restaurant, day)
    except Exception:
        w = None
    if not w:
        return None, None
    return w[0].strftime("%Y-%m-%dT%H:%M:%S"), w[1].strftime("%Y-%m-%dT%H:%M:%S")


def _due_at(kind, offset, start, end):
    if offset is None or not (start and end):
        return None
    s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
    at = (e - timedelta(minutes=offset)) if kind == "closing" else (s + timedelta(minutes=offset))
    return at.strftime("%Y-%m-%dT%H:%M:%S")


def _snapshot_lines(sheet, start, end):
    """The lines as issued, with each one's due time and the offset it came
    from (so a re-resolved shift can move its due times)."""
    return [{"line_id": l["id"], "label": l["label"], "section": l["section"],
             "due_offset_min": l["due_offset_min"],
             "due_at": _due_at(sheet["shift_kind"], l["due_offset_min"], start, end),
             "proof": l["proof"], "proof_label": l["proof_label"], "min_value": l["min_value"],
             "max_value": l["max_value"], "critical": l["critical"]} for l in sheet["lines"]]


def _moved_lines(kind, lines, old_start, old_end, start, end):
    """The issued lines with their due times following a re-resolved shift.
    A line issued with its offset is recomputed; an older snapshot (no
    offset kept) moves by the shift's own move — exact, since a due time is
    start + offset (end - offset for a closing sheet)."""
    anchor_old = old_end if kind == "closing" else old_start
    anchor_new = end if kind == "closing" else start
    delta = None
    if anchor_old and anchor_new:
        delta = datetime.fromisoformat(anchor_new) - datetime.fromisoformat(anchor_old)
    out = []
    for l in lines:
        l = dict(l)
        if l.get("due_offset_min") is not None:
            l["due_at"] = _due_at(kind, l["due_offset_min"], start, end)
        elif l.get("due_at") and delta is not None:
            l["due_at"] = (datetime.fromisoformat(l["due_at"]) + delta).strftime("%Y-%m-%dT%H:%M:%S")
        out.append(l)
    return out


def _sheets_due(restaurant_id, weekday, db_path=DB_PATH):
    """Ids of the active sheets with lines that run on this weekday — one
    small read, no line payloads."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT s.id, s.days_of_week FROM task_sheets s WHERE s.restaurant_id=? AND s.active=1 AND EXISTS "
            "(SELECT 1 FROM task_sheet_lines l WHERE l.sheet_id=s.id AND l.active=1)", (restaurant_id,)).fetchall()
    finally:
        conn.close()
    out = set()
    for r in rows:
        days = [int(d) for d in (r["days_of_week"] or "").split(",") if d.strip().isdigit()]
        if not days or weekday in days:
            out.add(r["id"])
    return out


def _settle(restaurant_id, day, r, db_path=DB_PATH, force=False) -> tuple:
    """(issued, re-resolved) for one business day. Issues every sheet due
    that day with no row yet, and re-resolves the people on each row still
    open whose published week has changed since (a same-day cover or swap,
    LG-15) — or on every open row, with force. A sheet nobody works that day
    is recorded status='none', so a settled day reads no schedule at all
    (PERF-08)."""
    due = _sheets_due(restaurant_id, day.weekday(), db_path)
    if not due:
        return 0, 0
    iso = day.isoformat()
    conn = get_conn(db_path)
    try:
        existing = {row["sheet_id"]: row for row in conn.execute(
            "SELECT * FROM task_assignments WHERE restaurant_id=? AND task_date=?", (restaurant_id, iso))}
    finally:
        conn.close()
    try:
        sig, owner = _schedule_sig(restaurant_id, day, db_path)
    except Exception:
        sig, owner = None, None
    todo = [sid for sid in sorted(due) if sid not in existing]
    stale = [row for row in existing.values() if row["status"] in ("open", "none")
             and (force or (sig is not None and (row["schedule_sig"] or "") != sig))]
    if not todo and not stale:
        return 0, 0
    try:
        shifts, published = published_day_shifts(restaurant_id, day, owner=owner) if owner else ([], False)
    except Exception:
        shifts, published = [], False
    made = changed = 0
    sheets = {s["id"]: s for s in list_sheets(restaurant_id, db_path=db_path)} if todo else {}
    conn = get_conn(db_path)
    try:
        for sid in todo:
            s = sheets.get(sid)
            if not s or not s["lines"]:
                continue
            status = "open"
            if published:
                pick = _pick(s, shifts)
                if pick is None:
                    # Nobody on this code works this shift today: recorded,
                    # never shown, re-resolved if the week changes.
                    names, unassigned, start, end, status = [], 0, None, None, "none"
                else:
                    names, start, end = pick
                    unassigned = 0
            else:
                names, unassigned = [], 1
                start, end = _hours_window(r, day)
            lines = _snapshot_lines(s, start, end)
            cur = conn.execute(
                "INSERT OR IGNORE INTO task_assignments (restaurant_id, sheet_id, task_date, job_code, shift_kind, title, "
                "assignees_json, unassigned, shift_start, shift_end, sheet_version, lines_json, line_count, status, "
                "schedule_sig) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (restaurant_id, s["id"], iso, s["job_code"], s["shift_kind"], s["title"],
                 json.dumps(names), unassigned, start, end, s["version"], json.dumps(lines), len(lines), status, sig))
            made += (cur.rowcount or 0) if status == "open" else 0
        for row in stale:
            changed += _reresolve(conn, row, shifts, published, sig)
        conn.commit()
    finally:
        conn.close()
    return made, changed


def _reresolve(conn, row, shifts, published, sig) -> int:
    """Put the people the live published week names on one still-open row
    (1 when they changed). The lines stay as issued; their due times follow
    the shift. No published week now: the row keeps its people. Nobody on
    the code now: a row nobody has ticked becomes 'none'; one somebody
    started keeps its people."""
    a_id = row["id"]
    current = json.loads(row["assignees_json"] or "[]")
    update = None
    if published:
        pick = _pick({"job_code": row["job_code"], "shift_kind": row["shift_kind"]}, shifts)
        if pick is None:
            if row["status"] == "open":
                ticked = conn.execute("SELECT 1 FROM task_line_completions WHERE assignment_id=? LIMIT 1",
                                      (a_id,)).fetchone()
                if not ticked:
                    update = ([], 0, row["shift_start"], row["shift_end"], "none")
        else:
            names, start, end = pick
            if (row["status"] == "none" or row["unassigned"] or names != current
                    or start != row["shift_start"] or end != row["shift_end"]):
                update = (names, 0, start, end, "open")
    if update is None:
        conn.execute("UPDATE task_assignments SET schedule_sig=? WHERE id=?", (sig, a_id))
        return 0
    names, unassigned, start, end, status = update
    lines = json.loads(row["lines_json"] or "[]")
    if status == "open":
        lines = _moved_lines(row["shift_kind"], lines, row["shift_start"], row["shift_end"], start, end)
    conn.execute("UPDATE task_assignments SET assignees_json=?, unassigned=?, shift_start=?, shift_end=?, "
                 "lines_json=?, status=?, schedule_sig=? WHERE id=? AND status IN ('open','none')",
                 (json.dumps(names), unassigned, start, end, json.dumps(lines), status, sig, a_id))
    return 1


def ensure_day(restaurant_id, day=None, restaurant=None, db_path=DB_PATH) -> int:
    """Issue the day's assignments from the published schedule, and keep the
    people on the ones still open in step with the live published week.
    Idempotent: a sheet already issued keeps its lines as issued (a line
    added at noon does not make this morning's opener late), and a day whose
    rows all match the live week reads no schedule at all. Returns how many
    sheets were issued."""
    r = restaurant or _restaurant(restaurant_id)
    if r is None:
        return 0
    day = day or business_day(restaurant_id, r)
    return _settle(restaurant_id, day, r, db_path=db_path)[0]


def refresh_assignees(restaurant_id, day=None, db_path=DB_PATH) -> int:
    """After a cover or swap on `day` (shift_requests._cover/_execute_swap):
    when `day` is the restaurant's business day, re-resolve the people on
    today's still-open sheets now, so the person now on the shift can tick
    it and a miss is filed against them, not the person who dropped it.
    Any other day: nothing (that day is issued from the live week when it
    comes). Returns how many sheets changed hands. Never raises — the cover
    has already happened, and staff_view re-resolves on its next read."""
    try:
        r = _restaurant(restaurant_id)
        if r is None:
            return 0
        today = business_day(restaurant_id, r)
        if day is not None:
            d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
            if d != today:
                return 0
        return _settle(restaurant_id, today, r, db_path=db_path, force=True)[1]
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="task_sheets_refresh", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
        return 0


# ── reading an assignment's state ───────────────────────────────────────────

def _latest(conn, assignment_ids):
    """{(assignment_id, line_id): newest completion row} — undone rows
    included — each with its proof photo's token (`media_token`), joined
    here rather than read one photo at a time (PERF-08)."""
    if not assignment_ids:
        return {}
    qs = ",".join("?" * len(assignment_ids))
    rows = conn.execute(f"SELECT c.*, m.token AS media_token FROM task_line_completions c "
                        f"LEFT JOIN task_proof_media m ON m.id=c.proof_media_id AND m.restaurant_id=c.restaurant_id "
                        f"WHERE c.assignment_id IN ({qs}) ORDER BY c.id",
                        tuple(assignment_ids)).fetchall()
    out = {}
    for r in rows:
        out[(r["assignment_id"], r["line_id"])] = r
    return out


def _assignment_view(conn, a, latest, now_local_iso):
    lines = json.loads(a["lines_json"] or "[]")
    out_lines, done = [], 0
    for l in lines:
        c = latest.get((a["id"], l["line_id"]))
        is_done = bool(c) and not c["undone"]
        done += 1 if is_done else 0
        overdue = (not is_done) and bool(l.get("due_at")) and l["due_at"] < now_local_iso and a["status"] == "open"
        out_lines.append({
            **l, "done": is_done, "overdue": overdue,
            "completed_by": c["completed_by"] if is_done else None,
            "completed_at": c["completed_local"] if is_done else None,
            "late": bool(c["late"]) if is_done else False,
            "flagged": bool(c["flagged"]) if is_done else False,
            "proof_value": c["proof_value"] if is_done else None,
            "photo": c["media_token"] if is_done and c["proof_media_id"] else None,
        })
    total = len(lines)
    status = a["status"]
    return {"id": a["id"], "sheet_id": a["sheet_id"], "task_date": a["task_date"], "job_code": a["job_code"],
            "shift_kind": a["shift_kind"], "shift_label": SHIFT_KIND_LABEL.get(a["shift_kind"], a["shift_kind"]),
            "title": a["title"] or f"{SHIFT_KIND_LABEL.get(a['shift_kind'], '')} — {a['job_code']}".strip(" —"),
            "assignees": json.loads(a["assignees_json"] or "[]"), "unassigned": bool(a["unassigned"]),
            "shift_start": a["shift_start"], "shift_end": a["shift_end"], "status": status,
            "done": done, "total": total, "overdue": sum(1 for l in out_lines if l["overdue"]),
            "critical_open": sum(1 for l in out_lines if l.get("critical") and not l["done"]),
            "late": sum(1 for l in out_lines if l["late"]), "flagged": sum(1 for l in out_lines if l["flagged"]),
            "manager": is_manager_code(a["job_code"]), "lines": out_lines}


def _assignments(restaurant_id, day_iso, db_path=DB_PATH, now_local=None):
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM task_assignments WHERE restaurant_id=? AND task_date=? AND status <> 'none' "
                            "ORDER BY CASE shift_kind WHEN 'opening' THEN 0 WHEN 'mid' THEN 1 WHEN 'any' THEN 2 ELSE 3 END, "
                            "lower(job_code), id", (restaurant_id, day_iso)).fetchall()
        latest = _latest(conn, [r["id"] for r in rows])
        now_iso = (now_local or local_now(restaurant_id)).strftime("%Y-%m-%dT%H:%M:%S")
        return [_assignment_view(conn, r, latest, now_iso) for r in rows]
    finally:
        conn.close()


def assignment_view(restaurant_id, assignment_id, db_path=DB_PATH, now_local=None) -> dict:
    """One sheet as staff_view shows it — what a tick returns, so the app
    replaces that sheet instead of reading every sheet again (PERF-08)."""
    conn = get_conn(db_path)
    try:
        a = _assignment_row(conn, restaurant_id, assignment_id)
        latest = _latest(conn, [a["id"]])
        now_iso = (now_local or local_now(restaurant_id)).strftime("%Y-%m-%dT%H:%M:%S")
        return _assignment_view(conn, a, latest, now_iso)
    finally:
        conn.close()


def _signoffs(restaurant_id, day_iso, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        return [{"shift_kind": r["shift_kind"], "signed_by": r["signed_by"], "signed_at": r["signed_at"], "note": r["note"],
                 "summary": json.loads(r["summary_json"] or "{}")}
                for r in conn.execute("SELECT * FROM task_signoffs WHERE restaurant_id=? AND task_date=? ORDER BY id",
                                      (restaurant_id, day_iso))]
    finally:
        conn.close()


def day_view(restaurant_id, day=None, db_path=DB_PATH) -> dict:
    """The owner's bird's-eye view: every sheet issued for the business day,
    each line's state, who ticked it and when, late and out-of-range ticks,
    the proof, and the sign-offs. Read-only — owners do not tick."""
    r = _restaurant(restaurant_id)
    today = business_day(restaurant_id, r)
    day = day or today
    if day == today:
        ensure_day(restaurant_id, day, restaurant=r, db_path=db_path)
    rows = _assignments(restaurant_id, day.isoformat(), db_path=db_path)
    lines = sum(a["total"] for a in rows)
    done = sum(a["done"] for a in rows)
    sheets = list_sheets(restaurant_id, db_path=db_path)
    need_signoff = sorted({a["shift_kind"] for a in rows
                           if any(s["id"] == a["sheet_id"] and s["requires_signoff"] for s in sheets)})
    published = None
    if rows:
        published = not all(a["unassigned"] for a in rows)
    from time_utils import mdy
    return {"date": day.isoformat(), "date_label": mdy(day), "is_today": day == today, "today": today.isoformat(),
            "sheets": rows, "summary": {"sheets": len(rows), "lines": lines, "done": done,
                                        "complete": sum(1 for a in rows if a["done"] == a["total"] and a["total"]),
                                        "overdue": sum(a["overdue"] for a in rows),
                                        "critical_open": sum(a["critical_open"] for a in rows),
                                        "late": sum(a["late"] for a in rows), "flagged": sum(a["flagged"] for a in rows),
                                        "unassigned": sum(1 for a in rows if a["unassigned"])},
            "schedule_published": published, "signoffs": _signoffs(restaurant_id, day.isoformat(), db_path),
            "signoff_needed": need_signoff, "has_sheets": bool(sheets)}


# ── staff: their own sheets, ticking, proof, the floor, sign-off ────────────

def _mine(a, employee_name, job_roles):
    names = {_key(n) for n in a["assignees"]}
    if _key(employee_name) in names:
        return True
    return a["unassigned"] and _key(a["job_code"]) in {_key(j) for j in job_roles if j}


def staff_view(restaurant_id, employee_name, job_roles=(), db_path=DB_PATH) -> dict:
    """What one employee sees: today's sheets that are theirs (named on the
    schedule for that job code and shift, or an unassigned sheet on their job
    code), in order, with due times. A manager also gets the floor — every
    sheet for today — and may sign a shift off; a manager opening today also
    gets last night's closing sign-off note (`last_night_note`, COM-16)."""
    r = _restaurant(restaurant_id)
    day = business_day(restaurant_id, r)
    ensure_day(restaurant_id, day, restaurant=r, db_path=db_path)
    rows = _assignments(restaurant_id, day.isoformat(), db_path=db_path)
    job_roles = [j for j in (job_roles or []) if j]
    mine = [a for a in rows if _mine(a, employee_name, job_roles)]
    manager = any(is_manager_code(j) for j in job_roles) or any(a["manager"] and _key(employee_name) in
                                                               {_key(n) for n in a["assignees"]} for a in rows)
    opening = manager and any(a["shift_kind"] == "opening" for a in mine)
    return {"task_date": day.isoformat(), "sheets": mine, "manager": manager,
            "floor": [a for a in rows if a not in mine] if manager else [],
            "signoffs": _signoffs(restaurant_id, day.isoformat(), db_path) if manager else [],
            "can_sign_off": sorted({a["shift_kind"] for a in rows}) if manager else [],
            "last_night_note": last_night_note(restaurant_id, day, db_path=db_path) if opening else None}


def last_night_note(restaurant_id, day, db_path=DB_PATH):
    """The note the manager left signing off the previous business day's
    close (an all-day sign-off when the restaurant runs all-day sheets), for
    today's opener: {note, signed_by, signed_at, task_date, date_label}, or
    None when there was no note. It was stored and shown only to managers,
    only on the day it was written (COM-16)."""
    prev = (day - timedelta(days=1)).isoformat()
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM task_signoffs WHERE restaurant_id=? AND task_date=? "
                           "AND shift_kind IN ('closing','any') AND COALESCE(note,'')<>'' "
                           "ORDER BY CASE shift_kind WHEN 'closing' THEN 0 ELSE 1 END, id DESC LIMIT 1",
                           (restaurant_id, prev)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    from time_utils import mdy
    return {"note": row["note"], "signed_by": row["signed_by"], "signed_at": row["signed_at"],
            "shift_kind": row["shift_kind"], "task_date": prev, "date_label": mdy(prev)}


def _assignment_row(conn, restaurant_id, assignment_id):
    a = conn.execute("SELECT * FROM task_assignments WHERE id=? AND restaurant_id=?",
                     (int(assignment_id), restaurant_id)).fetchone()
    if not a or a["status"] == "none":
        raise TaskSheetError("That sheet isn't here.")
    return a


class NotYourSheet(TaskSheetError):
    """The sheet is someone else's — the route answers 403."""


# A tick is filed under its assignment's business date, and only a sheet for
# today, or one day either side (a closer after midnight, an opener whose
# phone is a day off), can be ticked: an assignment that escaped closing must
# not be "completed" days later (LG-30).
TICK_WINDOW_DAYS = 1
# Proof photos one login may upload in an hour — far above a real shift
# (a sheet holds at most MAX_LINES lines), well below a flood (SEC-13).
PHOTO_UPLOADS_PER_HOUR = 60
TELL_MANAGER = "Tell your manager now"


def _range_label(lo, hi):
    if lo is not None and hi is not None:
        return f"{lo:g}–{hi:g}"
    if lo is not None:
        return f"at least {lo:g}"
    return f"at most {hi:g}"


def _photo_cap_reached(conn, user_id) -> bool:
    if not user_id:
        return False
    n = conn.execute("SELECT COUNT(*) FROM task_proof_media WHERE uploaded_by_user_id=? "
                     "AND created_at >= datetime('now', '-1 hour')", (user_id,)).fetchone()[0]
    return n >= PHOTO_UPLOADS_PER_HOUR


def complete_line(restaurant_id, assignment_id, line_id, done=True, employee_name=None, job_roles=(),
                  user_id=None, value=None, media_id=None, db_path=DB_PATH, now_local=None, photo=None) -> dict:
    """Tick (or un-tick) one line. Only a sheet that is this employee's —
    named on it, or an unassigned sheet on their job code — only while it
    is open, and only a sheet for the business day ± TICK_WINDOW_DAYS. A
    number is checked against its range (flagged, never refused); a photo,
    number or note line needs its proof to tick.

    `photo` is (raw bytes, mime): it is checked and stored only once the
    sheet and line are known to be this person's, in the same transaction
    as the tick, and counts toward PHOTO_UPLOADS_PER_HOUR (LG-31, SEC-13).

    A critical line read out of its range opens a high issue for the routed
    manager (`taskflag:<assignment>:<line>`, texted like a missed critical
    line) and the result carries `alert` with "Tell your manager now"
    (COM-10)."""
    now_local = now_local or local_now(restaurant_id)
    conn = get_conn(db_path)
    try:
        a = _assignment_row(conn, restaurant_id, assignment_id)
        today = business_day(restaurant_id, now_local=now_local)
        try:
            task_day = date.fromisoformat(str(a["task_date"])[:10])
        except ValueError:
            task_day = None
        if task_day is None or abs((task_day - today).days) > TICK_WINDOW_DAYS:
            raise TaskSheetError("That sheet is from another day and can't be ticked now.")
        view = {"assignees": json.loads(a["assignees_json"] or "[]"), "unassigned": bool(a["unassigned"]),
                "job_code": a["job_code"]}
        if not _mine(view, employee_name or "", job_roles) and task_day == today:
            # The week may have changed under the sheet (a cover, a swap)
            # since it was last read: re-resolve today's people, then judge.
            conn.close()
            ensure_day(restaurant_id, today, db_path=db_path)
            conn = get_conn(db_path)
            a = _assignment_row(conn, restaurant_id, assignment_id)
            view = {"assignees": json.loads(a["assignees_json"] or "[]"), "unassigned": bool(a["unassigned"]),
                    "job_code": a["job_code"]}
        if not _mine(view, employee_name or "", job_roles):
            raise NotYourSheet("That sheet isn't yours today.")
        if a["status"] != "open":
            raise TaskSheetError("That sheet closed at the end of the shift.")
        line = next((l for l in json.loads(a["lines_json"] or "[]") if l["line_id"] == int(line_id)), None)
        if not line:
            raise TaskSheetError("That line isn't on this sheet.")
        flagged, stored = 0, None
        if done:
            proof = line.get("proof") or "none"
            if proof == "number":
                try:
                    num = float(str(value).strip())
                except (TypeError, ValueError):
                    raise TaskSheetError(f"Enter the {line.get('proof_label') or 'number'} to tick this off.")
                lo, hi = line.get("min_value"), line.get("max_value")
                flagged = 1 if ((lo is not None and num < lo) or (hi is not None and num > hi)) else 0
                stored = ("%g" % num)
            elif proof == "note":
                stored = " ".join(str(value or "").split())[:300]
                if not stored:
                    raise TaskSheetError("Add a note to tick this off.")
            elif proof == "photo" and not media_id and not photo:
                raise TaskSheetError("Add a photo to tick this off.")
            elif value not in (None, ""):
                stored = " ".join(str(value).split())[:300]
        jpeg = None
        if done and photo is not None:
            if _photo_cap_reached(conn, user_id):
                raise TaskSheetError("That's a lot of photos in an hour — wait a few minutes and try again.")
            jpeg = _encode_photo(photo[0], photo[1] if len(photo) > 1 else "")
        late = 1 if (done and line.get("due_at") and now_local.strftime("%Y-%m-%dT%H:%M:%S") > line["due_at"]) else 0
        if jpeg is not None:
            cur = conn.execute("INSERT INTO task_proof_media (restaurant_id, token, mime, data, uploaded_by_user_id) "
                               "VALUES (?,?,?,?,?)",
                               (restaurant_id, secrets.token_urlsafe(18), "image/jpeg", jpeg, user_id))
            media_id = cur.lastrowid
        conn.execute("INSERT INTO task_line_completions (restaurant_id, assignment_id, line_id, completed_by, "
                     "completed_by_user_id, completed_local, proof_value, proof_media_id, late, flagged, undone) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (restaurant_id, a["id"], int(line_id), (employee_name or "")[:120] or None, user_id,
                      now_local.strftime("%Y-%m-%dT%H:%M:%S"), stored, media_id if done else None,
                      late, flagged, 0 if done else 1))
        conn.commit()
        a = dict(a)
    finally:
        conn.close()
    out = {"late": bool(late), "flagged": bool(flagged)}
    if done and flagged and line.get("critical"):
        out["alert"] = _flag_critical(restaurant_id, a, line, stored, employee_name, now_local, db_path)
    return out


def _flag_critical(restaurant_id, a, line, reading, who, now_local, db_path=DB_PATH) -> dict:
    """A critical line read out of its range (a walk-in at 46°F): a high
    issue for the routed manager, once per line per sheet, texted like a
    critical line missed. Returns what the employee is told. Never raises —
    the reading is already recorded."""
    import issues
    what = line.get("proof_label") or line["label"]
    allowed = _range_label(line.get("min_value"), line.get("max_value"))
    manager_told = False
    try:
        issue, _token = issues.create_issue(
            restaurant_id, "task_flag", f"Out of range: {what} read {reading}"[:200],
            detail=f"{line['label']} — allowed {allowed}. Read {reading} by {who or 'staff'} at "
                   f"{_clock(now_local.strftime('%Y-%m-%dT%H:%M:%S'))} on {a['title'] or a['job_code']} "
                   f"({SHIFT_KIND_LABEL.get(a['shift_kind'], '')}).",
            severity="high", source_key=f"taskflag:{a['id']}:{line['line_id']}",
            meta={"modules": ["labor"], "assignment_id": a["id"], "task_date": a["task_date"],
                  "line_id": line["line_id"], "reading": reading}, db_path=db_path)
        manager_told = bool(issue and issue.get("assignee_name"))
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="task_flag_issue", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
    return {"critical": True, "title": TELL_MANAGER,
            "message": f"{TELL_MANAGER}: {what} read {reading}, outside {allowed}.",
            "manager_alerted": manager_told}


def _encode_photo(raw: bytes, mime: str = "") -> bytes:
    """A proof photo resized and re-encoded like a marketing photo (1280 px
    on the long edge, JPEG), or TaskSheetError in words the person can act on."""
    import marketing_media
    try:
        data, _w, _h = marketing_media.encode_jpeg(raw, mime, max_edge=1280)
    except marketing_media.MediaError as e:
        raise TaskSheetError(str(e))
    return data


def store_photo(restaurant_id, raw: bytes, mime: str = "", db_path=DB_PATH, user_id=None) -> int:
    """A proof photo: resized and re-encoded like a marketing photo, kept in
    its own table (never in the marketing library, never public). The staff
    routes no longer call this — complete_line(photo=...) stores the photo
    only after the sheet is known to be the caller's (LG-31)."""
    data = _encode_photo(raw, mime)
    conn = get_conn(db_path)
    try:
        cur = conn.execute("INSERT INTO task_proof_media (restaurant_id, token, mime, data, uploaded_by_user_id) "
                           "VALUES (?,?,?,?,?)", (restaurant_id, secrets.token_urlsafe(18), "image/jpeg", data, user_id))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_photo(restaurant_id, token, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT data, mime FROM task_proof_media WHERE token=? AND restaurant_id=?",
                           (str(token), restaurant_id)).fetchone()
        return (row["data"], row["mime"]) if row else None
    finally:
        conn.close()


def sign_off(restaurant_id, shift_kind, signed_by, user_id=None, note=None, db_path=DB_PATH) -> dict:
    """The manager on duty signs the shift's sheets: what was complete and
    what was not, as it stood when they signed. Once per shift a day."""
    if shift_kind not in SHIFT_KINDS:
        raise TaskSheetError("Sign off opening, mid, closing or all day.")
    day = business_day(restaurant_id).isoformat()
    rows = [a for a in _assignments(restaurant_id, day, db_path=db_path) if a["shift_kind"] == shift_kind]
    if not rows:
        raise TaskSheetError("There are no sheets on that shift today.")
    summary = {"sheets": [{"title": a["title"], "done": a["done"], "total": a["total"], "assignees": a["assignees"]}
                          for a in rows]}
    conn = get_conn(db_path)
    try:
        try:
            conn.execute("INSERT INTO task_signoffs (restaurant_id, task_date, shift_kind, signed_by, signed_by_user_id, "
                         "note, summary_json) VALUES (?,?,?,?,?,?,?)",
                         (restaurant_id, day, shift_kind, (signed_by or "")[:120] or None, user_id,
                          " ".join(str(note or "").split())[:500] or None, json.dumps(summary)))
        except Exception:
            raise TaskSheetError("That shift is already signed off.")
        conn.commit()
    finally:
        conn.close()
    return {"task_date": day, "shift_kind": shift_kind, "summary": summary}


# ── closing shifts, and the misses that reach someone ───────────────────────

def _line_state(latest, a_id, line_id):
    c = latest.get((a_id, line_id))
    return bool(c) and not c["undone"]


def _describe(a, done, total):
    who = ", ".join(json.loads(a["assignees_json"] or "[]")) or "no one scheduled"
    title = a["title"] or f"{SHIFT_KIND_LABEL.get(a['shift_kind'], '')} sheet — {a['job_code']}"
    return f"{title}: {done} of {total} done ({who})"


def evaluate(restaurant_id, now_local=None, db_path=DB_PATH) -> dict:
    """One pass over the restaurant's open assignments: a critical line past
    due opens a texted issue for the routed manager (once per line a day); a
    sheet whose shift ended CLOSE_GRACE_MINUTES ago closes as done, partial
    or missed, and an unfinished one opens a quiet issue (Home, the issue
    list — not a text); a line one person missed PATTERN_MISSES times in
    PATTERN_DAYS days is a pattern the owner sees once a month.

    Every open row is swept, however old: one left open by a paused or
    failing job used to stay open (and tickable) forever once it fell out of
    a 3-day window (LG-30). A row older than STALE_DAYS closes quietly — no
    text about a line due last week, no issue on Home for it."""
    import issues
    r = _restaurant(restaurant_id)
    if r is None:
        return {"critical": 0, "closed": 0, "patterns": 0}
    now_local = now_local or local_now(restaurant_id, r)
    now_iso = now_local.strftime("%Y-%m-%dT%H:%M:%S")
    ensure_day(restaurant_id, business_day(restaurant_id, r, now_local), restaurant=r, db_path=db_path)
    recent_from = (now_local.date() - timedelta(days=STALE_DAYS)).isoformat()
    conn = get_conn(db_path)
    try:
        open_rows = conn.execute("SELECT * FROM task_assignments WHERE restaurant_id=? AND status='open' "
                                 "ORDER BY task_date, id", (restaurant_id,)).fetchall()
        latest = _latest(conn, [a["id"] for a in open_rows])
    finally:
        conn.close()
    critical = closed = 0
    closed_rows = []
    for a in open_rows:
        lines = json.loads(a["lines_json"] or "[]")
        stale = a["task_date"] < recent_from
        for l in ([] if stale else lines):
            if not l.get("critical") or not l.get("due_at") or l["due_at"] >= now_iso:
                continue
            if _line_state(latest, a["id"], l["line_id"]):
                continue
            key = f"task:{a['id']}:{l['line_id']}"
            if _issue_exists(restaurant_id, key, db_path):
                continue
            who = ", ".join(json.loads(a["assignees_json"] or "[]")) or "no one scheduled"
            try:
                _issue, token = issues.create_issue(
                    restaurant_id, "task_missed", f"Not done: {l['label']}"[:200],
                    detail=f"{a['title'] or a['job_code']} ({SHIFT_KIND_LABEL.get(a['shift_kind'], '')}), due "
                           f"{_clock(l['due_at'])} — {who}.",
                    severity="high", source_key=f"task:{a['id']}:{l['line_id']}",
                    meta={"modules": ["labor"], "assignment_id": a["id"], "task_date": a["task_date"]}, db_path=db_path)
                critical += 1 if _issue else 0
            except ValueError:
                pass
        end = a["shift_end"] or f"{a['task_date']}T23:59:00"
        if (datetime.fromisoformat(end) + timedelta(minutes=CLOSE_GRACE_MINUTES)) > now_local:
            continue
        done = sum(1 for l in lines if _line_state(latest, a["id"], l["line_id"]))
        total = len(lines)
        status = "done" if done == total else ("missed" if done == 0 else "partial")
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE task_assignments SET status=?, done_count=?, closed_at=datetime('now') WHERE id=? AND status='open'",
                         (status, done, a["id"]))
            conn.commit()
        finally:
            conn.close()
        closed += 1
        # An unassigned sheet nobody touched (no schedule published — Simple
        # EJ's has none yet) is not a nightly issue about work no one owned:
        # the owner's day view says "no schedule published". One someone
        # started, or one with a name on it, is.
        if status != "done" and not stale and not (a["unassigned"] and done == 0):
            closed_rows.append(a)
            try:
                issues.create_issue(restaurant_id, "task_sheet", _describe(a, done, total)[:200],
                                    detail="Left undone: " + "; ".join(
                                        l["label"] for l in lines if not _line_state(latest, a["id"], l["line_id"]))[:1500],
                                    source_key=f"tasksheet:{a['id']}", notify=False,
                                    meta={"modules": ["labor"], "assignment_id": a["id"], "task_date": a["task_date"]},
                                    db_path=db_path)
            except ValueError:
                pass
    patterns = _patterns(restaurant_id, now_local, db_path) if closed_rows else 0
    return {"critical": critical, "closed": closed, "patterns": patterns}


def _issue_exists(restaurant_id, source_key, db_path=DB_PATH) -> bool:
    conn = get_conn(db_path)
    try:
        return conn.execute("SELECT 1 FROM ops_issues WHERE restaurant_id=? AND source_key=?",
                            (restaurant_id, source_key)).fetchone() is not None
    finally:
        conn.close()


def _clock(iso):
    try:
        t = datetime.fromisoformat(iso)
        return t.strftime("%-I:%M%p").lower().replace(":00", "")
    except Exception:
        return iso


def misses(restaurant_id, since: date, until: date = None, db_path=DB_PATH) -> list:
    """Every line left undone on a closed sheet between two business dates:
    [{task_date, assignment_id, line_id, label, critical, assignees, job_code, shift_kind}]."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM task_assignments WHERE restaurant_id=? AND status IN ('partial','missed') "
                            "AND task_date >= ? AND task_date <= ? ORDER BY task_date",
                            (restaurant_id, since.isoformat(), (until or date.max).isoformat())).fetchall()
        latest = _latest(conn, [a["id"] for a in rows])
    finally:
        conn.close()
    out = []
    for a in rows:
        for l in json.loads(a["lines_json"] or "[]"):
            if not _line_state(latest, a["id"], l["line_id"]):
                out.append({"task_date": a["task_date"], "assignment_id": a["id"], "line_id": l["line_id"],
                            "label": l["label"], "critical": bool(l.get("critical")),
                            "assignees": json.loads(a["assignees_json"] or "[]"), "job_code": a["job_code"],
                            "shift_kind": a["shift_kind"]})
    return out


def _patterns(restaurant_id, now_local, db_path=DB_PATH) -> int:
    import issues
    since = now_local.date() - timedelta(days=PATTERN_DAYS)
    counts = {}
    for m in misses(restaurant_id, since, db_path=db_path):
        for who in m["assignees"]:
            counts.setdefault((who, m["line_id"]), []).append(m)
    opened = 0
    for (who, line_id), ms in counts.items():
        if len(ms) < PATTERN_MISSES:
            continue
        label = ms[-1]["label"]
        key = f"taskpattern:{_key(who)}:{line_id}:{now_local.strftime('%Y-%m')}"
        if _issue_exists(restaurant_id, key, db_path):
            continue
        try:
            _i, _t = issues.create_issue(
                restaurant_id, "task_pattern", f"{who} has missed “{label}” {len(ms)} times in {PATTERN_DAYS} days"[:200],
                detail="On " + ", ".join(m["task_date"] for m in ms) + ".",
                source_key=key, notify=False,
                meta={"modules": ["labor"], "employee": who}, db_path=db_path)
            opened += 1 if _i else 0
        except ValueError:
            pass
    return opened


# ── the consistency report ──────────────────────────────────────────────────

REPORT_MIN_SHEETS = 3
REPORT_MAX_DAYS = 90


def report(restaurant_id, days=14, db_path=DB_PATH, today=None) -> dict:
    """Per job code and per person over the last `days` business days of
    CLOSED sheets: completion rate, on-time rate, critical misses — and the
    managers side by side. Deterministic, from the completions; a person on
    fewer than REPORT_MIN_SHEETS sheets gets counts, not rates."""
    today = today or business_day(restaurant_id)
    since = today - timedelta(days=int(days))
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM task_assignments WHERE restaurant_id=? AND status NOT IN ('open','none') "
                            "AND task_date >= ? AND task_date < ?", (restaurant_id, since.isoformat(), today.isoformat())
                            ).fetchall()
        latest = _latest(conn, [a["id"] for a in rows])
        signed = {(s["task_date"], s["shift_kind"]) for s in conn.execute(
            "SELECT task_date, shift_kind FROM task_signoffs WHERE restaurant_id=? AND task_date >= ?",
            (restaurant_id, since.isoformat()))}
    finally:
        conn.close()

    def blank():
        return {"sheets": 0, "lines": 0, "done": 0, "on_time": 0, "timed": 0, "critical_missed": 0, "complete_sheets": 0}

    people, codes = {}, {}
    for a in rows:
        lines = json.loads(a["lines_json"] or "[]")
        stat = blank()
        stat["sheets"] = 1
        for l in lines:
            c = latest.get((a["id"], l["line_id"]))
            ok = bool(c) and not c["undone"]
            stat["lines"] += 1
            stat["done"] += 1 if ok else 0
            if l.get("due_at"):
                stat["timed"] += 1
                stat["on_time"] += 1 if (ok and not c["late"]) else 0
            if l.get("critical") and not ok:
                stat["critical_missed"] += 1
        stat["complete_sheets"] = 1 if stat["done"] == stat["lines"] else 0
        for bucket, k in ((codes, a["job_code"]),) + tuple((people, n) for n in json.loads(a["assignees_json"] or "[]")):
            cur = bucket.setdefault(k, blank())
            for f in cur:
                cur[f] += stat[f]
        if a["unassigned"]:
            cur = people.setdefault("(no one scheduled)", blank())
            for f in cur:
                cur[f] += stat[f]

    def rates(name, s, job_code=None):
        enough = s["sheets"] >= REPORT_MIN_SHEETS
        return {"name": name, "job_code": job_code, **s, "enough": enough,
                "completion_pct": round(100 * s["done"] / s["lines"]) if enough and s["lines"] else None,
                "on_time_pct": round(100 * s["on_time"] / s["timed"]) if enough and s["timed"] else None}

    person_codes = {}
    for a in rows:
        for n in json.loads(a["assignees_json"] or "[]"):
            person_codes.setdefault(n, set()).add(a["job_code"])
    by_person = sorted((rates(n, s, ", ".join(sorted(person_codes.get(n, [])))) for n, s in people.items()),
                       key=lambda x: (x["completion_pct"] is None, x["completion_pct"] or 0, x["name"]))
    managers = [p for p in by_person if any(is_manager_code(c) for c in person_codes.get(p["name"], []))]
    need_ids = {s["id"] for s in list_sheets(restaurant_id, include_inactive=True, db_path=db_path) if s["requires_signoff"]}
    shifts_needing = {(a["task_date"], a["shift_kind"]) for a in rows if a["sheet_id"] in need_ids}
    from time_utils import mdy
    return {"days": int(days), "since": since.isoformat(), "until": (today - timedelta(days=1)).isoformat(),
            "window_label": f"{mdy(since)} – {mdy(today - timedelta(days=1))}",
            "sheets": len(rows), "min_sheets": REPORT_MIN_SHEETS,
            "by_job_code": sorted((rates(k, s) for k, s in codes.items()), key=lambda x: x["name"].lower()),
            "by_person": by_person, "managers": managers,
            "signoffs_missed": len(shifts_needing - signed), "signoffs_expected": len(shifts_needing)}


def person_record(restaurant_id, employee_name, days=30, db_path=DB_PATH) -> dict:
    """One person's sheets for the Staff screen, next to the operational
    score: sheets, lines done, late ticks, critical misses, recent misses."""
    rep = report(restaurant_id, days=days, db_path=db_path)
    me = next((p for p in rep["by_person"] if _key(p["name"]) == _key(employee_name)), None)
    recent = [m for m in misses(restaurant_id, date.today() - timedelta(days=days), db_path=db_path)
              if _key(employee_name) in {_key(n) for n in m["assignees"]}][-5:]
    return {"days": days, "summary": me, "recent_misses": recent}


def yesterday_line(restaurant_id, today=None, db_path=DB_PATH):
    """The morning brief's line about last business day's sheets, or None
    when every sheet was finished (or there were none)."""
    today = today or date.today()
    y = (today - timedelta(days=1)).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM task_assignments WHERE restaurant_id=? AND task_date=? AND status <> 'none'",
                            (restaurant_id, y)).fetchall()
        latest = _latest(conn, [a["id"] for a in rows])
    finally:
        conn.close()
    short = []
    for a in rows:
        lines = json.loads(a["lines_json"] or "[]")
        done = sum(1 for l in lines if _line_state(latest, a["id"], l["line_id"]))
        # The same rule as evaluate(): an unassigned sheet nobody touched (no
        # schedule published) is not work anyone left undone. Simple EJ's
        # brief said "Server PM sheet: 0 of 9 done (no one scheduled)" every
        # morning once a published week was taken back (10/5/26).
        if a["unassigned"] and done == 0:
            continue
        if lines and done < len(lines):
            short.append(_describe(a, done, len(lines)))
    if not short:
        return None
    more = f" (+{len(short) - 2} more)" if len(short) > 2 else ""
    return {"key": "task_sheets", "tone": "bad",
            "text": "Yesterday's sheets left unfinished: " + "; ".join(short[:2]) + more + ".",
            "ask": "Which task sheet lines were skipped yesterday, and by whom?"}


# ── the job ─────────────────────────────────────────────────────────────────

def run_job(restaurants=None, db_path=DB_PATH) -> dict:
    """Every 20 minutes (the intraday slot): issue today's sheets, open the
    critical-miss issues, close the shifts that ended. Bounded and resumable
    (strategy_jobs._BoundedWalk)."""
    import ops
    from strategy_jobs import _BoundedWalk, _counts, _restaurants
    rs = restaurants if restaurants is not None else _restaurants(db_path)
    conn = get_conn(db_path)
    try:
        with_sheets = {row[0] for row in conn.execute("SELECT DISTINCT restaurant_id FROM task_sheets WHERE active=1")}
    finally:
        conn.close()
    walk = _BoundedWalk("task_sheets", [r for r in rs if r.id in with_sheets], db_path, TASK_JOB_MAX_SECONDS)
    attempted = failed = critical = closed = 0
    for r in walk:
        attempted += 1
        try:
            out = evaluate(r.id, db_path=db_path)
            critical += out["critical"]
            closed += out["closed"]
        except Exception as e:
            failed += 1
            ops.capture(e, job="task_sheets", context=f"restaurant_id={r.id}")
    return _counts(attempted, attempted - failed, failed, hit_bound=walk.hit_bound, critical=critical, closed=closed)


TASK_JOB_MAX_SECONDS = 8 * 60


# ── Cavnar AI's starter sheet (drafts only, accepted line by line) ──────────

STARTER_MAX_LINES = 18


def starter_lines(restaurant_id, job_code, shift_kind, existing=(), db_path=DB_PATH) -> list:
    """A draft sheet for one job code and shift from general restaurant
    practice and this restaurant's own profile — never saved: the owner adds
    the lines he wants, one by one (plan principle 5). Each line through
    response_validation (surface task_draft); a refused line is dropped."""
    import data_health
    import response_validation as rv
    from ai_utils import create_with_retry, get_client, model_for, extract_text
    from ai_guard import wrap_untrusted
    r = _restaurant(restaurant_id)
    if r is None:
        raise TaskSheetError("That restaurant isn't here.")
    if shift_kind not in SHIFT_KINDS:
        raise TaskSheetError("Pick opening, closing, mid or all day.")
    about = ", ".join(x for x in (getattr(r, "vibe", "") or "", getattr(r, "known_for", "") or "") if x)[:400]
    have = "; ".join(str(x) for x in list(existing)[:40])
    prompt = (
        f"Draft a {SHIFT_KIND_LABEL[shift_kind].lower()} task sheet for the job code \"{job_code}\" at a restaurant.\n"
        f"About the restaurant (the owner's profile): {wrap_untrusted(about) if about else 'not given'}\n"
        + (f"Lines already on the sheet (do not repeat them): {wrap_untrusted(have)}\n" if have else "")
        + f"Return ONLY a JSON array of up to {STARTER_MAX_LINES} objects: "
        "{\"label\": \"one concrete duty, imperative, max 14 words\", \"section\": \"optional area name or null\", "
        "\"proof\": \"none|photo|number|note\", \"proof_label\": \"what to record, e.g. Walk-in °F, or null\", "
        "\"critical\": true|false}.\n"
        "Rules: general restaurant practice only — never invent this restaurant's equipment, menu items, codes, "
        "people or numbers; no temperatures or amounts in the label (a number line asks for the reading instead); "
        "critical only for food safety, cash and security; in the order the work is done."
    )
    msg = create_with_retry(get_client(), model=model_for("task_sheets"), max_tokens=1600,
                            messages=[{"role": "user", "content": prompt}], restaurant_id=restaurant_id,
                            action="task_sheet_starter", readiness=data_health.NOT_APPLICABLE)
    text = extract_text(msg)
    m = re.search(r"\[.*\]", text or "", re.S)
    try:
        raw = json.loads(m.group(0)) if m else []
    except Exception:
        raw = []
    ctx = rv.ValidationContext(restaurant_id=restaurant_id, surface="task_draft", audience="owner",
                               delivery="interactive", context_text=about)
    have_keys = {_key(x) for x in existing}
    out = []
    for item in raw[:STARTER_MAX_LINES]:
        if not isinstance(item, dict):
            continue
        try:
            line = _clean_line({k: item.get(k) for k in ("label", "section", "proof", "proof_label", "critical")})
        except TaskSheetError:
            continue
        if _key(line["label"]) in have_keys:
            continue
        v = rv.validate(line["label"], ctx)
        if v.verdict == "refuse" or not v.text.strip():
            continue
        line["label"] = v.text.strip()[:LABEL_MAX]
        line.setdefault("proof", "none")
        line["critical"] = bool(line.get("critical"))
        out.append(line)
    return out
