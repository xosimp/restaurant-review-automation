"""The nightly determinism gate (scripts/run_determinism_check.py,
release-safety audit 10/26/26 A01-12 / A02-12): the solver's determinism
tests skip only on a reported cut, and a run with more than one skip — or
with nothing run — must not read green."""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _gate():
    spec = importlib.util.spec_from_file_location("run_determinism_check",
                                                  os.path.join(ROOT, "scripts", "run_determinism_check.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _junit(skips=0, failures=0, tests=10):
    cases = "".join(f'<testcase classname="tests.test_solver_behaviour" name="t{i}"/>' for i in range(tests - skips))
    cases += "".join(f'<testcase classname="tests.test_solver_behaviour" name="cut{i}">'
                     f'<skipped type="pytest.skip" message="a solve was cut by the wall cap">x</skipped></testcase>'
                     for i in range(skips))
    return (f'<?xml version="1.0"?><testsuites><testsuite name="pytest" errors="0" failures="{failures}" '
            f'skipped="{skips}" tests="{tests}">{cases}</testsuite></testsuites>')


def test_one_cut_skip_passes_and_two_fail_the_gate():
    g = _gate()
    one = g.summarize(_junit(skips=1))
    assert one["skipped"] == 1 and one["skips"][0][1] == "a solve was cut by the wall cap"
    assert g.verdict(one, 0) == 0
    assert g.verdict(g.summarize(_junit(skips=2)), 0) == 1
    assert g.verdict(g.summarize(_junit(skips=0)), 0) == 0


def test_a_failure_or_an_empty_run_fails_the_gate():
    g = _gate()
    assert g.verdict(g.summarize(_junit(failures=1)), 1) == 1
    assert g.verdict(g.summarize(_junit(tests=0)), 5) == 1          # nothing collected: nothing verified
    assert g.verdict(g.summarize(_junit()), 2) == 2                 # interrupted: pytest's own status


def test_the_gate_runs_the_determinism_files_serially_on_one_worker(monkeypatch, tmp_path):
    g = _gate()
    seen = {}

    def fake_run(cmd, cwd=None, env=None, timeout=None):
        seen.update(cmd=cmd, env=env)
        report = next(a.split("=", 1)[1] for a in cmd if a.startswith("--junitxml="))
        with open(report, "w") as f:
            f.write(_junit(skips=0))

        class Done:
            returncode = 0
        return Done()
    monkeypatch.setattr(g.subprocess, "run", fake_run)
    assert g.main(["--", "-k", "same_inputs"]) == 0
    assert seen["env"]["SCHEDULE_CPSAT_WORKERS"] == "1"
    assert "no:xdist" in seen["cmd"] and "-n" not in seen["cmd"]
    assert all(f in seen["cmd"] for f in g.DETERMINISM_FILES) and seen["cmd"][-2:] == ["-k", "same_inputs"]
