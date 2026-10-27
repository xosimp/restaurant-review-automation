"""Shared fixtures: every test gets a real, throwaway SQLite database built by
the same init_db() the app uses, so schema drift is caught here instead of in
production."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

# ── Never production (audit #25 A01-06, A01-17, R16) ─────────────────────────
# Before anything else is imported. Inside a Railway environment (`railway
# run`, `railway shell`, a `railway ssh` session — what sets DATABASE_URL to
# production's Postgres) the suite refuses to start: the default database's
# boot DDL and the autouse ledger reset would run against production. Any
# platform database URL is dropped, so a shell that exported one for a psql
# session never reaches models.get_conn (which also ignores DATABASE_URL off
# Railway — config.platform_database_url_allowed); a test that wants the
# Postgres backend sets DATABASE_URL and ALLOW_LOCAL_PG=1 with monkeypatch.
import config as _cfg_guard


def _refuse(message):
    """Stop the run before anything else is imported (exit status 3)."""
    sys.stderr.write("\n" + message + "\n")
    raise SystemExit(3)


if _cfg_guard.railway_marked():
    _refuse("Refusing to run the test suite inside a Railway environment (one of %s is set): it would run boot "
            "migrations and per-test deletes against production. Run it from a plain local shell."
            % ", ".join(_cfg_guard.RAILWAY_MARKERS))
for _var in ("DATABASE_URL", "SHADOW_DATABASE_URL", "ALLOW_LOCAL_PG"):
    os.environ.pop(_var, None)

# The checkout's .env holds production keys (Anthropic, Resend, Twilio,
# Stripe…). hosted_dashboard.py and scheduler.py call load_dotenv() at
# import, so on a developer machine every test that imported either one
# carried the live keys — a test could reach a real provider, and which
# ones did depended on test order (CI has no .env, so it never showed).
# Loading is a no-op for the whole session, before any app module imports
# it: the suite sees the environment CI sees. A test that needs a key sets
# it with monkeypatch.setenv.
import dotenv as _dotenv
_dotenv.load_dotenv = lambda *a, **k: False

# Security controls that would otherwise refuse or block in a test process:
# a pepper so staff PINs can be set, no live breach lookups, and the admin
# 2FA gate off except in the test that turns it on.
os.environ.setdefault("CAVNAR_PIN_PEPPER", "test-pepper")
# The suite's season files are a frozen copy (as refreshed 10/8/26): the live
# event_intel/seasons files are refreshed every day by a workflow, and a test
# that pinned a game's time or result broke with the calendar. Only
# tests/test_event_fix2_w5.py reads the live files, to check them. Refresh the
# copy by hand when a test needs newer data.
os.environ.setdefault("CAVNAR_SEASONS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "seasons"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("HIBP_DISABLED", "1")
os.environ.setdefault("ADMIN_REQUIRE_2FA", "0")
# The learning holdout (rec_learning.holdout_arm) is off in the suite, so a
# ranking test never lands on a held-out day by the calendar; its own tests
# turn it on with monkeypatch.setenv.
os.environ.setdefault("LEARNING_HOLDOUT_PCT", "0")
# The orchestrator's shadow reviewer (ai_orchestrator._shadow_review) scores
# a random share of passing runs with one more model call on a background
# thread — the DSR narrative's 20%, Ask's none — so a test counting its fake
# client's calls would pass or fail by the dice. Off in the suite; its own
# tests turn it on with monkeypatch.setenv.
os.environ.setdefault("AI_SHADOW_REVIEW", "0")
# The solver is production's draft source (blind re-audit 10/11/26 ARCH-1);
# the model's draft is an arm. The suite's generation tests drive the model
# arm's pipeline with a stubbed model answer (labor.generate_optimized_
# schedule), so the suite runs that arm by default; the tests of the default
# source, the draft_v1 arms and the benchmark delete or set it themselves
# (tests/test_sf11_p_pipeline.py, test_sf10_e_fallback.py, schedule_benchmark).
os.environ.setdefault("SCHEDULE_DRAFT_SOURCE", "model")
# A generation's state declares every key it may hold (schedule_state, blind
# re-audit 10/11/26 ARCH-5): in the suite an undeclared key — a misspelling, or
# a key no stage writes — raises instead of reading None.
os.environ.setdefault("SCHEDULE_STATE_STRICT", "1")
# Home's kept blocks are never built on a request in production: a missing
# one is queued for the queue worker and the route answers `preparing`
# (home_snapshots, blind re-audit 10/20/26 R6-F4). The suite's route tests
# read a block once and check what it says, so the suite builds a missing
# block on the request (the rollback mode); tests/test_g6_home_offthread.py
# runs the production path with monkeypatch.setenv.
os.environ.setdefault("HOME_SNAPSHOT_COLD", "request")
# Cavnar AI's own marketing is not sent without a CAN-SPAM postal address
# (emails.postal_address, #159). Set here as production must set it; the
# tests of the unset case delete it.
os.environ.setdefault("CAVNAR_POSTAL_ADDRESS", "100 Test St, Testville, IL 60000")
# Every test has a ceiling (pytest-timeout, requirements-dev.txt; blind
# re-audit 10/27/26 B1 F16): a deadlocked thread or barrier names its test
# instead of burning the CI job's whole timeout. Generous by default (600 s);
# CI passes --timeout=300. Inert where the plugin is not installed.
os.environ.setdefault("PYTEST_TIMEOUT", "600")

# The default database (models.DB_PATH, used by every call that passes no
# db_path — init_db's ensure_columns(), status_manager, lazily imported
# modules) must never be the developer's ./reviews.db. It was, and a full
# run grew that file by ~550 KB. Point the default at a throwaway volume
# before models is imported. The file is created empty first so
# adopt_legacy_db sees an existing database and does not copy the real
# reviews.db into it.
import tempfile as _tempfile
_TEST_VOLUME = _tempfile.mkdtemp(prefix="cavnar-test-volume-")
open(os.path.join(_TEST_VOLUME, "reviews.db"), "a").close()
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = _TEST_VOLUME

from models import init_db, ensure_columns, create_restaurant, save_reviews, Restaurant, Review

# ── Postgres mode (blind re-audit 10/11/26, SCALE-10; 10/20/26 R9-02) ──────
# CAVNAR_TEST_PG_URL=postgresql://user@host:port/postgres (a SCRATCH server —
# never production's) runs the suite's database on Postgres: the default
# database and every test's db_path are routed (models._PG_ROUTES) to a
# database of their own, copied per test from a template, then dropped.
#
# The template is built the way production's database was: the boot's
# migrations on SQLite, pg_copy.copy_database into Postgres (so the fact
# tables in db_dialect.PARTITIONED are month-partitioned, their keys carrying
# the partition column, as they are in production), then the boot again on
# Postgres (every deploy boots on the copied database).
#
# A test's own sqlite3.connect(<its db_path>) — seeding a fixture, reading a
# row back — is served by the same Postgres database (_pg_aware_connect), so
# the test reads what the code under test wrote. The app's own
# sqlite3.connect calls are never redirected.
#
# CI's `postgres` job runs EVERY test file this way. What cannot run on
# Postgres is listed in ONE place, with the reason: the `sqlite_only` marker
# on a test (it reads the SQLite file itself — a PRAGMA, a snapshot, a query
# plan, a file copy) and PG_MODE_SKIP_FILES below for a whole file.
_PG_BASE = (os.getenv("CAVNAR_TEST_PG_URL") or "").strip()


def _is_local_host(host):
    """localhost, a *.localhost name, or a loopback address (v4, v6, mapped)."""
    import ipaddress
    host = (host.decode() if isinstance(host, bytes) else str(host or "")).strip("[]").lower()
    if host in ("localhost", "") or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


if _PG_BASE:
    # The suite creates and DROPs databases on this server: a scratch one on
    # this machine (or CI's service container), never anything remote.
    from urllib.parse import urlsplit as _urlsplit
    _pg_host = _urlsplit(_PG_BASE).hostname or ""
    if not _is_local_host(_pg_host) and os.getenv("CAVNAR_TEST_PG_ALLOW_REMOTE") != "1":
        _refuse(f"CAVNAR_TEST_PG_URL names a non-local server ({_pg_host}); the suite creates and drops databases "
                "there. Use a scratch Postgres on localhost (CAVNAR_TEST_PG_ALLOW_REMOTE=1 to override "
                "deliberately).")


# ── The network guard (audit #25 A01-17) ─────────────────────────────────────
# An allowlist at the socket, not a denylist of provider hosts (APNs, Google
# OAuth, Meta, RPOWER, Untappd, Tock and R2 were all reachable before): a
# connection to anything but this machine (loopback, a unix socket) or the
# scratch Postgres raises NetworkBlocked — an OSError, so the code under
# test meets an unreachable network exactly as it would offline — and the
# attempt is listed at the end of the run so it gets stubbed. (Postgres
# itself connects through libpq in C, below this guard; CAVNAR_TEST_PG_URL is
# held to localhost above.)
import socket as _socket


class NetworkBlocked(ConnectionRefusedError):
    """A test tried to open a connection off this machine."""


_NET_BLOCKED = []
_PG_GUARD_HOST = ""
if _PG_BASE:
    from urllib.parse import urlsplit as _urlsplit_net
    _PG_GUARD_HOST = (_urlsplit_net(_PG_BASE).hostname or "").lower()


def _connection_allowed(sock, address):
    if getattr(_socket, "AF_UNIX", None) is not None and sock.family == _socket.AF_UNIX:
        return True
    if not isinstance(address, tuple) or not address:
        return True
    host = address[0]
    host_s = (host.decode() if isinstance(host, bytes) else str(host or "")).lower()
    return _is_local_host(host_s) or (bool(_PG_GUARD_HOST) and host_s == _PG_GUARD_HOST)


def _note_blocked(address):
    where = os.environ.get("PYTEST_CURRENT_TEST", "(collection)").rsplit(" (", 1)[0]
    _NET_BLOCKED.append((where, "%s:%s" % (address[0], address[1] if len(address) > 1 else "")))


_real_socket_connect = _socket.socket.connect
_real_socket_connect_ex = _socket.socket.connect_ex


def _guarded_connect(self, address):
    if not _connection_allowed(self, address):
        _note_blocked(address)
        raise NetworkBlocked(f"test tried to open a network connection to {address!r} — only this machine "
                                 "may be reached in the suite; stub the call (tests/conftest.py network guard)")
    return _real_socket_connect(self, address)


def _guarded_connect_ex(self, address):
    if not _connection_allowed(self, address):
        _note_blocked(address)
        import errno
        return errno.ECONNREFUSED
    return _real_socket_connect_ex(self, address)


_socket.socket.connect = _guarded_connect
_socket.socket.connect_ex = _guarded_connect_ex


def _pg_url(name):
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(_PG_BASE)
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))


def _pg_admin(sql):
    import psycopg
    with psycopg.connect(_PG_BASE, autocommit=True, connect_timeout=10) as c:
        c.execute(sql)


# A frozen clock and a Postgres connect (10/26/26 integration run): time-
# machine freezes time.monotonic too, and psycopg's binary connect then trips
# its own connect_timeout at once — and a connect that failed that way left
# the process unable to connect at all, so every later test on that worker
# errored (7,066 setup errors). While a travel is active the test process
# connects with no wall-clock deadline once time-machine is loaded (the scratch
# server is local).
if _PG_BASE:
    import psycopg as _psycopg_for_clock
    _real_psycopg_connect = _psycopg_for_clock.connect

    def _clock_safe_psycopg_connect(*args, **kwargs):
        # Once time-machine has patched the clock (its first travel), its
        # monotonic is no longer the one psycopg's binary connect measures
        # against, even between travels: no deadline from then on.
        if "time_machine" in sys.modules:
            kwargs.pop("connect_timeout", None)
        return _real_psycopg_connect(*args, **kwargs)

    _psycopg_for_clock.connect = _clock_safe_psycopg_connect


_PG_VERSION = []


def _pg_server_version():
    """The scratch server's server_version_num (e.g. 160015), read once."""
    if not _PG_VERSION:
        import psycopg
        with psycopg.connect(_PG_BASE, autocommit=True, connect_timeout=10) as c:
            _PG_VERSION.append(int(c.execute("SHOW server_version_num").fetchone()[0]))
    return _PG_VERSION[0]


