"""
scripts/upgrade_boot_check.py — boot THIS code on the previous release's
database, the way a deploy does (blind re-audit 10/27/26, B5 P1: the gate's
boots all started from an empty database, where every ALTER reads
"duplicate column" and every new index, NOT NULL column and run-once rewrite
meets no rows; only production's tables lack the column).

    python scripts/upgrade_boot_check.py                                   # base: see below; SQLite
    python scripts/upgrade_boot_check.py --base-ref origin/main
    python scripts/upgrade_boot_check.py --database-url postgresql://postgres@127.0.0.1:55433/postgres
    python scripts/upgrade_boot_check.py --base-root DIR [--head-root DIR]  # trees already on disk

For each backend (SQLite always; Postgres too with --database-url — a
SCRATCH server on this machine, where a fresh database is created and dropped):

  1. BASE BOOT. The previous release's code — `git archive` of the base ref
     into a temporary directory — boots a fresh volume exactly as production
     ran it (import hosted_dashboard: its whole boot chain, whatever it was
     then), and answers /health.
  2. SEED. Every table the base built gets representative rows: a client
     restaurant, an owner, a staff PIN identity, live and legacy sessions,
     old sign-ins, then two synthetic rows in every other table (a full row
     and a row with only the required columns, so a backfill's `IS NULL` has
     something to match). Read from the catalog, so it needs nothing of the
     base's code. What could not be seeded is reported, never fatal.
  3. UPGRADE BOOT. HEAD's code boots that database. It must answer /health
     200 with no schema gap, no missing index (a new UNIQUE index over
     existing rows, an INVALID one), no failed boot statement and no failed
     run-once migration.
  4. SECOND BOOT. HEAD again (the next deploy): the same.
  5. ROLLBACK BOOT (unless --no-rollback). The base's code on the upgraded
     database — a rollback — must answer /health 200 (additive schema,
     ARCHITECTURE_MANIFEST.md → Release compatibility).
  6. INDEX RULE (SQLite). An index HEAD's boot built on a table the base
     already had must be prebuilt (migrations/prebuilt_indexes.txt) — the
     rule tests/test_boot_ddl_rules.py holds against the committed
     schema_baseline.json, here against the base release itself, so the
     baseline cannot drift.

The base, unless named: origin/production when it exists (the deployed
branch); otherwise the merge-base with origin/main when HEAD is not on it;
otherwise HEAD~1 (main deploys every push, so its previous commit is what
production ran). CI needs the history: fetch-depth 0, or fetch the base ref.

No provider key reaches a child (never a .env, keys stripped), the
scheduler cannot start off Railway, and every database is a temporary one.
Exit 0 when every check passed; 1 when one failed; 2 when the check could
not run (no base commit, the base itself would not boot).
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# What the base tree leaves out: nothing a boot reads.
_ARCHIVE_EXCLUDES = (":!brand", ":!ios", ":!docs", ":!tests")
BOOT_TIMEOUT = 300

# A boot's own words for a statement or a migration it could not apply
# (each is logged, or printed, and the boot goes on): any of them fails the
# upgrade check.
FAILURE_PATTERNS = (
    "run-once data migration failed", "failed (will retry at next boot)", "boot backfill skipped",
    "index creation failed", "not applied (retried next boot)", "DB init error", "session normalization skipped",
    "ownerless chats not attributed", "legacy adoption skipped", "init_ai_ops:", "not built (",
)

_CHILD = r"""
import json, logging, os, sys, time
sys.path.insert(0, os.getcwd())
import dotenv
dotenv.load_dotenv = lambda *a, **k: False       # never a checkout's .env (production's keys)
records = []
_orig_handle = logging.Logger.handle
def _handle(self, record):
    if record.levelno >= logging.WARNING:
        try:
            records.append([record.levelname, record.name, record.getMessage()[:600]])
        except Exception as e:
            records.append(["ERROR", "upgrade_check", "unformattable record: %r" % (e,)])
    return _orig_handle(self, record)
logging.Logger.handle = _handle
t0 = time.monotonic()
try:
    import hosted_dashboard as h
except BaseException as e:
    print("UPGRADE " + json.dumps({"booted": False, "boot_error": repr(e)[:3000], "records": records[-80:]}))
    sys.stdout.flush()
    os._exit(3)
