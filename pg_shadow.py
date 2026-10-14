"""
pg_shadow.py — the normalized schedule model written beside SQLite, behind a
flag (scheduler audit 10/10/26: RM-13, ARCH-34; POSTGRES_AND_WORKERS_PLAN.md
step 3, "run as a shadow … dual-write, compare, before the switch").

While SQLite is the primary, every schedule revision (a schedule_versions
row: generated, edited, fixes, swap, published, rename …) is also written to
Postgres as the model the readers will move to (migrations/postgres/
0001_schedule_model.sql): its week (sched.schedule_weeks, with the working
and published revision pointers), the revision (sched.schedule_revisions),
every shift of it by its stable shift_id on real time (sched.shifts —
timestamptz in the restaurant's zone; a published revision cannot hold a
double booking, the database refuses it), and the shift operations it made
(sched.schedule_events: add / remove / change, generate / publish).

THE OUTBOX. A revision is noted in `pg_shadow_outbox` in the same
transaction that writes it (note_revision, from schedule_versions.
_insert_version) when PG_SHADOW_WRITE=1 — a row, never a network call on the
save path, so the shadow can never fail or slow a save. The `pg_shadow` job
drains it to SHADOW_DATABASE_URL every ten minutes (run_shadow_pass,
bounded, in order, each row retried with backoff; a row the database
refuses — a double booking in a published week — keeps its error, which is
itself a finding). backfill() notes the revisions written before the flag.
compare() checks both sides: the counts, and each sent revision's shifts
hashed field by field on both sides (blind re-audit 10/11/26 SCALE-10: equal
counts said nothing about a shift written with the wrong time or person) —
the shadow is trusted when every hash agrees.
"""
import hashlib
import json
import os
import time

OUTBOX_DDL = """CREATE TABLE IF NOT EXISTS pg_shadow_outbox (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id  INTEGER NOT NULL,
    history_id     INTEGER NOT NULL,
    version        INTEGER NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at        TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    next_at        TEXT,
    error          TEXT,
    UNIQUE (history_id, version)
)"""
PASS_SECONDS = 60
BATCH = 200
BACKOFF_MINUTES = (5, 30, 120, 720)


def get_conn(db_path=None):
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def enabled() -> bool:
    return (os.getenv("PG_SHADOW_WRITE") or "").strip().lower() in ("1", "true", "on", "yes")


def target_url():
    import db_dialect
    return db_dialect.database_url("SHADOW_DATABASE_URL")