_PG_SEQ = [0]


def _pg_new_database(template=None):
    """A fresh database on the scratch server (from `template` when
    given); its URL."""
    import db_dialect
    _PG_SEQ[0] += 1
    name = f"cavnar_t{os.getpid()}_{_PG_SEQ[0]}"
    _pg_admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    if template:
        db_dialect.close_idle(_pg_url(template))
        # FILE_COPY on Postgres 15+: its default (WAL_LOG) copies the ~40 MB
        # template through the WAL — 0.86 s against 0.27 s a clone here, and
        # under 12 workers the WAL traffic timed every connection out (the
        # 10/26/26 integration run: 7,066 setup errors). CI's PG16 too.
        strategy = " STRATEGY FILE_COPY" if _pg_server_version() >= 150000 else ""
        _pg_admin(f"CREATE DATABASE {name} TEMPLATE {template}{strategy}")
    else:
        _pg_admin(f"CREATE DATABASE {name}")
    return name


def _pg_drop(name):
    import db_dialect
    db_dialect.close_idle(_pg_url(name))
    try:
        _pg_admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    except Exception:
        pass


def pytest_configure(config):
    config.addinivalue_line("markers", "sqlite_only: reads the SQLite file itself (a snapshot, a query "
                            "plan, a PRAGMA); skipped in the Postgres mode")
    # The release gate's tiers (audit #25, TESTING.md "Tiers"): `contract` is
    # the deploy-gate slice (`pytest -m contract`, < 60 s, run before a push
    # that touches a top-10 component); `quarantine` is a known-flaky test,
    # left out of the gate and run and reported nightly (never a silent
    # skip); `browser` is a Playwright test, nightly.
    config.addinivalue_line("markers", "contract: the deploy-gate contract slice (pytest -m contract)")
    config.addinivalue_line("markers", "quarantine: known flaky; excluded from the gate, run and reported nightly")
    config.addinivalue_line("markers", "browser: a Playwright browser test (tests/browser/), nightly")
    # `pg_nightly`: a contract test the SQLite gate runs but too slow for the
    # 10-minute Postgres gate (the crawl: 140 s alone on a laptop, past 300 s
    # on GitHub's 4 cores under -n 8, 10/27/26); the nightly full Postgres
    # run carries it.
    config.addinivalue_line("markers", "pg_nightly: contract test left out of gate-postgres; nightly on Postgres")


