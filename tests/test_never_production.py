"""No test, and no local app, can reach production (audit #25 A01-06, A01-17,
A01-18, R16).

- The suite refuses to start inside a Railway environment and drops any
  platform database URL before anything is imported (tests/conftest.py).
- models.get_conn / db_dialect ignore DATABASE_URL off Railway unless
  ALLOW_LOCAL_PG=1 (config.platform_database_url_allowed) — the same Railway
  markers config.scheduling_allowed reads, so production keeps its database.
- The network guard is an allowlist at the socket: only this machine (and the
  scratch Postgres) may be reached.
- A skip whose reason tests/expected_skips.txt does not explain fails CI.
"""
import os
import socket
import subprocess
import sys

import pytest

import config
import db_dialect
import models

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.contract


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("RAILWAY_") and k not in ("DATABASE_URL", "SHADOW_DATABASE_URL", "ALLOW_LOCAL_PG",
                                                         "CAVNAR_TEST_PG_URL", "PYTEST_XDIST_WORKER",
                                                         "PYTEST_CURRENT_TEST")
           and not k.endswith(("_API_KEY", "_AUTH_TOKEN", "_SECRET_KEY"))}
    env.update(extra)
    return env


def _pytest(env, *args):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
                           "--collect-only", *args], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=120)


# ── the suite itself ─────────────────────────────────────────────────────────

def test_the_suite_refuses_to_start_inside_a_railway_environment():
    r = _pytest(_clean_env(RAILWAY_ENVIRONMENT="production"), "tests/test_ci_gate_config.py")
    assert r.returncode == 3, r.stdout[-800:] + r.stderr[-800:]
    assert "Refusing to run the test suite inside a Railway environment" in (r.stdout + r.stderr)


def test_the_suite_refuses_a_scratch_postgres_that_is_not_on_this_machine():
    r = _pytest(_clean_env(CAVNAR_TEST_PG_URL="postgresql://postgres@db.example.com:5432/postgres"),
                "tests/test_ci_gate_config.py")
    assert r.returncode == 3, r.stdout[-800:] + r.stderr[-800:]
    assert "names a non-local server (db.example.com)" in (r.stdout + r.stderr)


def test_a_platform_database_url_never_survives_into_the_suite():
    # conftest pops them before models is imported; an exported URL from a
    # psql session never reaches models.get_conn.
    for var in ("DATABASE_URL", "SHADOW_DATABASE_URL", "ALLOW_LOCAL_PG"):
        assert var not in os.environ, var


def test_a_run_with_production_database_url_exported_still_never_routes_to_it():
    # The whole chain in a fresh process, as a developer's shell would start
    # it: conftest drops the URL, and the session's default database is the
    # throwaway SQLite volume.
    probe = os.path.join(ROOT, "tests", "test_ci_gate_config.py")
    r = subprocess.run([sys.executable, "-c",
                        "import os, sys; sys.argv=['pytest']; import pytest; "
                        "rc = pytest.main(['-q', '-p', 'no:cacheprovider', '-p', 'no:xdist', '--collect-only', "
                        f"{probe!r}]); "
                        "import models; print('ROUTE', models._pg_url_for(models.DB_PATH)); "
                        "print('ENV', os.environ.get('DATABASE_URL'))"],
                       cwd=ROOT, env=_clean_env(DATABASE_URL="postgresql://prod-user:pw@prod.example.com/railway"),
                       capture_output=True, text=True, timeout=120)
    out = r.stdout
    assert "ROUTE None" in out and "ENV None" in out, out[-1200:] + r.stderr[-800:]


# ── the app's own guard: DATABASE_URL only on Railway ───────────────────────

def test_database_url_is_ignored_off_railway(monkeypatch):
    for v in config.RAILWAY_MARKERS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod-user:pw@prod.example.com/railway")
    monkeypatch.setenv("SHADOW_DATABASE_URL", "postgresql://prod-user:pw@prod.example.com/shadow")
    assert models._db_backend() == "sqlite"
    assert models._pg_url_for(models.DB_PATH) is None
    assert db_dialect.backend() == "sqlite"
    assert db_dialect.database_url() is None and db_dialect.database_url("SHADOW_DATABASE_URL") is None
    c = models.get_conn()                      # the platform's database stays the local SQLite file
    try:
        assert c.execute("SELECT 1").fetchone()[0] == 1 and not isinstance(c, db_dialect.PgConnection)
    finally:
        c.close()


