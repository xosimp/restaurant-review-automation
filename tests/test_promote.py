"""scripts/promote.py's decision (audit #25 deployment strategy; A05-01, R2,
R4; blind re-audit 10/27/26 B1 F3/F16, B2, B4, B5, B6): production moves
only to a main SHA with a green gate, gate-postgres and gate-boot, inside the 9am-3pm
Chicago weekday window, after a green (or fully, freshly quarantined)
nightly THAT RAN ON THAT SHA — dispatched when it has not — and, when the
promotion carries an ios/ change, a green iOS run on it; --emergency skips
the window, the nightly and the iOS run, never the gate or main."""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import promote  # noqa: E402

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract

CHI = ZoneInfo("America/Chicago")
TUE_11AM = datetime(2026, 10, 27, 11, 0, tzinfo=CHI)
SHA = "a" * 40
OLDER = "b" * 40
GATE_OK = [{"name": "gate", "status": "completed", "conclusion": "success"},
           {"name": "gate-postgres", "status": "completed", "conclusion": "success"},
           {"name": "gate-boot", "status": "completed", "conclusion": "success"}]
GREEN = {"id": 9, "conclusion": "success", "summary": None, "tested_sha": SHA}


def _decide(**kw):
    args = dict(on_main=True, check_runs=GATE_OK, now_local=TUE_11AM, emergency=False, nightly=GREEN,
                quarantined=set(), sha=SHA)
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
    [{"name": "gate", "status": "completed", "conclusion": "failure"}, GATE_OK[1], GATE_OK[2]],
    [{"name": "gate", "status": "in_progress", "conclusion": None}, GATE_OK[1], GATE_OK[2]],
    [{"name": "test", "status": "completed", "conclusion": "success"}],
    # The SQLite gate alone: the slice never ran on Postgres, the app never booted.
    [GATE_OK[0]],
    [GATE_OK[0], {"name": "gate-postgres", "status": "completed", "conclusion": "failure"}, GATE_OK[2]],
    [GATE_OK[0], GATE_OK[1]],
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


def test_a_nightly_that_tested_an_older_sha_never_clears_a_newer_one():
    """The gap the re-audit found: the latest nightly on main tested
    yesterday's head, and promote accepted it for this morning's commit."""
    v = _decide(nightly=dict(GREEN, tested_sha=OLDER))
    assert not v["ok"] and "only a nightly on this very SHA counts" in v["reasons"][0]
    v = _decide(nightly=None)
    assert not v["ok"] and "no finished nightly" in v["reasons"][0]
    v = _decide(nightly=None, nightly_running=True)
    assert not v["ok"] and "is running" in v["reasons"][0]
    # The emergency path does not wait for one.
    assert _decide(nightly=None, emergency=True)["ok"]


def test_a_red_nightly_summary_about_another_sha_is_refused():
    red = {"id": 7, "conclusion": "failure", "tested_sha": SHA,
           "summary": {"failed": ["tests/test_a.py::test_x"], "jobs": {"full-sqlite": "failure"},
                       "jobs_with_test_failures": ["full-sqlite"], "sha": OLDER}}
    assert not _decide(nightly=red, quarantined={"tests/test_a.py"})["ok"]


def test_an_ios_change_waits_for_a_green_ios_run_on_the_sha():
    assert _decide(ios=None)["ok"]                                   # nothing under ios/
    green = {"id": 5, "conclusion": "success"}
    assert _decide(ios={"run": green, "running": False})["ok"]
    v = _decide(ios={"run": {"id": 6, "conclusion": "failure"}, "running": False})
    assert not v["ok"] and "iOS tests on this SHA are failure" in v["reasons"][0]
    assert not _decide(ios={"run": None, "running": True})["ok"]
    assert not _decide(ios={"run": None, "running": False})["ok"]
    assert _decide(ios={"run": None, "running": False}, emergency=True)["ok"]