out = {"booted": True, "boot_ms": int((time.monotonic() - t0) * 1000)}
r = h.app.test_client().get("/health")
out["status"] = r.status_code
out["health"] = r.get_json(silent=True) or {"raw": r.get_data(as_text=True)[:400]}
import models
out["missing_indexes"] = {k: dict(v) for k, v in (getattr(models, "BOOT_MISSING_INDEXES", None) or {}).items()}
try:
    import status_manager
    conn = models.get_conn()
    try:
        out["schema_gaps"] = status_manager._schema_gaps(conn)
    finally:
        conn.close()
except Exception as e:
    out["schema_gaps_error"] = repr(e)[:400]
try:
    out["migrations"] = sorted(m["name"] for m in models.applied_migrations())
except Exception as e:
    out["migrations"] = None
out["records"] = records[-300:]
print("UPGRADE " + json.dumps(out, default=str))
sys.stdout.flush()
os._exit(0)
"""


# ── the base tree ───────────────────────────────────────────────────────────

def _git(root, *args, check=True):
    r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:400]}")
    return r.stdout.strip() if r.returncode == 0 else ""


def resolve_base_ref(root=ROOT, base_ref=None) -> str:
    """The commit the previous release ran (the module docstring's rule)."""
    if base_ref:
        return _git(root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    prod = _git(root, "rev-parse", "--verify", "--quiet", "origin/production^{commit}", check=False)
    if prod:
        return prod
    head = _git(root, "rev-parse", "HEAD")
    main = _git(root, "rev-parse", "--verify", "--quiet", "origin/main^{commit}", check=False)
    if main and main != head:
        base = _git(root, "merge-base", "HEAD", main, check=False)
        if base and base != head:
            return base
    return _git(root, "rev-parse", "--verify", "HEAD~1^{commit}")


def extract_tree(root, sha, dest) -> str:
    """`git archive` of `sha` (what a boot reads) into `dest`."""
    os.makedirs(dest, exist_ok=True)
    proc = subprocess.Popen(["git", "-C", root, "archive", "--format=tar", sha, "--", ".", *_ARCHIVE_EXCLUDES],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            tar.extractall(dest, filter="data")
    finally:
        proc.stdout.close()
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.stderr.close()
        if proc.wait(timeout=120) != 0:
            raise RuntimeError(f"git archive {sha}: {err.strip()[:400]}")
    return dest


# ── booting a tree ──────────────────────────────────────────────────────────

def child_env(volume, database_url=None, allow_empty=False):
    from scripts.boot_check import child_env as _boot_env
    env = _boot_env(volume, database_url)
    if allow_empty:
        env["ALLOW_EMPTY_DATABASE"] = "1"
    return env


def boot(tree, volume, database_url=None, allow_empty=False, timeout=BOOT_TIMEOUT) -> dict:
    """Boot `tree`'s hosted_dashboard on `volume` (and `database_url`) in a
    process of its own; its report, plus `ok` (answered at all), `seconds`
    and the output's tail."""
    started = time.monotonic()
    try:
        r = subprocess.run([sys.executable, "-c", _CHILD], cwd=tree,
                           env=child_env(volume, database_url, allow_empty), capture_output=True, text=True,
                           timeout=timeout)
        stdout, stderr, rc = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired as e:
        stdout, stderr, rc = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else \
            (e.stdout or ""), "timed out", -9
    line = next((x for x in reversed(stdout.splitlines()) if x.startswith("UPGRADE ")), None)
    report = json.loads(line[len("UPGRADE "):]) if line else {"booted": False}
    report.update(returncode=rc, seconds=round(time.monotonic() - started, 1),
                  printed=[x for x in stdout.splitlines() if not x.startswith("UPGRADE ")][-400:],
                  tail=(stdout[-2000:] + "\n" + stderr[-3000:]))
    return report


def boot_failures(report, base=False) -> list:
    """Why a boot fails the check ([] when it passes). The base's own boot
    is held only to booting and answering 200."""
    out = []
    if not report.get("booted"):
        return [f"did not boot: {report.get('boot_error') or 'exit ' + str(report.get('returncode'))}"]
    if report.get("status") != 200:
        out.append(f"/health answered {report.get('status')}: {json.dumps(report.get('health'))[:300]}")
    if base:
        return out
    gaps = report.get("schema_gaps") or []
    if gaps:
        out.append(f"schema gaps after the boot: {gaps[:20]}")
    if report.get("schema_gaps_error"):
        out.append(f"schema check failed: {report['schema_gaps_error']}")
    for name, info in sorted((report.get("missing_indexes") or {}).items()):
        out.append(f"index {name} not built ({'unique' if info.get('unique') else 'plain'}): "
                   f"{str(info.get('error'))[:200]}")
    problems = (report.get("health") or {}).get("problems") or []
    for p in ("schema_gaps", "constraint_missing", "index_missing"):
        if p in problems:
            out.append(f"/health problem {p}")
    lines = [m for (_lvl, _name, m) in report.get("records") or []] + list(report.get("printed") or [])
    for line in lines:
        if any(pat in line for pat in FAILURE_PATTERNS):
            out.append(f"boot statement failed: {line[:300]}")
    seen, uniq = set(), []
    for f in out:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


# ── the database between boots ──────────────────────────────────────────────

class _Db:
    """The seeder's and the snapshot's view of one database (SQLite file or
    scratch Postgres), straight from the catalog — no app code."""

    def __init__(self, sqlite_path=None, pg_url=None):
        self.pg = pg_url is not None
        if self.pg:
            import psycopg
            self.conn = psycopg.connect(pg_url, autocommit=True, connect_timeout=10)
            try:
                # The seed's synthetic ids point at rows that may not exist:
                # no foreign-key triggers for this session (a scratch server's
                # superuser; without it, such rows are skipped).
                self.conn.execute("SET session_replication_role = replica")
            except Exception as e:
                print(f"note: foreign keys stay on for the seed ({e})")
        else:
            self.conn = sqlite3.connect(sqlite_path, timeout=30)

    def close(self):
        self.conn.close()

    def tables(self) -> list:
        if self.pg:
            rows = self.conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') AND NOT c.relispartition "
                "ORDER BY 1").fetchall()
        else:
            rows = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                     "AND name NOT LIKE 'sqlite_%' ORDER BY 1").fetchall()
        return [r[0] for r in rows]

    def indexes(self) -> dict:
        if self.pg:
            rows = self.conn.execute("SELECT indexname, tablename FROM pg_indexes "
                                     "WHERE schemaname = current_schema()").fetchall()
        else:
            rows = self.conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
                                     "AND sql IS NOT NULL").fetchall()
        return {r[0]: r[1] for r in rows}

    def columns(self, table) -> list:
        """[(name, type, notnull, has_default, auto)] — `auto`: the key the
        database assigns (an integer primary key, an identity or serial)."""
        if self.pg:
            rows = self.conn.execute(
                "SELECT column_name, data_type, is_nullable, column_default, is_identity "
                "FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s "
                "ORDER BY ordinal_position", (table,)).fetchall()
            return [(r[0], (r[1] or "").upper(), r[2] == "NO", r[3] is not None,
                     r[4] == "YES" or "nextval(" in str(r[3] or "")) for r in rows]
        rows = self.conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        pks = [r for r in rows if r[5]]
        return [(r[1], (r[2] or "").upper(), bool(r[3]), r[4] is not None,
                 len(pks) == 1 and r[5] and "INT" in (r[2] or "").upper()) for r in rows]

    def insert(self, table, row) -> bool:
        if not row:
            sql = f'INSERT INTO "{table}" DEFAULT VALUES'
            args = ()
        else:
            cols = ", ".join(f'"{c}"' for c in row)
            marks = ", ".join(["%s" if self.pg else "?"] * len(row))
            sql = f'INSERT INTO "{table}" ({cols}) VALUES ({marks})'
            args = tuple(row.values())
        try:
            self.conn.execute(sql, args)
            if not self.pg:
                self.conn.commit()
            return True
        except Exception:
            if not self.pg:
                self.conn.rollback()
            return False

    def scalar(self, sql, args=()):
        sql = sql.replace("?", "%s") if self.pg else sql
        row = self.conn.execute(sql, args).fetchone()
        return row[0] if row else None


