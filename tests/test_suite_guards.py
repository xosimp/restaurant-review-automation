"""The suite's own guards fail a CI run when they should (blind re-audit
10/27/26, B1 F11, F12, F16).

Each case runs a small throwaway test file through a real pytest process
with tests/conftest.py and CI's switch (CAVNAR_ENFORCE_SKIPS=1), alone and
under xdist, and reads the exit status:

- a module-level skip whose reason tests/expected_skips.txt does not explain
  fails the run — it is a collection report, which the enforcement used to
  miss (a module-level importorskip turned a whole file into "1 skipped");
- a connection off the machine made OUTSIDE a test function (a module
  fixture) on an xdist worker fails the run — it used to stay in the
  worker's process;
- a quarantine mark that does not say why and since when stops the run
  before anything executes;
- a test that runs past its timeout fails, named (pytest-timeout, CI's
  --timeout=300).
"""
import os
import shutil
import subprocess
import sys
import uuid

import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)


def _run(source, *args, enforce=True):
    """(exit status, output) of pytest over one probe file holding `source`,
    under tests/ so the real conftest applies."""
    probe_dir = os.path.join(TESTS, f"_probe_{uuid.uuid4().hex[:10]}")
    os.makedirs(probe_dir)
    try:
        path = os.path.join(probe_dir, "probe_case.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(source)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PYTEST_", "CAVNAR_TEST_PG", "CAVNAR_RECORD_SQL")) or k == "PYTEST_TIMEOUT"}
        env["CAVNAR_ENFORCE_SKIPS"] = "1" if enforce else "0"
        r = subprocess.run([sys.executable, "-m", "pytest", path, "-q", "-rs", "-p", "no:cacheprovider",
                            f"--rootdir={TESTS}", f"--confcutdir={TESTS}", *args],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)
        return r.returncode, r.stdout + r.stderr
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


MODULE_SKIP = (
    "import pytest\n"
    "pytest.skip('a reason nobody wrote down in expected_skips', allow_module_level=True)\n"
    "def test_never_runs():\n"
    "    assert False\n")


@pytest.mark.parametrize("xdist", [(), ("-n", "2")], ids=["alone", "xdist"])
def test_a_module_level_skip_nobody_explained_fails_the_run(xdist):
    code, out = _run(MODULE_SKIP, *xdist)
    assert code == pytest.ExitCode.TESTS_FAILED, out[-2000:]
    assert "UNEXPECTED SKIPS" in out and "a reason nobody wrote down" in out, out[-2000:]


def test_the_same_skip_passes_where_skips_are_not_enforced():
    code, out = _run(MODULE_SKIP, enforce=False)
    # 5: the file's one test was skipped at collection, so none ran.
    assert code == pytest.ExitCode.NO_TESTS_COLLECTED and "1 skipped" in out, out[-2000:]


FIXTURE_CONNECT = (
    "import socket\n"
    "import pytest\n"
    "@pytest.fixture(scope='module')\n"
    "def reached():\n"
    "    try:\n"
    "        socket.create_connection(('203.0.113.7', 9), timeout=1)\n"
    "    except OSError:\n"
    "        pass\n"
    "    return True\n"
    "def test_uses_it(reached):\n"
    "    assert reached\n"
    "def test_another():\n"
    "    assert True\n")


def test_a_connection_from_a_module_fixture_on_an_xdist_worker_fails_the_run():
    code, out = _run(FIXTURE_CONNECT, "-n", "2")
    assert code == pytest.ExitCode.TESTS_FAILED and "2 passed" in out, out[-2000:]
    assert "203.0.113.7:9" in out, out[-2000:]


def test_a_quarantine_without_a_reason_and_a_date_stops_the_run():
    code, out = _run("import pytest\n\n@pytest.mark.quarantine\ndef test_flaky():\n    assert True\n")
    assert code == pytest.ExitCode.USAGE_ERROR, out[-2000:]
    assert "quarantine needs reason=" in out, out[-2000:]
    code, out = _run("import pytest\n\n@pytest.mark.quarantine(reason='races the lease', since='10/27/26')\n"
                     "def test_flaky():\n    assert True\n")
    assert code == 0, out[-2000:]


def test_a_test_past_its_timeout_fails_and_is_named():
    pytest.importorskip("pytest_timeout")
    code, out = _run("import time\n\ndef test_hangs():\n    time.sleep(30)\n", "--timeout=2")
    assert code != 0 and "Timeout" in out and "test_hangs" in out, out[-2000:]
