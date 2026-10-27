# Cavnar AI — working instructions

Standing instructions for anyone (human or agent) working in this repo. This
file is loaded automatically at the start of every session.

## Where the architecture lives

These files are the authoritative reference. Read the relevant one before
exploring code:

| File | What it covers |
|---|---|
| `ARCHITECTURE_MANIFEST.md` | The map: folders, services and owners, layers and allowed imports, shared utilities, what stays separate, naming — read before adding a file |
| `README.md` | The way in: the business, running it locally, testing, deploying, how it is built and why (merged from `PROJECT_CONTEXT.md` and `CAVNAR_AI_ENGINEERING_GUIDE.md`, 9/30/26) |
| `MODULE_OVERVIEW.md` | Every module, its files, its design stance |
| `SYSTEM_ARCHITECTURE.md` | Processes, jobs, data flow |
| `DATABASE_SCHEMA.md` | Tables and their invariants |
| `API_REFERENCE.md` | Routes, web and mobile |
| `PROMPT_LIBRARY.md` | Every model call: which model, what input, what guard |
| `DESIGN_SYSTEM.md` | UI/UX: tokens, type, spacing, components, motion |
| `TESTING.md` | How the suite is organised |
| `ROADMAP.md` | Open work, recently shipped, deliberately not doing |
| `INTELLIGENCE_ENGINE.md` | The cross-restaurant learning layer and its privacy rules |
| `docs/ops/SECURITY.md`, `docs/ops/RECOVERY.md`, `docs/ops/ENVIRONMENT.md`, `docs/ops/RAILWAY_SCHEDULER_SPLIT.md`, `docs/ops/PIN_PEPPER_RUNBOOK.md`, `docs/ops/POSTGRES_CUTOVER.md` (and `migrations/`) | Controls, the emergency runbook, every environment variable, the deploy shape, the PIN pepper |
| `docs/plans/` | Designs, each opening with its status. Built: the DSR engine, task sheets. Not yet built: Back Office, Postgres and more workers, the restaurant health row, the review-fetch queue, the nonce CSP |
| `docs/audits/` | Audit registers whose finding IDs tests still cite (the 9/22/26 edge-case audit; every strict `xfail` it opened has since been fixed and removed — 0 remain) |
| `docs/history/` | Superseded material kept for the record; nothing in it is live |
| `docs/clients/`, `docs/integrations/`, `docs/app-store-submission.md` | Per-client config (Simple EJ's checklists and DSR config), vendor API collections (RPOWER), the App Store Connect entries |
| `ios/CavnarAI/README.md`, `scripts/README.md` | The iOS project's setup; who runs each script |

---

## CRITICAL SAFETY REQUIREMENT — code deletion is HIGH RISK

Before recommending the removal, consolidation, or replacement of any code,
service, model, component, API, utility, background job, scheduled task,
prompt, or database object, trace **all ten** of the following:

1. **Every known reference** — direct imports and calls.
2. **Indirect references** — re-exports, aliases, wrappers, base classes.
3. **Dynamic references** — `getattr`, `globals()`, string dispatch tables,
   `importlib`, anything keyed by a name built at runtime.
4. **Dependency injection usage** — registries, factories, provider maps.
5. **Scheduled and background jobs** — `scheduler.py`, cron, queues, threads.
6. **Feature flags** — module flags on `restaurants`, tier gating, env vars.
7. **Routing** — blueprints, URL rules, mobile twins of web routes.
8. **Notifications** — email, SMS, push, digests, alert types.
9. **Tests** — including tests that assert a thing is ABSENT.
10. **SwiftUI previews** — and anything else compiled but never called at
    runtime, which the compiler will still fail on.

**Only recommend removal when HIGH confidence exists that the code is truly
unused or fully superseded.**

If confidence is below High, **do not recommend deletion**. Mark the finding
instead as:

> Candidate for future cleanup after additional verification.

**Never recommend removing code based solely on the absence of obvious
references.** Favour false negatives over false positives: preserving
production stability matters more than maximising cleanup.

### Why this is stricter here than it looks

This codebase is unusually good at hiding a live reference from a grep:

- **Bound imports.** `from models import get_conn` at module top level is a
  reference that the name `models.get_conn` will never match. Most modules
  do this, and it has already caused silent wrong behaviour in tests: a
  patch of `models.get_conn` never reaches a bound copy. Every production
  module now resolves `get_conn` through `models` at call time (a
  module-level `get_conn` shim) — `tests/test_no_bound_get_conn.py` fails a
  new top-level `from models import get_conn`. `models.get_conn`'s default is
  `None` (DB_PATH read when called); a `db_path=DB_PATH` default in a function
  signature still binds at definition.
- **Lazy imports inside functions.** Most cross-module calls here are
  `import x` *inside* the function body, so a module-level grep misses them.
- **Registry-driven dispatch.** `ask_cavnar_tools.TOOLS`, `pos.PROVIDERS`,
  `ask_cavnar_tools._SETTABLE` and the `_do_*` handler lookups all reach code by
  string, not by symbol.
- **Web/mobile twins.** Almost every route exists twice (`client_api.py` and
  `mobile_api.py`); deleting one half leaves the other calling a shared body.
- **Scheduled-only callers.** A function whose only caller is a 5am job in
  `scheduler.py` looks unused from every screen in the product.
- **Tests that assert absence.** Some tests exist to prove a thing was
  deliberately removed and must stay removed — re-adding or renaming it
  breaks them in a way that reads like an unrelated failure.

### Checklist before proposing any deletion

Run these, and say in the finding what each turned up:

```bash
rg -n "\bNAME\b" --glob '!*.pyc'          # direct, including comments
rg -n "getattr|globals\(\)|importlib"     # dynamic dispatch near the call sites
rg -n "NAME" scheduler.py templates/ ios/ tests/
rg -n "NAME" --glob '*.md'                # documented as an entry point?
```

A deletion is only "verified" when the trace is written down alongside it.

---

## Other standing rules

- **Root-cause first.** Diagnose the real cause; never ship the fastest
  surface patch. Verify live before claiming something is fixed.
- **Push to `main` after every completed task.** Don't wait to be asked.
  Once Railway's production deploys the `production` branch (docs/ops/RECOVERY.md
  → Deploys), a push deploys nothing: production is promoted from a `main` SHA
  whose `gate` check is green, through `.github/workflows/promote.yml`
  (9am–3pm CT weekdays; `emergency` skips the window, never the gate). Until
  that switch is made, a push to `main` still deploys. Emergency deploys come
  from a pushed commit, never `railway up` of a working tree (`/health` must
  name its build).
