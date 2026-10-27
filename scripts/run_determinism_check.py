#!/usr/bin/env python3
"""
scripts/run_determinism_check.py — the schedule solver's "same inputs, same
week" promise, verified where it can be: serially, one CP-SAT worker, no
xdist (release-safety audit 10/26/26 A01-12, A02-12). The nightly CI job
runs it; anyone may.

    python3 scripts/run_determinism_check.py              # exit 0: the promise held
    python3 scripts/run_determinism_check.py -- -k same   # extra pytest arguments after --

It runs DETERMINISM_FILES with SCHEDULE_CPSAT_WORKERS=1 (one search thread
a solve) and -p no:xdist (one solve at a time on the box), so a solve the
wall clock cuts — the emergency cap, 4x a solve's deterministic budget — is
rare. Each determinism test is strict, and skips (naming the cut) only when
a solve reports one (schedule_cpsat_runtime._wall_capped, judged by the
clock alone; the benchmark's presses by their review's solve_capped).

The gate: a failed or erroring test fails it, and so does a run with more
than MAX_SKIPS skipped tests — a run that skipped more than that verified
nothing and must not read green (on a CI runner under load the old check
never fired and the tests flaked; a check that fires and skips silently
would hide a real nondeterminism instead). The exit status is pytest's when
it failed, else 1 over the skip limit, else 0; 2 when pytest left no report.
"""
import os
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETERMINISM_FILES = ("tests/test_solver_behaviour.py", "tests/test_schedule_benchmark.py")
MAX_SKIPS = 1
TIMEOUT_SECONDS = 1800


def summarize(junit_xml: str) -> dict:
    """{"tests", "failures", "errors", "skipped", "skips": [(test id, reason)]}
    from a pytest --junitxml report."""
    root = ET.fromstring(junit_xml)
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    out = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "skips": []}
    for s in suites:
        for k in ("tests", "failures", "errors", "skipped"):
            out[k] += int(s.get(k) or 0)
        for case in s.iter("testcase"):
            sk = case.find("skipped")
            if sk is not None:
                out["skips"].append((f"{case.get('classname')}::{case.get('name')}",
                                     (sk.get("message") or sk.text or "").strip()[:200]))
    return out


def verdict(summary: dict, pytest_status: int, max_skips: int = MAX_SKIPS) -> int:
    """The gate's exit status (module docstring)."""
    if pytest_status not in (0, 5) or summary["failures"] or summary["errors"]:
        return pytest_status or 1
    if summary["tests"] == 0:
        return 1                                   # nothing ran: nothing was verified
    return 1 if summary["skipped"] > max_skips else 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = argv[argv.index("--") + 1:] if "--" in argv else []
    env = dict(os.environ, SCHEDULE_CPSAT_WORKERS="1")
    with tempfile.TemporaryDirectory(prefix="cavnar-determinism-") as tmp:
        report = os.path.join(tmp, "junit.xml")
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:warnings", "-p", "no:xdist", "-rs",
               f"--junitxml={report}", *DETERMINISM_FILES, *extra]
        print("$ SCHEDULE_CPSAT_WORKERS=1 python3 " + shlex.join(cmd[1:]), flush=True)
        status = subprocess.run(cmd, cwd=ROOT, env=env, timeout=TIMEOUT_SECONDS).returncode
        if not os.path.exists(report):
            print("determinism check: pytest left no report", file=sys.stderr)
            return 2
        with open(report, encoding="utf-8") as f:
            summary = summarize(f.read())
    code = verdict(summary, status)
    print(f"determinism check: {summary['tests']} tests, {summary['failures']} failed, {summary['errors']} errors, "
          f"{summary['skipped']} skipped (at most {MAX_SKIPS} allowed) -> {'held' if code == 0 else 'FAILED'}")
    for test_id, reason in summary["skips"]:
        print(f"  skipped {test_id}: {reason}")
    return code


if __name__ == "__main__":
    sys.exit(main())