def init_pg_shadow(db_path=None):
    """The outbox — at boot (models.init_db)."""
    conn = get_conn(db_path)
    try:
        conn.execute(OUTBOX_DDL)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pg_shadow_outbox_open ON pg_shadow_outbox(sent_at, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pg_shadow_outbox_created ON pg_shadow_outbox(created_at)")
        conn.commit()
    finally:
        conn.close()


def note_revision(conn, restaurant_id, history_id, version) -> bool:
    """On the caller's connection and transaction (the version's own write):
    one outbox row for this revision, when the shadow is on."""
    if not enabled():
        return False
    conn.execute("INSERT OR IGNORE INTO pg_shadow_outbox (restaurant_id, history_id, version) VALUES (?,?,?)",
                 (int(restaurant_id), int(history_id), int(version)))
    return True


def backfill(restaurant_id=None, since_days=None, limit=5000, db_path=None) -> int:
    """Note the revisions already written (all, or one restaurant's, or the
    last `since_days`), oldest first. Returns how many were noted."""
    conn = get_conn(db_path)
    try:
        where, args = ["1=1"], []
        if restaurant_id is not None:
            where.append("restaurant_id=?")
            args.append(int(restaurant_id))
        if since_days:
            where.append("created_at >= datetime('now', ?)")
            args.append(f"-{int(since_days)} days")
        cur = conn.execute(
            "INSERT OR IGNORE INTO pg_shadow_outbox (restaurant_id, history_id, version) "
            "SELECT restaurant_id, history_id, version FROM schedule_versions WHERE " + " AND ".join(where)
            + " ORDER BY id LIMIT ?", args + [int(limit)])
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


# ── the projection ──────────────────────────────────────────────────────────

def _iso_utc(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00") if dt else None


def project(conn, restaurant_id, history_id, version) -> dict:
    """The normalized records of one revision, read from SQLite: {week,
    revision, shifts, events}. None when the revision is gone."""
    import schedule_versions as sv
    import schedule_time
    v = conn.execute("SELECT id, version, reason, saved_by, saved_authority, created_at, " + sv.VERSION_TEXT
                     + " FROM schedule_versions WHERE history_id=? AND version=? AND restaurant_id=?",
                     (int(history_id), int(version), int(restaurant_id))).fetchone()
    h = conn.execute("SELECT id, restaurant_id, week_start, week_end, superseded_by, published_at "
                     "FROM schedule_history WHERE id=?", (int(history_id),)).fetchone()
    if v is None or h is None:
        return None
    v = sv.materialize_one(conn, v, history_id)
    prev = None
    if int(version) > 1:
        prev = sv.materialize_one(conn, conn.execute(
            "SELECT version, " + sv.VERSION_TEXT + " FROM schedule_versions WHERE history_id=? AND version<? "
            "ORDER BY version DESC LIMIT 1", (int(history_id), int(version))).fetchone(), history_id)
    try:
        import time_utils
        import models
        tz = time_utils.restaurant_tz(models.get_restaurant(int(restaurant_id))).key
    except Exception:
        tz = "America/Chicago"
    published = v["reason"] == "published"
    # The revision's rows from the shift table when it is the revision the
    # table holds (a drain right after the write); an older one's stored
    # text otherwise — the table keeps one revision per copy
    # (schedule_shifts.revision_rows, schedule re-audit 10/11/26 ARCH-7).
    import schedule_shifts
    rows = schedule_shifts.revision_rows(conn, int(history_id), int(version), v["schedule_csv"])
    shifts = []
    seen = set()
    for i, r in enumerate(rows):
        start, end = schedule_time.shift_span(r, tz)
        if start is None:
            continue
        sid = r.get("shift_id") or f"row{i:04d}"
        if sid in seen:
            sid = f"{sid}-{i}"
        seen.add(sid)
        try:
            pid = int(r.get("person_id")) if r.get("person_id") else None
        except (TypeError, ValueError):
            pid = None
        try:
            hours = float(r.get("scheduled_hours")) if r.get("scheduled_hours") not in (None, "") else None
        except (TypeError, ValueError):
            hours = None
        shifts.append({"shift_id": sid, "person_id": pid, "employee": r.get("employee") or "",
                       "role": r.get("role") or None, "business_date": str(r.get("date"))[:10],
                       "start_ts": _iso_utc(start), "end_ts": _iso_utc(end), "scheduled_hours": hours,
                       "notes": r.get("notes") or None, "locked": bool(r.get("locked")), "published": published})
    events = _events(v, prev, rows, conn=conn, history_id=int(history_id))
    return {"week": {"id": int(h["id"]), "restaurant_id": int(h["restaurant_id"]),
                     "week_start": str(h["week_start"] or "")[:10], "week_end": str(h["week_end"] or "")[:10] or None,
                     "superseded_by": h["superseded_by"]},
            "revision": {"id": int(v["id"]), "week_id": int(h["id"]), "restaurant_id": int(restaurant_id),
                         "version": int(v["version"]), "parent_version": int(prev["version"]) if prev else None,
                         "kind": v["reason"], "author": v["saved_by"], "authority": v["saved_authority"],
                         "created_at": str(v["created_at"]).replace(" ", "T") + "+00:00"},
            "shifts": shifts, "events": events, "published": published}


def _events(v, prev, rows, conn=None, history_id=None):
    """The revision's operations on shifts, by shift_id: add / remove /
    change (before and after), and generate / publish for those kinds. The
    revision before is an older one: its stored text (revision_rows)."""
    import schedule_versions as sv
    import schedule_shifts
    out = []
    if v["reason"] in ("generated", "published"):
        out.append({"kind": "generate" if v["reason"] == "generated" else "publish", "payload": {"rows": len(rows)}})
    before_rows = (schedule_shifts.revision_rows(conn, history_id, prev["version"], prev["schedule_csv"])
                   if prev and conn is not None else (sv.rows_from_csv(prev["schedule_csv"]) if prev else []))
    before = {r.get("shift_id"): r for r in before_rows if r.get("shift_id")}
    after = {r.get("shift_id"): r for r in rows if r.get("shift_id")}
    for sid in sorted(set(before) | set(after)):
        b, a = before.get(sid), after.get(sid)
        if b is None:
            out.append({"kind": "add", "payload": {"shift_id": sid, "after": a}})
        elif a is None:
            out.append({"kind": "remove", "payload": {"shift_id": sid, "before": b}})
        elif sv._row_sig(a) != sv._row_sig(b):
            out.append({"kind": "change", "payload": {"shift_id": sid, "before": b, "after": a}})
    return out


# ── the write (portable SQL: `?` placeholders, through db_dialect on PG) ────

def write(target, proj) -> None:
    """One revision's records into the shadow, idempotent (a re-drain
    writes nothing twice). The caller commits. On Postgres (a db_dialect.
    PgConnection) the dates, stamps and payload are cast to their types; any
    other target (a SQLite stand-in) takes them as text."""
    import db_dialect
    pg = isinstance(target, db_dialect.PgConnection)

    def c(t):
        return f"CAST(? AS {t})" if pg else "?"
    w, rv = proj["week"], proj["revision"]
    target.execute(
        "INSERT INTO sched.schedule_weeks (id, restaurant_id, week_start, week_end, superseded_by, "
        f"working_revision_id, published_revision_id) VALUES (?, ?, {c('date')}, {c('date')}, ?, NULL, NULL) "
        "ON CONFLICT (id) DO UPDATE SET superseded_by = excluded.superseded_by, week_end = excluded.week_end",
        (w["id"], w["restaurant_id"], w["week_start"], w["week_end"], w["superseded_by"]))
    target.execute(
        "INSERT INTO sched.schedule_revisions (id, week_id, restaurant_id, version, parent_version, kind, author, "
        f"authority, created_at) VALUES (?,?,?,?,?,?,?,?, {c('timestamptz')}) ON CONFLICT (id) DO NOTHING",
        (rv["id"], rv["week_id"], rv["restaurant_id"], rv["version"], rv["parent_version"], rv["kind"],
         rv["author"], rv["authority"], rv["created_at"]))
    for s in proj["shifts"]:
        target.execute(
            "INSERT INTO sched.shifts (revision_id, shift_id, week_id, restaurant_id, person_id, employee, role, "
            "business_date, start_ts, end_ts, scheduled_hours, notes, locked, published) VALUES "
            f"(?,?,?,?,?,?,?, {c('date')}, {c('timestamptz')}, {c('timestamptz')}, ?,?,?,?) "
            "ON CONFLICT (revision_id, shift_id) DO NOTHING",
            (rv["id"], s["shift_id"], w["id"], w["restaurant_id"], s["person_id"], s["employee"], s["role"],
             s["business_date"], s["start_ts"], s["end_ts"], s["scheduled_hours"], s["notes"], s["locked"],
             s["published"]))
    for i, e in enumerate(proj["events"]):
        target.execute(
            "INSERT INTO sched.schedule_events (week_id, seq, restaurant_id, revision_id, actor, authority, kind, "
            f"payload, created_at) VALUES (?,?,?,?,?,?,?, {c('jsonb')}, {c('timestamptz')}) "
            "ON CONFLICT (week_id, seq) DO NOTHING",
            (w["id"], rv["version"] * 100000 + i, w["restaurant_id"], rv["id"], rv["author"], rv["authority"],
             e["kind"], json.dumps(e["payload"], default=str), rv["created_at"]))
    # The week's pointers: the newest revision is the working copy; a
    # published one is what staff have.
    target.execute("UPDATE sched.schedule_weeks SET working_revision_id = ? WHERE id = ? AND "
                   "(working_revision_id IS NULL OR working_revision_id < ?)", (rv["id"], w["id"], rv["id"]))
    if proj["published"]:
        target.execute("UPDATE sched.schedule_weeks SET published_revision_id = ? WHERE id = ? AND "
                       "(published_revision_id IS NULL OR published_revision_id < ?)", (rv["id"], w["id"], rv["id"]))


def drain(target=None, max_seconds=PASS_SECONDS, db_path=None) -> dict:
    """Write the outbox's due rows to the shadow, oldest first, until done
    or `max_seconds`. {attempted, ok, failed, skipped, hit_bound}."""
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    own = target is None
    if own:
        url = target_url()
        if not url:
            out["skipped"] = 1
            return out
        import db_dialect
        target = db_dialect.connect(url)
    src = get_conn(db_path)
    stop = time.monotonic() + float(max_seconds)
    try:
        rows = src.execute("SELECT id, restaurant_id, history_id, version, attempts FROM pg_shadow_outbox "
                           "WHERE sent_at IS NULL AND (next_at IS NULL OR next_at <= datetime('now')) "
                           "ORDER BY id LIMIT ?", (BATCH,)).fetchall()
        for r in rows:
            if time.monotonic() > stop:
                out["hit_bound"] = True
                break
            out["attempted"] += 1
            try:
                proj = project(src, r["restaurant_id"], r["history_id"], r["version"])
                if proj is not None:
                    write(target, proj)
                target.commit()
                src.execute("UPDATE pg_shadow_outbox SET sent_at=datetime('now'), error=NULL WHERE id=?", (r["id"],))
                src.commit()
                out["ok"] += 1
            except Exception as e:
                try:
                    target.rollback()
                except Exception:
                    pass
                n = int(r["attempts"] or 0) + 1
                wait = BACKOFF_MINUTES[min(n, len(BACKOFF_MINUTES)) - 1]
                src.execute("UPDATE pg_shadow_outbox SET attempts=?, error=?, next_at=datetime('now', ?) WHERE id=?",
                            (n, str(e)[:400], f"+{wait} minutes", r["id"]))
                src.commit()
                out["failed"] += 1
        return out
    finally:
        src.close()
        if own:
            try:
                target.close()
            except Exception:
                pass


def run_shadow_pass() -> dict:
    """The `pg_shadow` job: the drain when the shadow is on, else skipped."""
    if not enabled():
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 1, "hit_bound": False}
    return drain()


_SHIFT_FIELDS = ("shift_id", "person_id", "employee", "role", "business_date", "start_ts", "end_ts",
                 "scheduled_hours", "notes", "locked", "published")


def _norm(field, v):
    """One shift field as comparable text, whichever side read it (a
    Postgres timestamptz / date / numeric, or the SQLite stand-in's text)."""
    import datetime as _dt
    if v is None:
        return ""
    if isinstance(v, _dt.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(_dt.timezone.utc)
        return v.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    if isinstance(v, _dt.date):
        return v.isoformat()
    if field in ("locked", "published"):
        return "1" if v in (True, 1, "1", "true", "t") else "0"
    if field in ("scheduled_hours", "person_id"):
        try:
            return f"{float(v):.4f}"
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def shifts_hash(shifts) -> str:
    """A revision's shifts as one digest: every field of every shift, in
    shift_id order."""
    h = hashlib.sha256()
    for s_ in sorted(shifts, key=lambda r: str(r["shift_id"])):
        h.update("\x1f".join(_norm(f, s_[f]) for f in _SHIFT_FIELDS).encode())
        h.update(b"\x1e")
    return h.hexdigest()


def compare(target, restaurant_id=None, db_path=None, max_revisions=500) -> dict:
    """{weeks, revisions, outbox_open, outbox_failed: (source, shadow),
    content: {checked, mismatched: [{revision_id, history_id, version,
    source, shadow}]}} — the source's weeks and revisions the outbox has
    sent, against the shadow's, and the newest `max_revisions` sent
    revisions' shifts hashed on both sides; `ok` when the counts agree and
    no hash differs."""
    src = get_conn(db_path)
    try:
        args = [int(restaurant_id)] if restaurant_id is not None else []
        rw = " AND restaurant_id=?" if restaurant_id is not None else ""
        sent = src.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT history_id) AS w FROM pg_shadow_outbox "
                           "WHERE sent_at IS NOT NULL" + rw, args).fetchone()
        open_ = src.execute("SELECT COUNT(*) FROM pg_shadow_outbox WHERE sent_at IS NULL" + rw, args).fetchone()[0]
        failed = src.execute("SELECT COUNT(*) FROM pg_shadow_outbox WHERE sent_at IS NULL AND error IS NOT NULL"
                             + rw, args).fetchone()[0]
    finally:
        src.close()
    revs = target.execute("SELECT COUNT(*) FROM sched.schedule_revisions WHERE 1=1" + rw, args).fetchone()[0]
    weeks = target.execute("SELECT COUNT(*) FROM sched.schedule_weeks WHERE 1=1" + rw, args).fetchone()[0]
    out = {"revisions": (int(sent["n"]), int(revs)), "weeks": (int(sent["w"]), int(weeks)),
           "outbox_open": int(open_), "outbox_failed": int(failed)}
    out["content"] = _compare_content(target, restaurant_id, db_path, max_revisions)
    out["ok"] = out["revisions"][0] == out["revisions"][1] and out["weeks"][0] == out["weeks"][1] \
        and not out["content"]["mismatched"]
    return out


