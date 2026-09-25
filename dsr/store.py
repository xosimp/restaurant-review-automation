"""
dsr.store — where reports, their stages, their facts and the searchable
daily metrics live.

A report is one row per (restaurant, business date, VERSION). A night that
finalised provisionally (a block still awaiting at the deadline) and was
completed later gets version 2; the owner sees "Updated", never a silent
edit. Stage times are the real times each step finished — they stay on the
report as its provenance.

`dsr_metrics` holds one row per (restaurant, business date, metric) from the
latest version, so history questions ("every day labor was over 25%") are
indexed SQL, not a model reading old reports.
"""
import json
from datetime import datetime

import models as _models_mod
from models import DB_PATH

import dsr as _dsr


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def init_dsr(db_path=DB_PATH):
    """Tables, created at boot (models.init_db)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS dsr_reports (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            business_date   TEXT    NOT NULL,
            version         INTEGER NOT NULL DEFAULT 1,
            status          TEXT    NOT NULL DEFAULT 'scheduled',
            trigger         TEXT,
            stages_json     TEXT    NOT NULL DEFAULT '{}',
            facts_json      TEXT,
            narrative_json  TEXT,
            provisional     INTEGER NOT NULL DEFAULT 0,
            attempts        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            error           TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            finalized_at    TEXT,
            UNIQUE(restaurant_id, business_date, version)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dsr_reports_rid_date ON dsr_reports(restaurant_id, business_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dsr_reports_status ON dsr_reports(status, next_attempt_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS dsr_metrics (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            business_date   TEXT    NOT NULL,
            metric          TEXT    NOT NULL,
            value           REAL,
            status          TEXT    NOT NULL,
            source          TEXT,
            report_id       INTEGER,
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, business_date, metric)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dsr_metrics_series ON dsr_metrics(restaurant_id, metric, business_date)")
        conn.execute("""CREATE TABLE IF NOT EXISTS dsr_budgets (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            business_date   TEXT    NOT NULL,
            gross           REAL,
            net             REAL,
            updated_by      INTEGER,
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, business_date)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS dsr_category_map (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            pos_name        TEXT    NOT NULL,
            category        TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, pos_name)
        )""")
        # Last year from the owner's old DSR workbooks — RPower keeps only a
        # month of history for some stores, so Last Year would otherwise stay
        # empty for a year. One row per business date; a re-import replaces.
        conn.execute("""CREATE TABLE IF NOT EXISTS dsr_history_import (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            business_date   TEXT    NOT NULL,
            gross           REAL,
            net             REAL,
            categories_json TEXT,
            source_file     TEXT,
            imported_by     INTEGER,
            imported_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, business_date)
        )""")
        # Who was told about a night, on which channel, and whether it was
        # the first notice or the one "Updated" (dsr.deliver). The row is
        # the claim: it is written BEFORE the send, and UNIQUE makes a retry,
        # a second process or a re-run of the night a no-op rather than a
        # second email. user_id 0 is the restaurant's one notification-
        # history row (channel "history") for the night and kind.
        conn.execute("""CREATE TABLE IF NOT EXISTS dsr_deliveries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            business_date   TEXT    NOT NULL,
            user_id         INTEGER NOT NULL,
            channel         TEXT    NOT NULL,
            kind            TEXT    NOT NULL,
            report_id       INTEGER,
            version         INTEGER,
            provisional     INTEGER NOT NULL DEFAULT 0,
            view            TEXT,
            status          TEXT    NOT NULL,
            hold_until      TEXT,
            detail          TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            sent_at         TEXT,
            UNIQUE(restaurant_id, business_date, user_id, channel, kind)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dsr_deliveries_held ON dsr_deliveries(status, hold_until)")
        # What a night's report said about the NEXT night, and how it turned
        # out (dsr.predictions — "How did yesterday turn out?", 9/25/26).
        # Written once per (restaurant, night predicted, key) BEFORE that night
        # happens — a re-run never rewrites a prediction after the fact — and
        # graded from that night's own measured blocks.
        conn.execute("""CREATE TABLE IF NOT EXISTS dsr_predictions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            for_date        TEXT    NOT NULL,
            made_on         TEXT    NOT NULL,
            key             TEXT    NOT NULL,
            text            TEXT    NOT NULL,
            metric          TEXT    NOT NULL,
            op              TEXT    NOT NULL,
            value           REAL,
            low             REAL,
            high            REAL,
            basis           TEXT,
            outcome         TEXT,
            actual          REAL,
            graded_at       TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, for_date, key)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dsr_predictions_graded ON dsr_predictions(restaurant_id, for_date, outcome)")
        # The business dates the live clock-in check (strategy_jobs
        # .run_coverage_check) really ran for a restaurant during service
        # (D1-17): the Labor block counts no-shows only for a night the
        # check saw — "0 no-shows" on a night it never ran was a guess.
        conn.execute("""CREATE TABLE IF NOT EXISTS dsr_coverage_runs (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            business_date   TEXT    NOT NULL,
            first_at        TEXT    NOT NULL DEFAULT (datetime('now')),
            last_at         TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, business_date)
        )""")
        conn.commit()
    finally:
        conn.close()


# ── reports ────────────────────────────────────────────────────────────────

def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k, default in (("stages_json", {}), ("facts_json", None), ("narrative_json", None)):
        try:
            d[k[:-5]] = json.loads(d.get(k) or "null") if d.get(k) else default
        except (TypeError, ValueError):
            d[k[:-5]] = default
    d["provisional"] = bool(d.get("provisional"))
    return d


def get_report(restaurant_id, business_date, version=None, db_path=DB_PATH):
    """The report for a night — the latest version unless one is named."""
    day = str(business_date)[:10]
    conn = get_conn(db_path)
    try:
        if version is None:
            r = conn.execute("SELECT * FROM dsr_reports WHERE restaurant_id=? AND business_date=? "
                             "ORDER BY version DESC LIMIT 1", (restaurant_id, day)).fetchone()
        else:
            r = conn.execute("SELECT * FROM dsr_reports WHERE restaurant_id=? AND business_date=? AND version=?",
                             (restaurant_id, day, int(version))).fetchone()
    finally:
        conn.close()
    return _row(r)


def get_report_by_id(report_id, restaurant_id=None, db_path=DB_PATH):
    """By id, scoped to a restaurant when one is given (a route always gives
    one — a report id alone must never reach another tenant's report)."""
    conn = get_conn(db_path)
    try:
        if restaurant_id is None:
            r = conn.execute("SELECT * FROM dsr_reports WHERE id=?", (report_id,)).fetchone()
        else:
            r = conn.execute("SELECT * FROM dsr_reports WHERE id=? AND restaurant_id=?",
                             (report_id, restaurant_id)).fetchone()
    finally:
        conn.close()
    return _row(r)


FINISHED = ("final", "provisional")


def get_finished_report(restaurant_id, business_date, db_path=DB_PATH):
    """The latest version of a night that FINISHED (final or provisional) —
    what every reader after the night shows. A v2 still collecting does not
    hide the v1 the owner already has."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM dsr_reports WHERE restaurant_id=? AND business_date=? AND status IN (?,?) "
                         "ORDER BY version DESC LIMIT 1",
                         (restaurant_id, str(business_date)[:10], *FINISHED)).fetchone()
    finally:
        conn.close()
    return _row(r)


def latest_finished_report(restaurant_id, on_or_before, since=None, db_path=DB_PATH):
    """The most recent finished night on or before `on_or_before` (and on or
    after `since`, when given), its latest finished version; None if none."""
    args = [restaurant_id, str(on_or_before)[:10], *FINISHED]
    where = "restaurant_id=? AND business_date<=? AND status IN (?,?)"
    if since:
        where += " AND business_date>=?"
        args.append(str(since)[:10])
    conn = get_conn(db_path)
    try:
        r = conn.execute(f"SELECT * FROM dsr_reports WHERE {where} ORDER BY business_date DESC, version DESC LIMIT 1",
                         args).fetchone()
    finally:
        conn.close()
    return _row(r)


def create_report(restaurant_id, business_date, trigger=None, db_path=DB_PATH):
    """A new version for the night: 1 for the first, one more than the
    latest otherwise. Returns the report."""
    day = str(business_date)[:10]
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("SELECT COALESCE(MAX(version), 0) FROM dsr_reports WHERE restaurant_id=? AND business_date=?",
                           (restaurant_id, day)).fetchone()[0]
        rid = conn.execute(
            "INSERT INTO dsr_reports (restaurant_id, business_date, version, status, trigger, stages_json) "
            "VALUES (?,?,?,?,?,?)",
            (restaurant_id, day, int(cur) + 1, "scheduled", trigger,
             json.dumps({"scheduled": _now()}))).lastrowid
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_report_by_id(rid, db_path=db_path)


def set_stage(report_id, stage, db_path=DB_PATH, error=None):
    """Move a report to `stage`, stamping when it got there."""
    if stage not in _dsr.STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT stages_json FROM dsr_reports WHERE id=?", (report_id,)).fetchone()
        if r is None:
            raise KeyError(f"no dsr report {report_id}")
        stages = json.loads(r["stages_json"] or "{}")
        stages[stage] = _now()
        fields = ["status=?", "stages_json=?"]
        args = [stage, json.dumps(stages)]
        if stage in ("final", "provisional"):
            fields += ["finalized_at=?", "provisional=?"]
            args += [_now(), 1 if stage == "provisional" else 0]
        if error is not None:
            fields.append("error=?")
            args.append(str(error)[:1000])
        conn.execute(f"UPDATE dsr_reports SET {', '.join(fields)} WHERE id=?", (*args, report_id))
        conn.commit()
    finally:
        conn.close()
    if stage in FINISHED:
        sync_metrics(report_id, db_path=db_path)
    elif stage == "failed":
        # A failed re-run never leaves its half-collected figures in the
        # history: the night's metrics go back to the version that finished.
        r = get_report_by_id(report_id, db_path=db_path)
        done = get_finished_report(r["restaurant_id"], r["business_date"], db_path=db_path) if r else None
        if done:
            sync_metrics(done["id"], db_path=db_path)


def schedule_retry(report_id, next_attempt_at, db_path=DB_PATH, count=True):
    """Set when the pipeline should look again (None: never). `count`
    counts it as a collection attempt — the backoff reads `attempts` — and
    is False for the close-day poll and the late-data checks, which are
    waiting, not retrying."""
    conn = get_conn(db_path)
    try:
        conn.execute(f"UPDATE dsr_reports SET attempts=attempts+{1 if count else 0}, next_attempt_at=? WHERE id=?",
                     (str(next_attempt_at)[:19] if next_attempt_at else None, report_id))
        conn.commit()
    finally:
        conn.close()


# stages_json keys that are progress notes rather than stages: when each
# block was collected, how the day was known closed, the narrative's outcome,
# failure and crash counts, the version this one completes.
NOTE_KEYS = ("blocks", "closed_by", "narrative", "failures", "crashes", "supersedes", "rerun")


def note(report_id, key, value, db_path=DB_PATH):
    """Record a progress note beside the stage times (stages_json[key])."""
    if key not in NOTE_KEYS:
        raise ValueError(f"unknown note {key!r}")
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT stages_json FROM dsr_reports WHERE id=?", (report_id,)).fetchone()
        if r is None:
            raise KeyError(f"no dsr report {report_id}")
        stages = json.loads(r["stages_json"] or "{}")
        stages[key] = value
        conn.execute("UPDATE dsr_reports SET stages_json=? WHERE id=?", (json.dumps(stages), report_id))
        conn.commit()
    finally:
        conn.close()


def _upsert_metrics(conn, restaurant_id, business_date, name, blk, report_id):
    for key, value in (blk.get("metrics") or {}).items():
        conn.execute(
            "INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source, report_id, updated_at) "
            "VALUES (?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id, business_date, metric) DO UPDATE SET "
            "value=excluded.value, status=excluded.status, source=excluded.source, report_id=excluded.report_id, "
            "updated_at=excluded.updated_at",
            (restaurant_id, business_date, f"{name}.{key}", value, blk.get("status"), blk.get("source"), report_id))


def sync_metrics(report_id, db_path=DB_PATH):
    """Make the night's dsr_metrics exactly this version's READY blocks
    (D1-4) — called when a version finishes (set_stage final/provisional),
    so a block the new version could not measure takes its old figures out
    of the history rather than leaving the previous version's beside it."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT restaurant_id, business_date, facts_json FROM dsr_reports WHERE id=?",
                         (report_id,)).fetchone()
        if r is None:
            return
        blocks = (json.loads(r["facts_json"] or "null") or {}).get("blocks") or {}
        conn.execute("DELETE FROM dsr_metrics WHERE restaurant_id=? AND business_date=?",
                     (r["restaurant_id"], r["business_date"]))
        for name in _dsr.BLOCKS:
            blk = blocks.get(name)
            if isinstance(blk, dict) and blk.get("status") == _dsr.READY:
                _upsert_metrics(conn, r["restaurant_id"], r["business_date"], name, blk, report_id)
        conn.commit()
    finally:
        conn.close()