@pytest.mark.parametrize("marker", config.RAILWAY_MARKERS)
def test_on_railway_production_keeps_its_database(monkeypatch, marker):
    # Every marker scheduling_allowed reads also lets DATABASE_URL through:
    # production (and a railway ssh one-off) must keep reaching Postgres.
    for v in config.RAILWAY_MARKERS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.delenv("ALLOW_LOCAL_PG", raising=False)
    monkeypatch.delenv("DB_BACKEND", raising=False)
    monkeypatch.setenv(marker, "production")
    url = "postgresql://u:p@postgres.railway.internal:5432/railway"
    monkeypatch.setenv("DATABASE_URL", url)
    assert config.railway_marked() and config.platform_database_url_allowed()
    assert models._db_backend() == "postgres" and db_dialect.backend() == "postgres"
    assert models._pg_url_for(models.DB_PATH) == url
    # The same markers are what let the scheduler run (one test, two gates).
    monkeypatch.delenv("RESTORE_FROM", raising=False)
    monkeypatch.delenv("MAINTENANCE_READ_ONLY", raising=False)
    assert config.scheduling_allowed()


def test_allow_local_pg_is_the_deliberate_local_override(monkeypatch):
    for v in config.RAILWAY_MARKERS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.delenv("DB_BACKEND", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres@127.0.0.1:55432/scratch")
    assert models._db_backend() == "sqlite"
    monkeypatch.setenv("ALLOW_LOCAL_PG", "1")
    assert models._db_backend() == "postgres"
    assert models._pg_url_for(models.DB_PATH) == "postgresql://postgres@127.0.0.1:55432/scratch"
    monkeypatch.setenv("DB_BACKEND", "sqlite")               # the rollback pin still wins
    assert models._db_backend() == "sqlite"


# ── the network guard ───────────────────────────────────────────────────────

def test_a_connection_off_this_machine_is_refused_before_it_is_made():
    import conftest
    before = len(conftest._NET_BLOCKED)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(conftest.NetworkBlocked):
            s.connect(("203.0.113.7", 443))                    # TEST-NET-3: never routable
        assert s.connect_ex(("17.188.0.1", 443)) != 0             # an APNs address
    finally:
        s.close()
    assert [h for _w, h in conftest._NET_BLOCKED[before:]] == ["203.0.113.7:443", "17.188.0.1:443"]
    del conftest._NET_BLOCKED[before:]     # tripped on purpose; not ones to list at the end of the run


def test_requests_to_an_unlisted_provider_fail_as_an_unreachable_network():
    import requests
    import conftest
    # Not on the old denylist (APNs, RPOWER, Meta, Google OAuth were not):
    # refused at the socket, and requests reports a connection error, as
    # it would offline.
    with pytest.raises(requests.exceptions.ConnectionError):
        requests.get("https://17.188.0.1/3/device/x", timeout=2)
    assert conftest._NET_BLOCKED and conftest._NET_BLOCKED[-1][1] == "17.188.0.1:443"
    conftest._NET_BLOCKED.pop()            # handled on purpose; not one to list at the end of the run


def test_this_machine_is_still_reachable():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        for host in ("127.0.0.1", "localhost"):
            c = socket.create_connection((host, srv.getsockname()[1]), timeout=2)
            c.close()
            srv.accept()[0].close()
    finally:
        srv.close()


def test_the_local_host_rule():
    import conftest
    for host in ("localhost", "127.0.0.1", "127.8.9.1", "::1", "[::1]", "::ffff:127.0.0.1", "app.localhost", "0.0.0.0"):
        assert conftest._is_local_host(host), host
    for host in ("api.push.apple.com", "10.0.0.5", "203.0.113.7", "prod.example.com", "localhost.evil.com"):
        assert not conftest._is_local_host(host), host


# ── skips that say why, and only the expected ones ──────────────────────────

def test_an_unexplained_skip_reason_is_flagged_and_an_expected_one_is_not():
    import conftest
    got = conftest.unexpected_skips([
        ("a", "CAVNAR_TEST_PG_URL names no scratch Postgres"),
        ("b", "node is not installed"),
        ("c", "managers can open Food Cost in this build"),
        ("d", "Postgres mode: every test reads SQLite's EXPLAIN QUERY PLAN"),
    ])
    assert [nid for nid, _why in got] == ["b", "c"]


def test_ci_never_accepts_a_missing_tool_as_a_skip():
    import conftest
    for why in ("node is not installed", "node not installed", "OR-Tools not installed", "could not import 'resend'",
                "could not import 'pyflakes'"):
        assert conftest.unexpected_skips([("x", why)]), why
