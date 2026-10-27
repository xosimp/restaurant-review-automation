# Testing — Cavnar AI

The suite's size is not typed here: `python3 -m pytest --collect-only -q | tail -1` (or `python3 scripts/repo_inventory.py --tests`) is the count. A full run in parallel takes a few minutes (about 3½ on a laptop, 10/24/26) and far longer serially — never run it without `-n auto`. **The full suite is not the default verification step for a normal task.** This file exists because running it on every single edit was costing many minutes per turn for changes it had no chance of catching anything new in — a template color tweak doesn't need thousands of tests re-run to prove it's safe.

## Tiers: what runs where (audit #25, 10/26/26)

The suite used to run in one place — CI, after Railway had already deployed the push, two hours late, red for clock reasons on 196 of 200 runs. It now runs in tiers, the fast one in front of production:

| Tier | When | Budget | What | Blocks |
|---|---|---|---|---|
| 0 · Pre-push | You touched a component in the registry (below) or SQL | ≤ 2 min | `python3 -m pytest -m "contract and not quarantine" -n auto -q -p no:warnings tests/`; `scripts/pg_sql_audit.py --strict` after SQL; the targeted files as always | your push (habit) |
| 1 · The gate | Every push to `main` and `production`, every PR (`.github/workflows/ci.yml`, jobs `gate`, `gate-postgres` and `gate-boot`, in parallel) | ≤ 10–12 min each (timeout 15) | `gate`: `pip check`, compile, the lints (colours, timeouts, silent handlers, tenant scope, email tokens, labor math, `pg_sql_audit --strict`), the contract slice on SQLite, every inline template script parses, every static statement and every statement the slice ran PREPAREd. `gate-boot`: `scripts/boot_check.py` — twice on a fresh SQLite volume, twice with production's Railway markers (the scheduler must start and complete a tick), twice on Postgres 16 built the way production's was (`--partitioned`: pg_copy, month-partitioned fact tables) and once with the markers there, once under gunicorn as `railway.json` starts it (queue worker beside it, then SIGTERM) — and `scripts/upgrade_boot_check.py` (the previous release's data, booted by this tree). `gate-postgres`: the same slice on Postgres 16. All at `TZ=Pacific/Kiritimati`, every test under `--timeout=300` | promotion (all three are required checks; `scripts/promote.py` refuses a SHA without them) |
| 1b · Interim | Every push to `main` while the repository variable `PROMOTION_LIVE` is not `true` (`ci.yml`, `interim-full-sqlite`, `interim-full-postgres`) | ≤ 45 min | the full suite on SQLite and on Postgres (four shards): until Railway deploys `production`, a push to main is live at once, so the full-suite signal comes minutes after it, not the next night | — (never needed by the gate) |
| 2 · Nightly | 07:00 UTC on main's head, and dispatched by `scripts/promote.py` on the SHA it is asked to promote (`.github/workflows/nightly.yml`, input `sha`; the run-name says which commit) | ≤ 45 min a job | the full suite on SQLite (UTC+14) and again at UTC-11, the full suite on Postgres in four shards (`scripts/shard_tests.py`), every recorded statement PREPAREd, the solver's determinism and benchmark files alone without xdist (more than one skipped case fails), the quarantined tests (reported, never failing), the Playwright smoke as an owner and a manager (`tests/browser/`), the `production` branch's protection (reported) | the promotion of THAT SHA: no nightly on it yet, or a red one, holds it until green or every failure is freshly quarantined |
| iOS | pushes touching `ios/**`, nightly, by hand, and dispatched by `promote.py` on a SHA whose promotion carries an `ios/` change (`.github/workflows/ios.yml`, full-history checkout) | ≤ 40 min | the Metal toolchain on Xcode 26, `ios/CavnarAI/generate.sh`, `xcodebuild build-for-testing`, `test-without-building` on an iPhone simulator | a promotion carrying an `ios/` change |

**The upgrade boot.** The gate's fresh boots meet every migration on an empty database, where an `ALTER` reads "duplicate column" and a new index, NOT NULL column or run-once rewrite meets no rows. `scripts/upgrade_boot_check.py` (blind re-audit 10/27/26, B5 P1; in the gate's `gate-boot` job, beside `boot_check.py`) boots the previous release (`origin/production`, else the merge-base with `main`, else `HEAD~1` — it needs the history: `fetch-depth: 0`) on a fresh SQLite and, with `--database-url`, a scratch Postgres 16, seeds every table, boots this code twice and the previous release once more on the result, and fails on a non-200, a schema gap, a missing or INVALID index, a failed boot statement or run-once migration, or an index built at boot on a table that release already had. ~20 s a backend. `tests/test_upgrade_boot_check.py` proves it catches what a fresh boot cannot.

**Promotion.** ONCE Railway's production service deploys the `production` branch (docs/ops/RECOVERY.md → Deploys; then set `PROMOTION_LIVE=true`), a push to `main` deploys nothing and `.github/workflows/promote.yml` (or `python3 scripts/promote.py --sha <sha> --dry-run` to see the verdict) is how a change reaches customers. UNTIL THEN a push to `main` still deploys at once; the promote flow only moves the branch. Promotion fast-forwards `production` to a `main` SHA with green `gate`, `gate-postgres` and `gate-boot`, 9:00–15:00 Chicago on a weekday, after a nightly THAT RAN ON THAT SHA is green (or every failure is quarantined at that SHA with a reason and a `since` date at most 14 days old) — with none yet, the script dispatches one and refuses until it is green — and, when the promotion carries an `ios/` change, a green iOS run on it. `--emergency` skips the window, the nightly and the iOS run, never the gate.

**The contract registry** (`tests/test_contract_slice_registry.py`, `REGISTRY`) names, component by component, the modules and the contract tests that guard them — the audit's top ten (`models.get_conn` / `db_dialect`; `models.get_restaurant` / `update_restaurant` / `has_module`; `auth`; `client_api._m` ↔ `mobile_api`; the labor % producers; `time_utils`; `dsr/`; `scheduler` / `jobs_registry` / `job_queue`; `permissions`; `templates/dashboard.html`) and what the blind re-audit added (`models.boot_schema` and the boot DDL rules; `config`'s send gate; `ops`; `ai_utils` / `model_gateway`; the send points `emails` / `notify` / `push`; `blueprints` / `csrf`; `client_compat`; tenant isolation; the manager-every-minute rule; rollback; the provider corpus and Stripe's real SDK). A change to one runs the slice before the push, whatever the task's size. The registry fails when a registered file or test is gone or has lost its mark, or when none of a component's tests EXERCISES it — calls it or reads one of its attributes; an import, or a reference handed only to `monkeypatch.setattr`, is not coverage. A file joins the slice with `pytestmark = pytest.mark.contract`, a single test with `@pytest.mark.contract`; `pytest -m contract` (or `-m "contract and not quarantine"`) collects only files that carry the marker (tests/conftest.py).

**Measured** (10/27/26, an 18-core laptop): the slice is 1,408 tests (it was 499) — 1:05 at `-n auto`, 1:52 at `-n 4` (what a 4-core runner has) on SQLite; 4:13 at `-n 8` on Postgres 16 (bound by per-test database clones, not CPU — hence 8 workers on 4 cores; `test_ask_cavnar_tools.py` alone is 2:53 of it); each `boot_check.py` boot 4–10 s. The workflows' own timings on GitHub are not recorded yet: run each once by hand after the first push and write them here.

