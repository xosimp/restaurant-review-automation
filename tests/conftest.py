"""Shared fixtures: every test gets a real, throwaway SQLite database built by
the same init_db() the app uses, so schema drift is caught here instead of in
production."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

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


def _pg_url(name):
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(_PG_BASE)
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))


def _pg_admin(sql):
    import psycopg
    with psycopg.connect(_PG_BASE, autocommit=True, connect_timeout=10) as c:
        c.execute(sql)


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
        _pg_admin(f"CREATE DATABASE {name} TEMPLATE {template}")
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


# Whole files that cannot run in the Postgres mode, and why. Every entry is
# a file that is ABOUT the SQLite file (its bytes, its snapshot, its PRAGMAs)
# — never a file that fails on Postgres for a reason the app would meet in
# production: that is a bug to fix, not a file to list.
PG_MODE_SKIP_FILES = {
    "test_query_plans.py": "every test reads SQLite's EXPLAIN QUERY PLAN (Postgres's plans are its own; "
                           "the indexes themselves are copied by pg_copy)",
}


def pytest_collection_modifyitems(config, items):
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


def pytest_sessionfinish(session, exitstatus):
    if not (_RECORD_DIR and _RECORDED):
        return
    import json
    os.makedirs(_RECORD_DIR, exist_ok=True)
    with open(os.path.join(_RECORD_DIR, f"sql-{os.getpid()}.jsonl"), "w", encoding="utf-8") as out:
        for text, at in _RECORDED.items():
            out.write(json.dumps({"sql": text, "at": at}) + "\n")


def _build_default_schema():
    """Give the throwaway default database the same schema hosted_dashboard
    builds at boot, so a call that passes no db_path meets real tables —
    as it did when the default was the developer's reviews.db, but now
    empty of anyone's data. Mirrors hosted_dashboard.py's module-level init."""
    import models
    from auth import init_auth
    from webhooks import init_webhooks
    from guest_marketing import init_guest_marketing
    from push import init_push
    from sales_audits import init_sales_audits
    import platform_monitor
    import provider_health
    init_db()
    init_auth()
    # F's telemetry tables and provider probe ledger, created at boot right
    # after init_auth (hosted_dashboard); the code tolerates their absence,
    # but a test on the default database should meet what production has.
    platform_monitor.init_platform_tables()
    provider_health.init_provider_health()
    models.init_staff_notes()
    models.init_staff_availability()
    ensure_columns()
    for fn in ("init_email_log", "init_onboarding_emails", "init_two_fa_backup_codes",
               "init_competitor_snapshots", "init_ai_visibility_queries", "init_staff_capabilities",
               "init_shift_profiles", "init_capability_changes", "init_ask_memory"):
        getattr(models, fn)()
    init_webhooks()
    init_guest_marketing()
    init_push()
    init_sales_audits()


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
    """hosted_dashboard's init_* chain after init_db, on `path` (schema only)."""
    import auth
    import guest_marketing
    import models
    import platform_monitor
    import provider_health
    import push
    import sales_audits
    import webhooks
    auth.init_auth(db_path=path)
    models.init_two_fa_backup_codes(db_path=path)
    models.init_email_log(db_path=path)
    push.init_push(db_path=path)
    webhooks.init_webhooks(db_path=path)
    guest_marketing.init_guest_marketing(path)
    sales_audits.init_sales_audits(db_path=path)
    platform_monitor.init_platform_tables(db_path=path)
    provider_health.init_provider_health(db_path=path)


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
        init_db(db_path=path)
        ensure_columns(db_path=path)
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
            init_db(db_path=path)
            ensure_columns(db_path=path)
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