def save_block(report_id, name, blk, db_path=DB_PATH, stamped_at=None):
    """Store one block on the report, stamp when it was collected
    (stages_json["blocks"][name] — the progressive checklist's times) and
    refresh its metrics in dsr_metrics. `stamped_at` carries a block's
    original collection time into a later version that reuses it.

    Only a READY block writes history, and it replaces the block's metrics
    whole (D1-4): a key the new version no longer carries — "Unmapped" after
    the owner mapped the department — is removed, never left beside the new
    figures to add up to more than net. A block still awaiting (or
    unavailable) leaves the night's history as it was; when the version
    finishes, sync_metrics makes the history exactly that version."""
    if name not in _dsr.BLOCKS:
        raise ValueError(f"unknown block {name!r}")
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT restaurant_id, business_date, facts_json, stages_json FROM dsr_reports WHERE id=?",
                         (report_id,)).fetchone()
        if r is None:
            raise KeyError(f"no dsr report {report_id}")
        facts = json.loads(r["facts_json"] or "null") or {
            "schema": _dsr.SCHEMA_VERSION, "restaurant_id": r["restaurant_id"],
            "business_date": r["business_date"], "blocks": {}}
        facts.setdefault("blocks", {})[name] = blk
        facts["missing"] = _dsr.missing_reasons(facts["blocks"])
        stages = json.loads(r["stages_json"] or "{}")
        stages.setdefault("blocks", {})[name] = str(stamped_at)[:19] if stamped_at else _now()
        conn.execute("UPDATE dsr_reports SET facts_json=?, stages_json=? WHERE id=?",
                     (json.dumps(facts), json.dumps(stages), report_id))
        if blk.get("status") == _dsr.READY:
            keys = [f"{name}.{k}" for k in (blk.get("metrics") or {})]
            keep = f" AND metric NOT IN ({','.join('?' for _ in keys)})" if keys else ""
            conn.execute("DELETE FROM dsr_metrics WHERE restaurant_id=? AND business_date=? AND metric LIKE ?" + keep,
                         (r["restaurant_id"], r["business_date"], f"{name}.%", *keys))
            _upsert_metrics(conn, r["restaurant_id"], r["business_date"], name, blk, report_id)
        conn.commit()
    finally:
        conn.close()


