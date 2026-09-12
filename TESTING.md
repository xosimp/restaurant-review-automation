# Testing — Cavnar AI

101 test files, 1,908 tests, ~4 minutes to run in full. **The full suite is not the default verification step for a normal task.** This file exists because running it on every single edit was costing 10+ minutes per turn for changes it had no chance of catching anything new in — a template color tweak doesn't need 1,900 tests re-run to prove it's safe.

## Default verification (most tasks)

1. **Run only the test file(s) that cover what changed.** `tests/` is organized so this is almost always obvious from the filename: `test_labor_module_ui.py` for Labor template changes, `test_ask_assistant.py` for Ask Cavnar, `test_shift_quality.py` for the scoring engine, etc. If unsure which file, `grep -l "<function or route name>" tests/*.py` finds it in one call — cheaper than running everything to see what fails.
2. **Write a real test for a new fix or feature, revert-check it once** (temporarily undo the fix, confirm the specific new test fails, restore it), then move on. This is the methodology that actually catches a bad test — running the full suite doesn't.
3. **Run `scripts/check_colors.py` only when a color literal changed** in a template or CSS. Run `tests/test_frontend_rules.py` only when dashboard inline JS changed.
4. **Skip the full suite.** A targeted file is 1–5 seconds; the full suite is ~4 minutes and re-checks ~1,900 things that weren't touched.

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

# Full suite — pre-push or full-audit only
python3 -m pytest -q -p no:warnings

# Color lint — template/CSS color changes only
python3 scripts/check_colors.py

# ES5/frontend rule check — dashboard.html inline JS changes only
python3 -m pytest tests/test_frontend_rules.py -q
```

## A gotcha worth knowing about before you distrust a revert-check

Editing a `.py` file, running its tests, restoring the original content, and re-running **in the same second** can read stale `__pycache__` bytecode instead of the restored source — a revert-check can falsely show "still passes" when the fix is actually back in place, because Python never re-read the file. If a revert-check result looks wrong, `find . -name __pycache__ -exec rm -rf {} +` and re-run before concluding anything. This has produced a real false negative in this codebase's history — see the git log around the Ask Cavnar re-audit.

## Test file naming

Files read as sentences, not identifiers — `test_a_new_share_gets_an_expiry_about_sixty_days_out`, not `test_share_expiry`. This is deliberate: a failing test's name should tell you what broke without opening the file.

## What NOT to do

- Don't run the full suite "just to be safe" after a change scoped to one file.
- Don't re-run a suite that already passed in this same turn with no code change since.
- Don't chase a test failure that's clearly pre-existing/unrelated to the current change — note it, don't fix it as a drive-by unless asked.
- Don't add a test that only re-checks the fixture it was written against (see `feedback_fixture_shaped_tests` in project memory) — assert against the source/logic when a rule must hold everywhere, not against one rendered payload.