_TRUE_FLAGS = ("is_", "has_", "module_", "al_", "alert_", "auto_")


def synthetic_value(table, col, typ, n, ids):
    """A plausible value for `table.col` of declared type `typ` in synthetic
    row `n` (1 or 2): distinct per row, shaped by the column's name so a
    boot's backfill reads it the way it reads real data."""
    name = col.lower()
    if name in ("restaurant_id", "target_restaurant_id"):
        return ids["rid"]
    if name in ("user_id", "owner_user_id", "actor_user_id", "changed_by_user_id", "created_by_user_id"):
        return ids["uid"]
    if name == "membership_id":
        return ids["mid"]
    if "BOOL" in typ:
        return n == 1
    if "INT" in typ:
        if name.startswith(_TRUE_FLAGS) or name.endswith(("_enabled", "_ok", "_flag")):
            return 1 if n == 1 else 0
        return n
    if any(t in typ for t in ("REAL", "DOUB", "FLOA", "NUMERIC", "DECIMAL")):
        return 1.5 * n
    if "BLOB" in typ or "BYTEA" in typ:
        return b"seed"
    day = 19 + n
    if "json" in name:
        return "{}"
    if name.endswith(("_at", "_until", "_since", "_ts", "_expires")) or name in ("created", "updated", "timestamp"):
        return f"2026-10-{day} 12:00:00"
    if name in ("date", "day", "business_date", "week_start", "week", "period_start", "period_end") or \
            name.endswith(("_date", "_on", "_week")):
        return f"2026-10-{day}"
    if name == "month" or name.endswith("_month"):
        return "2026-10"
    if "email" in name:
        return f"seed{n}.{table}.{col}@example.test"
    if "phone" in name:
        return f"+1312555{n:04d}"
    if "url" in name or name.endswith("link"):
        return f"https://example.test/{table}/{n}"
    if name in ("timezone", "tz"):
        return "America/Chicago"
    if name.endswith(("_time", "_start", "_end")) or name in ("start", "end", "time"):
        return f"{8 + n:02d}:00"
    return f"seed {table}.{col} {n}"