def test_a_red_nightly_holds_the_promotion_unless_every_failure_is_quarantined():
    red = {"id": 7, "conclusion": "failure", "tested_sha": SHA,
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
    assert not _decide(nightly={"id": 3, "conclusion": "failure", "summary": None, "tested_sha": SHA})["ok"]
    infra = {"id": 4, "conclusion": "failure", "tested_sha": SHA,
             "summary": {"failed": [], "jobs": {"recorded-sql": "failure"}, "jobs_with_test_failures": []}}
    v = _decide(nightly=infra)
    assert not v["ok"] and "recorded-sql" in v["reasons"][0]
    assert not _decide(nightly=None)["ok"]


def test_a_quarantine_excuses_only_with_a_reason_and_a_fresh_date():
    marks = {"tests/test_a.py": ("races the lease", "10/20/26"),
             "tests/test_b.py::test_y": ("", "10/26/26"),
             "tests/test_c.py": ("old", "9/1/26"),
             "tests/test_d.py::test_z": ("no date", "")}
    live, stale = promote.live_quarantine(marks, TUE_11AM.date())
    assert live == {"tests/test_a.py"}
    assert len(stale) == 3 and any("no reason" in s for s in stale) and any("days ago" in s for s in stale)


def test_the_quarantine_is_read_at_the_promoted_sha_not_main(tmp_path):
    """A one-line quarantine commit on main must not clear an older SHA's
    red nightly: the marks are read with git at the SHA itself."""
    calls = []

    def run(args, timeout=60):
        calls.append(args)
        if args[:3] == ["git", "grep", "-l"]:
            return 0, f"{SHA}:tests/test_q.py\n{SHA}:tests/helper.py\n", ""
        if args[:2] == ["git", "show"]:
            assert args[2] == f"{SHA}:tests/test_q.py"
            return 0, ("import pytest\n\n@pytest.mark.quarantine(reason='flaky lease', since='10/26/26')\n"
                       "def test_flaky():\n    pass\n"), ""
        return 1, "", "unexpected"
    files = promote.quarantine_files_at(SHA, run)
    assert set(files) == {"tests/test_q.py"} and SHA in calls[0]
    assert promote.quarantine_marks(files) == {"tests/test_q.py::test_flaky": ("flaky lease", "10/26/26")}


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
                                                  "postgres": {"result": "failure"}}, sha=SHA)
    assert out["sha"] == SHA
    assert out["failed"] == ["tests/test_models.py::test_bad", "tests/test_pos.py::TestSync::test_err[2]",
                             "tests/test_q2_postgres.py::test_pg"]
    assert out["jobs"] == {"full-sqlite": "failure", "postgres": "failure"}
    assert out["jobs_with_test_failures"] == ["full-sqlite", "postgres"]


class _Fake:
    """git and gh, answered from a table; records every command."""

    def __init__(self, on_main=True, gate=GATE_OK, nightly=GREEN, nightly_runs=None, ios_touched=False,
                 ios_runs=(), production=True):
        self.calls, self.on_main, self.gate = [], on_main, gate
        if nightly_runs is None:
            nightly_runs = [] if nightly is None else [{"id": nightly["id"], "status": "completed",
                                                        "conclusion": nightly["conclusion"], "head_sha": SHA,
                                                        "display_title": f"Nightly {SHA}"}]
        self.nightly_runs, self.ios_touched, self.ios_runs, self.production = nightly_runs, ios_touched, \
            list(ios_runs), production

    def __call__(self, args, timeout=60):
        self.calls.append(args)
        if args[:2] == ["git", "rev-parse"] and "origin/production^{commit}" in args:
            return (0 if self.production else 1), "", ""
        if args[:2] == ["git", "rev-parse"]:
            return 0, SHA + "\n", ""
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            return (0 if self.on_main else 1), "", ""
        if args[:2] == ["git", "diff"]:
            return 0, ("ios/CavnarAI/X.swift\n" if self.ios_touched else "") + "models.py\n", ""
        if args[:2] == ["git", "grep"]:
            return 1, "", ""
        if args[:2] == ["gh", "api"] and "check-runs" in args[2]:
            return 0, json.dumps({"check_runs": self.gate}), ""
        if args[:2] == ["gh", "api"] and "/nightly.yml/runs" in args[2]:
            return 0, json.dumps({"workflow_runs": self.nightly_runs}), ""
        if args[:2] == ["gh", "api"] and "/ios.yml/runs" in args[2]:
            return 0, json.dumps({"workflow_runs": self.ios_runs}), ""
        if args[:2] == ["gh", "api"] and args[2].endswith("/branches/production"):
            return (0, json.dumps({"name": "production", "protected": True}), "") if self.production else \
                (1, "", "gh: Not Found (HTTP 404)")
        if args[:3] == ["gh", "workflow", "run"]:
            return 0, "", ""
        if args[:2] == ["git", "push"]:
            return 0, "pushed", ""
        return 1, "", "unexpected " + " ".join(args)


def _dispatched(fake):
    return [c for c in fake.calls if c[:3] == ["gh", "workflow", "run"]]


def test_a_dry_run_decides_and_never_pushes(capsys):
    fake = _Fake()
    assert promote.main(["--repo", "o/r", "--dry-run"], run=fake, now=TUE_11AM) == 0
    assert not any(c[:2] == ["git", "push"] for c in fake.calls)
    assert "would run: git push origin " + SHA + ":refs/heads/production" in capsys.readouterr().out