# `contract`, or `contract and not <marker>...` (the gate's own expression,
# "contract and not quarantine"): an expression that can only select
# contract-marked tests.
import re as _re_markexpr
_CONTRACT_ONLY = _re_markexpr.compile(r"^contract(\s+and\s+not\s+\w+)*$")


def pytest_ignore_collect(collection_path, config):
    """`pytest -m contract` (the gate's slice) imports only the files that
    carry the marker: collecting all ~1,000 files to deselect them took most
    of the slice's minute. Any other marker expression collects as usual."""
    if not _CONTRACT_ONLY.match((config.getoption("markexpr", "") or "").strip()):
        return None
    name = collection_path.name
    if collection_path.suffix == ".py" and name.startswith("test_"):
        try:
            text = collection_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        if "mark.contract" not in text:
            return True
    return None


# Whole files that cannot run in the Postgres mode, and why. Every entry is
# a file that is ABOUT the SQLite file (its bytes, its snapshot, its PRAGMAs)
# — never a file that fails on Postgres for a reason the app would meet in
# production: that is a bug to fix, not a file to list.
PG_MODE_SKIP_FILES = {
    "test_query_plans.py": "every test reads SQLite's EXPLAIN QUERY PLAN (Postgres's plans are its own; "
                           "the indexes themselves are copied by pg_copy)",
}


def _quarantine_problems(items):
    """[nodeid: what is wrong] for every quarantined test whose mark does
    not say why and since when: `@pytest.mark.quarantine(reason="…",
    since="10/27/26")` (blind re-audit 10/27/26 B1 F16). scripts/promote.py
    accepts a quarantined failure only while the mark is younger than
    promote.QUARANTINE_MAX_DAYS — a quarantine is a deadline, not a home."""
    from datetime import datetime as _datetime
    bad = []
    for item in items:
        for mark in item.iter_markers(name="quarantine"):
            reason = str(mark.kwargs.get("reason") or "").strip()
            since = str(mark.kwargs.get("since") or "").strip()
            try:
                _datetime.strptime(since, "%m/%d/%y")
                dated = True
            except ValueError:
                dated = False
            if not reason or not dated:
                bad.append(f"{item.nodeid}: quarantine needs reason=\"…\" and since=\"M/D/YY\" "
                           f"(got reason={reason!r}, since={since!r})")
    return bad


def pytest_collection_modifyitems(config, items):
    bad = _quarantine_problems(items)
    if bad:
        raise pytest.UsageError("A quarantine mark must say why and since when:\n  " + "\n  ".join(bad[:20]))
    if not _PG_BASE:
        return
    skip = pytest.mark.skip(reason="reads the SQLite file itself; the Postgres mode routes the database elsewhere")
    for item in items:
        name = os.path.basename(str(getattr(item, "fspath", "") or ""))
        if name in PG_MODE_SKIP_FILES:
            item.add_marker(pytest.mark.skip(reason="Postgres mode: " + PG_MODE_SKIP_FILES[name]))
        elif "sqlite_only" in item.keywords:
            item.add_marker(skip)


class _TestPgCursor:
    """A db_dialect cursor whose rows come back as the test's connection
    asked for them: tuples with no row_factory (sqlite3's default), name-
    and-position rows for sqlite3.Row, or what any other factory builds."""

    def __init__(self, cur, conn):
        self._cur, self._conn = cur, conn

    def _shape(self, row):
        if row is None:
            return None
        rf = self._conn.row_factory
        if rf is None:
            return tuple(row)
        if rf is sqlite3.Row:
            return row
        return rf(self, tuple(row))

    def fetchone(self):
        return self._shape(self._cur.fetchone())

    def fetchall(self):
        return [self._shape(r) for r in self._cur.fetchall()]

    def fetchmany(self, size=1):
        return [self._shape(r) for r in self._cur.fetchmany(size)]

    def __iter__(self):
        for r in self._cur:
            yield self._shape(r)

    def execute(self, sql, params=()):
        self._cur = self._conn._c.execute(sql, params)
        return self

    def executemany(self, sql, seq):
        self._cur = self._conn._c.executemany(sql, seq)
        return self

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _TestPgConnection:
    """What a test's sqlite3.connect(<its routed db_path>) returns in the
    Postgres mode: the db_dialect connection, rows shaped as sqlite3's."""

    def __init__(self, conn):
        object.__setattr__(self, "_c", conn)
        object.__setattr__(self, "row_factory", None)

    def execute(self, sql, params=()):
        return _TestPgCursor(self._c.execute(sql, params), self)

    def executemany(self, sql, seq):
        return _TestPgCursor(self._c.executemany(sql, seq), self)

    def cursor(self, *a, **k):
        return _TestPgCursor(self._c.cursor(), self)

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        return self._c.__exit__(et, ev, tb)

    def __getattr__(self, name):
        return getattr(self._c, name)

    def __setattr__(self, name, value):
        if name == "row_factory":
            object.__setattr__(self, name, value)
        else:
            setattr(self._c, name, value)


