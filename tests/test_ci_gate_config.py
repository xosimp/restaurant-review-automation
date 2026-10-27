"""The CI workflows keep the shape the release process depends on (audit #25
A01-02, A01-05, A01-20, A05-01, A05-17; R2, R4, R17; blind re-audit 10/27/26
B1 F2-F17, B5, B6, B8).

- ci.yml has `gate`, `gate-postgres` (the contract slice on Postgres 16) and
  `gate-boot` jobs in parallel, each finishing in at most 15 minutes, at
  UTC+14, cancelling superseded runs, on main, `production`, pull requests
  and by hand; `gate-boot` boots the app on SQLite, with production's
  markers, on a partitioned Postgres, under gunicorn and on the previous
  release's data.
- No full-suite job blocks a push: the full suites are nightly.yml's — and,
  UNTIL Railway deploys `production` (repository variable PROMOTION_LIVE not
  "true"), also run on every push to main, never needed by the gate.
- The recorded-SQL PREPARE runs even when a test failed (`always()`).
- The season bot pushes to main only, never to `production`, dispatches the
  gate for its own commit, and runs at 14:00 UTC (never deploying at dinner
  while main still deploys).
- The nightly and the iOS workflow test ONE commit — the `sha` promote.py
  dispatches them with — and say which in their run-name.
- iOS checks out the full history (generate.sh refuses a shallow clone).
- Every job names a timeout; every pytest run names a per-test --timeout.
- Nothing claims "main no longer deploys" while it still does.
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


def _pytest_runs(job):
    """Every pytest command line in a job's steps."""
    out = []
    for step in job.get("steps", []):
        run = " ".join(str(step.get("run", "")).split())
        out += [m.group(0) for m in re.finditer(r"pytest\b[^\n|;&]*", run) if not m.group(0).startswith("pytest-")]
    return out


def _full_suite(job):
    """Does this job run the whole suite (pytest over tests/ with no marker
    narrowing it to a slice)?"""
    for step in job.get("steps", []):
        run = " ".join(str(step.get("run", "")).split())
        for m in re.finditer(r"pytest\b([^\n|;&]*)", run):
            args = m.group(1)
            if re.search(r"(^|\s)tests/?(\s|$)", args) and "-m contract" not in args \
                    and '-m "contract' not in args and "-m quarantine" not in args:
                return True
            if "shard_tests.py" in args:
                return True
    return False


def test_the_gate_jobs_exist_and_are_bounded():
    wf, _ = _load("ci.yml")
    for name in ("gate", "gate-postgres", "gate-boot"):
        job = wf["jobs"][name]
        assert job.get("name", name) == name
        assert 0 < int(job["timeout-minutes"]) <= 15, name
        assert "needs" not in job, f"{name} must not wait on another job"
    assert wf["env"]["TZ"] == "Pacific/Kiritimati"
    assert wf["concurrency"] == {"group": "ci-${{ github.ref }}", "cancel-in-progress": True}
    on = _on(wf)
    assert set(on["push"]["branches"]) >= {"main", "production"}
    assert "pull_request" in on and "workflow_dispatch" in on


def test_the_gate_runs_every_check_the_release_depends_on():
    wf, _ = _load("ci.yml")
    text = _steps_text(wf["jobs"]["gate"])
    for needle in ("pip check", "py_compile", "check_colors.py", "check_timeouts.py", "check_silent_handlers.py",
                   "check_tenant_scope.py", "check_email_tokens.py", "check_labor_math.py", "pg_sql_audit.py --strict",
                   '-m "contract and not quarantine"', "test_inline_scripts_parse.py", "--recorded"):
        assert needle in text, needle
    assert "postgres" in wf["jobs"]["gate"].get("services", {})
    boot = _steps_text(wf["jobs"]["gate-boot"])
    for needle in ("scripts/boot_check.py", "boot_check.py --production", "--partitioned", "boot_check.py --gunicorn",
                   "scripts/upgrade_boot_check.py", "upgrade_boot_check.py --database-url"):
        assert needle in boot, needle
    assert wf["jobs"]["gate-boot"]["services"]["postgres"]["image"] == "postgres:16"


