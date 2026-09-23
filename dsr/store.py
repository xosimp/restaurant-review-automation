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


def schedule_retry(report_id, next_attempt_at, db_path=DB_PATH):
    """Count an attempt and set when the pipeline should look again."""
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE dsr_reports SET attempts=attempts+1, next_attempt_at=? WHERE id=?",
                     (str(next_attempt_at)[:19] if next_attempt_at else None, report_id))
        conn.commit()
    finally:
        conn.close()


def save_block(report_id, name, blk, db_path=DB_PATH):
    """Store one block on the report and refresh its metrics in dsr_metrics."""
    if name not in _dsr.BLOCKS:
        raise ValueError(f"unknown block {name!r}")
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT restaurant_id, business_date, facts_json FROM dsr_reports WHERE id=?",
                         (report_id,)).fetchone()
        if r is None:
            raise KeyError(f"no dsr report {report_id}")
        facts = json.loads(r["facts_json"] or "null") or {
            "schema": _dsr.SCHEMA_VERSION, "restaurant_id": r["restaurant_id"],
            "business_date": r["business_date"], "blocks": {}}
        facts.setdefault("blocks", {})[name] = blk
        facts["missing"] = _dsr.missing_reasons(facts["blocks"])
        conn.execute("UPDATE dsr_reports SET facts_json=? WHERE id=?", (json.dumps(facts), report_id))
        for key, value in (blk.get("metrics") or {}).items():
            conn.execute(
                "INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source, report_id, updated_at) "
                "VALUES (?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id, business_date, metric) DO UPDATE SET "
                "value=excluded.value, status=excluded.status, source=excluded.source, report_id=excluded.report_id, "
                "updated_at=excluded.updated_at",
                (r["restaurant_id"], r["business_date"], f"{name}.{key}", value, blk.get("status"),
                 blk.get("source"), report_id))
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


def save_narrative(report_id, narrative, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE dsr_reports SET narrative_json=? WHERE id=?",
                     (json.dumps(narrative) if narrative is not None else None, report_id))
        conn.commit()
    finally:
        conn.close()


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