if _PG_BASE:
    import sqlite3
    _real_sqlite_connect = sqlite3.connect
    _TESTS_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep

    def _pg_aware_connect(database, *a, **k):
        """sqlite3.connect, except that a TEST's connect to a database file
        the Postgres mode routes is that Postgres database."""
        try:
            caller = sys._getframe(1).f_code.co_filename
        except ValueError:
            caller = ""
        if caller.startswith(_TESTS_DIR) and isinstance(database, (str, os.PathLike)):
            import models
            url = models._PG_ROUTES.get(os.fspath(database))
            if url:
                import db_dialect
                return _TestPgConnection(db_dialect.connect(url))
        return _real_sqlite_connect(database, *a, **k)

    sqlite3.connect = _pg_aware_connect


def _pg_copy_into(sqlite_path, name):
    """The SQLite file at `sqlite_path` copied into the Postgres database
    `name` the way production's was (pg_copy: partitioned fact tables)."""
    import psycopg
    import pg_copy
    # Autocommit: the whole schema in one transaction holds a lock per
    # table and index, and eight workers building theirs at once ran the
    # server out of its shared lock table.
    with psycopg.connect(_pg_url(name), connect_timeout=10, autocommit=True) as dst:
        rep = pg_copy.copy_database(sqlite_path, dst)
    # A key to a table the per-test template does not build (users: the
    # auth chain, which a test runs when it needs it) waits for the boot,
    # as on a fresh Postgres; any other refusal is the copy's bug.
    bad = [r for r in rep["foreign_keys"]["refused"] if "does not exist" not in r["error"]]
    if bad:
        raise RuntimeError(f"Postgres mode: the template's foreign keys were refused: {bad[:2]}")


# ── The statement recorder (blind re-audit 10/20/26 R9-02) ──────────────────
# CAVNAR_RECORD_SQL=<dir> (the SQLite run; CI's `test` job sets it) records
# every distinct statement the APP executes through a models connection —
# the run-time-built ones included (f-strings, joined IN lists), which the
# source audit cannot PREPARE — into <dir>/sql-<pid>.jsonl at the session's
# end. scripts/pg_sql_audit.py --recorded <dir> translates and PREPAREs each
# on Postgres (CI's `postgres-recorded-sql` job). A test's own SQL is left
# out: it is not the app's.
_RECORD_DIR = (os.getenv("CAVNAR_RECORD_SQL") or "").strip()
_RECORDED = {}


def _record_statement(sql):
    import re
    try:
        f = sys._getframe(2)
        models_file = sys.modules["models"].__file__
        while f is not None and f.f_code.co_filename == models_file and \
                f.f_code.co_name in ("execute", "executemany", "executescript"):
            f = f.f_back
        if f is None or f.f_code.co_filename.startswith(os.path.dirname(os.path.abspath(__file__)) + os.sep):
            return
        # The text as run (a `--` comment ends at its line, so lines stay);
        # one shape per IN list, whatever its length.
        text = str(sql or "").strip()
        text = re.sub(r"(?i)\bIN\s*\(\s*\?(?:\s*,\s*\?)+\s*\)", "IN (?)", text)
        if text and text not in _RECORDED and len(_RECORDED) < 200000:
            _RECORDED[text] = f"{os.path.relpath(f.f_code.co_filename, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))}:{f.f_lineno}"
    except Exception:
        pass


if _RECORD_DIR and not _PG_BASE:
    import models as _models_rec
    _real_note_write = _models_rec._note_write

    def _note_write_recording(sql):
        _record_statement(sql)
        return _real_note_write(sql)

    _models_rec._note_write = _note_write_recording


def _unreported_blocks():
    """The blocked connections no test's report carried: made during
    collection, or in a module or session fixture."""
    return [hit for i, hit in enumerate(_NET_BLOCKED) if i not in _NET_REPORTED]


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    """(xdist controller) A worker's blocked connections that no test
    reported (blind re-audit 10/27/26 B1 F12): they stayed in that worker's
    process, and the run passed."""
    _BLOCKED_ON_WORKERS.extend(tuple(x) for x in (getattr(node, "workeroutput", {}) or {}).get("cavnar_net_blocked", ()))


def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput"):
        session.config.workeroutput["cavnar_net_blocked"] = _unreported_blocks()
    _enforce_expected_skips(session)
    # With CAVNAR_ENFORCE_SKIPS=1 (CI), a test that tried to reach the
    # network fails the run too: the guard refused it, so the test passed
    # on whatever the refusal made of it — two did, reaching RPOWER's and
    # Google's real APIs on every run until the guard listed them (audit #25).
    # So does an attempt outside any test (collection, a module or session
    # fixture), on the controller or handed over by a worker.
    if (os.getenv("CAVNAR_ENFORCE_SKIPS") or "").strip() == "1" and \
            (_BLOCKED_BY_TEST or _BLOCKED_ON_WORKERS or (_NET_BLOCKED and not hasattr(session.config, "workerinput"))):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if not (_RECORD_DIR and _RECORDED):
        return
    import json
    os.makedirs(_RECORD_DIR, exist_ok=True)
    with open(os.path.join(_RECORD_DIR, f"sql-{os.getpid()}.jsonl"), "w", encoding="utf-8") as out:
        for text, at in _RECORDED.items():
            out.write(json.dumps({"sql": text, "at": at}) + "\n")


# ── Skips that say why, and only the expected ones (audit #25 A01-18) ───────
# Every skip's reason is collected (on the xdist controller too: reports
# arrive there). With CAVNAR_ENFORCE_SKIPS=1 (CI) a skip whose reason
# matches no line of tests/expected_skips.txt fails the run: a data- or
# permission-conditional skip there is a regression reporting itself as
# SKIPPED. CI also passes -rs, so every reason is printed.
_SKIPS = []
_BLOCKED_BY_TEST = []
_BLOCKED_ON_WORKERS = []
_NET_REPORTED = set()       # indexes into _NET_BLOCKED a test's report carried


def _skip_reason(report):
    lr = report.longrepr
    if isinstance(lr, tuple) and len(lr) == 3:
        return str(lr[2]).replace("Skipped: ", "", 1)
    return str(lr or "")


def pytest_collectreport(report):
    """A module-level skip (`pytest.importorskip` at the top of a file,
    `pytest.skip(..., allow_module_level=True)`) is a collection report, not
    a test report: it turned a whole file into "1 skipped" and passed the
    enforcement (blind re-audit 10/27/26 B1 F11). Under xdist the workers
    forward skipped collection reports to the controller, which runs this."""
    if report.skipped:
        _SKIPS.append((report.nodeid or "(collection)", _skip_reason(report)))