def test_no_gate_check_passes_by_finding_nothing_to_run():
    """The inline-script step used to pass silently when its test file was
    missing (`if [ -f … ]`, B1 F17)."""
    wf, _ = _load("ci.yml")
    for name in ("gate", "gate-postgres", "gate-boot"):
        for step in wf["jobs"][name]["steps"]:
            assert "if [ -f" not in str(step.get("run", "")), (name, step.get("name"))


def test_the_slice_also_runs_on_postgres_16():
    wf, _ = _load("ci.yml")
    job = wf["jobs"]["gate-postgres"]
    assert job["services"]["postgres"]["image"] == "postgres:16"
    assert job["env"]["CAVNAR_TEST_PG_URL"].startswith("postgresql://")
    runs = _pytest_runs(job)
    assert any('-m "contract and not quarantine"' in r for r in runs), runs


def test_every_pytest_run_names_a_per_test_timeout():
    for name in ("ci.yml", "nightly.yml"):
        wf, _ = _load(name)
        for job_name, job in wf["jobs"].items():
            for cmd in _pytest_runs(job):
                assert "--timeout=" in cmd, f"{name}:{job_name}: {cmd}"


def test_the_dev_lock_pins_the_test_tools_ci_relies_on():
    with open(os.path.join(ROOT, "requirements-dev.txt"), encoding="utf-8") as fh:
        pins = dict(line.split("==", 1) for line in fh.read().split() if "==" in line)
    assert pins.get("pytest-timeout") and pins.get("playwright") == "1.64.1", pins
    wf, _ = _load("nightly.yml")
    assert "pip install playwright" not in _steps_text(wf["jobs"]["browser"])


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


_INTERIM = "vars.PROMOTION_LIVE != 'true'"


def test_no_full_suite_blocks_a_push_to_main_or_production():
    """A full suite on the push path is only the interim one — conditioned on
    PROMOTION_LIVE not being "true" and needed by no gate job."""
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
        needed = {n for job in wf["jobs"].values() for n in ([job.get("needs")] if isinstance(job.get("needs"), str)
                                                             else job.get("needs") or [])}
        for job_name, job in wf["jobs"].items():
            if not _full_suite(job):
                continue
            assert _INTERIM in str(job.get("if", "")), f"{name}:{job_name} runs the full suite on every push"
            assert job_name not in needed, f"{name}:{job_name} is a full suite something waits on"


def test_until_promotion_is_live_every_push_to_main_runs_both_full_suites():
    """B1 F2 / B8: while Railway still deploys main on push, the first
    full-suite signal must come minutes after the push, not the next night."""
    wf, _ = _load("ci.yml")
    interim = {n: j for n, j in wf["jobs"].items() if _INTERIM in str(j.get("if", ""))}
    assert {"interim-full-sqlite", "interim-full-postgres"} <= set(interim), sorted(interim)
    for job in interim.values():
        assert "github.event_name == 'push'" in job["if"] and "refs/heads/main" in job["if"]
        assert _full_suite(job)
    assert interim["interim-full-postgres"]["strategy"]["matrix"]["shard"] == [1, 2, 3, 4]
    assert "CAVNAR_TEST_PG_URL" in interim["interim-full-postgres"]["env"]


def test_nothing_claims_main_no_longer_deploys_while_it_still_does():
    """Promotion is in force only once Railway deploys `production`; until
    then the docs and comments say so conditionally (B1 F2, B5 P0)."""
    paths = [os.path.join(WF, n) for n in os.listdir(WF)] + [
        os.path.join(ROOT, p) for p in ("scripts/promote.py", "TESTING.md", "docs/ops/RECOVERY.md")]
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            text = " ".join(fh.read().split()).lower()
        for claim in ("main no longer deploys", "pushing to `main` no longer deploys",
                      "it is the required status check on `main`"):
            assert claim not in text, (os.path.relpath(path, ROOT), claim)