def _compare_content(target, restaurant_id, db_path, max_revisions) -> dict:
    src = get_conn(db_path)
    mismatched, checked = [], 0
    try:
        args = [int(restaurant_id)] if restaurant_id is not None else []
        sent = src.execute("SELECT restaurant_id, history_id, version FROM pg_shadow_outbox WHERE sent_at IS NOT NULL"
                           + (" AND restaurant_id=?" if restaurant_id is not None else "")
                           + " ORDER BY id DESC LIMIT ?", args + [int(max_revisions)]).fetchall()
        for r in sent:
            proj = project(src, r["restaurant_id"], r["history_id"], r["version"])
            if proj is None:
                continue
            rid = proj["revision"]["id"]
            rows = target.execute("SELECT " + ", ".join(_SHIFT_FIELDS) + " FROM sched.shifts WHERE revision_id = ?",
                                  (rid,)).fetchall()
            mine = shifts_hash(proj["shifts"])
            theirs = shifts_hash([{f: row[i] for i, f in enumerate(_SHIFT_FIELDS)} for row in rows])
            checked += 1
            if mine != theirs:
                mismatched.append({"revision_id": rid, "history_id": int(r["history_id"]),
                                   "version": int(r["version"]), "source": mine, "shadow": theirs})
    finally:
        src.close()
    return {"checked": checked, "mismatched": mismatched}
