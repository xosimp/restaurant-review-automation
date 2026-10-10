"""
scripts/pg_sql_audit.py — every SQL statement in the app that db_dialect
would not carry to Postgres as written (scheduler audit 10/10/26 RM-13;
docs/ops/POSTGRES_CUTOVER.md step 1). The remaining port is this list.

    python scripts/pg_sql_audit.py            # summary + every finding
    python scripts/pg_sql_audit.py --json     # machine-readable

It reads each `.execute(...)` / `.executemany(...)` call's SQL from the
source (string literals, f-strings with their holes as X, `+` joins),
translates it (db_dialect.Translator, with every table's primary key read
from a freshly migrated SQLite schema) and reports:
  untranslatable   — db_dialect raised (a strftime format, a date modifier,
                     a json path that is not a literal …)
  double_quoted    — a "string" used as a value: an identifier in Postgres
  sqlite_only      — typeof(), printf(), GLOB, last_insert_rowid(),
                     ATTACH / DETACH, VACUUM, CREATE TRIGGER, INSERT OR
                     ROLLBACK/ABORT/FAIL, a PRAGMA db_dialect does not answer
  reserved_columns — a column whose name Postgres reserves (user, order …)
"""
import ast
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

PG_RESERVED = {"all", "analyse", "analyze", "and", "any", "array", "as", "asc", "asymmetric", "both", "case",
               "cast", "check", "collate", "column", "constraint", "create", "current_catalog", "current_date",
               "current_role", "current_time", "current_timestamp", "current_user", "default", "deferrable",
               "desc", "distinct", "do", "else", "end", "except", "false", "fetch", "for", "foreign", "from",
               "grant", "group", "having", "in", "initially", "intersect", "into", "lateral", "leading", "limit",
               "localtime", "localtimestamp", "not", "null", "offset", "on", "only", "or", "order", "placing",
               "primary", "references", "returning", "select", "session_user", "some", "symmetric",
               "system_user", "table", "then", "to", "trailing", "true", "union", "unique", "user", "using",
               "variadic", "when", "where", "window", "with"}
_SQL_START = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|REPLACE|WITH|ALTER|DROP|PRAGMA|BEGIN)\b", re.I)
_SQLITE_ONLY = [(re.compile(p, re.I), what) for p, what in (
    (r"\btypeof\s*\(", "typeof()"), (r"\bprintf\s*\(", "printf()"),
    (r"\blast_insert_rowid\s*\(", "last_insert_rowid()"), (r"^\s*(ATTACH|DETACH)\b", "ATTACH/DETACH"),
    (r"^\s*VACUUM\b", "VACUUM"), (r"\bCREATE\s+TRIGGER\b", "CREATE TRIGGER"),
    (r"\bINSERT\s+OR\s+(ROLLBACK|ABORT|FAIL)\b", "INSERT OR ROLLBACK/ABORT/FAIL"))]
_PRAGMA_OK = re.compile(r"^\s*PRAGMA\s+(table_info|index_list|integrity_check|quick_check|journal_mode|"
                        r"foreign_keys|busy_timeout|synchronous|foreign_key_check|optimize|wal_checkpoint|"
                        r"cache_size|temp_store|database_list|schema_version|analysis_limit)\b", re.I)
# Statements db_dialect.PgConnection deliberately turns into no-ops because the
# copy installs their Postgres form (migrations/postgres/0002_triggers.sql).
_PORTED = re.compile(r"^\s*(CREATE|DROP)\s+TRIGGER\b", re.I)