# Rows a boot's own logic reads, with the shapes the legacy fixes exist for.
_CURATED = (
    ("restaurants", {"id": 1001, "name": "Seed Client", "owner_email": "owner1@example.test",
                     "billing_status": "active", "is_demo": 0, "timezone": "America/Chicago",
                     "service_tier": "full", "created_at": "2026-09-01 12:00:00"}),
    ("restaurants", {"id": 1002, "name": "Seed Demo", "owner_email": "demo@example.test", "is_demo": 1,
                     "timezone": "America/Chicago", "created_at": "2026-09-01 12:00:00"}),
    ("users", {"id": 2001, "restaurant_id": 1001, "username": "seedowner", "email": "owner1@example.test",
               "password_hash": "x", "role": "client", "is_active": 1}),
    ("users", {"id": 2002, "restaurant_id": 1001, "username": "SeedPin", "email": "pin1@staff.invalid",
               "password_hash": "x", "role": "client", "is_active": 1}),
    ("users", {"id": 2003, "restaurant_id": 1001, "username": "seedadmin", "email": "admin@example.test",
               "password_hash": "x", "role": "client", "is_admin": 1, "is_active": 1}),
    ("memberships", {"id": 3001, "user_id": 2001, "restaurant_id": 1001, "role": "client", "is_active": 1}),
    ("sessions", {"token": "seed-session-iso", "user_id": 2001, "expires_at": "2030-01-01T10:00:00+00:00",
                  "last_active": "2026-10-20T10:00:00+00:00", "device_type": "web"}),
    ("sessions", {"token": "seed-session-viewas", "user_id": 2001, "expires_at": "2030-01-01 10:00:00",
                  "device_type": "admin-view-as"}),
    ("sessions", {"token": "seed-session-admin", "user_id": 2003, "expires_at": "2030-01-01 10:00:00",
                  "created_at": "2026-10-20 10:00:00", "device_type": "web"}),
    ("login_history", {"user_id": 2001, "restaurant_id": 1001, "created_at": "2026-01-02 10:00:00"}),
    ("login_history", {"user_id": 2001, "restaurant_id": 1001, "created_at": "2026-10-20 10:00:00"}),
    ("email_suppressions", {"email": "bounced@example.test", "reason": "bounce"}),
    # The hot tables whose CHECKs a synthetic value would not pass.
    ("reviews", {"restaurant_id": 1001, "platform": "google", "external_id": "seed-review-1", "rating": 5,
                 "text": "Great brunch.", "author": "Seed Guest", "review_date": "2026-10-20",
                 "fetched_at": "2026-10-20 12:00:00", "sentiment": "positive"}),
    ("reviews", {"restaurant_id": 1001, "platform": "yelp", "external_id": "seed-review-2", "rating": 2,
                 "text": "Slow service.", "author": "Seed Guest 2", "fetched_at": "2026-10-21 12:00:00"}),
    ("ask_cavnar_messages", {"restaurant_id": 1001, "role": "user", "content": "How was last week?"}),
    ("ask_cavnar_messages", {"restaurant_id": 1001, "user_id": 2001, "role": "assistant", "content": "Fine."}),
    ("schedule_note_rules", {"restaurant_id": 1001, "scope": "every", "role": "Server", "min_people": 2,
                             "dayparts": "[\"pm\"]"}),
)
IDS = {"rid": 1001, "uid": 2001, "mid": 3001}