def pytest_runtest_logreport(report):
    if report.skipped and not hasattr(report, "wasxfail"):
        _SKIPS.append((report.nodeid, _skip_reason(report)))
    for name, value in getattr(report, "user_properties", ()) or ():
        if name == "network_blocked" and report.when == "teardown":
            _BLOCKED_BY_TEST.extend((report.nodeid, host) for host in value)


def expected_skip_patterns():
    import re
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "expected_skips.txt")
    with open(path, encoding="utf-8") as fh:
        return [re.compile(line.strip()) for line in fh
                if line.strip() and not line.lstrip().startswith("#")]


def unexpected_skips(skips):
    """The (nodeid, reason) pairs no expected_skips.txt pattern explains."""
    pats = expected_skip_patterns()
    return [(nid, why) for nid, why in skips if not any(p.search(why) for p in pats)]


def _enforce_expected_skips(session):
    if hasattr(session.config, "workerinput") or os.getenv("CAVNAR_ENFORCE_SKIPS") != "1":
        return
    bad = unexpected_skips(_SKIPS)
    if bad:
        tr = session.config.pluginmanager.get_plugin("terminalreporter")
        lines = ["UNEXPECTED SKIPS (no line of tests/expected_skips.txt explains them):"]
        lines += [f"  {nid}: {why}" for nid, why in bad[:50]]
        if tr is not None:
            tr.write_line("\n".join(lines), red=True, bold=True)
        else:
            print("\n".join(lines))
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _network_attempts_reported(request):
    """Hands the network guard's blocked connections for this test to its
    report, so the xdist controller can list them at the end of the run."""
    start = len(_NET_BLOCKED)
    yield
    end = len(_NET_BLOCKED)
    hits = [host for _where, host in _NET_BLOCKED[start:end]]
    if hits:
        _NET_REPORTED.update(range(start, end))
        request.node.user_properties.append(("network_blocked", hits))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    blocked = (_BLOCKED_BY_TEST + _BLOCKED_ON_WORKERS) or [(where, host) for where, host in _NET_BLOCKED]
    if blocked:
        terminalreporter.section("network connections blocked (stub these calls)", yellow=True)
        for nid, host in blocked[:40]:
            terminalreporter.write_line(f"{nid} -> {host}")
    # A change to the database layer, checked on SQLite only (audit #25
    # A04-02): production runs on Postgres, and models.get_conn / db_dialect
    # are where the two differ.
    if not _PG_BASE and not os.getenv("CI"):
        touched = _changed_files()
        hit = sorted(f for f in touched if os.path.basename(f) in ("models.py", "db_dialect.py"))
        if hit:
            terminalreporter.section("Postgres not exercised", yellow=True)
            terminalreporter.write_line(
                f"This change touches {', '.join(hit)} and CAVNAR_TEST_PG_URL is unset, so none of these tests "
                "ran on Postgres (production's database). Run the files that cover it again with "
                "CAVNAR_TEST_PG_URL=postgresql://postgres@127.0.0.1:55433/postgres (Postgres 16, as production; TESTING.md, On Postgres).")


def _changed_files():
    """Files changed in the working tree or committed ahead of the upstream
    branch — empty when git is unavailable."""
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = set()
    for args in (["git", "diff", "--name-only", "HEAD"], ["git", "diff", "--name-only", "@{upstream}...HEAD"]):
        try:
            r = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=5)
        except Exception:
            continue
        if r.returncode == 0:
            out.update(line.strip() for line in r.stdout.splitlines() if line.strip())
    return out


def _build_default_schema():
    """Give the throwaway default database the same schema hosted_dashboard
    builds at boot, so a call that passes no db_path meets real tables —
    as it did when the default was the developer's reviews.db, but now
    empty of anyone's data. hosted_dashboard.py's own boot schema
    (models.boot_schema — release-safety audit 10/26/26 A05-07)."""
    import models
    models.boot_schema()


_build_default_schema()
if _PG_BASE:
    # The default database: built on SQLite above, copied, then the boot
    # again on Postgres — the order production's database had.
    import models as _models_pg
    _PG_DEFAULT = _pg_new_database()
    import atexit as _atexit
    _atexit.register(_pg_drop, _PG_DEFAULT)
    _pg_copy_into(_models_pg.DB_PATH, _PG_DEFAULT)
    _models_pg._PG_ROUTES[_models_pg.DB_PATH] = _pg_url(_PG_DEFAULT)
    _build_default_schema()


@pytest.fixture(autouse=True)
def _reset_ai_rate_limiter():
    """The AI rate limiter's window is keyed by restaurant_id, and every test
    gets a fresh database whose ids start at 1 — so one test's calls counted
    against the next test's budget and a route would 429 only when the whole
    file ran, never in isolation. Since fix round G the window lives in the
    database (ai_rate_events) with ai_utils._ai_call_log as its fallback,
    and the process also remembers breakers, budget totals and warning
    claims; ai_utils.reset_process_state clears all of it, in the default
    database too (a test that redirects nothing writes there). The default
    database's ledger is emptied as well, so Places and AI ceilings never
    trip on spend earlier tests left behind."""
    import ai_utils
    import models

    def _reset():
        ai_utils.reset_process_state(models.DB_PATH)
        try:
            conn = models.get_conn(models.DB_PATH)
            try:
                conn.execute("DELETE FROM ai_usage")
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
    _reset()
    yield
    ai_utils.reset_process_state()


@pytest.fixture(autouse=True)
def _reset_request_limits(monkeypatch):
    """request_limits keeps its windows and buckets in memory, keyed by IP,
    login and restaurant id — which every test reuses (scale fix 10/19/26,
    SEC-07/08). Cleared per test; the per-login request share is off unless
    a test turns it on (REQUEST_LIMITS=1), so a test that makes a hundred
    calls as one login is not throttled by the one before it."""
    import request_limits
    request_limits.reset()
    if "REQUEST_LIMITS" not in os.environ:
        monkeypatch.setenv("REQUEST_LIMITS", "0")
    yield
    request_limits.reset()