- **Restart the local backend** (`PORT=5050 python3 hosted_dashboard.py`)
  after any backend edit — the user tests against it from a real device over
  ngrok.
- **UI work starts from `DESIGN_SYSTEM.md`.** It is the source of truth for
  colour, type, spacing, cards, buttons, charts, forms, tables, empty states
  and motion, on web and iOS. Do not introduce a new UI pattern unless no
  existing one fits — and if none does, document the new one there in the
  same commit.
- **Dates read `9/21/26`.** M/D/YY, no leading zeros, everywhere an owner
  sees a date — `time_utils.mdy()` in Python, `|format_date` in Jinja,
  `mdy()` in web JS. `DESIGN_SYSTEM.md` → *Dates and times* is the rule;
  an ISO date in owner-facing text is a bug.
- **Email colours come from `emails.BRAND`**, enforced as a ratchet by
  `scripts/check_email_tokens.py`. Email is light-mode only, inline-styled,
  and governed by `DESIGN_SYSTEM.md` → Email. Every sender returns an
  `emails.SendResult` and passes `restaurant_id`; act on `.ok`, and never
  write a `log_email` row for a send that did not happen. One email has one
  sender — the welcome is `emails.send_welcome_with_set_password_link`
  whoever sends it.
- **Value delivered is only what was measured.** `value_delivered.py`
  returns four figures — `delivered` (from `outcomes.py`, before/after,
  caveated), `avoided` (cost avoidance at STATED rates, counted only for
  work that happened), `surfaced` (dollars the alerts carried) and
  `opportunity` (gaps against target) — and **they are never summed into
  each other**. An opportunity figure must never be rendered as delivered
  value: that was the bug, and it made the number rise as the restaurant
  got worse. Any new "value" component states its rate inline and counts
  distinct work, never rows or months-since-signup.
- **Inline JS is ES5 only**, enforced by `tests/test_frontend_rules.py` (regex)
  and `tests/test_inline_scripts_parse.py` (every template's inline `<script>`
  parsed by node's acorn, both branches of each Jinja conditional; ES5 on every
  owner, staff and guest page — the console pages in `MODERN_JS_OK` only shrink).
  Colours come from CSS variables only, enforced by `scripts/check_colors.py`.
  Every web button carries `.cbtn`, enforced by `tests/test_button_system.py`.