def seed(db) -> dict:
    """Representative rows in every table: {tables, seeded, rows, unseeded}."""
    tables = db.tables()
    cols = {t: db.columns(t) for t in tables}
    seeded, rows = set(), 0
    for table, want in _CURATED:
        if table not in cols:
            continue
        # The columns it must have (required, no default) synthetic, then the
        # curated values over them.
        row = {c: synthetic_value(table, c, typ, 1, IDS) for c, typ, notnull, has_default, auto in cols[table]
               if notnull and not has_default and not auto}
        row.update({k: v for k, v in want.items() if k in {c[0] for c in cols[table]}})
        if db.insert(table, row):
            seeded.add(table)
            rows += 1
    for table in tables:
        if table in seeded or table in ("data_migrations", "schema_migrations"):
            continue
        for n in (1, 2):
            row = {}
            for col, typ, notnull, has_default, auto in cols[table]:
                if auto:
                    continue
                if n == 2 and not notnull and col.lower() not in ("restaurant_id", "user_id"):
                    continue                        # the second row: required columns only
                if n == 2 and notnull and has_default:
                    continue
                row[col] = synthetic_value(table, col, typ, n, IDS)
            if db.insert(table, row):
                seeded.add(table)
                rows += 1
    skipped = sorted(set(tables) - seeded - {"data_migrations", "schema_migrations"})
    return {"tables": len(tables), "seeded": len(seeded), "rows": rows, "unseeded": skipped}


def index_rule_violations(base_snapshot, head_snapshot, prebuilt=None) -> list:
    """Indexes HEAD's boot built on a table the base already had, without a
    prebuild — the boot DDL rule against the base release itself."""
    from scripts import schema_baseline
    prebuilt = schema_baseline.load_prebuilt() if prebuilt is None else prebuilt
    return schema_baseline.violations(head_snapshot, base_snapshot, prebuilt)


# ── one backend ─────────────────────────────────────────────────────────────

def _local_host(url) -> bool:
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1")