def save_fiscal(report_id, fiscal, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT restaurant_id, business_date, facts_json FROM dsr_reports WHERE id=?",
                         (report_id,)).fetchone()
        facts = json.loads(r["facts_json"] or "null") or {
            "schema": _dsr.SCHEMA_VERSION, "restaurant_id": r["restaurant_id"],
            "business_date": r["business_date"], "blocks": {}}
        facts["fiscal"] = fiscal
        conn.execute("UPDATE dsr_reports SET facts_json=? WHERE id=?", (json.dumps(facts), report_id))
        conn.commit()
    finally:
        conn.close()


def save_section(report_id, key, value, db_path=DB_PATH):
    """One top-level section of a night's facts that is not a block — today
    `tomorrow` (dsr.tomorrow: the next night's prep, forecast and confidence,
    as it stood when the report was built, so the email, the app and a later
    read all show the same thing)."""
    if key in ("blocks", "missing", "withheld", "schema", "restaurant_id", "business_date"):
        raise ValueError(f"{key!r} is not a free section")
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT restaurant_id, business_date, facts_json FROM dsr_reports WHERE id=?",
                         (report_id,)).fetchone()
        facts = json.loads(r["facts_json"] or "null") or {
            "schema": _dsr.SCHEMA_VERSION, "restaurant_id": r["restaurant_id"],
            "business_date": r["business_date"], "blocks": {}}
        facts[key] = value
        conn.execute("UPDATE dsr_reports SET facts_json=? WHERE id=?", (json.dumps(facts), report_id))
        conn.commit()
    finally:
        conn.close()