- **New `restaurants` column = 4 touch points**: the `Restaurant` dataclass,
  a migration entry (either `init_db()`'s ALTER list or `ensure_columns()`'s
  tuple list — both run at boot; pick one), `update_restaurant()`'s `allowed`
  whitelist, and `get_restaurant()`'s hydration. Miss the whitelist and writes
  silently no-op — `tests/test_models.py` now asserts every `al_*`/`alert_*`
  dataclass field is whitelisted.
- **An admin action audits itself through one typed call**,
  `admin_events.record_admin_action` (actor, target, before/after, result) —
  never `admin_events.record("admin", …)` from a route
  (`tests/test_fix_int2_admin.py` holds `admin_routes.py` to it). A sensitive
  one takes `@recent_auth_required()` directly under `@admin_required`, or
  `auth.reauth_refusal(current_user)` for the part of a route that needs it;
  `docs/ops/SECURITY.md` lists them and `tests/test_docs_controls.py` holds
  the list. Admin work that calls a provider or a model runs on the one admin
  job pool (`admin_routes._submit_admin_job`) or `ops.run_admin_task`, never
  a thread per click; a thread a request starts is `ai_utils.attributed(fn)`,
  so its model calls stay the requester's.
- **Targeted tests by default — and before a push too.** The full suite
  (several minutes even in parallel) is NOT a pre-push step. Run it only after an audit or fix
  round, a big code change, a large batch of changes, or when asked — never
  after a few tweaks, never twice "to be sure" (Will, 9/15/26 and again
  9/28/26: "it stalls productivity"). When it does run, in parallel:
  `python3 -m pytest -q -p no:warnings -n auto --dist loadfile`. One suite
  at a time: two at once halve each other. Touching a top-10 component
  (TESTING.md → Tiers: `models.get_conn`/`db_dialect`, `get_restaurant`/
  `update_restaurant`/`has_module`, the login decorators, `client_api._m` ↔
  `mobile_api`, the labor % producers, `time_utils`, `dsr/` payloads,
  `scheduler`/`jobs_registry`/`job_queue`, `permissions`, `dashboard.html`)
  runs `python3 -m pytest -m contract -n auto -q -p no:warnings tests/` (under
  a minute) before the push. CI's `gate` job (lints, contract slice, boots on
  SQLite and Postgres 16, recorded-SQL PREPARE; UTC+14) is the required check;
  the full suites run nightly (`.github/workflows/nightly.yml`). After a solver
  or benchmark change run `python3 scripts/run_determinism_check.py` (serial,
  one CP-SAT worker; also nightly).
- **Tests take the restaurant's clock.** `tests/clock.py` (`restaurant_today`,
  `today_for`, `next_monday`, `frozen`); never `date.today()`/`datetime.now(`
  in a test or a literal week that will pass — `tests/test_test_ratchets.py`
  blocks new ones and CI runs at UTC+14 (nightly also UTC-11). No new
  `inspect.getsource` assertion for behaviour (same ratchet). A flaky test is
  marked `quarantine` the day it flakes, never skipped; CI fails a skip whose
  reason `tests/expected_skips.txt` does not explain, and fails any test that
  tried to reach the network (the conftest guard allows only this machine).
  A test that boots the app in a subprocess stubs `dotenv.load_dotenv` first
  (`tests/test_subprocess_apps_never_load_dotenv.py`).
- **`get_restaurant()` is memoised per Flask request**, and only per request —
  outside one (scheduler, tests, scripts) it is uncached, deliberately, so a
  long-running job sees rows change under it — except inside an explicit
  `models.read_scope()` (one unit of work, e.g. a generation: the restaurant
  row, `client_data` and the parsed shift history are read once). Any new write path to
  `restaurants` outside `update_restaurant` must call
  `models._invalidate_request_cache(rid)`.