@pytest.fixture(autouse=True)
def _reset_rpower_caches():
    """rpower caches its lists (sales types, menu, void reasons, people,
    tables and rooms, the closed-day answer) per restaurant id for minutes;
    every test's fresh database reuses ids, so one test's stubbed list would
    be another's answer.

    RPOWER's requests go through a per-thread requests.Session (audit #23
    A1-12); under test they go through the `requests` module's own get/post,
    so a test's `monkeypatch.setattr("requests.get", ...)` is what answers
    (tests/test_audit23_f1.py exercises the real session)."""
    import requests
    import rpower
    rpower.clear_caches()
    real_session = rpower._session
    rpower._session = lambda: requests
    yield
    rpower._session = real_session
    rpower.clear_caches()


@pytest.fixture(autouse=True)
def _reset_labor_note_cache():
    """labor._NOTE_CACHE is keyed by restaurant id and data fingerprint; a
    fresh database per test reuses ids, so one test's stubbed note would be
    another's answer."""
    import labor
    labor._NOTE_CACHE.clear()
    yield
    labor._NOTE_CACHE.clear()


@pytest.fixture(autouse=True)
def _reset_restaurant_context_l1():
    """restaurant_context's L1 (60 s, in-process) is keyed by restaurant id,
    section and viewer scope; a fresh database per test reuses ids, and two
    empty databases give the same version, so one test's section text was
    another's (the h8 labor forecast read an earlier test's DATA STATE)."""
    import restaurant_context
    restaurant_context.invalidate()
    yield
    restaurant_context.invalidate()


@pytest.fixture(autouse=True)
def _reset_tenant_names_cache():
    """models.other_tenant_names caches every restaurant's name per process
    (the Response Validation Layer's T1 list); one test's restaurants must
    not be another test's "other tenants"."""
    import models
    models._invalidate_tenant_names()
    yield
    models._invalidate_tenant_names()


@pytest.fixture(autouse=True)
def _reset_home_brief_cache():
    """home_brief caches its whole payload per restaurant_id for 60s, and
    every test's fresh database starts its ids at 1 — so the second Home
    test in a file was served the first one's brief. Harmless in
    production (the TTL is the point); a silent cross-test leak here."""
    import home_brief
    home_brief.invalidate()
    yield
    home_brief.invalidate()


@pytest.fixture(autouse=True)
def _reset_prepublish_cache():
    """prepublish caches each week's review for PREPUBLISH_CACHE_SECONDS,
    keyed by the week's version and the inputs/dismissal stamps — the same
    on every test's fresh database, so one test's review (a withheld range)
    was the next test's answer. Cleared only where prepublish is loaded."""
    def _clear():
        pp = sys.modules.get("prepublish")
        if pp is not None and hasattr(pp, "invalidate"):
            pp.invalidate()
    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _reset_requirement_model_cache():
    """schedule_engine caches each restaurant's work-driven requirement model
    (staffing_model, the half-hour curve, the elasticity moves) for 15
    minutes per database file; a test on the shared default database reuses
    restaurant ids, so one test's model would be another's. Cleared only
    where the engine is loaded (never imported here for a test that does not
    use it)."""
    def _clear():
        se = sys.modules.get("schedule_engine")
        if se is not None and hasattr(se, "_REQUIREMENT_MODEL_CACHE"):
            se._REQUIREMENT_MODEL_CACHE.clear()
    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _reset_usage_schema_flag():
    """ai_utils remembers, per process, which databases it has already created
    the usage table in — so the DDL is off the hot path of every AI call.
    It is keyed by db_path, and a test that points at the default ./reviews.db
    would otherwise hand its "already done" to the next one and read a table
    that test had just recreated. Same shape as the three resets above."""
    import ai_utils
    ai_utils._usage_schema_ready.clear()
    yield
    ai_utils._usage_schema_ready.clear()


@pytest.fixture(autouse=True)
def _reset_ask_context_cache():
    """Identical hazard to _reset_home_brief_cache: ask_cavnar.build_context
    caches the assembled snapshot per restaurant_id for 60s, and every test's
    fresh database starts its ids at 1, so one test's context would be served
    to the next. The TTL is the point in production, where ids are unique."""
    import ask_cavnar
    ask_cavnar.invalidate_context()
    yield
    ask_cavnar.invalidate_context()


@pytest.fixture(autouse=True)
def _reset_staff_answer_cache():
    """staff_knowledge keeps a house-rules answer per (restaurant, the exact
    source lines, the question) — a day for an answer, an hour for "the
    lines don't cover it" (AI cost audit 10/7/26 re-audit #3) — and every
    test's fresh database starts its ids at 1 with the same fixture rules,
    so one test's answer would be served to the next. Same shape as the
    resets above."""
    import sys as _sys
    sk = _sys.modules.get("staff_knowledge")
    if sk is not None:
        sk.clear_answer_cache()
    yield
    sk = _sys.modules.get("staff_knowledge")
    if sk is not None:
        sk.clear_answer_cache()


@pytest.fixture(autouse=True)
def _reset_studio_cache():
    """schedule_engine keeps a Studio re-score's inputs per restaurant and
    week for two minutes (studio_prepared, schedule audit 10/3/26 P-25), and
    every test's fresh database starts its ids at 1 — so one test's inputs
    would judge the next one's edit. Same shape as the resets above."""
    import sys as _sys
    se = _sys.modules.get("schedule_engine")
    if se is not None:
        se.studio_invalidate()
    # And the Crew stage's kept matrix (crew_matrix.cached_build, SCALE-27).
    cm = _sys.modules.get("crew_matrix")
    if cm is not None:
        cm.invalidate_cache()
    yield
    se = _sys.modules.get("schedule_engine")
    if se is not None:
        se.studio_invalidate()
    cm = _sys.modules.get("crew_matrix")
    if cm is not None:
        cm.invalidate_cache()


@pytest.fixture(autouse=True)
def _reset_admin_rate_limits():
    """security's per-session /admin ceiling is a process-global window, and
    tests that fake an admin without a session share one key ("user:<id>"),
    so a long run would 429 whichever admin test came after the 240th
    request in a minute. Same shape as the resets above."""
    import security
    security.reset_admin_rate_limits()
    yield
    security.reset_admin_rate_limits()


@pytest.fixture(autouse=True)
def _fixture_logins_skip_the_password_policy(monkeypatch):
    """auth.create_user holds every typed password to the password policy
    (8+ characters, not breached — fix round A, #86). The suite's fixtures
    make throwaway logins with passwords like "pw" in ~300 places; the
    policy's own tests (tests/test_fix_a_*.py) switch it back on with
    monkeypatch.setattr(auth, "ENFORCE_PASSWORD_POLICY", True)."""
    import auth
    monkeypatch.setattr(auth, "ENFORCE_PASSWORD_POLICY", False)
    yield