def test_the_full_suites_are_nightly():
    wf, _ = _load("nightly.yml")
    on = _on(wf)
    assert "schedule" in on and "workflow_dispatch" in on and "push" not in on
    jobs = wf["jobs"]
    assert _full_suite(jobs["full-sqlite"]) and _full_suite(jobs["shifted-tz"])
    assert jobs["postgres"]["strategy"]["matrix"]["shard"] == [1, 2, 3, 4]
    assert "shard_tests.py" in _steps_text(jobs["postgres"])
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


def test_the_season_bot_lands_before_lunch_while_main_still_deploys():
    """14:00 UTC is 9am CDT / 8am CST; GitHub starts scheduled runs up to
    ~4 hours late, which still lands before dinner (B1 F10)."""
    wf, _ = _load("refresh-seasons.yml")
    assert [c["cron"] for c in _on(wf)["schedule"]] == ["0 14 * * *"]


def test_promotion_is_by_hand_and_only_through_the_script():
    wf, _ = _load("promote.yml")
    on = _on(wf)
    assert set(on) == {"workflow_dispatch"}
    assert set(on["workflow_dispatch"]["inputs"]) == {"sha", "emergency", "dry_run"}
    assert "scripts/promote.py" in _steps_text(wf["jobs"]["promote"])
    assert wf["concurrency"]["cancel-in-progress"] is False
    # It starts the nightly / iOS run on a SHA that has had none.
    assert wf["permissions"]["actions"] == "write"
    checkout = next(s for s in wf["jobs"]["promote"]["steps"] if "checkout" in str(s.get("uses", "")))
    assert checkout["with"]["fetch-depth"] == 0


def _checkouts(job):
    return [s for s in job.get("steps", []) if "actions/checkout" in str(s.get("uses", ""))]


def test_the_nightly_and_ios_runs_test_one_commit_and_say_which():
    """promote.py dispatches them with `sha` and matches the run by its
    run-name; a job that checked out main's head instead would vouch for a
    commit it never ran."""
    for name in ("nightly.yml", "ios.yml"):
        wf, _ = _load(name)
        assert "${{ inputs.sha || github.sha }}" in wf["run-name"], name
        assert "sha" in _on(wf)["workflow_dispatch"]["inputs"], name
        for job_name, job in wf["jobs"].items():
            for step in _checkouts(job):
                assert step.get("with", {}).get("ref") == "${{ inputs.sha || github.sha }}", (name, job_name)
    wf, _ = _load("nightly.yml")
    summary = next(s for s in wf["jobs"]["summary"]["steps"] if s.get("name") == "Summarise")
    assert summary["env"]["TESTED_SHA"] == "${{ inputs.sha || github.sha }}"


def test_the_ios_tests_run_on_ios_pushes_and_nightly():
    wf, _ = _load("ios.yml")
    on = _on(wf)
    assert on["push"]["paths"][0] == "ios/**" and "schedule" in on and "workflow_dispatch" in on
    job = wf["jobs"]["xctest"]
    assert int(job["timeout-minutes"]) == 40
    text = _steps_text(job)
    for needle in ("generate.sh", "build-for-testing", "test-without-building", "-scheme CavnarAI",
                   "-downloadComponent MetalToolchain"):
        assert needle in text, needle


def test_every_workflow_that_runs_generate_sh_can_count_the_commits():
    """generate.sh exits 1 on a shallow clone unless CAVNAR_BUILD_NUMBER is
    set (its build number is the commit count); the default checkout is depth
    1, so every iOS run failed at "Generate the project" (B1 F4, B6, B8)."""
    for name in os.listdir(WF):
        if not name.endswith((".yml", ".yaml")):
            continue
        wf, _ = _load(name)
        for job_name, job in wf["jobs"].items():
            gen = [s for s in job.get("steps", []) if "generate.sh" in str(s.get("run", ""))]
            if not gen:
                continue
            full = any(str((s.get("with") or {}).get("fetch-depth")) == "0" for s in _checkouts(job))
            numbered = all("CAVNAR_BUILD_NUMBER" in (s.get("env") or {}) for s in gen)
            assert full or numbered, f"{name}:{job_name} runs generate.sh on a shallow clone"