def _text(node):
    """The SQL text an expression builds, holes as X; None if not a string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "X" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _text(node.left), _text(node.right)
        if a is None and b is None:
            return None
        return (a or "X") + (b or "X")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _text(node.left)
    return None


def statements(root=ROOT):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "tests", ".venv", "node_modules", "ios", ".claude",
                                                "__pycache__", "scratch_scale")]
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dirpath, f)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr in ("execute", "executemany", "executescript") and node.args:
                    sql = _text(node.args[0])
                    if sql and _SQL_START.match(sql):
                        yield os.path.relpath(path, root), node.lineno, sql


def _schema():
    import tempfile
    import sqlite3
    import models
    p = os.path.join(tempfile.mkdtemp(), "audit.db")
    models.init_db(p)
    models.ensure_columns(p)
    # The rest of the boot's tables (hosted_dashboard's init chain), so every
    # table's primary key is known.
    import importlib
    import inspect
    for mod, fn in (("models", "init_email_log"), ("models", "init_onboarding_emails"),
                    ("models", "init_staff_notes"), ("models", "init_staff_availability"),
                    ("models", "init_two_fa_backup_codes"), ("models", "init_competitor_snapshots"),
                    ("models", "init_ai_visibility_queries"), ("models", "init_staff_capabilities"),
                    ("models", "init_shift_profiles"), ("models", "init_capability_changes"),
                    ("models", "init_ask_memory"), ("auth", "init_auth"), ("webhooks", "init_webhooks"),
                    ("guest_marketing", "init_guest_marketing"), ("push", "init_push"),
                    ("sales_audits", "init_sales_audits"), ("platform_monitor", "init_platform_tables"),
                    ("provider_health", "init_provider_health")):
        try:
            f = getattr(importlib.import_module(mod), fn)
            if "db_path" in inspect.signature(f).parameters:
                f(db_path=p)
        except Exception:
            pass
    conn = sqlite3.connect(p)
    pks, cols = {}, {}
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        info = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
        pks[t.lower()] = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
        cols[t.lower()] = [r[1] for r in info]
    conn.close()
    return pks, cols


def audit():
    import contextlib
    import io
    import db_dialect
    with contextlib.redirect_stdout(io.StringIO()):
        pks, cols = _schema()
    tr = db_dialect.Translator(lambda t: pks.get((t or "").lower()) or None,
                               lambda t: cols.get((t or "").lower()) or None)
    out = {"untranslatable": [], "dynamic": [], "ported": [], "double_quoted": [], "sqlite_only": [],
           "reserved_columns": [], "statements": 0}
    for path, line, sql in statements():
        out["statements"] += 1
        where = f"{path}:{line}"
        if _PORTED.match(sql):
            out["ported"].append({"at": where, "what": "TRIGGER (plpgsql in 0002_triggers.sql)"})
            continue
        for rx, what in _SQLITE_ONLY:
            if rx.search(sql):
                out["sqlite_only"].append({"at": where, "what": what})
        if sql.lstrip().upper().startswith("PRAGMA") and not _PRAGMA_OK.match(sql):
            out["sqlite_only"].append({"at": where, "what": "PRAGMA " + sql.split()[1][:30]})
        if re.search(r"(=|<>|!=|\bIN\s*\(|,)\s*\"[^\"]*\"", sql) and not re.search(r"\"(trigger|effort)\"", sql):
            out["double_quoted"].append({"at": where, "sql": sql[:120]})
        try:
            tr.translate(sql.replace("X", "hole"))
        except Exception as e:
            # A hole the f-string fills at run time (a table name, an
            # interval's number): translated then, not decidable here.
            key = "dynamic" if "hole" in str(e).lower() else "untranslatable"
            out[key].append({"at": where, "error": str(e)[:120]})
    for t, cs in sorted(cols.items()):
        for c in cs:
            if c.lower() in PG_RESERVED:
                out["reserved_columns"].append(f"{t}.{c}")
    return out


def main(argv=None):
    rep = audit()
    if argv and "--json" in argv:
        print(json.dumps(rep, indent=2))
        return 0
    print(f"{rep['statements']} SQL statements read")
    for k in ("untranslatable", "dynamic", "ported", "double_quoted", "sqlite_only", "reserved_columns"):
        print(f"\n{k}: {len(rep[k])}")
        for item in rep[k]:
            print("  ", item if isinstance(item, str) else " — ".join(str(v) for v in item.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