@pytest.fixture(autouse=True)
def _no_real_email_or_sms(monkeypatch):
    """The suite must never reach Resend, Twilio, Stripe, DocuSign, Anthropic,
    Google Places or Perplexity.

    It did: the full run sent ~75 real 2FA and notification emails to the
    tests' fake addresses through the production Resend key and exhausted
    the account's daily quota the night before a client meeting. Blank the
    keys so every send short-circuits, and trip loudly if anything still
    tries the network. Tests that exercise delivery stub `requests.post`
    and `emails._resend_key` themselves, which overrides this."""
    import requests
    import emails
    monkeypatch.setenv("RESEND_API_KEY", "")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    monkeypatch.setattr(emails, "_resend_key", lambda: "")
    # Anthropic, Places and Perplexity too: the hourly provider probes
    # (provider_health.run_probes, a registered job the registry test runs)
    # call them whenever a key is in the environment — and scheduler.py
    # loads the checkout's .env at import.
    blocked = ("api.resend.com", "api.twilio.com", "api.stripe.com", "docusign.net", "docusign.com",
               "api.anthropic.com", "maps.googleapis.com", "places.googleapis.com", "api.perplexity.ai")
    real_post, real_request = requests.post, requests.request

    def guard(fn):
        def wrapped(url, *a, **k):
            if any(b in str(url) for b in blocked):
                raise RuntimeError("test tried to reach %s — stub it" % url)
            return fn(url, *a, **k)
        return wrapped
    monkeypatch.setattr(requests, "post", guard(real_post))
    monkeypatch.setattr(requests, "request", guard(real_request))
    # requests.Session.request is what the Resend SDK (and any Session user)
    # goes through — the module-level functions above never see it.
    real_session_request = requests.Session.request

    def session_guard(self, method, url, *a, **k):
        if any(b in str(url) for b in blocked):
            raise RuntimeError("test tried to reach %s — stub it" % url)
        return real_session_request(self, method, url, *a, **k)
    monkeypatch.setattr(requests.Session, "request", session_guard)
    # The Resend SDK's own entry points, in case a call site bypasses
    # emails.deliver (admin_routes and webhook_routes still use the SDK).
    try:
        import resend

        def sdk_blocked(*a, **k):
            raise RuntimeError("test tried to send through the Resend SDK — stub it")
        monkeypatch.setattr(resend.Emails, "send", staticmethod(sdk_blocked), raising=False)
        monkeypatch.setattr(resend, "api_key", "", raising=False)
    except Exception:
        pass
    yield


class LiveAnthropicCall(BaseException):
    """A test reached the real Anthropic API. A BaseException on purpose:
    the SDK turns any Exception from its transport into APIConnectionError
    and ai_utils turns that into a quiet fallback, so a live call (with the
    checkout's real key, which scheduler.py loads from .env) would pass
    unseen, and spend."""


@pytest.fixture(autouse=True)
def _owner_rules_read_by_no_model(monkeypatch):
    """A generation reads the owner's words with a model (owner_rules.prepare,
    scheduler audit 10/10/26 RM-9). In the suite the model reads nothing
    unless a test hands owner_rules its own stub (`call`) or sets this back."""
    monkeypatch.setenv("OWNER_RULES_READ", "0")
    # Nor does it write the note from the finished week (schedule_narrative);
    # the generation's own bullets, checked against the final rows, stand.
    monkeypatch.setenv("SCHEDULE_NOTE_WRITE", "0")


@pytest.fixture(autouse=True)
def _no_live_anthropic_calls(monkeypatch):
    """Any request the Anthropic SDK sends fails the test (employee audit B7
    handoff). The guard sits on httpx's transport — what the SDK sends
    through — so a test that stubs ai_utils.get_client, create_with_retry or
    the client's messages.create never reaches it and is unaffected. With no
    key the SDK refuses before sending, so CI never trips it; a checkout with
    a real key does, which is the point. The attempt is also recorded and
    failed at teardown, in case a caller swallowed even this."""
    import httpx
    hits = []

    def _blocked(url):
        host = str(getattr(url, "host", "") or "")
        return host == "anthropic.com" or host.endswith(".anthropic.com")

    real_send, real_async_send = httpx.Client.send, httpx.AsyncClient.send

    def send(self, request, *a, **k):
        if _blocked(request.url):
            hits.append(str(request.url))
            raise LiveAnthropicCall(f"test tried to call the Anthropic API ({request.url}) — stub the model call")
        return real_send(self, request, *a, **k)

    async def async_send(self, request, *a, **k):
        if _blocked(request.url):
            hits.append(str(request.url))
            raise LiveAnthropicCall(f"test tried to call the Anthropic API ({request.url}) — stub the model call")
        return await real_async_send(self, request, *a, **k)

    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
    yield hits                     # the guard's own test clears what it tripped on purpose
    if hits:
        pytest.fail(f"a live Anthropic call was attempted: {hits[:3]}", pytrace=False)


@pytest.fixture
def reviewer_passes(monkeypatch):
    """The Haiku rubric (ai_reviewer.review_text) as a reviewer that ran and
    passed. With no model in the suite the real one cannot run, and since
    the 10/7/26 re-audit (#1) the auto-approve rule holds a reply a gate
    could not read — so a test about the rule itself, not its gate, takes
    this. Records what it read."""
    import ai_orchestrator
    import ai_reviewer
    calls = []

    def review_text(kind, draft, restaurant_id=None, context="", mode="haiku_gate"):
        calls.append({"kind": kind, "draft": draft, "mode": mode})
        return ai_orchestrator.Verdict(ok=True, score=0.9, label="pass")
    monkeypatch.setattr(ai_reviewer, "review_text", review_text)
    return calls


@pytest.fixture(autouse=True)
def _send_gate_reviewer_offline(monkeypatch):
    """Every guest text and email send now passes the Haiku gate on its
    final text (ai_reviewer.gate_send, re-audit 10/7/26 #8). In a test it
    answers as a reviewer that could not run — which passes, as in
    production — so no send test reaches the API with a checkout's real
    key. The gate's own tests replace ai_reviewer._gate_review."""
    import ai_reviewer
    monkeypatch.setattr(ai_reviewer, "_gate_review",
                        lambda kind, text, restaurant_id, context:
                        ai_reviewer.orch.Verdict.passed(label="reviewer_unavailable"))


_DB_TEMPLATE = None
_PG_TEMPLATE = None


def _boot_schema(path):
    """hosted_dashboard's whole boot schema on `path` (models.boot_schema:
    init_db, ensure_columns and the init_* chain — schema only)."""
    import models
    models.boot_schema(path)