- **Never put schema DDL on a request or per-call path.** Every table is
  created at boot by `models.boot_schema()` — the one boot DDL chain that
  hosted_dashboard, worker.py, `/health`'s reference schema and the test
  template all call; a new init step goes there, never into one caller. A boot
  data rewrite runs once (`models.run_once` / a `(name, sql)` entry, ledger
  `data_migrations`). An index on a table production already has is built
  before the deploy (`scripts/build_index.py`, CONCURRENTLY) and listed in
  `migrations/prebuilt_indexes.txt` (`tests/test_boot_ddl_rules.py`); schema
  changes are additive and format markers compare with `==`
  (ARCHITECTURE_MANIFEST.md, Release compatibility). Boot DDL is one booter at
  a time on Postgres (`models.boot_ddl_lock`, an advisory lock held for the
  whole `boot_schema`), and `RECURRING_BOOT_WRITES`
  (`tests/test_ws2_boot_schema.py`) may only shrink. `/health` is strict only
  for the sign-in tables (`status_manager.STRICT_SCHEMA_TABLES`) and missing
  tables: a missing column elsewhere is `schema_gaps` (degraded 200), named in
  the log, never the body. A `DATABASE_URL` set but unusable on Railway raises
  (`config.database_url_problem`) instead of opening the volume's frozen
  SQLite copy; `DB_BACKEND=sqlite` is the deliberate SQLite path. The chain: `init_db()` (which also calls `ops.init_ops`,
  `billing_jobs.init_billing`, `admin_events.init_admin_events`,
  `offboarding.init_offboarding`, `admin_ops.init_admin_ops` and
  `ai_utils.init_ai_ops`, and since 10/11/26 `job_queue.init_job_queue`,
  `model_gateway.init_model_gateway`, `pg_shadow.init_pg_shadow`, and since 10/26/26 `rollout.init_rollout`, `client_compat.init_client_compat`) and the `init_*` functions
  `hosted_dashboard.py` calls right after it (`auth`, `push`, `webhooks`,
  `guest_marketing`, `sales_audits`, `platform_monitor.init_platform_tables`,
  `provider_health.init_provider_health`). `ai_utils._ensure_usage_schema` is
  the pattern where a lazy table is unavoidable: once per database per
  process, with a self-healing retry if a write later fails on a missing
  column.
- **The scheduler only runs on Railway's production environment**
  (`config.scheduling_allowed()`: false on any other named environment —
  `config.deploy_env()` from `CAVNAR_ENV`, else Railway's environment name —
  and while `RESTORE_FROM` is set): a local backend has its own SQLite
  file and lease but production's Resend and Twilio keys, so a local
  scheduler re-sends real briefs, digests, alerts and issue texts. The same
  gate refuses every admin action that emails, texts, or changes Stripe or
  DocuSign (`admin_routes._send_blocked`, `_live_actions_refused`,
  `LOCAL_SENDS_REFUSED`, `billing_jobs._sending_allowed`); a send that is only
  a side effect of an admin action is skipped with a note instead, and a new
  admin send checks the gate too. Deliberately NOT gated: the admin's own 2FA
  code (`/admin/two-factor/send`), the owner and login flows in
  `auth_routes`, and `/api/send-referral` — a local backend with the
  production `.env` still sends those. `ALLOW_LOCAL_SCHEDULER=1` overrides
  both deliberately. The scheduler also stands down while
  `MAINTENANCE_READ_ONLY` is set (the Postgres cutover's write freeze).
- **Staging never reaches real people.** `config.outbound_sends_allowed()` is
  false outside `production` (and `local`) and is checked at the provider call
  of every write to a third party on a customer's behalf — `emails.deliver`,
  the Twilio send, APNs and Web Push, `config.stripe_api` with a live key,
  DocuSign's live account, Google replies, posts, retractions and listing
  edits, customer webhooks, the RPOWER and Toast schedule pushes, every Meta
  Graph write. A new send point checks it too and refuses through
  `config.refused_send("<channel>")` (counted on `/health`), never
  `sends_refused_reason()` alone; `ALLOW_NONPROD_SENDS=1` is the deliberate
  override. Set `CAVNAR_ENV` explicitly on Railway: a production-looking
  service not named `production` is `/health`'s `not_production_env` and a
  CRITICAL at boot (`config.environment_name_problem`, the one check).
- **Never run a copy of the app from a worktree with Flask's own `.env`
  loading.** `hosted_dashboard.py` loads the `.env` beside it and runs
  `app.run(..., load_dotenv=False)`, because Flask's lookup walks up from the
  working directory — a copy under `.claude/worktrees/` loaded the main
  checkout's `.env` (production's keys). A throwaway server for a UI check
  runs with no provider keys in its environment — copy
  `tests/browser/keyless_server.py` (allowlisted environment, stubbed
  `load_dotenv`, loopback-only sockets, a free port, dies with the test), as
  the Playwright smoke `tests/browser/test_dashboard_smoke.py` does.
