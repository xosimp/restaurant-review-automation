"""
scripts/pg_sql_audit.py — every SQL statement in the app that db_dialect
would not carry to Postgres as written (scheduler audit 10/10/26 RM-13;
docs/ops/POSTGRES_CUTOVER.md step 1). The remaining port is this list.

    python scripts/pg_sql_audit.py            # summary + every finding
    python scripts/pg_sql_audit.py --json     # machine-readable
    python scripts/pg_sql_audit.py --database-url postgresql://…/scratch
                                              # + every statement PREPAREd
                                              #   on a real Postgres

Reading the text is not running it (blind re-audit 10/11/26, SCALE-10: the
text-level "0 untranslatable" missed SUM(<boolean>), COLLATE NOCASE, a
HAVING alias, UPDATE OR IGNORE, the boot's own DDL). With --database-url
(a SCRATCH database — never production's) the app's boot builds the schema
there (models.init_db and the init chain, through db_dialect) and every
statement with no run-time hole is translated against that catalog and
PREPAREd — Postgres's parser, name resolution and type checks, without
running it. `prepare_failed` is then the list that would fail at run time.

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
    (r"\bINSERT\s+OR\s+(ROLLBACK|ABORT|FAIL)\b", "INSERT OR ROLLBACK/ABORT/FAIL"),
    (r"\bUPDATE\s+OR\s+(REPLACE|ROLLBACK|ABORT|FAIL)\b", "UPDATE OR REPLACE (db_dialect runs only OR IGNORE)"),
    (r"\bCOLLATE\s+(?!NOCASE\b)\w+", "a collation other than NOCASE"))]
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


def static_statements(root=ROOT):
    """(path, line, sql) of every statement whose SQL is fully known in the
    source — no f-string hole, no variable — the ones PREPARE can check."""
    for path, line, sql, node in _nodes(root):
        if _static(node):
            yield path, line, sql


# Modules whose SQL is Postgres's own (the dialect layer's catalog reads, the
# copy, the shadow's typed writes): never translated, so not audited as
# SQLite.
_NATIVE = {"db_dialect.py", "pg_copy.py", "pg_sql_audit.py"}


def _nodes(root=ROOT):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "tests", ".venv", "node_modules", "ios", ".claude",
                                                "__pycache__", "scratch_scale")]
        for f in files:
            if not f.endswith(".py") or f in _NATIVE:
                continue
            path = os.path.join(dirpath, f)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                if isinstance(node.func, ast.Attribute) and node.func.attr in ("execute", "executemany"):
                    cands = node.args[:1]
                else:
                    # SQL handed to a helper (admin_ops._rows_dict(conn, sql),
                    # _one_dict, …): the first two positional arguments, when
                    # they read as a whole statement.
                    cands = [a for a in node.args[:2] if _whole_sql(_text(a))]
                for arg in cands:
                    sql = _text(arg)
                    if sql and _SQL_START.match(sql):
                        yield os.path.relpath(path, root), node.lineno, sql, arg


def _whole_sql(sql):
    """A string that is a statement, not a fragment or prose."""
    if not sql or not _SQL_START.match(sql):
        return False
    up = sql.upper()
    return (up.lstrip().startswith("SELECT") and " FROM " in up) or \
        re.match(r"^\s*(INSERT\s+(OR\s+\w+\s+)?INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|WITH\s+\w+\s+AS)\b", up) is not None


def _static(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static(node.left) and _static(node.right)
    if isinstance(node, ast.JoinedStr):
        return all(isinstance(v, ast.Constant) for v in node.values)
    return False


# Tables a statement meets only where they exist: a migration's own
# scratch copy (created and dropped by that migration) and the shadow's
# schedule model (SHADOW_DATABASE_URL, not the platform database).
RUNTIME_TABLES = re.compile(r"\b(reviews_rekeyed|ask_memory_rekeyed|sched\.\w+)\b")

_SKIP_PREPARE = re.compile(r"^\s*(PRAGMA|BEGIN|COMMIT|ROLLBACK|END|SAVEPOINT|RELEASE|CREATE|ALTER|DROP|VACUUM|"
                           r"ATTACH|DETACH|REINDEX|ANALYZE)\b", re.I)


def prepare_check(url, root=ROOT, build=True) -> dict:
    """Every static DML statement translated against the Postgres at `url`
    (its catalog — the app's own boot builds it when `build`) and PREPAREd
    there. {"prepared": n, "prepare_failed": [{at, error, sql}], "skipped": n}.
    `url` must be a scratch database: the boot writes its schema there."""
    import contextlib
    import io
    import db_dialect
    import psycopg
    if build:
        _boot_on(url)
    conn = db_dialect.connect(url)
    raw = psycopg.connect(url, autocommit=True, connect_timeout=10)
    out = {"prepared": 0, "prepare_failed": [], "skipped": 0, "runtime_tables": 0}
    try:
        for path, line, sql in static_statements(root):
            if _SKIP_PREPARE.match(sql):
                out["skipped"] += 1
                continue
            if RUNTIME_TABLES.search(sql):
                out["runtime_tables"] += 1
                continue
            m = db_dialect._UPDATE_OR.match(sql)
            if m:
                # Run row by row (db_dialect): the SET and WHERE are checked.
                sql = re.sub(r"^\s*UPDATE\s+OR\s+\w+", "UPDATE", sql, flags=re.I)
            try:
                text, _ret = conn._final_text(sql)
            except Exception as e:
                out["prepare_failed"].append({"at": f"{path}:{line}", "error": f"translate: {e}"[:200],
                                              "sql": sql[:160]})
                continue
            n = [0]

            def _dollar(_m):
                n[0] += 1
                return f"${n[0]}"
            text = re.sub(r"%s", _dollar, text).replace("%%", "%")
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    raw.execute(f"PREPARE cavnar_audit AS {text}", prepare=False)
                raw.execute("DEALLOCATE cavnar_audit", prepare=False)
                out["prepared"] += 1
            except Exception as e:
                out["prepare_failed"].append({"at": f"{path}:{line}", "error": str(e).split("\n")[0][:200],
                                              "sql": sql[:160]})
    finally:
        raw.close()
        conn.close()
    return out


def _boot_on(url):
    """The app's boot (models.init_db and the init chain hosted_dashboard
    runs) against `url`, through db_dialect."""
    import contextlib
    import io
    import models
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    # The platform's database is `url` for this boot, whatever a caller had
    # routed it to (the suite's Postgres mode routes it per process).
    old_route = models._PG_ROUTES.get(models.DB_PATH)
    models._PG_ROUTES[models.DB_PATH] = url
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            models.init_db()
            for mod, fn in (("auth", "init_auth"), ("platform_monitor", "init_platform_tables"),
                            ("provider_health", "init_provider_health"), ("webhooks", "init_webhooks"),
                            ("guest_marketing", "init_guest_marketing"), ("push", "init_push"),
                            ("sales_audits", "init_sales_audits")):
                import importlib
                getattr(importlib.import_module(mod), fn)()
            for fn in ("init_staff_notes", "init_staff_availability", "ensure_columns", "init_email_log",
                       "init_onboarding_emails", "init_two_fa_backup_codes", "init_competitor_snapshots",
                       "init_ai_visibility_queries", "init_staff_capabilities", "init_shift_profiles",
                       "init_capability_changes", "init_ask_memory"):
                getattr(models, fn)()
    finally:
        if old_route is None:
            models._PG_ROUTES.pop(models.DB_PATH, None)
        else:
            models._PG_ROUTES[models.DB_PATH] = old_route
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old


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
    argv = list(argv or [])
    rep = audit()
    url = None
    if "--database-url" in argv:
        url = argv[argv.index("--database-url") + 1]
        rep.update(prepare_check(url))
    if "--json" in argv:
        print(json.dumps(rep, indent=2))
        return 1 if rep.get("prepare_failed") else 0
    print(f"{rep['statements']} SQL statements read")
    keys = ["untranslatable", "dynamic", "ported", "double_quoted", "sqlite_only", "reserved_columns"]
    if url:
        print(f"{rep['prepared']} PREPAREd on Postgres, {rep['skipped']} DDL / control skipped")
        keys.append("prepare_failed")
    for k in keys:
        print(f"\n{k}: {len(rep[k])}")
        for item in rep[k]:
            print("  ", item if isinstance(item, str) else " — ".join(str(v) for v in item.values()))
    return 1 if rep.get("prepare_failed") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