def _pg_database(admin_url, name):
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(admin_url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


def run_backend(base_tree, head_tree, pg_admin_url=None, rollback=True, out=print) -> dict:
    """The five boots and the seed on one fresh database. Returns
    {backend, ok, inconclusive, failures, seed, steps}."""
    backend = "Postgres" if pg_admin_url else "SQLite"
    work = tempfile.mkdtemp(prefix="cavnar-upgrade-check-")
    volume = os.path.join(work, "volume")
    os.makedirs(volume)
    db_file = os.path.join(volume, "reviews.db")
    open(db_file, "a").close()                     # adopt_legacy_db sees a database and copies nothing in
    result = {"backend": backend, "ok": False, "inconclusive": False, "failures": [], "steps": []}
    pg_url, pg_name = None, None
    try:
        if pg_admin_url:
            import psycopg
            import secrets
            pg_name = f"cavnar_upgrade_{os.getpid()}_{secrets.token_hex(4)}"
            with psycopg.connect(pg_admin_url, autocommit=True, connect_timeout=10) as c:
                c.execute(f'CREATE DATABASE "{pg_name}"')
            pg_url = _pg_database(pg_admin_url, pg_name)

        def step(label, tree, base=False):
            # The base's emptied-database guard read the volume's SQLite file
            # even on Postgres (fixed in RF6): on Postgres it is told the
            # emptiness is deliberate, so the old defect is not what fails.
            rep = boot(tree, volume, pg_url, allow_empty=base and bool(pg_url))
            fails = boot_failures(rep, base=base)
            result["steps"].append({"step": label, "seconds": rep.get("seconds"), "status": rep.get("status"),
                                    "failures": fails})
            out(f"  {backend} {label}: {'ok' if not fails else 'FAILED'} in {rep.get('seconds')}s "
                f"(/health {rep.get('status')})")
            for f in fails:
                out(f"    - {f}")
            if fails and not rep.get("booted"):
                out("    " + rep.get("tail", "")[-1500:].replace("\n", "\n    "))
            return rep, fails

        # Twice: production's database has been booted many times (on SQLite
        # a first boot's table rebuild drops indexes the second boot makes).
        for label in ("base boot (previous release)", "base boot again"):
            _rep, fails = step(label, base_tree, base=True)
            if fails:
                result.update(inconclusive=True, failures=[f"base: {f}" for f in fails])
                return result
        db = _Db(sqlite_path=None if pg_url else db_file, pg_url=pg_url)
        try:
            base_snapshot = {"tables": db.tables(), "indexes": db.indexes()}
            result["seed"] = seed(db)
        finally:
            db.close()
        s = result["seed"]
        out(f"  {backend} seed: {s['rows']} rows in {s['seeded']} of {s['tables']} tables"
            + (f" (not seeded: {', '.join(s['unseeded'][:12])}{'…' if len(s['unseeded']) > 12 else ''})"
               if s["unseeded"] else ""))
        failures = []
        _rep, fails = step("upgrade boot (this code)", head_tree)
        failures += [f"upgrade: {f}" for f in fails]
        if not pg_url:
            db = _Db(sqlite_path=db_file)
            try:
                head_snapshot = {"tables": db.tables(), "indexes": db.indexes()}
            finally:
                db.close()
            bad = index_rule_violations(base_snapshot, head_snapshot)
            for name, table in bad:
                failures.append(f"index rule: {name} built at boot on {table}, a table the previous release "
                                "already has — build it first with scripts/build_index.py and list it in "
                                "migrations/prebuilt_indexes.txt")
            out(f"  {backend} index rule: {'ok' if not bad else 'FAILED'}")
            for name, table in bad:
                out(f"    - {name} on {table}")
        _rep, fails = step("second boot (the next deploy)", head_tree)
        failures += [f"second boot: {f}" for f in fails]
        if rollback:
            _rep, fails = step("rollback boot (previous release on this schema)", base_tree, base=True)
            failures += [f"rollback: {f}" for f in fails]
        result.update(ok=not failures, failures=failures)
        return result
    finally:
        if pg_name:
            try:
                import db_dialect
                db_dialect.close_idle(pg_url)
            except Exception as e:
                out(f"  note: idle connections not closed: {e}")
            try:
                import psycopg
                with psycopg.connect(pg_admin_url, autocommit=True, connect_timeout=10) as c:
                    c.execute(f'DROP DATABASE IF EXISTS "{pg_name}" WITH (FORCE)')
            except Exception as e:
                out(f"  note: scratch database {pg_name} not dropped: {e}")
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-ref", default=None, help="the previous release (default: see the docstring)")
    ap.add_argument("--base-root", default=None, help="the previous release's tree, already on disk")
    ap.add_argument("--head-root", default=ROOT, help="this code's tree (default: this checkout)")
    ap.add_argument("--database-url", default=None, help="a SCRATCH Postgres on this machine (never production's)")
    ap.add_argument("--no-sqlite", action="store_true", help="Postgres only")
    ap.add_argument("--no-rollback", action="store_true", help="skip the previous release's boot on this schema")
    ap.add_argument("--json", default=None, help="also write the results here")
    args = ap.parse_args(argv)
    if args.database_url and not _local_host(args.database_url):
        print("Refusing: --database-url must be a scratch server on this machine (localhost); this check "
              "creates and drops databases on it.")
        return 2
    if args.no_sqlite and not args.database_url:
        print("Nothing to check: --no-sqlite without --database-url.")
        return 2
    cleanup = None
    if args.base_root:
        base_tree, label = os.path.abspath(args.base_root), args.base_root
    else:
        try:
            sha = resolve_base_ref(ROOT, args.base_ref)
        except RuntimeError as e:
            print(f"No previous release to boot ({e}). In CI fetch the history (fetch-depth: 0) or the base ref.")
            return 2
        cleanup = tempfile.mkdtemp(prefix="cavnar-upgrade-base-")
        base_tree, label = extract_tree(ROOT, sha, os.path.join(cleanup, "base")), sha[:12]
    print(f"upgrade boot check: previous release {label} -> {os.path.abspath(args.head_root)}")
    results = []
    try:
        if not args.no_sqlite:
            results.append(run_backend(base_tree, os.path.abspath(args.head_root), None, not args.no_rollback))
        if args.database_url:
            results.append(run_backend(base_tree, os.path.abspath(args.head_root), args.database_url,
                                       not args.no_rollback))
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"base": label, "results": results}, f, indent=1, default=str)
    if any(r["inconclusive"] for r in results):
        print("INCONCLUSIVE: the previous release did not boot here — see above.")
        return 2
    failed = [r for r in results if not r["ok"]]
    print("upgrade boot check: " + ("ok" if not failed else "FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