def save_narrative(report_id, narrative, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE dsr_reports SET narrative_json=? WHERE id=?",
                     (json.dumps(narrative) if narrative is not None else None, report_id))
        conn.commit()
    finally:
        conn.close()


def versions(restaurant_id, business_date, db_path=DB_PATH):
    """Every version of one night, oldest first — what "Updated 7:10am:
    sales now final" is read from."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT version, status, trigger, provisional, created_at, finalized_at FROM dsr_reports "
                            "WHERE restaurant_id=? AND business_date=? ORDER BY version",
                            (restaurant_id, str(business_date)[:10])).fetchall()
    finally:
        conn.close()
    return [{**dict(r), "provisional": bool(r["provisional"])} for r in rows]


def list_reports(restaurant_id, limit=30, before=None, db_path=DB_PATH):
    """The latest version of each night, newest first."""
    conn = get_conn(db_path)
    try:
        args = [restaurant_id]
        where = "restaurant_id=?"
        if before:
            where += " AND business_date < ?"
            args.append(str(before)[:10])
        rows = conn.execute(
            f"SELECT * FROM dsr_reports d WHERE {where} AND version = (SELECT MAX(version) FROM dsr_reports x "
            f"WHERE x.restaurant_id=d.restaurant_id AND x.business_date=d.business_date) "
            f"ORDER BY business_date DESC LIMIT ?", (*args, int(limit))).fetchall()
    finally:
        conn.close()
    return [_row(r) for r in rows]


# ── the searchable history ──────────────────────────────────────────────────

def metric_series(restaurant_id, metric, start, end, db_path=DB_PATH):
    """[(business_date, value)] for one metric over a range; unmeasured
    nights are absent, never zero."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT business_date, value FROM dsr_metrics WHERE restaurant_id=? AND metric=? "
                            "AND business_date BETWEEN ? AND ? AND value IS NOT NULL ORDER BY business_date",
                            (restaurant_id, metric, str(start)[:10], str(end)[:10])).fetchall()
    finally:
        conn.close()
    return [(r["business_date"], r["value"]) for r in rows]


