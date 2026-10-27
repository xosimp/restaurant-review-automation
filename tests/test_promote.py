"""scripts/promote.py's decision (audit #25 deployment strategy; A05-01, R2,
R4): production moves only to a main SHA with a green gate, inside the
9am-3pm Chicago weekday window, after a green (or fully quarantined)
nightly — and --emergency skips the window and the nightly, never the gate
or main."""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import promote  # noqa: E402

CHI = ZoneInfo("America/Chicago")
TUE_11AM = datetime(2026, 10, 27, 11, 0, tzinfo=CHI)
GATE_OK = [{"name": "gate", "status": "completed", "conclusion": "success"}]
GREEN = {"id": 9, "conclusion": "success", "summary": None}


def _decide(**kw):
    args = dict(on_main=True, check_runs=GATE_OK, now_local=TUE_11AM, emergency=False, nightly=GREEN,
                quarantined=set())
    args.update(kw)
    return promote.decide(**args)


def test_a_main_sha_with_a_green_gate_and_nightly_inside_the_window_is_promoted():
    v = _decide()
    assert v["ok"] and not v["reasons"]


def test_a_sha_off_main_is_refused_even_in_an_emergency():
    v = _decide(on_main=False, emergency=True)
    assert not v["ok"] and "not on main" in v["reasons"][0]


@pytest.mark.parametrize("runs", [
    [],
    [{"name": "gate", "status": "completed", "conclusion": "failure"}],
    [{"name": "gate", "status": "in_progress", "conclusion": None}],
    [{"name": "test", "status": "completed", "conclusion": "success"}],
])
def test_no_green_gate_is_refused_even_in_an_emergency(runs):
    assert not _decide(check_runs=runs)["ok"]
    assert not _decide(check_runs=runs, emergency=True)["ok"]


@pytest.mark.parametrize("when", [
    datetime(2026, 10, 27, 8, 59, tzinfo=CHI),      # before 9
    datetime(2026, 10, 27, 15, 0, tzinfo=CHI),      # 3pm is closed
    datetime(2026, 10, 27, 19, 30, tzinfo=CHI),     # dinner service
    datetime(2026, 10, 31, 11, 0, tzinfo=CHI),      # Saturday
    datetime(2026, 11, 1, 11, 0, tzinfo=CHI),       # Sunday (the DST change)
])
def test_outside_the_weekday_window_is_refused_unless_an_emergency(when):
    v = _decide(now_local=when)
    assert not v["ok"] and "outside the promotion window" in v["reasons"][0]
    assert _decide(now_local=when, emergency=True)["ok"]


def test_the_window_is_read_in_chicago_whatever_the_callers_zone():
    # 15:30 UTC on a Tuesday in daylight time is 10:30 in Chicago: open.
    utc = datetime(2026, 10, 27, 15, 30, tzinfo=ZoneInfo("UTC"))
    assert promote.in_window(utc.astimezone(CHI))
    # 21:00 UTC is 16:00 in Chicago: closed.
    assert not promote.in_window(datetime(2026, 10, 27, 21, 0, tzinfo=ZoneInfo("UTC")).astimezone(CHI))


def test_a_red_nightly_holds_the_promotion_unless_every_failure_is_quarantined():
    red = {"id": 7, "conclusion": "failure",
           "summary": {"failed": ["tests/test_a.py::test_x", "tests/test_b.py::test_y[case1]"],
                       "jobs": {"full-sqlite": "failure", "postgres": "success"},
                       "jobs_with_test_failures": ["full-sqlite"]}}
    v = _decide(nightly=red)
    assert not v["ok"] and "not quarantined" in v["reasons"][0]
    # Quarantined since (the file, and the test whatever its parameters).
    v = _decide(nightly=red, quarantined={"tests/test_a.py", "tests/test_b.py::test_y"})
    assert v["ok"], v
    # One still open.
    assert not _decide(nightly=red, quarantined={"tests/test_a.py"})["ok"]
    # An emergency does not wait for the nightly.
    assert _decide(nightly=red, emergency=True)["ok"]


def test_a_red_nightly_with_no_summary_or_a_job_that_failed_without_a_test_is_refused():
    assert not _decide(nightly={"id": 3, "conclusion": "failure", "summary": None})["ok"]
    infra = {"id": 4, "conclusion": "failure",
             "summary": {"failed": [], "jobs": {"recorded-sql": "failure"}, "jobs_with_test_failures": []}}
    v = _decide(nightly=infra)
    assert not v["ok"] and "recorded-sql" in v["reasons"][0]
    assert not _decide(nightly=None)["ok"]