- **Deployment shape.** The scheduler runs in the web process by default.
  **Never run it as a separate Railway service**: volumes cannot be shared
  between services, so it would schedule against an empty database while the
  real jobs stopped. `worker.py` is only valid as a second process in the same
  service — see `docs/ops/RAILWAY_SCHEDULER_SPLIT.md`. Background work
  (schedule generations, auto-drafts, the per-restaurant nightly learners) is
  the durable job queue (`job_queue.py`, `jobs` rows with leases, priority
  classes and per-tenant fairness) — the one path (`JOB_QUEUE=0` and the
  in-process `GenerationSlots` are gone, 10/11/26): consumed by default by
  the queue worker process `worker.py --queue`, which gunicorn's master
  starts beside the web worker in the SAME container (`gunicorn.conf.py`; the
  start command is unchanged) and restarts after it recycles itself
  (`QUEUE_WORKER_MAX_JOBS` / `QUEUE_WORKER_MAX_RSS_MB`); the web process
  consumes only while no worker heartbeat is fresh, or with
  `QUEUE_WORKER=0`. A generation gives its CPU slot back while the model
  writes (`job_queue.model_wait`). A deploy hands running rows back and the next
  process resumes them from their checkpoints (`schedule_resume`), never
  paying for a model answer twice. At the deploy's SIGTERM the queue worker
  hands back in parallel with the web worker's 30 s graceful window (35 s
  budget); `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=60` must be set — Railway's
  default is 0, and the boot logs an ERROR while it is unset. The single-runner guarantee is
  `ops.acquire_scheduler_lease()`, and the lease lives in the SQLite file:
  the lease keeper renews it every minute, a kept lease silent for 4 minutes
  is taken over, and the web process's supervisor restarts a dead scheduler
  thread together with its keeper (`scheduler._run_scheduler_thread`). With a
  volume attached Railway runs one deployment at a time: a boot that fails,
  or a `/health` that is not 200 within the 120-second healthcheck, is
  downtime until a rollback — `/health` answers 200 for a degraded platform
  on purpose and must stay read-only.
- **Do not raise gunicorn `--workers` past 1** until the remaining blockers in
  `docs/plans/POSTGRES_AND_WORKERS_PLAN.md` → *Multi-worker readiness* are done.
  The bounded pools no longer multiply: each pool's setting is the CONTAINER's
  bound and every web worker takes `bound // workers` (`pool_sizing.per_worker`;
  gunicorn's master publishes its real `--workers` as `CAVNAR_WEB_WORKERS`;
  `worker.py` keeps whole bounds) — a new bounded pool is sized through
  `pool_sizing.sized(name, bound)`. One-thread pools stay one thread per worker;
  the schedule call pool is not split (claimed generation rows hold the fleet's
  streams). A polled Ask turn's events go to `ai_lane_events` when there is
  more than one worker, so any worker answers its poll. The inventory is in
  `docs/plans/POSTGRES_AND_WORKERS_PLAN.md`. Per process BY DESIGN (each
  guards that process's own four request threads, so it is right per
  worker): `ASK_MAX_CONCURRENT` and `ai_utils._INTERACTIVE_SLOTS`. Caches
  keyed on a database stamp, so right in every worker: the Studio's kept
  inputs (`_studio_cache`, on `models.schedule_inputs_version`), its live
  check (`_studio_live`) and the requirement model cache
  (`_REQUIREMENT_MODEL_CACHE`) on `schedule_engine.shifts_stamp`, and the
  pre-publish review cache (`prepublish._CACHE`, `PREPUBLISH_CACHE_SECONDS`
  300, keyed on the week's version, `schedule_inputs_version`, the rows on
  screen and the dismissal/override stamps), and the parsed shift history
  web requests share (`labor._shared_history`, on the history's stamp). (Moved
  into the database 10/11/26, blind re-audit ARCH-26/SCALE-23: the admin
  request ceiling — `admin_request_hits`, `security._admin_hits` only its
  fallback; the console's fleet memo — dropped in every worker through the
  shared generation `job_cursors['admin_fleet_gen']`. Already durable: the login
  limiter — `security.login_throttled` on `login_attempts`,
  `auth_routes._login_attempts` only its fail-closed fallback; the
  supplier-email cooldown — `ops.claim_cooldown` rows,
  `client_api._order_send_last` is gone; the AI rate limit —
  `ai_rate_events`, `ai_utils._ai_call_log` only its fallback; since
  10/11/26 schedule generation — `job_queue` rows, slots are claimed rows
  across processes; the "your draft is ready" watchers — `generation_watchers`
  rows; the AI breakers — shared through `ai_breakers`.) Railway
  usage (Sep 2026) showed ~19 vCPU-minutes of CPU for the whole billing
  period, so there is no load reason to do it yet.
- **Every outbound HTTP call names a timeout**, enforced by
  `scripts/check_timeouts.py`. With `--workers 1 --threads 4`, one call
  waiting on the OS's TCP behaviour is a quarter of the platform.