def metric_names(restaurant_id, db_path=DB_PATH):
    """Every metric this restaurant's reports have recorded, sorted — the
    vocabulary find_days accepts."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT metric FROM dsr_metrics WHERE restaurant_id=? ORDER BY metric",
                            (restaurant_id,)).fetchall()
    finally:
        conn.close()
    return [r["metric"] for r in rows]


_OPS = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "=": "="}


def find_days(restaurant_id, metric, op, value, start=None, end=None, db_path=DB_PATH):
    """Nights where `metric op value` ("labor.pct", ">", 25). Only measured
    nights can match."""
    if op not in _OPS:
        raise ValueError(f"op must be one of {sorted(_OPS)}")
    sql = (f"SELECT business_date, value FROM dsr_metrics WHERE restaurant_id=? AND metric=? "
           f"AND value IS NOT NULL AND value {_OPS[op]} ?")
    args = [restaurant_id, metric, float(value)]
    if start:
        sql += " AND business_date >= ?"
        args.append(str(start)[:10])
    if end:
        sql += " AND business_date <= ?"
        args.append(str(end)[:10])
    conn = get_conn(db_path)
    try:
        rows = conn.execute(sql + " ORDER BY business_date DESC", args).fetchall()
    finally:
        conn.close()
    return [(r["business_date"], r["value"]) for r in rows]


# ── budgets and the category map ───────────────────────────────────────────

def set_budget(restaurant_id, business_date, gross=None, net=None, updated_by=None, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO dsr_budgets (restaurant_id, business_date, gross, net, updated_by, updated_at) "
                     "VALUES (?,?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id, business_date) DO UPDATE SET "
                     "gross=excluded.gross, net=excluded.net, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                     (restaurant_id, str(business_date)[:10],
                      float(gross) if gross is not None else None,
                      float(net) if net is not None else None, updated_by))
        conn.commit()
    finally:
        conn.close()


def budgets_for(restaurant_id, start, end, db_path=DB_PATH):
    """{date: {"gross", "net"}} for the range; a night with no budget is absent."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT business_date, gross, net FROM dsr_budgets WHERE restaurant_id=? "
                            "AND business_date BETWEEN ? AND ?", (restaurant_id, str(start)[:10], str(end)[:10])).fetchall()
    finally:
        conn.close()
    return {r["business_date"]: {"gross": r["gross"], "net": r["net"]} for r in rows}