def test_a_promotion_fast_forwards_production_and_nothing_else():
    fake = _Fake()
    assert promote.main(["--repo", "o/r", "--sha", "abc"], run=fake, now=TUE_11AM) == 0
    pushes = [c for c in fake.calls if c[:2] == ["git", "push"]]
    assert pushes == [["git", "push", "origin", SHA + ":refs/heads/production"]]   # no +: fast-forward only
    assert _dispatched(fake) == []


def test_a_refused_promotion_pushes_nothing():
    fake = _Fake(gate=[])
    assert promote.main(["--repo", "o/r", "--emergency"], run=fake, now=TUE_11AM) == 1
    assert not any(c[:2] == ["git", "push"] for c in fake.calls)
    fake = _Fake()
    assert promote.main(["--repo", "o/r"], run=fake, now=datetime(2026, 10, 27, 20, 0, tzinfo=CHI)) == 1
    assert not any(c[:2] == ["git", "push"] for c in fake.calls)


def test_a_sha_newer_than_the_last_nightly_is_refused_and_its_nightly_dispatched(capsys):
    """The latest nightly on main tested an older commit: refused, and a
    nightly is started on this SHA (its `sha` input) for the next try."""
    older = [{"id": 8, "status": "completed", "conclusion": "success", "head_sha": OLDER,
              "display_title": f"Nightly {OLDER}"}]
    fake = _Fake(nightly_runs=older)
    assert promote.main(["--repo", "o/r"], run=fake, now=TUE_11AM) == 1
    assert not any(c[:2] == ["git", "push"] for c in fake.calls)
    assert _dispatched(fake) == [["gh", "workflow", "run", "nightly.yml", "-R", "o/r", "--ref", "main",
                                  "-f", f"sha={SHA}"]]
    # A dry run says it would, and starts nothing.
    fake = _Fake(nightly_runs=older)
    assert promote.main(["--repo", "o/r", "--dry-run"], run=fake, now=TUE_11AM) == 1
    assert _dispatched(fake) == [] and "would start: gh workflow run nightly.yml" in capsys.readouterr().out


def test_a_nightly_dispatched_for_the_sha_counts_by_its_run_name():
    """A dispatched run's head_sha is main's head; the commit it tested is
    in its run-name (nightly.yml `run-name`)."""
    dispatched = [{"id": 12, "status": "completed", "conclusion": "success", "head_sha": OLDER,
                   "display_title": f"Nightly {SHA}"}]
    fake = _Fake(nightly_runs=dispatched)
    assert promote.main(["--repo", "o/r", "--sha", "abc"], run=fake, now=TUE_11AM) == 0
    # ...and a run on main's head (OLDER) with this SHA's head_sha but another title does not.
    assert promote.tested_sha({"head_sha": SHA, "display_title": f"Nightly {OLDER}"}) == OLDER


def test_a_nightly_still_running_on_the_sha_is_waited_for_not_dispatched_again():
    running = [{"id": 13, "status": "in_progress", "conclusion": None, "head_sha": SHA,
                "display_title": f"Nightly {SHA}"}]
    fake = _Fake(nightly_runs=running)
    assert promote.main(["--repo", "o/r"], run=fake, now=TUE_11AM) == 1
    assert _dispatched(fake) == []


def test_an_ios_change_needs_the_ios_run_on_the_sha_and_starts_one():
    fake = _Fake(ios_touched=True)
    assert promote.main(["--repo", "o/r"], run=fake, now=TUE_11AM) == 1
    assert [c[3] for c in _dispatched(fake)] == ["ios.yml"]
    fake = _Fake(ios_touched=True, ios_runs=[{"id": 3, "status": "completed", "conclusion": "success",
                                              "head_sha": SHA, "display_title": f"iOS {SHA}"}])
    assert promote.main(["--repo", "o/r"], run=fake, now=TUE_11AM) == 0
    # No production branch yet: what the promotion carries is unknown, so the iOS run is required.
    fake = _Fake(production=False)
    assert promote.main(["--repo", "o/r"], run=fake, now=TUE_11AM) == 1


def test_a_missing_or_unprotected_production_branch_is_said(capsys):
    fake = _Fake(production=False, ios_runs=[{"id": 3, "status": "completed", "conclusion": "success",
                                              "head_sha": SHA, "display_title": f"iOS {SHA}"}])
    assert promote.main(["--repo", "o/r", "--dry-run"], run=fake, now=TUE_11AM) == 0
    out = capsys.readouterr().out
    assert "warn the `production` branch does not exist yet" in out
