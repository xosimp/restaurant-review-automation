"""sales_audits.py — storage for the in-person Cavnar AI sales audit.

One row per restaurant audit. Answers, notes, sales observations and the
last generated results all live as JSON on the row: the shape is owned by
sales_audit_schema.py / sales_audit_engine.py, and a column per question
would mean a migration every time a question changes.

Autosave uses an optimistic version counter. The browser sends the version
it last saw; if the row has moved on (another tab, a phone), the save is
refused with the newer copy so nothing is silently overwritten.

Internal notes and sales observations never leave the admin side: the
customer-facing report reads from `public_view()`, which strips them.
"""
import json
import secrets
from datetime import datetime, date, timezone

from models import get_conn, DB_PATH
from sales_audit_schema import STATUSES

_SQL = """
CREATE TABLE IF NOT EXISTS sales_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_name TEXT NOT NULL DEFAULT '',
    owner_name TEXT NOT NULL DEFAULT '',
    restaurant_type TEXT DEFAULT '',
    service_model TEXT DEFAULT '',
    locations INTEGER DEFAULT 1,
    city TEXT DEFAULT '',
    state TEXT DEFAULT '',
    audit_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Draft',
    answers_json TEXT NOT NULL DEFAULT '{}',
    notes_json TEXT NOT NULL DEFAULT '{}',
    sales_json TEXT NOT NULL DEFAULT '{}',
    results_json TEXT,
    pricing_override TEXT,
    report_generated_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER,
    linked_restaurant_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    archived_at TEXT
);
CREATE TABLE IF NOT EXISTS sales_audit_shares (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id INTEGER NOT NULL REFERENCES sales_audits(id) ON DELETE CASCADE,
    token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at TEXT,
    views INTEGER NOT NULL DEFAULT 0,
    last_viewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sales_audit_shares_token ON sales_audit_shares(token);
"""

_HEADER_KEYS = ("restaurant_name", "owner_name", "restaurant_type", "service_model", "locations", "city", "state")