def test_quarantine_marks_are_read_from_the_test_files(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_whole.py").write_text("import pytest\npytestmark = pytest.mark.quarantine\n")
    (tests / "test_some.py").write_text("import pytest\n\n@pytest.mark.quarantine\ndef test_flaky():\n    pass\n\n"
                                        "def test_fine():\n    pass\n")
    (tests / "test_list.py").write_text("import pytest\npytestmark = [pytest.mark.contract, pytest.mark.quarantine]\n")
    (tests / "test_none.py").write_text("import pytest\n# quarantine is only a word here\n")
    got = promote.quarantined_tests(str(tmp_path))
    assert got == {"tests/test_whole.py", "tests/test_some.py::test_flaky", "tests/test_list.py"}


def test_the_nightly_summary_names_each_failed_test_by_its_pytest_id(tmp_path):
    (tmp_path / "full-sqlite.xml").write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.test_models" name="test_ok"/>'
        '<testcase classname="tests.test_models" name="test_bad"><failure message="x"/></testcase>'
        '<testcase classname="tests.test_pos.TestSync" name="test_err[2]"><error message="y"/></testcase>'
        '<testcase classname="tests.test_models" name="test_skip"><skipped/></testcase>'
        '</testsuite></testsuites>')
    (tmp_path / "postgres-3.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.test_q2_postgres" name="test_pg">'
        '<failure/></testcase></testsuite></testsuites>')
    out = promote.summarize_junit(str(tmp_path), {"full-sqlite": {"result": "failure"},
                                                  "postgres": {"result": "failure"}})
    assert out["failed"] == ["tests/test_models.py::test_bad", "tests/test_pos.py::TestSync::test_err[2]",
                             "tests/test_q2_postgres.py::test_pg"]
    assert out["jobs"] == {"full-sqlite": "failure", "postgres": "failure"}
    assert out["jobs_with_test_failures"] == ["full-sqlite", "postgres"]


class _Fake:
    """git and gh, answered from a table; records every command."""

    def __init__(self, on_main=True, gate=GATE_OK, nightly=GREEN):
        self.calls, self.on_main, self.gate, self.nightly = [], on_main, gate, nightly

    def __call__(self, args, timeout=60):
        self.calls.append(args)
        if args[:2] == ["git", "rev-parse"]:
            return 0, "a" * 40 + "\n", ""
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            return (0 if self.on_main else 1), "", ""
        if args[:2] == ["gh", "api"] and "check-runs" in args[2]:
            return 0, json.dumps({"check_runs": self.gate}), ""
        if args[:2] == ["gh", "api"] and "/runs?" in args[2]:
            runs = [] if self.nightly is None else [dict(self.nightly, head_sha="b" * 40)]
            return 0, json.dumps({"workflow_runs": runs}), ""
        if args[:2] == ["git", "push"]:
            return 0, "pushed", ""
        return 1, "", "unexpected " + " ".join(args)


def test_a_dry_run_decides_and_never_pushes(capsys):
    fake = _Fake()
    assert promote.main(["--repo", "o/r", "--dry-run"], run=fake, now=TUE_11AM) == 0
    assert not any(c[:2] == ["git", "push"] for c in fake.calls)
    assert "would run: git push origin " + "a" * 40 + ":refs/heads/production" in capsys.readouterr().out


def test_a_promotion_fast_forwards_production_and_nothing_else():
    fake = _Fake()
    assert promote.main(["--repo", "o/r", "--sha", "abc"], run=fake, now=TUE_11AM) == 0
    pushes = [c for c in fake.calls if c[:2] == ["git", "push"]]
    assert pushes == [["git", "push", "origin", "a" * 40 + ":refs/heads/production"]]   # no +: fast-forward only


def test_a_refused_promotion_pushes_nothing():
    fake = _Fake(gate=[])
    assert promote.main(["--repo", "o/r", "--emergency"], run=fake, now=TUE_11AM) == 1
    assert not any(c[:2] == ["git", "push"] for c in fake.calls)
    fake = _Fake()
    assert promote.main(["--repo", "o/r"], run=fake, now=datetime(2026, 10, 27, 20, 0, tzinfo=CHI)) == 1
    assert not any(c[:2] == ["git", "push"] for c in fake.calls)