PREFILL_SOURCES = ("last_week", "last_year", "forecast")
LAST_YEAR_DAYS = 364     # the same weekday a year back (dsr.block_sales)


def _measured(restaurant_id, day, db_path):
    """{"gross", "net"} measured for one night: the report's sales metrics,
    else an imported workbook row. A figure not measured is None, never 0."""
    from datetime import date as _date
    iso = day.isoformat() if isinstance(day, _date) else str(day)[:10]
    got = {}
    for key in ("gross", "net"):
        s = metric_series(restaurant_id, f"sales.{key}", iso, iso, db_path=db_path)
        got[key] = round(float(s[0][1]), 2) if s else None
    if got["gross"] is None and got["net"] is None:
        h = history_for(restaurant_id, iso, iso, db_path=db_path).get(iso) or {}
        got = {"gross": h.get("gross"), "net": h.get("net")}
    return got


def budget_prefill(restaurant_id, week_dates, source, pct=0.0, db_path=DB_PATH) -> dict:
    """Suggested budget figures for a week the owner then edits and saves
    (friction audit U2-11: fourteen blank boxes every week). Nothing is
    written here. `source`:

      last_week  each night's budget from seven days before, else what that
                 night actually took;
      last_year  what the same weekday took 52 weeks back (report or
                 imported workbook), raised by `pct` percent;
      forecast   Cavnar's sales forecast for the night (demand.forecast_day)
                 as the net figure - it forecasts one sales figure, so gross
                 is left for the owner.

    Returns {"days": [{"date", "gross", "net", "from"}], "missing": [dates
    with nothing to suggest], "basis": one line saying what was used}. A
    night with no source is left blank and named, never filled with 0."""
    from datetime import date as _date, timedelta
    if source not in PREFILL_SOURCES:
        raise ValueError(f"source must be one of {PREFILL_SOURCES}")
    dates = [d if isinstance(d, _date) else _date.fromisoformat(str(d)[:10]) for d in week_dates]
    days, missing = [], []
    factor = 1.0 + float(pct or 0) / 100.0
    fc = None
    if source == "forecast":
        import demand
        fc = demand.week_projection(restaurant_id, dates, db_path=db_path).get("by_day") or {}
    for d in dates:
        row = {"date": d.isoformat(), "gross": None, "net": None, "from": None}
        if source == "last_week":
            prev = d - timedelta(days=7)
            b = budgets_for(restaurant_id, prev, prev, db_path=db_path).get(prev.isoformat())
            if b and (b.get("gross") is not None or b.get("net") is not None):
                row.update(gross=b.get("gross"), net=b.get("net"), **{"from": "last week's budget"})
            else:
                m = _measured(restaurant_id, prev, db_path)
                if m["gross"] is not None or m["net"] is not None:
                    row.update(gross=m["gross"], net=m["net"], **{"from": "last week's sales"})
        elif source == "last_year":
            m = _measured(restaurant_id, d - timedelta(days=LAST_YEAR_DAYS), db_path)
            if m["gross"] is not None or m["net"] is not None:
                row.update(gross=round(m["gross"] * factor, 2) if m["gross"] is not None else None,
                           net=round(m["net"] * factor, 2) if m["net"] is not None else None,
                           **{"from": "last year" + (f" {pct:+g}%" if pct else "")})
        else:
            v = fc.get(d.isoformat())
            if v is not None:
                row.update(net=round(float(v), 2), **{"from": "Cavnar AI's forecast"})
        if row["from"] is None:
            missing.append(d.isoformat())
        else:
            # A night this source fills is filled from it alone: a field it
            # has no figure for is named to be cleared, or last week's gross
            # stayed beside the forecast's net, gross under net (F2-19).
            row["clear"] = [k for k in ("gross", "net") if row[k] is None]
        days.append(row)
    blank = sorted({k for r in days for k in r.get("clear") or []})
    basis = {"last_week": "Last week's budget, or what the night took where there was none.",
             "last_year": "What the same weekday took a year ago" + (f", {pct:+g}%." if pct else "."),
             "forecast": "Cavnar AI's sales forecast for each night, as net."}[source]
    if blank:
        basis += " " + " and ".join(k.capitalize() if i == 0 else k for i, k in enumerate(blank)) + \
                 " left blank for you to fill."
    return {"days": days, "missing": missing, "basis": basis}


