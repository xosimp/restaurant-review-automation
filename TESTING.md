# Testing — Cavnar AI

About 160 test files and 3,400 tests (`python3 -m pytest --collect-only -q | tail -1` is the count; do not type one into a doc), about 3 minutes to run in full in parallel (well over 10 serially — never run it without `-n auto`). **The full suite is not the default verification step for a normal task.** This file exists because running it on every single edit was costing 10+ minutes per turn for changes it had no chance of catching anything new in — a template color tweak doesn't need 1,900 tests re-run to prove it's safe.

## Default verification (most tasks)

1. **Run only the test file(s) that cover what changed.** `tests/` is organized so this is almost always obvious from the filename: `test_labor_module_ui.py` for Labor template changes, `test_ask_assistant.py` for Ask Cavnar, `test_shift_quality.py` for the scoring engine, etc. If unsure which file, `grep -l "<function or route name>" tests/*.py` finds it in one call — cheaper than running everything to see what fails.
2. **Write a real test for a new fix or feature, revert-check it once** (temporarily undo the fix, confirm the specific new test fails, restore it), then move on. This is the methodology that actually catches a bad test — running the full suite doesn't.
3. **Run `scripts/check_colors.py` only when a color literal changed** in a template or CSS. Run `tests/test_frontend_rules.py` only when dashboard inline JS changed. The other lints run inside the suite: `check_silent_handlers.py` (CI and `test_silent_handler_lint.py`), `check_timeouts.py` (`test_resiliency.py`), `check_email_tokens.py` (`test_email_audit_fixes.py`).
4. **Skip the full suite.** A targeted file is 1–5 seconds; the full suite is about 4 minutes in parallel and re-checks thousands of things that weren't touched.

## When the full suite *is* worth running

- Immediately before a **push to `main`** for anything non-trivial (a genuine pre-flight check, once, not repeated per edit within the same task).
- After a change to a **widely shared file** — `models.py`, `auth.py`, `ai_utils.py`, a shared template component used by every module — where the blast radius is genuinely unknown.
- When explicitly asked for a full audit or a re-audit closing out a set of findings.

Even then: run it **once**, not once "to be sure" and again "just to double check." If it's green, it's green.

## Commands

```bash
# One file — the normal case
python3 -m pytest tests/test_labor_module_ui.py -q -p no:warnings

# One test by name, when iterating on a single fix
python3 -m pytest tests/test_shift_quality.py::test_a_specific_case -q

# Full suite — pre-push or full-audit only. Parallel across every core
# (pytest-xdist, in requirements-dev.txt); --dist loadfile keeps a file's
# tests on one worker, so module-scoped fixtures such as the app
# subprocesses boot once per file. Each worker imports conftest on its own
# and gets its own throwaway default database. The db_path fixture copies
# one database per worker built by the real init_db() + ensure_columns()
# (conftest._db_template) instead of migrating a fresh file per test —
# ~230 ms a test, which was most of a full run.
python3 -m pytest -q -p no:warnings -n auto --dist loadfile

# Color lint — template/CSS color changes only
python3 scripts/check_colors.py

# ES5/frontend rule check — dashboard.html inline JS changes only
python3 -m pytest tests/test_frontend_rules.py -q
```

## A gotcha worth knowing about before you distrust a revert-check

Editing a `.py` file, running its tests, restoring the original content, and re-running **in the same second** can read stale `__pycache__` bytecode instead of the restored source — a revert-check can falsely show "still passes" when the fix is actually back in place, because Python never re-read the file. If a revert-check result looks wrong, `find . -name __pycache__ -exec rm -rf {} +` and re-run before concluding anything. This has produced a real false negative in this codebase's history — see the git log around the Ask Cavnar re-audit.

## Tests that read source

About 140 test sites read a template, a module or a doc as text (`open(...)`, `inspect.getsource`) — 24 files read `templates/dashboard.html`, 11 of them slicing it by position, and `docs/ops/RECOVERY.md`, `docs/app-store-submission.md` and `DESIGN_SYSTEM.md`'s Email section are pinned the same way. Before moving, splitting or renaming any of those, grep `tests/` for the path.