def init_sales_audits(db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.executescript(_SQL)
        conn.commit()
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _loads(s, default):
    try:
        v = json.loads(s) if s else default
        return v if isinstance(v, type(default)) else default
    except (TypeError, ValueError):
        return default


def _row_to_dict(r, full=True):
    d = dict(r)
    d["answers"] = _loads(d.pop("answers_json", None), {})
    d["notes"] = _loads(d.pop("notes_json", None), {})
    d["sales"] = _loads(d.pop("sales_json", None), {})
    d["results"] = _loads(d.pop("results_json", None), {}) or None
    if not full:
        d.pop("answers", None); d.pop("notes", None); d.pop("sales", None); d.pop("results", None)
    return d


def _header_from_answers(answers, current):
    """The list page needs name/owner/type without parsing every answer
    blob, so the profile answers are mirrored onto columns on every save."""
    out = {}
    for k in _HEADER_KEYS:
        v = answers.get(k)
        if k == "locations":
            try:
                v = int(float(str(v).replace(",", ""))) if v not in (None, "") else None
            except (TypeError, ValueError):
                v = None
            out[k] = v if v is not None else (current.get(k) if current else 1)
        else:
            out[k] = (v if isinstance(v, str) else "") if v is not None else (current.get(k) if current else "")
    return out


def create_audit(answers=None, audit_date=None, created_by=None, status="Draft", db_path=DB_PATH):
    answers = dict(answers or {})
    hdr = _header_from_answers(answers, None)
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO sales_audits (restaurant_name, owner_name, restaurant_type, service_model, locations, city, state, "
            "audit_date, status, answers_json, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (hdr["restaurant_name"], hdr["owner_name"], hdr["restaurant_type"], hdr["service_model"], hdr["locations"] or 1,
             hdr["city"], hdr["state"], audit_date or date.today().isoformat(), status if status in STATUSES else "Draft",
             json.dumps(answers), created_by, _now(), _now()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_audit(audit_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM sales_audits WHERE id=?", (audit_id,)).fetchone()
        return _row_to_dict(r) if r else None
    finally:
        conn.close()


def list_audits(q=None, status=None, include_archived=False, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        sql = "SELECT id, restaurant_name, owner_name, restaurant_type, service_model, locations, city, state, audit_date, status, " \
              "report_generated_at, version, created_at, updated_at, archived_at, results_json FROM sales_audits WHERE 1=1"
        args = []
        if not include_archived:
            sql += " AND archived_at IS NULL AND status != 'Archived'"
        if status:
            sql += " AND status=?"
            args.append(status)
        if q:
            like = "%" + q.strip().lower() + "%"
            sql += " AND (lower(restaurant_name) LIKE ? OR lower(owner_name) LIKE ? OR lower(city) LIKE ? OR audit_date LIKE ? OR lower(status) LIKE ?)"
            args += [like, like, like, like, like]
        sql += " ORDER BY updated_at DESC"
        rows = conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            res = _loads(d.pop("results_json", None), {})
            d["opportunity"] = (res.get("totals") or {}).get("annual") if res else None
            d["health"] = (res.get("health") or {}).get("score") if res else None
            d["completion"] = (res.get("completion") or {}).get("pct") if res else None
            out.append(d)
        return out
    finally:
        conn.close()


class VersionConflict(Exception):
    def __init__(self, current):
        super().__init__("version conflict")
        self.current = current


def save_audit(audit_id, answers=None, notes=None, sales=None, status=None, audit_date=None,
               pricing_override=None, expected_version=None, db_path=DB_PATH):
    """Autosave. Replaces whichever blobs are given (the client always sends
    the whole blob it owns), bumps the version, mirrors header columns.
    Raises VersionConflict with the current row if expected_version is stale."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM sales_audits WHERE id=?", (audit_id,)).fetchone()
        if not r:
            return None
        cur = _row_to_dict(r)
        if expected_version is not None and int(expected_version) != int(cur["version"]):
            raise VersionConflict(cur)
        fields, args = [], []
        if answers is not None:
            fields.append("answers_json=?"); args.append(json.dumps(answers))
            hdr = _header_from_answers(answers, cur)
            for k in _HEADER_KEYS:
                fields.append("%s=?" % k); args.append(hdr[k])
        if notes is not None:
            fields.append("notes_json=?"); args.append(json.dumps(notes))
        if sales is not None:
            fields.append("sales_json=?"); args.append(json.dumps(sales))
        if status is not None and status in STATUSES:
            fields.append("status=?"); args.append(status)
            if status == "Archived":
                fields.append("archived_at=?"); args.append(_now())
            else:
                fields.append("archived_at=NULL")
        elif status is None and cur["status"] == "Draft" and answers is not None and len(answers) > 3:
            fields.append("status=?"); args.append("In Progress")
        if audit_date:
            fields.append("audit_date=?"); args.append(audit_date)
        if pricing_override is not None:
            fields.append("pricing_override=?"); args.append(pricing_override or None)
        fields.append("version=version+1")
        fields.append("updated_at=?"); args.append(_now())
        args.append(audit_id)
        conn.execute("UPDATE sales_audits SET %s WHERE id=?" % ", ".join(fields), args)
        conn.commit()
        r = conn.execute("SELECT * FROM sales_audits WHERE id=?", (audit_id,)).fetchone()
        return _row_to_dict(r)
    finally:
        conn.close()


def store_results(audit_id, results, mark_generated=False, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        if mark_generated:
            conn.execute("UPDATE sales_audits SET results_json=?, report_generated_at=?, updated_at=? WHERE id=?",
                         (json.dumps(results), _now(), _now(), audit_id))
        else:
            conn.execute("UPDATE sales_audits SET results_json=? WHERE id=?", (json.dumps(results), audit_id))
        conn.commit()
    finally:
        conn.close()


def duplicate_audit(audit_id, created_by=None, db_path=DB_PATH):
    src = get_audit(audit_id, db_path=db_path)
    if not src:
        return None
    answers = dict(src["answers"])
    answers["restaurant_name"] = (answers.get("restaurant_name") or src["restaurant_name"] or "Audit") + " (copy)"
    return create_audit(answers=answers, created_by=created_by, db_path=db_path)


def archive_audit(audit_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE sales_audits SET status='Archived', archived_at=?, updated_at=? WHERE id=?", (_now(), _now(), audit_id))
        conn.execute("UPDATE sales_audit_shares SET revoked_at=? WHERE audit_id=? AND revoked_at IS NULL", (_now(), audit_id))
        conn.commit()
    finally:
        conn.close()


def delete_audit(audit_id, db_path=DB_PATH):
    """Hard delete. Only offered from the archived state in the UI."""
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM sales_audit_shares WHERE audit_id=?", (audit_id,))
        conn.execute("DELETE FROM sales_audits WHERE id=?", (audit_id,))
        conn.commit()
    finally:
        conn.close()


# ── Share links ──────────────────────────────────────────────────────────────

def create_share(audit_id, db_path=DB_PATH):
    token = secrets.token_urlsafe(24)
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE sales_audit_shares SET revoked_at=? WHERE audit_id=? AND revoked_at IS NULL", (_now(), audit_id))
        conn.execute("INSERT INTO sales_audit_shares (audit_id, token, created_at) VALUES (?,?,?)", (audit_id, token, _now()))
        conn.commit()
        return token
    finally:
        conn.close()


def active_share(audit_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM sales_audit_shares WHERE audit_id=? AND revoked_at IS NULL ORDER BY id DESC LIMIT 1", (audit_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def revoke_shares(audit_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE sales_audit_shares SET revoked_at=? WHERE audit_id=? AND revoked_at IS NULL", (_now(), audit_id))
        conn.commit()
    finally:
        conn.close()


def resolve_share(token, db_path=DB_PATH):
    """The audit behind a live token, or None. Counts the view."""
    if not token or len(token) > 64:
        return None
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM sales_audit_shares WHERE token=? AND revoked_at IS NULL", (token,)).fetchone()
        if not r:
            return None
        conn.execute("UPDATE sales_audit_shares SET views=views+1, last_viewed_at=? WHERE id=?", (_now(), r["id"]))
        conn.commit()
        a = conn.execute("SELECT * FROM sales_audits WHERE id=? AND archived_at IS NULL", (r["audit_id"],)).fetchone()
        return _row_to_dict(a) if a else None
    finally:
        conn.close()


# ── Customer-facing view ─────────────────────────────────────────────────────

def public_view(audit):
    """Everything the customer report may see. Internal notes, sales
    observations, per-question answers and calculation debug never pass
    through here — the report template only ever receives this."""
    if not audit:
        return None
    res = audit.get("results") or {}
    notes = audit.get("notes") or {}
    audit_notes = {k: v.get("audit") for k, v in notes.items() if isinstance(v, dict) and v.get("audit") and v.get("audit_in_report")}
    cats = {}
    for k, c in (res.get("categories") or {}).items():
        calc = c.get("calc") or {}
        cats[k] = {"key": k, "label": c.get("label"), "status": c.get("status"), "low": c.get("low"), "likely": c.get("likely"),
                   "high": c.get("high"), "confidence": c.get("confidence"), "current_state": c.get("current_state"),
                   "opportunity": c.get("opportunity"),
                   "how": {"current_metric": calc.get("current_metric"), "benchmark": calc.get("benchmark"),
                           "base": calc.get("base"), "assumptions": calc.get("assumptions")} if calc else None,
                   "module": c.get("module")}
    return {
        "id": audit["id"],
        "restaurant_name": audit.get("restaurant_name"), "owner_name": audit.get("owner_name"),
        "restaurant_type": audit.get("restaurant_type"), "service_model": audit.get("service_model"),
        "locations": audit.get("locations"), "city": audit.get("city"), "state": audit.get("state"),
        "audit_date": audit.get("audit_date"), "report_generated_at": audit.get("report_generated_at"),
        "health": res.get("health"),
        "scores": {k: {"score": v.get("score"), "label": v.get("label")} for k, v in (res.get("scores") or {}).items()},
        "totals": res.get("totals"), "findings": res.get("findings"),
        "wins": res.get("wins"), "problems": res.get("problems"), "opportunities": res.get("opportunities"),
        "categories": cats, "recommended_modules": res.get("recommended_modules"), "upcoming_modules": res.get("upcoming_modules"),
        "plan": res.get("plan"), "roi": res.get("roi"), "owner_hours_week": res.get("owner_hours_week"),
        "disclaimer": res.get("disclaimer"), "audit_notes": audit_notes,
        "financials": {k: v for k, v in (res.get("financials") or {}).items() if k in ("annual_revenue", "labor_pct", "food_pct", "bev_pct", "prime_cost_pct", "alcohol_pct")},
    }


# ── First audit ──────────────────────────────────────────────────────────────

FIRST_AUDIT = {
    "restaurant_name": "Simple EJ's", "owner_name": "Erik", "locations": 1,
    "restaurant_type": "Upscale sports bar", "service_model": "Full-service",
}


def ensure_first_audit(db_path=DB_PATH):
    """Tomorrow's audit, pre-filled with only the profile facts Will already
    knows. Runs once: only when there are no audits at all, so it never
    re-creates itself after being renamed, duplicated or archived."""
    conn = get_conn(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM sales_audits").fetchone()[0]
    finally:
        conn.close()
    if n:
        return None
    return create_audit(answers=dict(FIRST_AUDIT), db_path=db_path)