def set_category(restaurant_id, pos_name, category, db_path=DB_PATH):
    name = str(pos_name or "").strip()
    cat = str(category or "").strip()
    if not name or not cat:
        raise ValueError("both a POS name and a category are required")
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO dsr_category_map (restaurant_id, pos_name, category, updated_at) VALUES (?,?,?,datetime('now')) "
                     "ON CONFLICT(restaurant_id, pos_name) DO UPDATE SET category=excluded.category, updated_at=excluded.updated_at",
                     (restaurant_id, name[:120], cat[:60]))
        conn.commit()
    finally:
        conn.close()


def category_map(restaurant_id, db_path=DB_PATH):
    """{pos_name_lower: category}."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT pos_name, category FROM dsr_category_map WHERE restaurant_id=?",
                            (restaurant_id,)).fetchall()
    finally:
        conn.close()
    return {r["pos_name"].strip().lower(): r["category"] for r in rows}


def category_for(pos_name, mapping):
    """The DSR category a POS department/category name maps to — only what
    the owner mapped; anything else is UNMAPPED, never guessed."""
    return (mapping or {}).get(str(pos_name or "").strip().lower(), _dsr.UNMAPPED)


# ── last year, imported ────────────────────────────────────────────────────

def import_history(restaurant_id, rows, source_file=None, imported_by=None, db_path=DB_PATH):
    """Upsert [{"date", "gross", "net", "categories": {cat: value}}]; returns
    how many rows were written. A row with neither gross nor net is skipped —
    an empty cell in a workbook is not a $0 night."""
    n = 0
    conn = get_conn(db_path)
    try:
        for r in rows or []:
            day = str(r.get("date") or "")[:10]
            gross, net = r.get("gross"), r.get("net")
            if len(day) != 10 or (gross is None and net is None):
                continue
            cats = {str(k): float(v) for k, v in (r.get("categories") or {}).items() if v is not None}
            conn.execute("INSERT INTO dsr_history_import (restaurant_id, business_date, gross, net, categories_json, "
                         "source_file, imported_by, imported_at) VALUES (?,?,?,?,?,?,?,datetime('now')) "
                         "ON CONFLICT(restaurant_id, business_date) DO UPDATE SET gross=excluded.gross, net=excluded.net, "
                         "categories_json=excluded.categories_json, source_file=excluded.source_file, "
                         "imported_by=excluded.imported_by, imported_at=excluded.imported_at",
                         (restaurant_id, day, float(gross) if gross is not None else None,
                          float(net) if net is not None else None, json.dumps(cats) if cats else None,
                          (source_file or "")[:200] or None, imported_by))
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def history_for(restaurant_id, start, end, db_path=DB_PATH):
    """{date: {"gross", "net", "categories"}} imported for the range."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT business_date, gross, net, categories_json FROM dsr_history_import "
                            "WHERE restaurant_id=? AND business_date BETWEEN ? AND ?",
                            (restaurant_id, str(start)[:10], str(end)[:10])).fetchall()
    finally:
        conn.close()
    return {r["business_date"]: {"gross": r["gross"], "net": r["net"],
                                 "categories": json.loads(r["categories_json"]) if r["categories_json"] else {}}
            for r in rows}


# ── another night's net: the one resolver (D1-10, D1-13) ───────────────────