**Markers** (registered in tests/conftest.py): `contract` (the slice), `quarantine` (a known-flaky test: out of the gate and the nightly's main jobs, run and reported every night — `@pytest.mark.quarantine(reason="…", since="10/27/26")`, a mark without both stops the run at collection; promote.py honours it for at most 14 days: quarantined the day it flakes, fixed or deleted soon after, never `skip`ped quietly), `browser` (Playwright, nightly), `sqlite_only` (reads the SQLite file itself; skipped on Postgres).

## Rules that keep the signal trustworthy

- **A fixed clock, always.** Take "today" the way the code does: `tests/clock.py` — `restaurant_today()`, `today_for(rid)`, `this_monday()`, `next_monday()`, `utc_now()`, and `frozen("2026-11-01 01:30", tz=…)` (time-machine) for a fixed instant. Never `date.today()` / `datetime.now(` / `utcnow(` in a test, and never a literal week that will one day be "already happened". `tests/test_test_ratchets.py` counts those reads per file against `tests/ratchets/wall_clock.json` and fails when one grows; CI runs at UTC+14, where a test that mixes the process's date with the restaurant's fails all day.
- **Behaviour over source text.** No new `inspect.getsource` assertion for behaviour: run the job twice and count the sends, make the requests and read the 429, gauge the lock. The same ratchet holds `inspect.getsource` call sites per file to `tests/ratchets/getsource.json`. Lower a baseline when a count drops: `python3 tests/test_test_ratchets.py --update`.
- **A skip says why, and only the expected ones pass CI.** CI prints every reason (`-rs`) and, with `CAVNAR_ENFORCE_SKIPS=1`, fails on a skip whose reason no line of `tests/expected_skips.txt` explains — a module-level skip at collection too (`pytest_collectreport`, forwarded by xdist's workers). A permission or data shape a test depends on is an `assert`, never a `pytest.skip`; a dev dependency the suite needs is a plain import, never a module-level `importorskip`.
- **Every test has a ceiling.** pytest-timeout (requirements-dev.txt): 600 s by default (`PYTEST_TIMEOUT`, tests/conftest.py), `--timeout=300` in CI — a deadlocked thread or barrier names its test instead of burning the job.
- **No test can reach production or the internet.** The suite refuses to start inside a Railway environment, drops `DATABASE_URL` / `SHADOW_DATABASE_URL` before anything imports (and the app ignores them off Railway without `ALLOW_LOCAL_PG=1`), holds `CAVNAR_TEST_PG_URL` to this machine, and blocks every socket connection that is not loopback, a unix socket or the scratch Postgres (an `OSError`, as offline; the attempt is listed at the end of the run — stub it — and fails a CI run, including one made outside a test function: during collection or in a module or session fixture, on an xdist worker too). A subprocess app gets the same guard from `scripts/boot_guard/sitecustomize.py` (`PYTHONPATH` + `CAVNAR_NET_GUARD=1`), as `scripts/boot_check.py`'s boots do. DNS lookups are not blocked (webhook URL vetting resolves names).
- **Production's configuration is tested too.** conftest flips switches production never flips (the model arm instead of the solver, Home built on the request, no request share, no password policy…). `tests/test_production_config_smoke.py` undoes every one and runs a solver generation, a Home load, logins under the policy and two-factor, and a burst; it fails when conftest gains a switch it does not undo.
- **A Postgres-only failure is a production bug until shown otherwise** — triage it within a day, and say "prod-real" or "test-only" in the commit. A run that touched `models.py` or `db_dialect.py` without `CAVNAR_TEST_PG_URL` ends with a "Postgres not exercised" note.
- **No customer's data in the repository** (it is public): fixtures are synthetic (`scripts/make_bar_invoice_fixture.py`); `tests/test_no_customer_data_in_repo.py` holds it.

## Default verification (most tasks)

1. **Run only the test file(s) that cover what changed.** `tests/` is organized so this is almost always obvious from the filename: `test_labor_module_ui.py` for Labor template changes, `test_ask_assistant.py` for Ask Cavnar, `test_shift_quality.py` for the scoring engine, etc. If unsure which file, `grep -l "<function or route name>" tests/*.py` finds it in one call — cheaper than running everything to see what fails.
2. **Write a real test for a new fix or feature, revert-check it once** (temporarily undo the fix, confirm the specific new test fails, restore it), then move on. This is the methodology that actually catches a bad test — running the full suite doesn't.
3. **Run `scripts/check_colors.py` only when a color literal changed** in a template or CSS. Run `tests/test_frontend_rules.py` only when dashboard inline JS changed. The other lints run inside the suite: `check_silent_handlers.py` (CI and `test_silent_handler_lint.py`), `check_timeouts.py` (`test_resiliency.py`), `check_email_tokens.py` (`test_email_audit_fixes.py`).
4. **Skip the full suite.** A targeted file is 1–5 seconds; the full suite is about 4 minutes in parallel and re-checks thousands of things that weren't touched.

## When the full suite *is* worth running

- After an **audit, re-audit or fix round**, or a **big code change / large batch of changes** (many files, a refactor, a new module).
- When explicitly asked.
- **Not** before every push, and **not** after a few tweaks — those get the targeted test files that cover them, then push (Will, 9/15/26 and 9/28/26: the 4-5 minute suite after small changes "stalls productivity"). Touching a registered component (Tiers, above) runs `pytest -m "contract and not quarantine" -n auto` (under a minute on a laptop) before the push, besides the files that cover the change; the full suite runs nightly.

Even then: run it **once**, not once "to be sure" and again "just to double check." If it's green, it's green.

## Commands

```bash
# One file — the normal case
python3 -m pytest tests/test_labor_module_ui.py -q -p no:warnings

# One test by name, when iterating on a single fix
python3 -m pytest tests/test_shift_quality.py::test_a_specific_case -q

# Full suite — audits, fix rounds and big changes only (never a routine
# pre-push step). Parallel across every core
# (pytest-xdist, in requirements-dev.txt); --dist loadfile keeps a file's
# tests on one worker, so module-scoped fixtures such as the app
# subprocesses boot once per file. Each worker imports conftest on its own
# and gets its own throwaway default database. The db_path fixture copies
# one database per worker built by the real init_db() + ensure_columns()
# (conftest._db_template) instead of migrating a fresh file per test —
# ~230 ms a test, which was most of a full run. Since 10/20/26 the template
# also holds hosted_dashboard's other boot tables (conftest._boot_schema:
# init_auth, push, webhooks, guest_marketing and the rest), so a per-test
# init_auth finds them instead of building them plus ~40 ALTERs each time
# (15:01 -> 7:47 for the whole suite). A test that copies or compares a whole
# database builds its own target with _boot_schema too.
python3 -m pytest -q -p no:warnings -n auto --dist loadfile

# Color lint — template/CSS color changes only
python3 scripts/check_colors.py

# ES5/frontend rule check — template inline JS changes only (~2 s)
python3 -m pytest tests/test_frontend_rules.py tests/test_inline_scripts_parse.py -q

# The browser smoke — once playwright is installed (see "Web pages in a real browser")
python3 -m pytest tests/browser -q -p no:warnings
```

## On Postgres (`CAVNAR_TEST_PG_URL`, blind re-audit 10/11/26 SCALE-10)

The same tests run with their database on Postgres when `CAVNAR_TEST_PG_URL`
names a scratch server (never production's; a throwaway local cluster or CI's
service): `tests/conftest.py` routes the default database and every
`db_path` to a database of its own (`models._PG_ROUTES`), copied per test
from a template, and dropped after. The template is built the way
production's database was (blind re-audit 10/20/26 R9-02): the boot's
migrations on SQLite, `pg_copy.copy_database` into Postgres (so the fact
tables in `db_dialect.PARTITIONED` are month-partitioned, their keys
carrying the partition column), then the boot again on Postgres. A test's
own `sqlite3.connect(<its db_path>)` — seeding a fixture, reading a row back
— is served by that same Postgres database, rows shaped as sqlite3's; the
app's own `sqlite3.connect` calls are never redirected. Postgres enforces
foreign keys SQLite's `PRAGMA foreign_keys=OFF` let a fixture skip: a
fixture names real parent rows.

The scratch server is Postgres 16, as production and CI (`scripts/check_workstation.sh` checks :55433): an older one fails statements 16 accepts. What cannot run on Postgres is listed in ONE place, with its reason: a test
that reads the SQLite file itself (a snapshot, `EXPLAIN QUERY PLAN`, `PRAGMA
query_only`, the file's bytes) is marked `@pytest.mark.sqlite_only`, and a
whole such file goes in `PG_MODE_SKIP_FILES` in `tests/conftest.py`. A file
that fails on Postgres for a reason production would meet is a bug to fix,
never an entry. `tests/test_q2_postgres.py`'s live half (the boot twice in
its own process, the copy then the boot, every static statement PREPAREd,
the dialect's semantics) runs only in this mode. The nightly `postgres` job
runs EVERY test file this way, in four shards (it was a hand-picked list of
~40, and the notification badge's Postgres-only failure sat outside it; then
one ~2-hour job); locally:

```
CAVNAR_TEST_PG_URL=postgresql://postgres@127.0.0.1:55433/postgres \
    python -m pytest -q -p no:warnings -n 3 --dist loadfile tests/test_q2_postgres.py tests/test_models.py
python scripts/pg_sql_audit.py --database-url postgresql://postgres@127.0.0.1:55433/scratch
```

**The statements built at run time.** The source audit PREPAREs only SQL
whose text is fully written in the source. `CAVNAR_RECORD_SQL=<dir>` on a
SQLite run records every distinct statement the app executed through a
models connection (f-string holes filled, IN lists folded to one shape) into
`<dir>/sql-<pid>.jsonl`; `pg_sql_audit.py --database-url … --recorded <dir>`
translates and PREPAREs each (`recorded_failed` must be empty; a table the
boot does not build is listed apart as `recorded_unknown_table`). The gate
records the contract slice's statements and PREPAREs them in the same job,
even when a test failed (`if: always()`); the nightly does the same for the
whole SQLite suite.

## A gotcha worth knowing about before you distrust a revert-check

Editing a `.py` file, running its tests, restoring the original content, and re-running **in the same second** can read stale `__pycache__` bytecode instead of the restored source — a revert-check can falsely show "still passes" when the fix is actually back in place, because Python never re-read the file. If a revert-check result looks wrong, `find . -name __pycache__ -exec rm -rf {} +` and re-run before concluding anything. This has produced a real false negative in this codebase's history — see the git log around the Ask Cavnar re-audit.

## Tests that read source

Many tests read a template, a module or a doc as text — 768 `inspect.getsource` call sites in 208 files (10/26/26; the ratchet above only lets that fall) and about 200 files reading `templates/dashboard.html`, some slicing it by position, and `docs/ops/RECOVERY.md`, `docs/app-store-submission.md` and `DESIGN_SYSTEM.md`'s Email section are pinned the same way. `tests/test_docs_controls.py` reads `docs/ops/SECURITY.md`, `RECOVERY.md` and `RAILWAY_SCHEDULER_SPLIT.md` sentence by sentence against the code, and `tests/test_architecture_manifest.py` reads `ARCHITECTURE_MANIFEST.md`'s module table. Before moving, splitting or renaming any of those, grep `tests/` for the path.

## iOS request bodies against their routes (`tests/test_ios_request_body_parity.py`, parity audit #24)

Five Critical parity bugs were one bug: a hand-written Swift `Encodable` body left out a key the server reads, and the server quietly did something else. This test reads both halves from source and fails when a route reads a key that no iOS caller of that route sends. It runs in about 7 s.

- **Swift side.** It parses every Swift file of the app's targets, not the tests. It finds each call that names a write `method:` (`send`, `sendKeepingBody`, `sendWithHeaders`, `sendWithBearer`, `QueuedWrite`, a view model's own `post(_:body:)`, and so on) and follows wrapper functions out to the call that names the path. It then turns the body into its JSON keys:
  - a struct's stored properties
  - `CodingKeys` raw values
  - the `forKey:` cases of a custom `encode(to:)`
  - a dictionary literal's keys, and the `body["k"] = …` writes to it
  - keys sent in a `query:` string

  `JSONEncoder.cavnar` has no key strategy, and `test_the_encoder_has_no_key_strategy` keeps it that way.
- **Server side.** A subprocess imports `hosted_dashboard`. Importing it in-process would wire CSRF onto `client_bp` for the rest of the worker. For every write rule under `/mobile/` and `/staff/`, the subprocess reads the keys the view takes from the JSON body:
  - `.get`, `[...]` and `in`
  - a `_body()` helper
  - `request.args.get` for keys the route also takes from the query string

  It follows the functions the body is handed to, the callbacks it is handed through, and the functions that reach `request` themselves, three levels deep. `b.get("a") or b.get("b")` counts as one value under two names.
- **The comparison** is per route, over every iOS caller: a partial update spread over several sheets counts as one body.

  A key no caller sends fails, unless `NOT_SENT_BY_IOS` lists it. Each entry there was checked against the route and the web's own body: optional with a default the web also omits, the note beside a reason code, a web-only control, or a key that travels as a header or under another name.

  A write whose route or body the server named, such as a push action's path, a confirmed Ask proposal or the offline queue's replay, sits in `UNREADABLE_OK`. Both lists fail when they go stale.

  A key the phone sends that the route never reads is only a warning, unless it is one edit away from a key the route reads; then it is a typo, and fails.

When it fails on new code, fix the Swift body, and add an XCTest for the body's shape (`RequestBodyParityTests.swift`). Don't extend the allowlist to get it passing.

## iOS responses against the server's real payloads (`tests/test_ios_schedule_payload_contract.py`, schedule audit 10/10/26 TRUST-1)

The other direction. The server sent the schedule's `narrative` as a list; the phone declared a string, so every finished generation polled on the phone failed to decode — and the XCTest that should have caught it decoded a hand-written fixture holding a string. A fixture an iOS test decodes is now **captured**, never typed:

- The Python test runs a real generation (only the model's answer stubbed), polls `/mobile/api/labor/schedule-status` while it runs and when it is done, and reopens the week through `/mobile/api/labor/schedule-history/<id>`. It fails when any of the three payloads has changed the KIND of a value the fixture holds (a list where a string was, a fractional number where only whole ones were), and when a key `GeneratedSchedule` declares is sent but missing from the fixture.
- `CAVNAR_WRITE_IOS_FIXTURES=1 python -m pytest -q tests/test_ios_schedule_payload_contract.py` re-captures `ios/CavnarAI/CavnarAITests/Fixtures/schedule_status_done.json`, `schedule_status_pending.json` and `schedule_history_detail.json` (volatile timestamps written as `<volatile>`). Review the diff.
- `ScheduleStatusContractTests.swift` decodes each one whole and key by key and asserts `undecodedKeys` is empty: `GeneratedSchedule` decodes field by field, so an odd field costs that field (named in `undecodedKeys`), never the week.

## Provider payloads and the real Stripe SDK (`tests/test_provider_recorded_payloads.py`, `tests/test_stripe_real_sdk.py`, audit #25 A06-08 / A01-13)

`tests/fixtures/providers/` holds payloads in the providers' real shapes, every one SYNTHETIC (the repo is public): `rpower/postman_shape_day.json` (one invented store's night, each record carrying exactly the fields of RPOWER's own Postman example, plus the shapes found live — a Windows time-zone name, the 2000-01-01 "never set" stamp, a void as an Error Correct line, timeclock's exclusive enddate), `rpower/demo_store_day.json` (the showcase simulator's night of 10/10/26, frozen and trimmed) and `stripe/*.json` (webhook events in both invoice API shapes the code reads, 2025-02-24.acacia and 2025-03-31.basil). The RPOWER files are answered through the whole HTTP read path (`requests.get` stubbed, `rpower._request`, the response contract, paging) into the readers; the Stripe files are built into the installed SDK's objects with `stripe.Event.construct_from`. Each test states the facts a reader must derive.

- A Stripe TEST-mode event (`stripe trigger …`, or a test-mode event's JSON from the dashboard) carries no customer data and may replace a synthetic file as is. A live RPOWER answer must have every name, phone, id and amount replaced first.
- `tests/test_stripe_real_sdk.py` puts no stand-in in `sys.modules`: events are signed with `stripe.WebhookSignature` and POSTed through `/stripe-webhook`, the tolerance clock pinned (`stripe._webhook.time`), and it fails when the SDK in the venv is not the version `requirements.txt` locks. Use it, not a stand-in module, for anything about signatures, object types or `config.stripe_api`'s bound.

## Every screen's main read, and the builds that read it (`tests/test_ios_response_shapes.py`, release-safety audit #25)

The schedule contract's pattern, for the top GET routes the app reads: `me`, `account`, `home` (it carries the brief), `reviews`, `notifications`, `labor`, `food_cost`, `food_cost_bar`, `marketing`, `intel`, `actions`, `staff_shifts`, `staff_me`, `dsr_preview` and `dsr_week` (the night and the week, as the owner and as a manager) — and since the blind re-audit (10/27/26) 42 more: the rest of every main screen's reads (Modules, the badges, Labor's team, time off and forecast, Food Cost's CFO, waste trend, Intel's movement, the DSR list, data health, task sheets, outcomes, value, goals, DNA, guests, people, recommendations, benchmarks, Ask's conversations), the manager's own Home, Labor, Reviews and Food Cost (a second manager granted Food Cost), and sixteen staff-app screens. `ROUTES` in `tests/ios_shape_capture.py` is the list; each new route needs a decode line in `ResponseShapeContractTests.swift` naming the model its screen holds.

- `tests/ios_shape_capture.py` builds Halsted & Rye — the fictional demo store (`demo_store`), its POS answering RPOWER's own API, so the archive, the DSR, Labor, Food Cost and the Bar come from the real pipelines — in the test's database (about 20 seconds; the restaurant's own today, so always fresh), signs in an owner, a manager and one employee, and calls each route. The only model answer is the night's DSR narrative, written from the prompt's own figures.
- Each payload is compared with `ios/CavnarAI/CavnarAITests/Fixtures/<route>.json` by kind, all the way down (`shape_mismatches`). `<route>.v1.json` is what the builds installed today read: every schema key in it (snake_case; a data key such as a department is not schema) must still be sent where its object is, and keep its kind — `CONDITIONAL` lists the few keys sent only some nights, each with why. Remove a key from a v1 file only when `MOBILE_MIN_BUILD` retires the builds that read it, and replace v1 with a v2 copy in that commit.
- `CAVNAR_WRITE_IOS_FIXTURES=1 python -m pytest -q tests/test_ios_response_shapes.py` re-captures every `<route>.json` (volatile stamps and tokens as `<volatile>`); a missing `<route>.v1.json` is written then too, an existing one only with `CAVNAR_WRITE_IOS_V1=1`. Every address in a fixture is reserved (`.example`, `.invalid`, `.test`) and the test fails on anything shaped like a credential.
- **A null is a break where Swift holds a non-optional** (blind re-audit 10/27/26): `tests/ios_swift_optionality.py` reads, from the Swift sources (the body-parity test's index), which keys each model that decodes a route REQUIRES — a synthesized non-optional property, a key read with `try c.decode(…, forKey:)` or bound by `guard let … = try? … else { throw }` — and a null in the live payload where the fixture held a value fails there (`null_breaks`; `NULLABLE` lists, with why, a key the parser misreads). The same reading simulates JSONDecoder on every fixture and on the live payload (`decode_problems`: a required key missing or null, a string for a number, a fraction for an Int) — a Python mirror of `ResponseShapeContractTests` that runs without a simulator.
- `ResponseShapeContractTests.swift` decodes every fixture and its v1 copy with the app's real model. The hand-written DSR and Bar files the view tests read their content from are `dsr_preview_scenario.json`, `dsr_week_scenario.json` and `food_cost_bar_scenario.json`, held to the server's shape by the Python test.
- `DynamicTypeRenderTests.swift` renders the six main screens' glance (Home's brief and KPI tiles, the inbox rows, Labor, Food Cost, the night, the staff app's days) from those fixtures at `.accessibility3` with `ImageRenderer` and checks every piece's size: never zero, never wider than the phone, and taller than at the default size (a piece that does not grow is a fixed frame clipping its text).
- `ClientCompatibilityTests.swift`: the `X-Cavnar-App` / `X-Cavnar-Platform` headers on every transport, the 426 update sheet's state and its re-check (the gate clears when the server stops answering 426), and the decode-failure report — its hour spent only by a delivered report (`MockURLProtocol`). The server's half is `tests/test_client_compat.py`.
- **`tests/test_ios_api_v1.py`** freezes what the installed builds SEND: `tests/fixtures/ios_api_v1.json` holds every path literal (with its method where the call site says), and every write's method, path and body keys, read from the Swift at the commits those builds came from (`generated_from`). Every v1 path must still resolve (taking its method), every v1 write must still land on a route taking its method, a body key a v1 build sends that the server read then must still be read (a rename fails), and a key the server newly reads that v1 never sends must be listed in `V1_OPTIONAL` with why it is optional. Regenerate only in the commit that raises `MOBILE_MIN_BUILD`: `python tests/test_ios_api_v1.py --write --ref <store build's commit> --ref HEAD`.

## The web dashboard's main reads (`tests/test_web_response_shapes.py`, blind re-audit 10/27/26)

The phone's pattern for the browser: 36 reads the dashboard's panels make on open (the Home brief, actions, value, goals, DNA, data health, Reviews' stats and why-line, Labor's team, roster, time off, trend, gap and inbox, Food Cost's CFO, Bar, COGS, PARs and dishes, Intel, Marketing, guests, people, task sheets, the staff brief, Settings, billing, the DSR list, night and week — as the owner and, where the figures are per viewer, as a manager) captured on the same demo store with a web session cookie through `blueprints.url_map_app()`, compared by kind with `tests/fixtures/web/<name>.json`, and `<name>.v1.json` holding what an open tab's script reads — a key in it the server stops sending fails. `CAVNAR_WRITE_WEB_FIXTURES=1` re-captures; `CAVNAR_WRITE_WEB_V1=1` replaces the v1 copies deliberately.

The iOS command (after `ios/CavnarAI/generate.sh`; the build number comes from the commit count, so a shallow clone must set `CAVNAR_BUILD_NUMBER`):

```bash
cd ios/CavnarAI && xcodebuild -project CavnarAI.xcodeproj -scheme CavnarAI \
  -destination 'platform=iOS Simulator,name=iPhone 17' test \
  -only-testing:CavnarAITests/ResponseShapeContractTests -only-testing:CavnarAITests/ClientCompatibilityTests \
  -only-testing:CavnarAITests/DynamicTypeRenderTests -only-testing:CavnarAITests/StaffKnowledgePlanTests
```

## The iPhone's schedule fix round wires (`tests/test_sf10_i2b_ios.py`, 10/10/26)

The pre-publish review, Crew, locks, Stop, the quality delta, the closing stay, the full-time line, the lunch/dinner boundary, the staff app's consent and agreements, the labor-laws screen and On the floor: each test holds both sides of one wire — the keys the Swift type decodes against the keys the server module writes, and the path the phone calls against the route table — so a renamed key fails in the Python suite. The decode logic itself is `CavnarAITests/SchedFixI2bTests.swift` (compiled with `xcodebuild build-for-testing`).

## Strategic features
`tests/test_strategic_foundations.py` (metrics, outcomes, goals, menu, demand, loss, issues) and `tests/test_strategic_features.py` (invoices with a fake Anthropic client, the web/mobile route twins and permission lines, the public issue link, the scheduled jobs). Both patch `get_conn` on every module they touch — each binds it at import.

## Edge-case regression net (`tests/test_edge_*`, iOS `Edge*Tests.swift`)

Written from the Production Edge Case & Failure Scenario Audit of 9/22/26, one file per area and topic: `test_edge_sec_*` (auth, staff PIN, permissions, imports, webhooks, admin), `test_edge_data_*` (data layer, async jobs, scheduler loop, backups, settings, idempotency), `test_edge_ai_*` (every model call and outside service), `test_edge_sched_*` (the schedule pipeline), `test_edge_mod_a_*` (reviews, labor, food cost, home, performance), `test_edge_mod_b_*` (marketing, intel, team, notifications, email, billing), `test_edge_client_web_*` (dashboard, staff portal, admin, accessibility). iOS: `ios/CavnarAI/CavnarAITests/Edge*Tests.swift`.

**A known defect is a strict expected failure, never a weakened assertion.** The test asserts the correct behaviour and carries `@pytest.mark.xfail(strict=True, reason="<FINDING-ID>: <defect>")` (Swift: `XCTExpectFailure("<ID>: …", strict: true)`). The day the defect is fixed the test XPASSes, strict turns that into a failure, and the marker comes off in the same commit as the fix — so `grep -rn "SEC-1:" tests/` finds the test that proves a fix. The IDs are the findings in `docs/audits/2026-09-22-edge-case-audit.md` (section 19 has every P0 and P1 in full). `pytest -q --runxfail tests/test_edge_<file>.py` shows every such test failing for the defect it names; use it after touching a pinned area.

Some older tests pin the defective behaviour and must change with the fix (the edge files' docstrings and the audit list them): e.g. `test_memberships.py::test_an_inactive_membership_falls_back_rather_than_granting_its_role` (SEC-1), `test_mobile_connections.py::test_connect_toast_saves_but_reports_error_when_token_fetch_fails` (SEC-25), `test_email_delivery.py`'s GET-unsubscribe and 2023-timestamp webhook tests (MOD-EML-5/9), `test_security.py`'s bare-id join link (MOD-MKT-9), `test_marketing_features.py`'s retry-on-500 (MOD-MKT-4).

**The test process never touches `./reviews.db`.** `conftest.py` points `RAILWAY_VOLUME_MOUNT_PATH` at a throwaway directory before `models` is imported (with an empty `reviews.db` already in it, so `adopt_legacy_db` does not copy the real one). Anything that opens the default database — `init_db`'s `ensure_columns()`, `status_manager`, a lazily imported module — lands there. Before this, a full run grew the developer's `reviews.db` by about 550 KB.

**Import modules up front in a test file that patches `get_conn`.** A module first imported inside a test while `models.get_conn` is patched keeps that test's redirect for the rest of the run; the edge files pre-import the modules they touch and patch every one.

## Admin-console fix round (`tests/test_fix_*`, 9/29/26)

One file per workstream and topic, named `test_fix_<workstream>_<topic>.py`; each docstring names the audit findings (#1–#162) it proves. Run the files for the workstream whose code you touch — `grep -l "#<finding>" tests/test_fix_*.py` finds the test that proves a fix.

| Files | Cover |
|---|---|
| `test_fix_a_admin_two_factor.py`, `test_fix_a_sessions.py`, `test_fix_a_view_as.py`, `test_fix_a_login_actions.py`, `test_fix_a_lockout_seed_policy.py` | auth and sessions: an internal login's own second factor on every sign-in path and its enrolment; session expiry form, the 12-hour admin session and step-up; view-as (the banner through the real app, attribution, the 2-hour cap, stop-viewing); what the console does to one login (role change, deactivate, reset 2FA, clear a lockout, revoke sessions, support logins); per-address lockouts, the boot seed, the password policy, where a sign-in code went |
| `test_fix_b1_client_settings.py`, `test_fix_b1_admin_scoping.py`, `test_fix_b1_admin_tools.py`, `test_fix_b1_data_layer.py` | the legacy admin pages: touched-only settings saves with field-level concurrency, escaped rendering (a JS scanner over the template and a node render of hostile names), tools that act on the client they manage, menu extraction and new-client setup as bounded jobs, staff notes that add, one formula-safe CSV writer, one POS store per restaurant |
| `test_fix_b2_audit.py`, `test_fix_b2_support_routes.py`, `test_fix_b2_lifecycle.py`, `test_fix_b2_sales_audit.py` | the audit trail (after-response, refused attempts apart, typed before/after), support routes that send links and report real results, the demo guard, offboarding and delete, bug reports and notes, add-location, hashed expiring share links |
| `test_fix_c_console.py`, `test_fix_c_billing.py`, `test_fix_c_signals.py` | the console's data layer: one zone per column, failures that surface, occurrence-scoped resolve, the fleet memo and its 503, paging; MRR from the subscription mirror; the signals it raises (revoked Google, suppressed owners, Data Health, stalled AI, deletion deadlines, AI quality apart from failed jobs, bounce rates) |
| `test_fix_d_backup.py`, `test_fix_d_jobs.py`, `test_fix_d_ops.py`, `test_fix_d_sweeps.py`, `test_fix_d_watchdog.py` | the backup (SigV4 against AWS's published vectors, the scrub registry — it fails on an unclassified credential-looking column), the job registry against the loop's `run_job` calls and the standard counts (`_PENDING`, the jobs not yet converted, is empty since the integration wave), Run now, ops bookkeeping, bounded sweeps and missed windows, the heartbeat and operator paging — including a real tick |
| `test_fix_e_email_core.py`, `test_fix_e_sms.py`, `test_fix_e_sends.py`, `test_fix_e_jobs.py` | every sender returns a `SendResult`; UTC `email_log`; scoped, audited suppressions; the SMS ledger and Twilio's status callback; `textable`; issue texts claimed and retried; outboxes; the brief's per-person ledger; the storm cap; onboarding gates |
| `test_fix_f_health.py`, `test_fix_f_platform.py`, `test_fix_f_routes.py`, `test_fix_f_provider_health.py`, `test_fix_f_credentials.py`, `test_fix_f_http_logging.py`, `test_fix_f_deploy.py` | `/health` (read-only, the keyword, the codes, the volume marker), telemetry, boots, the supervisor, the system card and incidents, provider probes, credentials at rest, request ids and logs, and the deploy shape (the Python pin, the lock, one start command, the healthcheck) |
| `test_fix_g_ledger.py`, `test_fix_g_health.py`, `test_fix_g_places_perplexity.py`, `test_fix_g_quality_trace.py`, `test_fix_g_admin.py` | the AI ledger (every outcome, attribution, ceilings), outage paging and the public AI status, Places and Perplexity behind their own ceiling and breakers, quality events, the redacted call trace and the rollup, the console's AI pages |
| `test_fix_h_billing_state.py`, `test_fix_h_outbox_and_jobs.py`, `test_fix_h_docusign.py`, `test_fix_h_owner_surfaces.py`, `test_fix_h_admin_billing.py`, `test_fix_h_billing_emails.py` | billing: status history, holds, the mirror and the Stripe lifecycle; the owed-sends outbox and the billing jobs; the contract side; what the owner sees (billing info, pay links, pause and resume); the console's billing actions; the billing emails |
| `test_fix_ui_ui1.py` (UI wave 1, merged after this list was first written) | `templates/admin.html`'s Overview, Customers, client page, rail, palette and modals: unknown is not zero, Resolve carries its occurrence, every write control is hidden from support (`.w`), no password path, the timezone list, and a render of the console over 31 JSON reads |
| `test_fix_ui_ui2.py` (UI wave 2, 59 tests) | Operations, Engineering and Analytics in `templates/admin.html`: source rules (every write control `.w`, Run now refreshes its own page, no "Every key is configured" or "since the last deploy", incident keys from the server); behaviour under node on real payload shapes, skipped where node is not installed (a failed read is unknown, never all clear; failed or stuck jobs, a wedged scheduler, an AI outage and a Twilio account error turn red; failures split by kind; the disabled sending-job button; run squares; empty days kept; the withheld score); and a render of `GET /admin` with 31 reads checked field by field |
| `test_fix_int1_console.py`, `test_fix_int1_jobs.py`, `test_fix_int1_wiring.py` (integration wave INT-1) | the console's data layer reading every workstream's stores (E's messaging and outboxes, H's billing ledgers, G's AI issues, B2's checklist, D's backup and failure columns, the storm caps, webhook health from `inbound_webhook_health`); the eight jobs other workstreams built, registered and driven by a real `scheduler_loop` tick, the minute duties reaping both outboxes, the new retention entries and the AI rollup before its prune; a job run's log and AI context, the redacted traceback a capture logs, paging on broken messaging, the brief's retry, view-as attribution, the worker's boot order, a page by text never held up by a locked database, every `unparseable` mark, a test text's last four digits |
| `test_fix_int2_admin.py`, `test_fix_int2_owner.py` (integration wave INT-2) | the admin side: the step-up on every sensitive route and the three partial cases, the alert-contact test and contract resend refused locally, one typed audit call (actor, before, after, result), one admin job pool, one welcome email (409 suppressed), the admin billing override's attribution, the offboarding steps that act (a covered location's subscription never cancelled; nothing reaches Stripe or DocuSign from a local backend), `delete_restaurant` never taking an internal login, `/admin/audits/new` creating nothing on a GET, the settings JSON; the owner side: POS connects refusing a store bound elsewhere, holds shown as holds, view-as logging, redraft-all never overwriting an owner edit, the brand-colour and email-history rendering, operator mail never suppressed, the manual POS syncs' shared path, generated passwords |
| `test_fix_ui_ui3.py` (UI wave 3, merged after the integration wave) | what the console shows of the integration wave: issue texts (given up, retrying, not texted by choice) on Operations → Push & alerts and the client's Texts, each alert's channels, email still in flight apart from delivered and bounced, and the client's menu notes edited on the Data tab under the settings contract (only the field, `expected_version`, `base`; a 409 refills the box and keeps the admin's text) — source rules, behaviour under node, and a render check over every read and the save |
| `test_fix_integration_lead.py` | the lead's integration fixes: the audit keeps only a phone's last four digits, a thread a request starts keeps its AI attribution (`ai_utils.attributed` on schedule generation and the Ask stream), the console's busy refusal is not a server error; and from the full suite on the merged round: a stale count is never its own repeat waste offender, a failed messaging read on the client page is a query error (never an empty, healthy list), and every link to a legacy client page is hidden from a support login (a source-wide rule) |
| `test_fix_owner_decisions.py` | the owner's decisions after the fix round (9/29/26): a past-due client keeps the paid AI ceiling — `ai_utils._PAID_BILLING_STATES` equals `models.PAYING_BILLING_STATES` plus internal, and past-due spend draws on the paying pool and is counted in it (the console's tier too); every trial together answers to the trial pool (`AI_TRIAL_POOL_DAILY_USD` / `_MONTHLY_USD`), apart from the paying pool — never a demo's or an admin's spend, a burst inside the cache window still trips it, 80% and a stop page; the console shows it |
| `test_docs_controls.py` | every control `docs/ops/SECURITY.md`, `RECOVERY.md` and `RAILWAY_SCHEDULER_SPLIT.md` state, against the code that implements it (#146) — and `DATABASE_SCHEMA.md`'s scrub list and retention table against their registries: change a control and its sentence together |

Several of these files bind `get_conn` at import (CLAUDE.md's bound-import hazard) and pre-import the modules they patch; the D files also pass alone and in any order. `tests/conftest.py` resets the fix round's process-local state before each test (the admin request window, the AI limiter and breakers) and turns off `auth.ENFORCE_PASSWORD_POLICY` so fixtures keep short passwords — the policy's own tests turn it back on.

## Memory fix round (`tests/test_mem_*`, 9/29/26)

From the AI Memory & Long-Term Learning audit (86 items). One file per workstream and topic, named `test_mem_<workstream>_<topic>.py`; each docstring names the audit item it proves. Run the files for the workstream whose code you touch — `grep -l "<item or module>" tests/test_mem_*.py` finds them. The files patch `models.get_conn` and `models.DB_PATH` (the fixture in `test_fix_owner_decisions.py`) and pass `db_path` explicitly to modules that still bind it at import (`morning_brief`, `goals`, `delayed`); a model client is always stubbed.

| Files | Cover |
|---|---|
| `test_memory_context.py`, `test_mem_m2_assembler.py` | the one memory reader every model call uses: the provider contract, relevance order, per-section budgets, viewer scoping once, section sizes logged |
| `test_mem_m1_silences.py`, `test_mem_m1_answers.py`, `test_mem_m1_kinds.py`, `test_mem_m1_signatures.py`, `test_mem_m1_conflicts.py`, `test_mem_m1_trust.py`, `test_mem_m1_prompts.py` | recommendation memory: what an answer holds and for how long, who answered and what the reasons teach, quiet and held kinds, the weekly plan and support's hands (view-as), advice signatures, lever conflicts, the trust automations earn, decisions as a model reads them (fenced, relevance-ranked) |
| `test_mem_m2_owner_memory.py`, `test_mem_m2_goals.py`, `test_mem_m2_preferences.py`, `test_mem_m2_conversations.py`, `test_mem_m2_ask_reach.py`, `test_mem_m2_ask_feedback.py`, `test_mem_m2_ask_dates.py`, `test_mem_m2_sales_audit.py` | the owner's memory in lanes (typed, attributed, archived, restorable), goals as the target a module judges against and a teammate's goal as a proposal, preference layers (login → location → organisation → product), Ask's chats beyond the replayed turns, Ask's reach into the other surfaces, ratings that teach, M/D/YY in what Ask reads, the sales audit as founding memory |
| `test_mem_m3_identity.py`, `test_mem_m3_shift_facts.py`, `test_mem_m3_attendance.py`, `test_mem_m3_staff_notes.py`, `test_mem_m3_tenure.py`, `test_mem_m3_standing_patterns.py`, `test_mem_m3_labor_read.py`, `test_mem_m3_labor_periods.py`, `test_mem_m3_cross_module.py`, `test_mem_m3_uncaptured.py`, `test_mem_m3_roster_changes.py`, `test_mem_m3_memory.py` | people and staffing: a person, not a display name; per-shift history an upload never erases; attendance (unwatched is unknown); dated, ending staff notes; standing patterns; the stored labor read; calendar payroll weeks; reviews, the DSR and marketing into staffing; covers, promotions and guest mentions; roster changes in the change log; the people provider in the schedule and labor prompts |
| `test_mem_m4_ai_reads.py`, `test_mem_m4_claims.py`, `test_mem_m4_stale_diagnoses.py`, `test_mem_m4_diagnosis_slice.py`, `test_mem_m4_dsr_own.py`, `test_mem_m4_forecasts.py`, `test_mem_m4_what_worked.py`, `test_mem_m4_positive_volume.py`, `test_mem_m4_thresholds.py`, `test_mem_m4_scorecard.py`, `test_mem_m4_links.py`, `test_mem_m4_memory_wiring.py` | Cavnar AI's own record: read history, claims scored at their horizon, stale diagnoses retired, the diagnosis's own slice, the DSR's own last actions and predictions, forecasts that correct themselves, what worked by kind and tag, finer trackers, fitted thresholds and held kinds, the monthly learning scorecard, link memory |
| `test_mem_m5_event_memory.py`, `test_mem_m5_canonical_facts.py`, `test_mem_m5_public_history.py`, `test_mem_m5_imported_year.py`, `test_mem_m5_prime_cost.py`, `test_mem_m5_dsr_to_brief.py`, `test_mem_m5_time_axis.py`, `test_mem_m5_memory_wiring.py` | events and sales facts: measured event effects and observed weather, one net basis and the canonical final-day readers, public rating history, last year from an import, prime cost against the owner's goal, the brief reading the report, one review time axis, memory in the DSR narrative, the digest and the brief |
| `test_mem_m6_reply_voice.py`, `test_mem_m6_rejected_drafts.py`, `test_mem_m6_drafter_fixes.py`, `test_mem_m6_mkt_edits.py`, `test_mem_m6_mkt_results.py`, `test_mem_m6_food_corrections.py`, `test_mem_m6_link_trackers.py`, `test_mem_m6_uncaptured.py` | marketing, replies and food learning: the owner's reply voice by star band and grant, rejected drafts as signals (never words), confirmed fixes in a reply, the owner's marketing edits, results from sales and returning guests, order/invoice/recipe/price corrections, trackers linked to the recommendation they measure, re-tags and review-request conversion |
| `test_mem_m7_retention.py`, `test_mem_m7_rollups.py`, `test_mem_m7_draft_thinning.py`, `test_mem_m7_change_log.py`, `test_mem_m7_eligibility.py`, `test_mem_m7_delete_policy.py`, `test_mem_m7_outside_value.py` | forgetting and records: the one retention registry with floors, caps and readers; the summaries that outlive a prune; schedule draft thinning; the change log; one learning-eligibility rule; what a delete keeps; the nightly value history |
| `test_mem_m8_episodes.py`, `test_mem_m8_features.py`, `test_mem_m8_priors.py`, `test_mem_m8_history.py`, `test_mem_m8_google.py` | the cross-restaurant platform: per-episode learning rows, versioned and backfilled features, the prior ladder and decay, pattern and verdict history, Google user data kept out of pooled learning |
| `test_mem_int_wiring.py`, `test_mem_int_privacy.py`, `test_mem_int_eligibility.py`, `test_mem_int_measurement.py`, `test_mem_int_records.py` | the integration wave: every provider reaching every surface that reads it; the team viewer, answer authority and owner-only lines across the wires (a new `memory_context` surface must be classified shared or not, or `test_mem_int_privacy` fails); learning eligibility in every reader; one measured effect across the workstreams; the change-log writers and setter wording owed between them |
| `test_mem_ui_wa_server.py`, `test_mem_ui_wa_web.py`, `test_mem_ui_wb.py`, `test_mem_ui_admin.py`, `test_mem_ui_ia.py`, `test_mem_ui_ib.py` | the UI wave: the web owner dashboard (part A: Home, the daily report, Ask, Account, Data Health, the policy notice; part B: Labor & Schedule and people, Reviews, Marketing, Food Cost, Intel, What Connects, Events), the admin console's learning pages, and source pins for the iOS screens — each element pinned against the payload field it reads |

iOS: `ios/CavnarAI/CavnarAITests/MemoryRoundIATests.swift` and `MemoryRoundIBTests.swift` — every new payload decodes leniently (a new or odd field is nil, an unknown kind is kept, a malformed entry is skipped) and the lines the screens print say what the server meant (M/D/YY, "Not watched yet", "—" below a floor).

Known order-dependent failures seen during the round (each passes alone; the cause is a module that binds `get_conn` or `DB_PATH` at import): `test_rec_trust_modules.py::test_an_expired_quiet_night_draft_is_neither_approved_nor_revived` after `test_automation.py` (`delayed`), `test_security_parity_audit.py::test_7_a_manager_cannot_end_a_food_cost_goal` (`goals`), `test_reaudit_m.py::test_m21`. Timing-sensitive under load: `test_mkt_fix_a_consent.py::test_cs19`, `test_automation_days.py::test_reservations_sync`.

## Employee-app fix round (`tests/test_empfix_*`, 10/2/26)

From the staff app audit. One file per server workstream; each test is named for the behaviour it holds and was revert-checked when written. Run the file for the code you touch:

| File | Covers |
|---|---|
| `test_empfix_b1.py` | auth and identity: one active login per name (code and index), the claim transaction, delete my account, the OTP gate (US only, the hourly ceiling, the dev-code flag), the PIN denylist, attempts counted before the KDF, owner notices on lock / spray / claim, Change PIN's own counter, server sign-out, forgot PIN by text, per-restaurant revoke, the Me fields and the switch |
| `test_empfix_b2.py` | staff notifications: the device tier filter, staff registration and the reminders switch, `people.deliver` (delivered-only, the fallback chain, quiet hours and held texts), staff alert types and payloads, the `staff_reminders` job, consent scope and `sms_available` |
| `test_empfix_b3.py` | shifts and requests: the restaurant's date, every leg's state and actions, offers and posted shifts, start-time gates, expiry and escalation, voiding, the role check, the swap picker, time-off cancel, notes and reasons, who's on with me, a standing staffing gap not charged to a cover |
| `test_empfix_b4.py` | task sheets: a same-day cover takes the sheet, settled days, one round trip per tick, photo authorization and cap, the tick window and sweep, the critical out-of-range alert, last night's note, no-store |
| `test_empfix_b5.py` | running late (attendance strength, the coverage hold), announcements with acknowledgement, the staff ↔ manager thread, the owner's Team inbox twins |
| `test_empfix_b6.py` | the employee's own earnings, stats, recognition and pulse, the calendar feed, hashed and live-week schedule links, the staff web pages |
| `test_empfix_b7.py` | the personal brief, the rush and 86'd lines, the approved rewrite and the S1 staff rule, the focus item, translation and figure parity, docs and certifications, house-rules answers; every model call mocked |
| `test_empfix_b8.py` | availability by hours and dates (one versioned save, conflicts, the time-off hint) and floor sections |
| `test_empfix_s9.py` | the integration: retention for the staff ledgers, rename and erase across the staff stores, announcements translated per recipient, the brief line on the reminder, expired certificates in the rules, the live week by one rule, sections carried on any rewrite, running late on an open issue |

**No live model calls.** `tests/conftest.py`'s autouse `_no_live_anthropic_calls` patches `httpx.Client.send` / `AsyncClient.send`: a request to `*.anthropic.com` raises `LiveAnthropicCall` (a `BaseException`, so neither the SDK nor `ai_utils` can turn it into a quiet fallback), and any attempt also fails the test at teardown. A test that stubs `ai_utils.get_client`, `create_with_retry` or `messages.create` never reaches it; with no key the SDK refuses before sending, so CI cannot trip it.

iOS: `ios/CavnarAI/CavnarAITests/StaffSignInTests.swift`, `StaffTodayTests.swift`, `StaffRequestsMeInboxTests.swift` and `StaffTasksTests.swift` — the staff app's decoding (lenient: an odd or missing field never fails a screen), its sign-in and session rules, and the pure logic of each tab.

## The schedule suite by behaviour, and the benchmark (blind re-audit 10/11/26, ARCH-19, ARCH-22)

Most schedule test files are named after the audit round that wrote them. Read by behaviour, the suite is this map (a file under one heading may touch others):

| Behaviour | Files |
|---|---|
| Rules — legality, the rule sweep, the tiers, who is held to what | `test_schedule_rules.py`, `test_schedule_rules_reaudit_1004.py`, `test_manager_every_minute.py`, `test_max_shift_owners_exempt.py`, `test_owner_hours_exempt_1007.py`, `test_salaried_on_the_schedule.py`, `test_schedule_time.py`, `test_schedule_time_off_parts.py`, `test_schedule_closures.py`, `test_sf10_j1_compliance.py`, `test_edge_sched_rules.py`, `test_schedule_breach_identity.py`, `test_sched_f1_rules.py` |
| The drafting engine — shapes, CP-SAT, the manager plan, the code drafts | `test_solver_behaviour.py` (the behaviour spec: random weeks, legality, determinism, locks, tiers, reasons, `_solver_first_rows`), `test_sf10_f_cpsat.py`, `test_schedule_solver.py`, `test_sched_fix_d2_solver.py`, `test_schedule_skeleton.py`, `test_schedule_dedicated.py`, `test_overtime_rebalance.py`, `test_schedule_requirements.py`, `test_sf10_g2_requirements.py`, `test_schedule_trim_and_fill.py`, `test_sf10_e_fallback.py`, `test_sf10_e_templates.py` |
| The pipeline — the generation job, the repair loop, the gate, the score | `test_schedule_benchmark.py` (archetypes end to end), `test_sf10_f_pipeline.py`, `test_schedule_a2_generation.py`, `test_schedule_b1_loop.py`, `test_schedule_b2_calls.py`, `test_schedfix_c_pipeline.py`, `test_sf10_c2_pipeline.py`, `test_schedule_output_contract.py`, `test_sf10_e_typed_rows.py`, `test_sf10_e_redo.py`, `test_schedule_row_repair.py`, `test_schedule_optimizer.py`, `test_shift_quality.py` |
| Lifecycle — versions, locks, publish, requests | `test_schedule_publish_gate.py`, `test_sf_g_publish_gate.py`, `test_publish_claim.py`, `test_sf10_i1_locks.py`, `test_sf10_i1_prepublish.py`, `test_sf10_d2_requests.py`, `test_sf10_h2_versions.py`, `test_schedule_history_and_tail.py`, `test_staff_schedule_publish.py`, `test_sf10_i2a_save_idempotency.py` |
| Learning — memory, habits, calibration, experiments | `test_schedule_learning.py`, `test_sf10_g1_*.py`, `test_sched_fix_h1_learning.py`, `test_sched_fix_h2_memory.py`, `test_schedule_experiments.py` |

**The benchmark.** `tests/test_schedule_benchmark.py` runs `schedule_benchmark.FAST_PLAN` (six archetypes, about a minute — the 24-hour diner and the multi-location org joined 10/26/26 at scale 0.5) and `TZ_PLAN` (a Los Angeles restaurant's next week, and the diner in New York on the next week with a daylight-saving change, its overnight shifts held to their real hours) once each, in a child process with its own throwaway database, no provider key and no network, and makes each (archetype, arm, invariant) its own test. What the code breaks today is a strict xfail naming the finding that fixes it (`KNOWN`): when a fix makes one pass, the xfail fails until its entry is removed — the gate only tightens. The full run, every archetype at full size on both arms with three presses, is `scripts/run_schedule_benchmark.py` (about 25 minutes; `--heavy` adds the 500-person one, ~8 GB). The week drafted is the restaurant's next one on its own clock (sales are judged against today), moved past a holiday week, so a run in late November still measures an ordinary week; a week further out (the DST week) is drafted with its history up to the day before it.

**Determinism.** The solver's "same inputs, same week" tests (`test_solver_behaviour.py`, the benchmark's `deterministic` invariant) are strict, and skip — naming the cut — only when a solve reports that an emergency cap ended it (`schedule_cpsat_runtime._wall_capped`, by the clock alone since 10/26/26: the old check read CP-SAT's deterministic time, which multi-worker solves over-report, and never fired, so the tests flaked on loaded CI runners — release-safety audit A02-01). Under the suite's xdist workers such a skip can happen; the nightly `scripts/run_determinism_check.py` runs both files serially on one CP-SAT worker and fails on more than one skip.

## Web pages: every inline script parses, and a real browser opens each place (audit 10/26/26 A01-14, A06-05/06)

- **`tests/test_inline_scripts_parse.py` (~2 s, in the gate).** Every `static/**/*.js` file is parsed whole and held to ES5 too (each is loaded by a page an owner, an employee or a guest opens). Every inline `<script>` of every template (Jinja neutralised, line numbers kept; a block with `{% if %}`/`{% for %}` parsed once with the first branch of each conditional and once with the `else`) goes through one node process, the program on stdin. The parser is the acorn node ships inside itself (`node --expose-internals`): `ecmaVersion: "latest"` for every block — a stray brace kills a whole block — and `ecmaVersion: 5` for every page an owner, an employee or a guest opens, which catches what the regex lint in `test_frontend_rules.py` cannot (a default parameter, a spread, a trailing comma in a call, `for…of`, shorthand properties). The internal pages (admin console, sales audit tool) are held to "parses" only, in `MODERN_JS_OK`, and an entry that becomes ES5-clean fails, so the list only shrinks. Where node's acorn is unavailable it falls back to `vm.Script` plus the regex lint and says so; without node it skips (fails under `CI`).
- **`tests/browser/test_dashboard_smoke.py` (marker `browser`, ~45 s).** Playwright drives Chromium against a throwaway, keyless app (`tests/browser/keyless_server.py`: a fresh volume, an allowlisted environment with no provider keys, `dotenv.load_dotenv` a no-op, loopback-only sockets, no scheduler, a free port on 127.0.0.1, gone when the test is). At 390px and 1280px the seeded demo owner, then a manager of the same restaurant (blind re-audit 10/27/26), signs in through the form and opens Home, Reviews, Labor, Food Cost, Marketing, Intel, Account, Ask and Bar (the manager: each place the page offers a manager; a 403 JSON refusal of an owner-only setting or a module outside the login's access is the truth for that role): no page error, no console error, the root visible, no same-origin 4xx/5xx (a 503 saying the model "isn't configured on this server" is the truth there and is excused), and no false "Cavnar AI was updated" bar. Then the bar in the real page, and that the app reached for no network. Other origins are answered empty inside the browser. Playwright is pinned in requirements-dev.txt (1.64.1); the browser is a separate download: `python -m playwright install chromium` (CI: `--with-deps chromium`). Skipped where playwright is not installed — and a CI job fails on that skip, since CI installs requirements-dev.txt.
- **`tests/test_stale_tab_reload.py` (node, in the gate).** The fetch wrapper rendered as a page includes it, run against stubbed responses: same build, the full 40-character commit, "unknown", HTML and external answers show nothing; another build shows one bar whose Reload reloads; Not now dismisses it for the page; a page without a build never shows it.

## Driving the loop, jobs and AI quality (Audit #25 WS6, 10/26/26)

- `tests/test_scheduler_drive.py` runs the real `scheduler_loop` under a frozen clock (`time_machine`, requirements-dev) with every job body stubbed: a whole day ticked every 10 minutes (every daily-SLA registry job starts; no job runs more often than its claim was taken; no claim is left unrun), and 00:30–03:30 on the 2026 DST dates (the 2am jobs once, an hour-keyed job once per local hour that exists). ~50 s; the behaviour the regex tests in `test_fix_d_jobs.py` only read.
- `tests/test_ws6_job_health.py` (failing and idle jobs, queue dead letters, the unknown-kind deferral, the partial retry, paging channels), `tests/test_ws6_ai_quality.py` (AI quality warnings, per-template rates, the schedule's fallback share), `tests/test_ai_eval.py` and `tests/test_ai_model_change_needs_eval.py` (the eval sets and the model-change gate — no model is ever called: `send` is a stub), `tests/test_ws6_validated_surfaces.py` (each `VALIDATED_IN` call site end to end with a stubbed answer stating an invented figure), `tests/test_ws6_ai_routes.py`, `tests/test_ws6_atomic_units.py`, `tests/test_watchdog_worker.py` (the Worker in Node, stubbed KV and fetch: the degraded window, flapping, degraded turning down). Re-fix round RF4 (10/27/26): `tests/test_rf4_job_health.py` (a failed generation or auto-draft is a dead letter, driven through the real handlers; pos_sync's fan-out settled from its rows; unknown kinds named and expired; a pager incident per condition, resolved when it clears; /health's read-only reads), `tests/test_rf4_marker_fail_closed.py` (marker reads fail closed), the eval gate's verdicts in `tests/test_ai_model_change_needs_eval.py`, and `tests/test_scheduler_drive.py`'s weekly, monthly and partial-retry drives.

## Test file naming

Files read as sentences, not identifiers — `test_a_new_share_gets_an_expiry_about_sixty_days_out`, not `test_share_expiry`. This is deliberate: a failing test's name should tell you what broke without opening the file.

## What NOT to do

- Don't run the full suite "just to be safe" after a change scoped to one file.
- Don't re-run a suite that already passed in this same turn with no code change since.
- Don't chase a test failure that's clearly pre-existing/unrelated to the current change — note it, don't fix it as a drive-by unless asked.
- Don't add a test that only re-checks the fixture it was written against (see `feedback_fixture_shaped_tests` in project memory) — assert against the source/logic when a rule must hold everywhere, not against one rendered payload.