- **Dependencies are locked.** Edit `requirements.in` (direct dependencies,
  pinned exactly), then re-lock `requirements.txt` with `pip-compile` under
  Python 3.12 (`.python-version` pins 3.12.7). Railway and CI install the
  lock (CI with `--no-deps`, then `pip check`). Never edit a version in
  `requirements.txt` by hand.
- **Work that iterates every restaurant must be bounded and resumable.**
  `run_daily_fetch` is the pattern: a worker pool, a wall-clock bound, and a
  cursor in `job_cursors` so the next pass starts where the last one
  stopped. A time bound without a cursor is worse than no bound — it starves
  the same tail every pass.
- **A scheduled job is one `jobs_registry.JOBS` entry plus one
  `ops.run_job("<name>", fn)` call in `scheduler_loop`** (or, for long
  platform work the queue worker runs — the nightly backup, the restore
  drill — one `queue_scheduled_job("<name>")` call; registry `queue`), returning
  `{attempted, ok, failed, skipped, hit_bound}` or raising
  (`tests/test_fix_d_jobs.py` keeps the two in step; `ops.run_outcome` reads
  any other result as partial, never clean). A heavy per-restaurant pass
  fans out as `job_queue` rows (`strategy_jobs.fan_out_learner` is the
  pattern) instead of a long inline sweep on the scheduler thread. A growing ledger gets an entry
  in `ops._RETENTION_DAYS` / `_RETENTION_COLUMN`; a finding about a model's
  output is `ai_utils.record_quality_event`, not `ops.capture`. A job's health
  is its failure share, not its score: `ops.jobs_failing` warns when its last
  two runs each failed half or more of what they attempted, or attempted
  nothing while `eligible` restaurants waited (so a per-restaurant sweep reports
  `eligible`); a partial run of a non-sending `retry` job that failed ≥ 20%
  retries once, 30 minutes later. `job_queue` defers a kind it has no handler
  for (never fails it).
