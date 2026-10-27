"""The CI workflows keep the shape the release process depends on (audit #25
A01-02, A01-05, A01-20, A05-01, A05-17; R2, R4, R17).

- ci.yml has a `gate` job that finishes in at most 15 minutes, runs at
  UTC+14, cancels superseded runs, and runs on main, `production`, pull
  requests and by hand.
- No full-suite job blocks a push to main or `production`: the full SQLite
  and Postgres suites are nightly.yml's.
- The recorded-SQL PREPARE runs even when a test failed (`always()`).
- The season bot pushes to main only, never to `production`, and dispatches
  the gate for its own commit.
- Every job names a timeout.
"""
import os
import re

import pytest
import yaml

pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows")


def _load(name):
    with open(os.path.join(WF, name), encoding="utf-8") as fh:
        text = fh.read()
    return yaml.safe_load(text), text


def _on(wf):
    # YAML 1.1 reads a bare `on:` key as True.
    return wf.get("on", wf.get(True)) or {}


def _steps_text(job):
    return "\n".join(str(s.get("run", "")) + " " + str(s.get("name", "")) for s in job.get("steps", []))


def _full_suite(job):
    """Does this job run the whole suite (pytest over tests/ with no marker
    narrowing it to a slice)?"""
    for step in job.get("steps", []):
        run = " ".join(str(step.get("run", "")).split())
        for m in re.finditer(r"pytest\b([^\n|;&]*)", run):
            args = m.group(1)
            if re.search(r"(^|\s)tests/?(\s|$)", args) and "-m contract" not in args \
                    and "-m quarantine" not in args:
                return True
            if "test_shards.py" in args:
                return True
    return False


def test_the_gate_job_exists_and_is_bounded():
    wf, _ = _load("ci.yml")
    gate = wf["jobs"]["gate"]
    assert gate.get("name", "gate") == "gate"
    assert 0 < int(gate["timeout-minutes"]) <= 15
    assert wf["env"]["TZ"] == "Pacific/Kiritimati"
    assert wf["concurrency"] == {"group": "ci-${{ github.ref }}", "cancel-in-progress": True}
    on = _on(wf)
    assert set(on["push"]["branches"]) >= {"main", "production"}
    assert "pull_request" in on and "workflow_dispatch" in on


def test_the_gate_runs_every_check_the_release_depends_on():
    wf, _ = _load("ci.yml")
    text = _steps_text(wf["jobs"]["gate"])
    for needle in ("pip check", "py_compile", "check_colors.py", "check_timeouts.py", "check_silent_handlers.py",
                   "check_tenant_scope.py", "check_email_tokens.py", "pg_sql_audit.py --strict",
                   "-m contract", "test_inline_scripts_parse.py", "scripts/boot_check.py",
                   "boot_check.py --database-url", "--recorded"):
        assert needle in text, needle
    assert "postgres" in wf["jobs"]["gate"].get("services", {})


def test_the_recorded_sql_prepare_runs_even_when_a_test_failed():
    for name in ("ci.yml", "nightly.yml"):
        wf, _ = _load(name)
        found = False
        for job_name, job in wf["jobs"].items():
            for step in job.get("steps", []):
                if "--recorded" in str(step.get("run", "")):
                    cond = str(step.get("if", "")) + " " + str(job.get("if", ""))
                    assert "always()" in cond, (name, job_name)
                    found = True
            if job.get("needs") and "--recorded" in _steps_text(job):
                assert "always()" in str(job.get("if", "")), (name, job_name)
        assert found, name


def test_no_full_suite_blocks_a_push_to_main_or_production():
    for name in os.listdir(WF):
        if not name.endswith((".yml", ".yaml")):
            continue
        wf, _ = _load(name)
        on = _on(wf)
        push = on.get("push") if isinstance(on, dict) else None
        if push is None:
            continue
        branches = set((push or {}).get("branches") or ["*"])
        if not branches & {"main", "production", "*"}:
            continue
        for job_name, job in wf["jobs"].items():
            assert not _full_suite(job), f"{name}:{job_name} runs the full suite on a push to {branches}"


def test_the_full_suites_are_nightly():
    wf, _ = _load("nightly.yml")
    on = _on(wf)
    assert "schedule" in on and "workflow_dispatch" in on and "push" not in on
    jobs = wf["jobs"]
    assert _full_suite(jobs["full-sqlite"]) and _full_suite(jobs["shifted-tz"])
    assert jobs["postgres"]["strategy"]["matrix"]["shard"] == [1, 2, 3, 4]
    assert "test_shards.py" in _steps_text(jobs["postgres"])
    assert jobs["shifted-tz"]["env"]["TZ"] == "Pacific/Pago_Pago"
    assert "-p no:xdist" in _steps_text(jobs["solver-serial"])
    assert jobs["quarantine"].get("continue-on-error") is True
    assert "-m quarantine" in _steps_text(jobs["quarantine"])
    assert "playwright install" in _steps_text(jobs["browser"])
    for main_job in ("full-sqlite", "postgres", "shifted-tz"):
        assert 'not quarantine' in _steps_text(jobs[main_job]), main_job


def test_every_job_names_a_timeout():
    for name in os.listdir(WF):
        if not name.endswith((".yml", ".yaml")):
            continue
        wf, _ = _load(name)
        for job_name, job in wf["jobs"].items():
            assert int(job.get("timeout-minutes", 0)) > 0, f"{name}:{job_name} has no timeout-minutes"


def test_the_season_bot_never_pushes_to_production_and_gates_its_commit():
    wf, text = _load("refresh-seasons.yml")
    runs = _steps_text(wf["jobs"]["refresh"])
    assert "git push origin HEAD:main" in runs
    assert "production" not in "\n".join(str(s.get("run", "")) for s in wf["jobs"]["refresh"]["steps"])
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"push[^\n]*production|refs/heads/production", code)
    assert "gh workflow run ci.yml --ref main" in runs
    assert wf["permissions"].get("actions") == "write"
    # The files are validated and the event tests run before the commit.
    steps = [s.get("name", "") for s in wf["jobs"]["refresh"]["steps"]]
    assert steps.index("Validate the refreshed files") < steps.index("Commit and push")
    assert "tests/test_event_*.py" in runs


def test_promotion_is_by_hand_and_only_through_the_script():
    wf, _ = _load("promote.yml")
    on = _on(wf)
    assert set(on) == {"workflow_dispatch"}
    assert set(on["workflow_dispatch"]["inputs"]) == {"sha", "emergency", "dry_run"}
    assert "scripts/promote.py" in _steps_text(wf["jobs"]["promote"])
    assert wf["concurrency"]["cancel-in-progress"] is False


def test_the_ios_tests_run_on_ios_pushes_and_nightly():
    wf, _ = _load("ios.yml")
    on = _on(wf)
    assert on["push"]["paths"][0] == "ios/**" and "schedule" in on and "workflow_dispatch" in on
    job = wf["jobs"]["xctest"]
    assert int(job["timeout-minutes"]) == 40
    text = _steps_text(job)
    for needle in ("generate.sh", "build-for-testing", "test-without-building", "-scheme CavnarAI"):
        assert needle in text, needle