## Strategic features
`tests/test_strategic_foundations.py` (metrics, outcomes, goals, menu, demand, loss, issues) and `tests/test_strategic_features.py` (invoices with a fake Anthropic client, the web/mobile route twins and permission lines, the public issue link, the scheduled jobs). Both patch `get_conn` on every module they touch — each binds it at import.

## Edge-case regression net (`tests/test_edge_*`, iOS `Edge*Tests.swift`)

Written from the Production Edge Case & Failure Scenario Audit of 9/22/26, one file per area and topic: `test_edge_sec_*` (auth, staff PIN, permissions, imports, webhooks, admin), `test_edge_data_*` (data layer, async jobs, scheduler loop, backups, settings, idempotency), `test_edge_ai_*` (every model call and outside service), `test_edge_sched_*` (the schedule pipeline), `test_edge_mod_a_*` (reviews, labor, food cost, home, performance), `test_edge_mod_b_*` (marketing, intel, team, notifications, email, billing), `test_edge_client_web_*` (dashboard, staff portal, admin, accessibility). iOS: `ios/CavnarAI/CavnarAITests/Edge*Tests.swift`.

**A known defect is a strict expected failure, never a weakened assertion.** The test asserts the correct behaviour and carries `@pytest.mark.xfail(strict=True, reason="<FINDING-ID>: <defect>")` (Swift: `XCTExpectFailure("<ID>: …", strict: true)`). The day the defect is fixed the test XPASSes, strict turns that into a failure, and the marker comes off in the same commit as the fix — so `grep -rn "SEC-1:" tests/` finds the test that proves a fix. The IDs are the findings in `docs/audits/2026-09-22-edge-case-audit.md` (section 19 has every P0 and P1 in full). `pytest -q --runxfail tests/test_edge_<file>.py` shows every such test failing for the defect it names; use it after touching a pinned area.

Some older tests pin the defective behaviour and must change with the fix (the edge files' docstrings and the audit list them): e.g. `test_memberships.py::test_an_inactive_membership_falls_back_rather_than_granting_its_role` (SEC-1), `test_mobile_connections.py::test_connect_toast_saves_but_reports_error_when_token_fetch_fails` (SEC-25), `test_email_delivery.py`'s GET-unsubscribe and 2023-timestamp webhook tests (MOD-EML-5/9), `test_security.py`'s bare-id join link (MOD-MKT-9), `test_marketing_features.py`'s retry-on-500 (MOD-MKT-4).

**The test process never touches `./reviews.db`.** `conftest.py` points `RAILWAY_VOLUME_MOUNT_PATH` at a throwaway directory before `models` is imported (with an empty `reviews.db` already in it, so `adopt_legacy_db` does not copy the real one). Anything that opens the default database — `init_db`'s `ensure_columns()`, `status_manager`, a lazily imported module — lands there. Before this, a full run grew the developer's `reviews.db` by about 550 KB.

**Import modules up front in a test file that patches `get_conn`.** A module first imported inside a test while `models.get_conn` is patched keeps that test's redirect for the rest of the run; the edge files pre-import the modules they touch and patch every one.

## Test file naming

Files read as sentences, not identifiers — `test_a_new_share_gets_an_expiry_about_sixty_days_out`, not `test_share_expiry`. This is deliberate: a failing test's name should tell you what broke without opening the file.

## What NOT to do

- Don't run the full suite "just to be safe" after a change scoped to one file.
- Don't re-run a suite that already passed in this same turn with no code change since.
- Don't chase a test failure that's clearly pre-existing/unrelated to the current change — note it, don't fix it as a drive-by unless asked.
- Don't add a test that only re-checks the fixture it was written against (see `feedback_fixture_shaped_tests` in project memory) — assert against the source/logic when a rule must hold everywhere, not against one rendered payload.