- **A queue handler whose work failed but must not be retried returns
  `job_queue.dead_letter(result, error)`** — the row ends `failed` and the
  dead-letter rules read it (a schedule generation that failed on the
  platform's side does; an owner-actionable refusal does not). A partial retry
  re-runs only the restaurants whose failures the run captured (`ops.capture`
  naming `restaurant_id=N`), only for a job that goes through
  `scheduler.resumable_sweep`, never work that succeeded.
- **Paging thresholds are rates with absolute minimums**, never a bare count
  that pages at any volume; platform conditions reach the pager one dedup key
  each (`sla:<condition>`) and are resolved when they clear; release health is
  keyed on `RAILWAY_DEPLOYMENT_ID` and never advises a rollback for a
  provider's outage.
- **Recovery lives in `docs/ops/RECOVERY.md`.** The local backup snapshot is
  deliberately NOT redacted (it never leaves the volume and is the restore
  artifact); only the off-site copies are — the object-storage and emailed
  copies are scrubbed through `offsite_backup`'s one registry and
  Fernet-encrypted, and the backup fails when no off-site copy was made. Do
  not reintroduce redaction on the local path. `tests/test_docs_controls.py`
  holds `docs/ops/SECURITY.md`, `RECOVERY.md` and `RAILWAY_SCHEDULER_SPLIT.md`
  (and `DATABASE_SCHEMA.md`'s scrub list and retention table) to the code:
  change a control and its sentence together.
- **Static assets:** the dashboard's script and style blocks are served as
  content-hashed, immutable bundles (`/b/<hash>`, `static_bundles`), so they
  may be cached forever. Since 10/19/26 (FE-04) every literal `/static/<file>`
  in a template, and every `url()` in a `/static` stylesheet, is served as
  `/static/<file>?v=<content hash>` (`static_bundles.version_refs`), and only
  a request carrying the file's CURRENT hash is answered immutable. A bare
  `/static/` URL (one built at run time, an email's absolute link, a
  reference in a `/static` script) still has no cache-busting: never give
  those a far-future `max-age`, or a fix will be stranded in browser caches.
  A page knows its build (`cavnar_build`, `config.build_id()`), every JSON
  answer carries `X-Cavnar-Build`, and `_csrf_fetch.html` offers one "Cavnar AI
  was updated." reload bar when they differ.
- **SQL must stay translatable to Postgres.** `models.get_conn` returns a
  Postgres connection when `DATABASE_URL` names one (`db_dialect`); run
  `scripts/pg_sql_audit.py` after adding SQL — it must report
  `untranslatable: 0, sqlite_only: 0`. The text check alone hid 50+
  statements that failed on a real server (10/11/26 SCALE-10): CI's `gate`
  PREPAREs every static statement and every statement the contract slice ran
  (`pg_sql_audit.py --strict --database-url … --recorded …`), and the nightly
  runs every test file on Postgres in four shards; run it locally against the
  scratch Postgres 16 when a change is SQL-heavy. A hot path's query plan is
  held by `tests/test_pg_query_plans.py`. **Production runs on
  Postgres since 10/19/26** (Railway service `Postgres`, Pro plan,
  point-in-time recovery on; `DATABASE_URL=${{Postgres.DATABASE_URL}}` on
  `web`): every SQL change must work on both backends, and a `railway ssh`
  one-off reaches the live data through `models.get_conn` — the volume's
  `reviews.db` is the frozen pre-switch copy. Never set `DATABASE_URL` for a
  LOCAL app (production's database) — tests use `CAVNAR_TEST_PG_URL` on a
  scratch **Postgres 16** server (locally
  `postgresql://postgres@127.0.0.1:55433/postgres`; a 14 gives false failures,
  `docs/ops/POSTGRES_CUTOVER.md`). Off Railway the app ignores `DATABASE_URL`
  unless `ALLOW_LOCAL_PG=1` (a scratch server only), and the suite refuses to
  start inside a Railway environment.
- **Every Anthropic call passes the model gateway** (`model_gateway`, inside
  `ai_utils.create_with_retry`): call classes, per-tenant budgets and the
  organisation's tokens a minute. The fleet AI ceiling is per paying
  restaurant (`AI_GLOBAL_PER_CLIENT_USD`) with a runaway detector and
  `AI_EMERGENCY_STOP` — never an absolute fleet-wide cap again.
- **A model change, or an edit to an eval surface's prompt, ships with its
  eval.** `tests/test_ai_model_change_needs_eval.py` re-checks every
  baseline's verdict: measured on the model and prompt in force, accepted
  against the baseline in force before it on the SAME frozen set, over the
  floors (≥10 scored, ≤10% provider errors, ≤30% refused). On Railway:
  `scripts/run_ai_eval.py freeze` / `run --surface S --baseline` (what is in
  force first), then `--model M`; a prompt: `template --surface S` locally,
  then `run --surface S --candidate - --baseline < file` on Railway; commit
  `eval_baselines/` (scores only, never customer text), then `fingerprint
  --write`. The CLI never writes an invalid run, and a regression only with
  `--accept-regression "<why>"`. A tier's default model changing names the
  tier in `ai_workflows.TIER_CANARY_DEFAULT` for at least a week. A call with
  a registered workflow policy goes through `route_for(...).apply(...)`.
- **Silent handlers only go down.** `scripts/check_silent_handlers.py` reads
  every app module (packages too) and holds each file's count of broad handlers
  that leave no trace to `scripts/silent_handlers_baseline.json`; a new one logs
  or `ops.capture`s. A unit of several writes under one broad handler is one
  `models.atomic(conn)`. New app code logs, never `print()`s.
- **Labor % is one function.** An owner-facing labor figure is read through
  `metrics.labor_figures(rid, start, end, view)` (or `metrics.daily_labor`):
  all-in for a viewer who sees salaries, hourly for anyone else, basis stated.
  `labor_daily_history.labor_cost`/`labor_pct` are HOURLY and never quoted to
  the owner as "labor %". A cached read whose figures carry labor is kept per
  salary view. `scripts/check_labor_math.py` fails new labor-cost ÷ sales
  arithmetic outside `metrics.py`, `labor.py` and `dsr/`.
- **A manager never sees salaries — in any surface, text, alert, cache or AI
  output.** `models.viewer_sees_salaries()` is False off a request (a job is
  manager-safe) and for support acting through view-as;
  `permissions.sees_salaries(user)` is the per-login rule. Every registered
  job runs in `models.salary_view(False)` (`ops._job_context`) unless its
  `jobs_registry` entry says `salary_view="owner"` (listed in
  `OWNER_SALARY_JOBS` with the reason) — only when every output goes to the
  account holder alone. A job that sends to owners and managers builds each
  recipient in their own view. Anything shared (an issue, an alert's text, a
  stored read, a diagnosis, a cache key) is hourly or kept per salary view:
  with anyone salaried it names the hourly figure, never the all-in % or the
  points over. A pool thread carries no view — wrap the unit in
  `models.in_salary_view`.
- **Two labor figures are compared only on one pay basis**: read periods
  through `models.get_labor_history` (`pay_basis`) or `metrics.labor_figures`,
  never the stored hourly `labor_pct` (`check_labor_math.py`'s second
  ratchet). Prime cost's parts are both over net sales (`food_share_pct` +
  `labor_pct` = `prime_cost_pct`; `metrics.prime_cost_pct` for a closed
  window); food cost % is over food sales and is never added to labor %.
- **The manager rule holds after publish too.** Anything that changes who can
  be on a published shift — time off, an availability change (approved, a
  manager's save, or one applied without approval), deactivating someone, the
  floor-manager / acting / trainee settings, a weekday set off — goes through
  `shift_requests.hold_manager_rule` before it is written: refused with the
  gap (`ManagerGapRefused` → 409 `needs_cover`) unless who runs the floor is
  named (`covered_by`), and refused when the rule can't be read (never
  approved on no answer). Support and admins (view-as included) never cover a
  gap by naming themselves (`shift_requests.acting_admin`). A new path that
  changes who can work a live shift calls it too, on web and iPhone
  (`FloorCoverAsk`); `manager_watch` (hourly) is the backstop, not the gate.
- **A restaurant's time zone is either chosen or flagged.** A write of
  `restaurants.timezone` through `update_restaurant` stamps
  `timezone_confirmed_at`; never set the zone with raw SQL.
  `data_health.timezone_problem` flags a zone nobody chose (unless the
  restaurant's location agrees), one the location contradicts, or an unknown
  name.
- **Blueprints live in one list, `blueprints.plan()`.** hosted_dashboard
  registers only through `blueprints.register_all(app)`; the module-gate sweep,
  the cross-tenant crawl, the client-path and twin-wiring tests read
  `blueprints.all_blueprints()`. A new blueprint or app-wide hook is a step in
  that plan.
- **Tenant isolation has a crawler.** `tests/test_cross_tenant_crawl.py` calls
  every rule as tenant A with tenant B's ids; any response carrying B's marker,
  any change to B's rows or any unhandled exception fails. The SQL ratchet
  (`scripts/check_tenant_scope.py`) counts every `id=?` / `id IN (` without
  `restaurant_id`; a new baseline entry carries its `why`. An Ask tool with
  `module: None` is in a `tool_allowed` name set or
  `ask_cavnar_tools._MODULELESS_REVIEWED` with the reason. A permission test
  that depends on app hooks uses `tests/real_app.py`.
- **A module-level cache registers with `process_caches`**
  (`process_caches.register("module._NAME", _NAME)` beside its definition);
  the suite clears them all per test (`tests/test_process_caches.py`).
- **A risky new behaviour ships behind a cohort switch:**
  `rollout.enabled("<feature>", restaurant_id, default=…)`, registered in
  `rollout.FEATURES`. New owner-visible behaviour: default off, 0% →
  allow-list → 10% → 100% from the console. A change that must reach
  everyone: default on, the flag is its kill switch. `ROLLOUT_<FEATURE>` wins
  over the console's `rollout_flags` row (`POST /admin/api/rollout`, audited).
- **The phone's payloads are a contract.** `tests/test_ios_response_shapes.py`
  holds every main iOS read to `ios/CavnarAI/CavnarAITests/Fixtures/<route>.json`
  and `<route>.v1.json` (what installed builds read): a v1 key is never renamed,
  removed or re-typed until `MOBILE_MIN_BUILD` retires the builds that read it.
  Fixtures are re-captured (`CAVNAR_WRITE_IOS_FIXTURES=1`), never typed; a
  null under a non-optional Swift field fails. The paths, methods and body keys
  installed builds send are frozen in `tests/fixtures/ios_api_v1.json`, and
  the web's main reads in `tests/fixtures/web/<name>.v1.json`; regenerate
  `ios_api_v1.json` (`python tests/test_ios_api_v1.py --write --ref <store
  build> --ref HEAD`) and the v1 fixtures only in the commit that raises the
  minimum. A new phone read goes in `tests/ios_shape_capture.ROUTES` with a
  decode line in `ResponseShapeContractTests.swift`. Every
  request the app builds names its build (`X-Cavnar-App`); `ios/CavnarAI/generate.sh`
  stamps the build number from the commit count — archive only from a project it
  generated.
- **A send whose outcome is unknown is never retried as failed.** A Twilio read
  timeout is `uncertain` (`notify.SmsResult.uncertain`, sms_log `unknown`) and
  counts as sent for retries; an outbox that calls a provider stamps the row
  before the call and never lets a lease takeover send it again
  (`schedule_publish`). Stripe is tested through the real SDK
  (`tests/test_stripe_real_sdk.py`); provider fixtures in
  `tests/fixtures/providers/` are all SYNTHETIC (the repo is public).