# The POS whose daily total in the nightly sync's archive
# (labor_daily_history.sales) is built exactly as the DSR's own net
# (pos.fetch_day_sales' docstring states it for RPOWER). Any other POS's
# daily total (Toast's businessDay netSales) is a different figure until a
# live night proves otherwise, so it is never set beside tonight's net as
# "last week" — a gap in how the two are built would read as a change in
# sales.
POS_SYNC_SAME_BASIS = ("rpower",)


def pos_sync_same_basis(provider) -> bool:
    return str(provider or "").lower() in POS_SYNC_SAME_BASIS


def baselines_net(restaurant_id, days, db_path=DB_PATH, pos_sync=True) -> dict:
    """{day_iso: (net, source)} for other nights, in the ONE order every
    surface reads them — the nightly report's yesterday / last week / last
    year and the weekly grid's Last Year alike (D1-10):

      dsr        that night's own report (dsr_metrics sales.net)
      import     the owner's old DSR workbook (dsr_history_import)
      pos_sync   the nightly POS sync's daily sales, a positive figure only
                 (0 there means nothing synced), and only when `pos_sync`:
                 the POS's daily total is the DSR's own basis (D1-13)

    (None, None) for a night none of them has."""
    isos = sorted({(d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]) for d in days if d})
    out = {d: (None, None) for d in isos}
    if not isos:
        return out
    marks = ",".join("?" for _ in isos)
    conn = get_conn(db_path)
    try:
        measured = {r["business_date"]: float(r["value"]) for r in conn.execute(
            f"SELECT business_date, value FROM dsr_metrics WHERE restaurant_id=? AND metric='sales.net' "
            f"AND value IS NOT NULL AND business_date IN ({marks})", (restaurant_id, *isos)).fetchall()}
        synced = {}
        if pos_sync:
            try:
                synced = {r["date"]: float(r["sales"]) for r in conn.execute(
                    f"SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND date IN ({marks}) "
                    "AND sales IS NOT NULL AND sales > 0", (restaurant_id, *isos)).fetchall()}
            except Exception:
                synced = {}
    finally:
        conn.close()
    imported = history_for(restaurant_id, isos[0], isos[-1], db_path=db_path)
    for d in isos:
        if d in measured:
            out[d] = (measured[d], "dsr")
        elif (imported.get(d) or {}).get("net") is not None:
            out[d] = (float(imported[d]["net"]), "import")
        elif d in synced:
            out[d] = (synced[d], "pos_sync")
    return out


def baseline_net(restaurant_id, day, db_path=DB_PATH, pos_sync=True):
    """(net, source) for one other night — baselines_net for one day."""
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]
    return baselines_net(restaurant_id, [iso], db_path=db_path, pos_sync=pos_sync)[iso]


def last_year_day(restaurant, day):
    """The night a year back that `day` is compared with (D1-16): the same
    fiscal week and weekday of the previous fiscal year where the restaurant
    keeps a fiscal calendar (so the year after a 53-week year lines up with
    Back Office), else the same weekday 364 days back."""
    from dsr import fiscal
    return fiscal.same_day_last_year(restaurant, day)


# ── the clock-in check's own record (D1-17) ────────────────────────────────

def mark_coverage_ran(restaurant_id, business_date, db_path=DB_PATH):
    """strategy_jobs.run_coverage_check read the POS's clock-ins for this
    restaurant during `business_date`'s service. Never raises."""
    try:
        conn = get_conn(db_path)
    except Exception:
        return
    try:
        conn.execute("INSERT INTO dsr_coverage_runs (restaurant_id, business_date) VALUES (?,?) "
                     "ON CONFLICT(restaurant_id, business_date) DO UPDATE SET last_at=datetime('now')",
                     (restaurant_id, str(business_date)[:10]))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def coverage_ran(restaurant_id, business_date, db_path=DB_PATH) -> bool:
    """Whether the clock-in check ran during that night's service."""
    conn = get_conn(db_path)
    try:
        return bool(conn.execute("SELECT 1 FROM dsr_coverage_runs WHERE restaurant_id=? AND business_date=?",
                                 (restaurant_id, str(business_date)[:10])).fetchone())
    except Exception:
        return False
    finally:
        conn.close()