def _db_template():
    """One database per test process built by the real boot migrations —
    init_db() AND ensure_columns(), the two separate paths hosted_dashboard
    runs (skipping ensure_columns once meant tests had a schema production
    never has) — which every db_path copies. Building it per test cost
    ~230 ms each, most of a full run; a copy costs well under 1 ms and is
    byte-for-byte what the migrations produce."""
    global _DB_TEMPLATE
    if _DB_TEMPLATE is None:
        import sqlite3
        path = os.path.join(_tempfile.mkdtemp(prefix="cavnar-test-template-"), "template.db")
        # And the rest of hosted_dashboard's boot schema (tables only, no
        # seeds): most files ran init_auth and friends in a per-test fixture,
        # and on a bare copy that was the users/sessions/memberships tables
        # plus ~40 ALTERs every test — about a third of the suite's setup
        # time (10/20/26). Here they are built once; a per-test call finds
        # them and does next to nothing.
        _boot_schema(path)
        if _PG_BASE:
            # Copied (partitioned, as production's) then booted again on
            # Postgres; the SQLite file stays the empty-of-rows schema.
            import models
            global _PG_TEMPLATE
            _PG_TEMPLATE = _pg_new_database()
            import atexit
            atexit.register(_pg_drop, _PG_TEMPLATE)
            _pg_copy_into(path, _PG_TEMPLATE)
            models._PG_ROUTES[path] = _pg_url(_PG_TEMPLATE)
            _boot_schema(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        _DB_TEMPLATE = path
    return _DB_TEMPLATE


@pytest.fixture
def db_path(tmp_path):
    import shutil
    path = str(tmp_path / "test_reviews.db")
    shutil.copyfile(_db_template(), path)
    if not _PG_BASE:
        yield path
        return
    import models
    name = _pg_new_database(template=_PG_TEMPLATE)
    models._PG_ROUTES[path] = _pg_url(name)
    try:
        yield path
    finally:
        models._PG_ROUTES.pop(path, None)
        _pg_drop(name)


def stamp_scheduler_heartbeat(db_path, minutes_ago, loop_minutes_ago=None, running=None, running_minutes=None):
    """Make the scheduler loop's heartbeat `minutes_ago` old in `db_path`.

    It lives in its own table, scheduler_heartbeat (fix round D, #4), written
    only by the loop — the one source /health, the platform SLA check, the
    public status page and the console read. It used to be
    service_status.updated_at, which every status write reset; stamping that
    column now changes nothing, which is how a /health test and a console
    test kept passing against a heartbeat nobody read. `loop_minutes_ago` is
    the last COMPLETED tick (defaults to the beat); `running` /
    `running_minutes` put the loop inside a job for the watchdog (#121)."""
    import sqlite3
    loop = minutes_ago if loop_minutes_ago is None else loop_minutes_ago
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO scheduler_heartbeat (id) VALUES (1)")
        conn.execute("UPDATE scheduler_heartbeat SET beat_at=datetime('now', ?), loop_completed_at=datetime('now', ?), "
                     "running_job=?, running_since=CASE WHEN ? IS NULL THEN NULL ELSE datetime('now', ?) END "
                     "WHERE id=1",
                     (f"-{float(minutes_ago)} minutes", f"-{float(loop)} minutes", running, running,
                      f"-{float(running_minutes or 0)} minutes"))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def scheduler_heartbeat(db_path):
    """stamp_scheduler_heartbeat bound to this test's database:
    `scheduler_heartbeat(60)` is a scheduler that died an hour ago;
    pass db_path= to stamp another file."""
    def _stamp(minutes_ago, loop_minutes_ago=None, running=None, running_minutes=None, db_path=db_path):
        stamp_scheduler_heartbeat(db_path, minutes_ago, loop_minutes_ago=loop_minutes_ago, running=running,
                                  running_minutes=running_minutes)
    return _stamp


@pytest.fixture
def two_restaurants(db_path):
    """Two restaurants with one review each — the minimum world in which
    cross-tenant bugs (IDOR) are observable."""
    rid_a = create_restaurant(Restaurant(name="Alpha Cafe", owner_email="a@x.com"), db_path=db_path)
    rid_b = create_restaurant(Restaurant(name="Bravo Bistro", owner_email="b@x.com"), db_path=db_path)
    save_reviews([
        Review(restaurant_id=rid_a, platform="google", external_id="ext-a1",
               author="Ann", rating=2, text="Cold food and a long wait."),
        Review(restaurant_id=rid_b, platform="google", external_id="ext-b1",
               author="Bob", rating=5, text="Fantastic dinner, will be back."),
    ], db_path=db_path)
    return {"db_path": db_path, "rid_a": rid_a, "rid_b": rid_b}




@pytest.fixture(autouse=True)
def _fresh_generation_slots():
    """A generation's slot is its job_queue row since blind re-audit
    10/11/26 ARCH-10 (the process-wide GenerationSlots are gone). What is
    still process-wide is the queue's drain flag: a test that ran
    job_queue.shutdown() must not leave every later test's consumer
    refusing to claim."""
    import job_queue
    job_queue._draining.clear()
    yield
    job_queue._draining.clear()


@pytest.fixture(autouse=True)
def _reset_process_caches():
    """Every module-level cache that registered with process_caches (release-
    safety audit 10/26/26, A04-13), reset before and after each test: ids
    restart at 1 in every test database, so a cache keyed by restaurant id
    hands one test's answer to the next. tests/test_process_caches.py keeps
    every `_*cache*` / `_*memo*` container registered. The per-cache resets
    above stay (they also reset state that is not a registered cache)."""
    import process_caches
    process_caches.clear_all()
    yield
    process_caches.clear_all()


@pytest.fixture(scope="module")
def module_db_path(tmp_path_factory):
    """db_path for a whole module: one copy of the boot-schema template that
    every test in the file shares (blind re-audit 10/27/26). The twin smoke's
    ~150 routes each copied a database, and on Postgres each copy is a
    template clone — 3.5 minutes of the gate's Postgres job. Only for a
    module whose tests keep to rows of their own (a restaurant per test)."""
    import shutil
    path = str(tmp_path_factory.mktemp("module-db") / "test_reviews.db")
    shutil.copyfile(_db_template(), path)
    if not _PG_BASE:
        yield path
        return
    import models
    name = _pg_new_database(template=_PG_TEMPLATE)
    models._PG_ROUTES[path] = _pg_url(name)
    try:
        yield path
    finally:
        models._PG_ROUTES.pop(path, None)
        _pg_drop(name)
