# System Architecture — Cavnar AI

## Folder structure

```
review_automation/
├── hosted_dashboard.py        # Flask app factory / entrypoint — registers every blueprint, boots the scheduler
├── config.py                  # the environment values several modules read (base URL, sender, on-Railway, Places key)
├── demo_seed.py               # the Simple EJ's demo account and its boot-time seed, off the request path
├── main.py                    # pre-hosted CLI (--demo, --report-only); never a second scheduler (exit 2 without --legacy-scheduler)
├── models.py                  # (12k lines) schema, migrations, every dataclass, most DB read/write functions
├── auth.py / auth_routes.py   # session/user model, staff portal tables, /auth/* routes (web)
├── security.py / security_headers.py / credentials.py / csrf.py / guest_links.py / http_layer.py / net_safety.py
│                              # durable login throttling + freeze + the admin request ceiling, response headers, Fernet at rest,
│                              # CSRF, signed links, gzip/metrics/request ids, SSRF-safe outbound fetches
├── logging_setup.py           # one log format (JSON on Railway), request/job context on every line
├── platform_monitor.py / provider_health.py   # boot records, request telemetry, the supervisor thread, the system card; credential probes
├── client_api.py              # (11k lines) web dashboard's API — 229 routes; 100 delegate to mobile_api via _m(), the rest own or share a _do_* body
├── mobile_api.py              # (8k lines) iOS API — 239 routes; the "real" implementation for shared logic
├── strategy_routes.py         # 126 route bodies registered once each at /api/… and /mobile/api/… (the twin pattern to prefer)
├── staff_routes.py / staff_schedule.py / staff_roster.py / time_off.py / labor_replacements.py / preshift.py
│                              # the staff portal: PIN sign-in, today's schedule, availability, time off, pre-shift read
├── admin_routes.py / admin_ops.py / admin_events.py   # /admin console (Will-only)
├── labor.py                   # shift CSV ingestion, labor % math, the one schedule model call
├── schedule_engine.py / schedule_rules.py / staff_settings.py / demand_signals.py / schedule_versions.py / shift_requests.py
│                              # the schedule pipeline: constraints, backstops, rule sweep, versions, roster, dated demand, shift swaps
├── scheduler.py               # background job loop: its gates are in §Scheduling below
├── jobs_registry.py           # the one table of scheduled jobs (SLA, sends, runnable, bound, retry, lane)
├── offsite_backup.py          # the S3/R2 copy of the nightly backup and the credential scrub registry
├── billing_jobs.py            # the billing outbox (owed_sends), invoices, the subscription mirror, dunning, reconcile
├── offboarding.py             # closing an account: the audited checklist, integration revocation, the delete
├── strategy_jobs.py           # the scheduled half of the strategic features (loss sync, outcomes, weekly plan, ...)
├── delayed.py / decisions.py  # undo-window actions; the owner's decision record
├── milestones.py / good_news.py / first_look.py / monthly_review.py / weekly_review.py / review_common.py
│                              # the moments an owner reads: firsts, wins, the month and week in review (shared sentences in review_common)
├── intelligence/              # the cross-restaurant learning layer (INTELLIGENCE_ENGINE.md)
├── shift_quality.py           # pure scoring engine for a generated schedule (no I/O)
├── ask_cavnar.py / ask_cavnar_tools.py   # the in-app AI assistant: context builder + tool registry
├── home_brief.py              # deterministic Home-tab payload (no AI on load)
├── notify.py / push.py        # email/SMS alert firing, APNs push delivery
├── ai_utils.py                # the model and data-API call path: budgets, breakers, retry, the usage ledger, the call trace, Places
├── inventory.py / inventory_ledger.py   # Food Cost module
├── marketing*.py              # Marketing module (drafts, publishing, media, links, signals)
├── competitor*.py             # Intel module's competitor snapshots
├── docusign_helper.py         # contract send/status via DocuSign eSignature API
├── pricing.py                 # single source of truth for every dollar figure quoted anywhere
├── webhook_routes.py / webhooks.py       # outbound webhook delivery + inbound Stripe/Twilio webhooks
├── pos.py                     # the provider registry every POS feature dispatches through
├── toast.py, square.py, clover.py, rpower.py (+ *_routes.py), gmb.py, meta_api.py, weather.py   # POS / platform integrations
├── sales_audit_*.py, sales_audits.py   # /admin/audits in-person sales tool (audit_app.py is a separate, standalone scorecard app on :9000)
├── emails.py                  # every transactional/marketing email template + send function
├── models.py's ensure_columns()   # additive migration path, runs on every boot
├── templates/
│   └── dashboard.html         # the ENTIRE web app — one Jinja template, one inline <style>/<script>
│                               #   per module panel (#panel-labor, #panel-reviews, ...), ES5-only JS
├── static/                    # fonts, images, cavnar-orb.js (loading animation)
├── ios/CavnarAI/CavnarAI/
│   ├── Core/                  # APIClient, SessionStore, AppEnvironment
│   ├── DesignSystem/          # colors, type, motion (CavnarMotion.swift), AccountSheetKit
│   ├── Features/              # one folder per module — Home, Labor, Reviews, FoodCost, Marketing,
│   │                          #   Intel, AskCavnar, Account, Auth, Notifications, ScheduleHistory, ...
│   ├── Models/                # Codable structs mirroring API JSON shapes
│   └── Push/                  # APNs registration/handling
├── scripts/                   # one-off / CI scripts: check_colors.py, build_contract_pdf.py, ...
├── tests/                     # pytest, one file per concern (see TESTING.md for the count command)
├── docs/ops/                  # RECOVERY, SECURITY, RAILWAY_SCHEDULER_SPLIT, PIN_PEPPER_RUNBOOK
├── docs/plans/                # designs with no code yet
├── docs/history/              # superseded material, kept for the record
├── docs/contracts/            # the contract PDF DocuSign's template is built from
├── public/                    # the Cloudflare-Worker-served marketing site (cavnar.ai) — separate deploy
└── brand/                     # brand sources (assets/), the social upload kit, the print one-sheet
```

## Request flow

```
Browser/iOS ──HTTPS──▶ gunicorn (Railway) ──▶ Flask app (hosted_dashboard.py)
                                                  │
                                     Blueprint dispatch (registered in hosted_dashboard.py):
                                     admin_bp · audit_bp · webhook_bp · social_bp · auth_bp · client_bp ·
                                     toast_bp · square_bp · clover_bp · rpower_bp · status_bp · mobile_bp ·
                                     strategy_bp · strategy_mobile_bp · issue_link_bp · staff_bp
                                     (16; hosted_dashboard refuses to boot on a duplicate (path, method))
                                                  │
                          ┌───────────────────────┼────────────────────────┐
                          ▼                       ▼                        ▼
                 client_bp route          mobile_bp route           admin_bp route
                 (web, session cookie)    (iOS, Bearer token)       (Will only)
                          │                       │
                          └──────_m("mobile_x")───┘   (client route delegates to the mobile
                                                        implementation so both surfaces compute
                                                        the same payload from one code path)
                                                  │
                                        models.py / labor.py / ask_cavnar.py / ...
                                                  │
                                          get_conn(DB_PATH) → SQLite (reviews.db, Railway volume)
```

Around every request, `http_layer` gives it an id (an inbound `X-Request-ID` / `X-Railway-Request-Id` of 8–128 safe characters is kept, else a new one), binds the id, route and method into the log context (`logging_setup.bind`), returns it as `X-Request-ID`, counts it into the in-process latency window and the per-minute rollup buffer, samples any 5xx, compresses and sets cache headers (`no-store` on `/api/`, `/mobile/api/`, `/admin`, `/audit/r/` and `/health`). gunicorn's access log writes the route RULE and the request id, never the raw path (paths carry tokens such as `/reset-password/<token>`).

`client_api.py`'s `_m(name)` helper (`getattr(mobile_api, name)`, unwrapping the auth decorator) is the load-bearing pattern that keeps web and iOS from drifting into two different answers to the same question — see `PROJECT_CONTEXT.md`'s "delegation over duplication" rule.

## Database

One SQLite file (`reviews.db`), WAL mode, on a Railway persistent volume. `models.get_conn(db_path)` opens a tracked connection (`_TrackedConnection`, weak-referenced so a leaked connection can be swept by `close_thread_connections()` on request teardown). Schema is created at boot: `init_db()` (most tables, plus its own ALTER list; it also calls `ops.init_ops`, `billing_jobs.init_billing`, `admin_events.init_admin_events`, `offboarding.init_offboarding`, `admin_ops.init_admin_ops` and `ai_utils.init_ai_ops`), `ensure_columns()` (a second additive list), then the `init_*` functions `hosted_dashboard.py` calls next (`auth`, `platform_monitor.init_platform_tables`, `provider_health.init_provider_health`, `webhooks`, `guest_marketing`, `push`, `sales_audits`, and models' own smaller `init_*` helpers). There is no migration runner or version table — ordinary `ALTER TABLE ADD COLUMN` guarded by `try/except`. Full table list and shapes: `DATABASE_SCHEMA.md`.

## Failure & resiliency (audit #21)

**Bounds, because the platform is one process with four request threads.**

| Bound | Where | Why |
|---|---|---|
| `FETCH_WORKERS` / `FETCH_MAX_SECONDS` + `job_cursors` | `scheduler.py` | A serial pass over every restaurant ran for hours and never reached the tail; the cursor stops the bound from starving the same tail every pass |
| `ASK_MAX_CONCURRENT` | `client_api.py` | Ask spawned an unbounded daemon thread per request |
| `MAX_CSV_ROWS` | `client_api.py` | Parse/analyse/store run synchronously in a request |
| `CB_FAILURE_THRESHOLD` / `CB_OPEN_SECONDS` | `ai_utils.py` | Retry is the wrong answer to a provider outage |
| `timeout=` on every outbound call | enforced by `scripts/check_timeouts.py` | One hung call is 25% of capacity |
| `_claim_fallback` | `ops.py` | `claim_period` and the scheduler lease both fail open on the same dependency; a claim insert that hits "database is locked" reads the claims table and refuses if the row exists, running from memory only when the row is absent and the table cannot be written |
| `max_minutes` per job | `jobs_registry.py` | a run older than its own bound is "stuck" on Operations → Jobs, the pulse stops vouching for the heartbeat and captures the overrun once, and the SLA check pages a wedged job |
| `ADMIN_FLEET_TTL_SECONDS` (45) / `ADMIN_FLEET_MAX_WAITERS` (1) / `ADMIN_FLEET_WAIT_SECONDS` (30), `ADMIN_HEAVY_CONCURRENCY` (1) | `admin_ops.py` | the console's fleet build is memoised on request threads, single-flight with one waiter; past that a request is refused 503 + `Retry-After` + `X-Admin-Busy` (`AdminBusy`) instead of a fifth request queuing on four threads. `heavy_slot` does the same for recommendation analytics |
| 240 requests / 60 writes a minute per admin session | `security.admin_request_allowed` | 429 + `Retry-After`; process-local (`security._admin_hits`) |
| `ADMIN_TASK_WORKERS` (2) | `ops.run_admin_task` | the console's per-client "Fetch reviews now" and every manual POS sync (`scheduler.start_manual_pos_sync`, the owner's button too) run here as job runs (`review_fetch_one` / `pos_sync_one`), polled at `/admin/api/tasks/<id>` |
| `ADMIN_JOB_WORKERS` (2, max 4) + `ADMIN_JOB_QUEUE_MAX` (8) | `admin_routes._submit_admin_job` | the ONE admin job pool for every other admin job that calls a provider or a model — menu extraction, new-client setup, and through `_start_admin_job` seed drafting, redraft-all, the Meta token refresh and the review-account seed; tracked in `async_jobs`, polled at `/admin/api/admin-jobs/<id>` (and the menu-extract and create-client polls). `_AdminJobs`, a second queue of 20, is gone |
| `WEBHOOK_MAX_QUEUED` (500) | `webhooks.py` | outbound customer webhooks: the pool is bounded and every event is an outbox row first |
| `RETENTION_CHUNK_ROWS` (5,000) / `RETENTION_MAX_SECONDS` (10 min) | `ops.prune_ledgers` | a delete per table held the write lock for as long as the table was old |
| `BACKUP_FREE_SPACE_FACTOR` (3.5) | `scheduler.backup_db` | the snapshot, its scrubbed copy and the encryption exist at once; a 2am run that filled the volume would take every write down with it |
| `_HEALTH_CACHE_SECONDS` (5) | `hosted_dashboard.health` | a monitor polling hard never becomes load |

**Signals.**

*`/health` — the contract.* `status_manager.health_snapshot` (cached 5 seconds by the route) is READ-ONLY: it writes nothing and sends nothing. Three answers:
- `200 {"status":"ok"}` — everything below checked out.
- `200 {"status":"degraded", "problems":[…]}` — serving, but a person is needed: `db_busy` (the write probe waited past two seconds), `db_not_wal`, `disk_low`, `disk_critical`, `scheduler_stale`, `scheduler_wedged`, `scheduler_stalled`, `jobs_overdue`, `backup_stale`, `offsite_backup_stale`.
- `500 {"status":"error", "error":<code>}` — this deployment should not take traffic: `db_unavailable`, `db_unreadable`, `schema_mismatch` (with `missing_count`), `db_not_writable`, `data_missing` (an empty database on a volume whose marker says it held client restaurants). Error bodies carry a code, never exception text.

Railway's deploy check needs only the 200, so a degraded platform still deploys — failing it would block the very deploy that fixes the problem. An external uptime monitor asserts the keyword `"status":"ok"`, so degraded and error both alert it; no nested key is named `status`. The 200 body carries `database {write, write_ms, journal, size_mb, wal_mb}`, `scheduler` (ok / stale / wedged / stalled / unknown) with `scheduler_heartbeat_age_minutes` and `scheduler_loop_age_minutes`, `disk` (free, total, database and WAL sizes and both thresholds — low below max(250 MB, 4 × (database + WAL)) or under 10% free, critical below max(50 MB, 1.5 × (database + WAL)) or under 2% free), `backup {state, age_hours, offsite, offsite_age_hours, last_run}`, `jobs_overdue` (names), and `build` (the commit). The write probe is `BEGIN IMMEDIATE` then `ROLLBACK`; the schema check compares against a reference schema built once at boot (`status_manager.warm_reference_schema`), cached by `PRAGMA schema_version`. The SLA read inside it reuses the probe's answer (`ops.check_platform_sla(send=False, write_ok=…)`), so an uncached `/health` takes the write lock once.

*The watchdog and paging.* `ops.check_platform_sla` runs on the web process's supervisor thread every five minutes with `send=True` (`platform_monitor.PlatformSupervisor`) — never on the request path (`/health` calls it with `send=False`) and never inside the loop it watches. It pages the operator (`ops.page_operator` → `ops.alert_will`: a text to `WILL_PHONE`, an email, and a push that does not count as delivered) when the heartbeat is older than `jobs_registry.HEARTBEAT_STALE_MINUTES` (15), a job has run past its own bound, the loop has stopped completing ticks with nothing running, an expected job is overdue, the disk is low or critical, the database refuses a write, the backup is stale (26 hours), failed or has no off-site copy, or a DSR night is missing an hour past its deadline. At most one page per problem key an hour (the cooldown is claimed only after a text or the email went out, and held in memory when the database cannot be written); only where `scheduler.scheduling_allowed()`; owners are never contacted from it. Every page is an `operator_alerts` row.

*The dead-man ping.* `ops.ping_healthcheck` calls `HEALTHCHECK_PING_URL` (3/5-second timeouts, never raises): at the end of every tick, from the pulse during a long job — but only while the job is inside its own bound and the loop has been completing its ticks — and after the backup and the digest; a failed backup or digest pings `<url>/fail`. The monitor's silence is what pages when the whole process is gone.

*The heartbeat.* `scheduler_heartbeat` has one row, written only by the loop: `loop_completed_at` at the end of a tick, `beat_at` then and from the pulse while a job is inside its bound, and the running job. `status_manager.scheduler_state(db_path)` is the ONE reading of it — `/health`, the SLA check, the lease keeper's rule, the system card and the console all call it, on the database being judged: it derives `wedged` (a job past its bound), `loop_stalled` (no completed tick with nothing running), `stale` (no beat for 15 minutes) and one `state` word (unknown / stale / wedged / stalled / ok, in that order of precedence). The supervisor's tick runs `check_scheduler_liveness`, which writes only the public status page's scheduler row (an outage while the beat is stale), and only on a change; `/health` never writes it.

*Telemetry.* `http_layer.request_metrics` is a rolling in-process latency/error window; the per-minute rollups (by route rule and traffic class) and the 5xx samples are buffered in memory and persisted by the supervisor every 30 seconds (`request_rollups`, `http_5xx_log`); handled 5xx also go to Sentry, at most once a minute per route. `admin_ops.overview` raises platform issues for job failures, a stale scheduler, a 5xx spike (the console's own 503 busy refusal, `X-Admin-Busy`, is recorded as a 429 — never sampled as a 5xx or sent to Sentry — so the console cannot raise `platform:error_rate` on itself), and **fetch coverage** — the one check that can tell "no new reviews" apart from "never reached".

**Concurrency.** `update_restaurant(..., expected_version=)` is optimistic
locking on the restaurants row; omitting it keeps last-write-wins. The admin
client-settings save sends only the fields the admin touched, with
`expected_version` and `base` (the values it loaded): it is refused (409
`conflict`) only when a field it changes was changed by someone else —
`row_version` also moves on Google token refreshes and POS sync stamps, so a
plain version check would refuse almost every save.

**Recovery.** `docs/ops/RECOVERY.md`.

---

## Scheduling / background jobs (`scheduler.py`)

A single `scheduler_loop()` running in a background thread, ticking every five minutes (`SCHEDULER_TICK_SECONDS`), that checks a `scheduler_lease` row before doing anything — one process holds the lease and runs the jobs; any other idles and takes over only when the holder goes quiet. Each job claims its period in `job_period_claims` before starting (claim-before-work), so a slow run and a subsequent tick can't both fire the same job. Failures are recorded in `job_failures`/`job_runs` rather than silently retried into a notification storm. A claim whose `job_runs` row started and never finished (a deploy killed it) is reclaimed after `ops.CLAIM_RECLAIM_MINUTES` (120) — never while the run is alive in this process or its `pulse_at` shows another process still running it; a claim that cannot be written falls back to a per-process memo (run once, then refuse) and is logged. Runs a dead process left open are closed as interrupted at boot and when a process newly takes the lease (`ops.close_orphaned_runs`), which also clears the running job the dead holder was inside.

**The registry.** `jobs_registry.JOBS` is the one table of jobs (67 on 9/29/26, after the integration wave registered eight): for each, `cadence` (for people — the loop's gates below are the source of truth), `sla_minutes` (the most time between SUCCESSFUL runs before it is overdue — about 3 hours for hourly jobs, 26 for daily, 8 days for weekly; none for `restore_drill` and `quarterly_summaries`), `sends` (it can email, text or push a person — "Run now" refuses these on a backend where `scheduling_allowed()` is false), `runnable` (every job but `minute_duties`), `label`, `description`, `target` (the function "Run now" calls) and `run_kwargs`, `max_minutes` (its own bound plus a margin: past it a run is stuck), `claim` (the `claim_period` key where it differs from the name), `retry`, `lane`. `ops.EXPECTED_JOBS` (the SLA the watchdog checks) and `admin_ops.RUNNABLE_JOBS` (the console's Operations → Jobs) derive from it. A new job is one registry entry plus one `_ops.run_job("<name>", fn)` call in the loop under its gate; `tests/test_fix_d_jobs.py` fails when the two drift apart, and `job_expected_since` (written at boot) keeps a job a release adds from reading as weeks overdue on its first day.

**Standard counts.** Every scheduled job returns `{attempted, ok, failed, skipped, hit_bound}` or raises (`_PENDING` in `tests/test_fix_d_jobs.py`, the list of jobs not yet converted, is empty); `ops.run_outcome` records ok=1 clean, 0 failed (raised, or every restaurant attempted failed), 2 partial (some failed, or the bound cut it short — `failed > 0` with no `attempted` is partial too). A result without counts is no longer taken for a clean run: `True` is clean, `False` failed, and anything else — a bare None, a dict counting nothing recognisable — partial. A run that returned normally but captured failures under its own name meanwhile (`ops.capture`) is partial too, never clean.

**What a run carries** (`ops._job_context`): `job=<name>` on every log line (`logging_setup.context`) and, for every model and Places call it makes, the AI attribution trigger `scheduler` and correlation id `job:<name>:<run id>` — unless the caller already set them (an admin's Run now, an owner's background task keep theirs). A job that raises is logged with its text redacted (`ops._redacted`, `ai_guard.redact_secrets`), and `ops.capture` puts the redacted traceback on the log record as a `traceback` field (its last 6,000 characters) rather than `exc_info`, so a key in an exception's text never reaches the log.

**The pulse and the watchdog.** Every job the loop starts runs through `_PulsedOps.run_job`: a pulse thread beside it renews the lease, stamps the heartbeat and runs the minute duties once per tick interval, so a three-hour fetch no longer starves them or lets a standby take the lease. Past the job's own `max_minutes` the pulse stops vouching for the heartbeat (so the loop reads `wedged`), captures the overrun once, and stops pinging the dead-man monitor. The loop never starts a job that is already running in this process or another (`ops.is_running` / `running_elsewhere`); it gives the period back instead, so a later tick starts it.

**Retries.** A failed non-sending daily or weekly job with `retry` gives its period back and is retried after 10, 30 and then 90 minutes (`jobs_registry.RETRY_BACKOFF_MINUTES`); a job that sends is never retried (it must not be sent twice), and an hourly job's next hour is its retry. The failure digest is the exception: `DigestNotSent` gives the day's claim back up to three times.

**Lanes.** A long, network-bound sweep runs on a worker lane beside the loop instead of on the loop thread (`scheduler._LANES`; today only `intel`, which runs `competitor_analysis` and `ai_visibility`): one job at a time, only while this process holds the lease, with its own lease renewal, through `ops.run_job`. A lane never stamps the loop's heartbeat — a live lane must not make a dead loop look alive — and a busy lane gives the claim back. The review fetch (bound about 200 minutes) and the diagnoses (40) still run on the loop thread, so at scale briefs, the DSR and intraday slots can still wait behind them (`docs/plans/REVIEW_FETCH_QUEUE_PLAN.md`).

**The lease keeper.** `scheduler._LEASE_KEEPER` (started with the scheduler thread by `_run_scheduler_thread`, and by `worker.py`) renews this process's lease every `ops.LEASE_RENEW_SECONDS` (60) and marks it `kept`; a kept lease silent for `LEASE_OWNER_GONE_SECONDS` (240) is taken over, an unkept one keeps the 30-minute `SCHEDULER_LEASE_STALE_SECONDS` window. It stops renewing while the loop is stuck in a job past twice its bound, so a wedged runner loses the lease to a standby. `ops.shutdown_scheduler()` (registered at exit, and `worker.py`'s SIGTERM handler) marks the process as exiting BEFORE it releases the lease, so no daemon thread still running can take it back.

**Run now.** The console's "Run now" writes a `job_run_requests` row; the loop takes it at the top of its next tick (`_run_manual_requests`, compare-and-set, at most three a tick) and runs it under the lease through the same `run_job`, never beside a live run of the same job. A request not taken within 30 minutes expires rather than running late. On a laptop (`scheduling_allowed()` false) a non-sending job runs in-process instead; a sending one is refused with a 409 and the refusal is audited. The console's per-client "Fetch reviews now" and every manual POS sync — Toast, Square, Clover and RPOWER, the console's and the owner's (`scheduler.start_manual_pos_sync`) — run on the bounded admin task pool (`ops.run_admin_task`) as `review_fetch_one` / `pos_sync_one` job runs, under the requester's AI attribution (`ai_utils.attribution_for_thread`); the console polls them at `/admin/api/tasks/<job_id>` (admin-only, those two kinds, only the poll's keys).

**Missed windows.** A per-restaurant window that closed with nothing claimed (`scheduler.local_due` past its `until`, and `morning_brief.run_due`) is recorded in `missed_windows` — shown on Operations → Jobs and in the failure digest. Skipped intraday and DSR slots are not recorded or caught up yet.

**The loop's gates** (Chicago time unless marked *local*; "hourly" jobs are attempted every hour and gate per restaurant inside on its own local hour, claiming once per restaurant per local day — `scheduler.local_due`):

| Claim key | When | Runs |
|---|---|---|
| `backup_db` | 2am | `backup_db` — a free-space check (3.5× the database), a consistent, integrity-checked, **unredacted** snapshot on the volume, then a scrubbed, Fernet-encrypted off-site copy to object storage (`BACKUP_S3_*`) and, when it fits, by email; FAILS (raises, pages, pings `/fail`) when the snapshot fails, there is no room, or no off-site copy was made; one `backup_runs` row either way. Then `prune_ledgers` (`run_nightly_retention`: `ops.prune_ledgers` — every declared rollup first (AI daily, monthly alerts and engagement, newsletter results), then each table in chunks, at most `RETENTION_PASS_MAX_ROWS` a table a pass, a window under its floor refused and paged — and each owner's review retention, whose monthly stats are written in the purge's transaction) |
| `restore_drill` | 2nd of Jan/Apr/Jul/Oct | `run_restore_drill` |
| `pos_sync`, `loss_sync` | 3am | `run_toast_sync` (every provider in `pos.PROVIDERS`, restaurants in service (`models.in_service`), four fetches at a time with the SQLite write section under `pos._WRITE_LOCK`, 45-minute bound captured when hit; after each restaurant `pos.note_sync_failure` writes one account-visible `pos_sync_failing` activity event per failure run once a provider has failed `SYNC_FAILURE_NOTICE_DAYS` (2) with no success — never an SMS or push), `run_loss_sync`. A day still trading at pull time is stored provisional (`pos.complete_through`). Toast/Square/Clover calls go through `pos.http_call` (3 tries, backoff, Retry-After, never on 401/403) |
| `stripe_reconcile` | 3:30am | `billing_jobs.reconcile_stripe` — refreshes the subscription mirror from Stripe and records where Stripe and the local billing state disagree (`billing_reconcile`); never changes a billing status; bounded, resumable, retried |
| `pos_retry` | hourly | `run_pos_retry` — every POS sync whose retry is due (`data_health.due_retries("pos")`, +1h/+3h/+6h after each failure, never an auth failure) until 11am *local*, at most three a day per restaurant; bounded and resumable, `hit_bound` captured |
| `intelligence_features`, `intelligence_learning` | 3am, 4am | `intelligence.jobs.run_features` (bounded, cursor in `job_cursors`; demo accounts get their own row), `run_learning` (cross-restaurant: `active_restaurants()` and every cohort reader leave out `jobs.seeded_restaurant_ids()` — every restaurant `models.learning_exclusion` rules out: demo, excluded by an admin, internal billing, the admin's home, a test-pattern name, with the admin's `learning_override` — and every row a converted demo recorded before its `learning_since`) |
| `marketing_metrics_sync` | 4am | `run_marketing_metrics_sync` (each restaurant's result stamped in `job_cursors metrics_sync:<rid>`, failures captured — `scheduler.metrics_sync_state`) |
| `inventory_depletion`, `food_cost_snapshots`, `forecast_scoring` | 5am | `run_daily_depletion_sync` (from the last depleted business day, never fewer than 3 days, at most 14 — `source_health depletion`), `run_food_cost_snapshots` (a restaurant whose depletion did not land for last night is held, not snapshotted — `data_freshness.depletion_behind`), `run_forecast_scoring` (every frozen forecast whose period closed, all restaurants) |
| `data_health_daily` | 6am | `run_data_health_daily` — one `data_health.record_daily` per restaurant in service, bounded and resumable; the admin rollup reads it |
| `review_diagnoses`, `food_cost_diagnoses`, `outcome_evaluations`, `outcome_rechecks` | 6am | the two root-cause passes, then `run_outcome_evaluations`, then `run_outcome_rechecks` |
| `value_snapshots` | 6am, after the outcome evaluations | `value_delivered.run_value_snapshots` — each restaurant in service's NET measured monthly figure into `value_snapshots` (the Home sparkline), dated on its local day; bounded and resumable (`job_cursors value_snapshots`); sends nothing |
| `value_figures` | 6am, after the outcome rechecks | `intelligence.dashboard.snapshot_value_figures` — each restaurant's four value figures into `value_figures_daily`, which the admin Intelligence page sums; on the `intel` lane (a busy lane gives the claim back) |
| `competitor_analysis`, `ai_visibility` (per ISO week), `competitor_retry`, `ai_visibility_retry` (per day) | Mon–Wed 6am / 7am, then daily | `run_weekly_competitor_analysis`, `run_weekly_ai_visibility` — on the `intel` lane beside the loop; claimed per ISO week with Monday–Wednesday catch-up (a busy lane gives the claim back); after the weekly pass a daily `retry_only` pass for restaurants with no success this week (`source_health`); soft failures captured; each sweep keeps a per-restaurant prefix cursor and skips restaurants already done this week |
| `auto_draft_schedule` | hourly → 6am *local* on the owner's draft day (Thu by default) | `run_auto_draft_schedules(now=)` — the tick's instant decides whose day it is |
| `refresh_tokens` | 7am | `refresh_expiring_tokens` |
| `outcome_wins`, `milestones` | hourly | `run_outcome_wins`, `run_milestones` — each restaurant at its own 9am local (`strategy_jobs.WIN_HOUR`), bounded and resumable; pushes inside quiet hours arrive silently |
| `weekly_plan` | Mon, hourly → 7am *local* | `run_weekly_plan` |
| `recipe_drafts` | Tue, hourly → 5am *local* | `run_recipe_drafts` |
| `trusted_orders` | hourly → 8am *local* on the owner's order day (Mon by default) | `run_trusted_orders` |
| `auto_publish_schedule` | hourly → 9am *local* the day after the draft day (Fri by default) | `run_auto_publish_schedules` — held when `client_api.publish_blockers` is non-empty |
| `quality_calibration` | Sun 5am | `strategy_jobs.run_quality_calibration` — records the Shift Quality weight suggestion (`schedule_learning.calibrate_weights`, the same numbers Apply writes) once per new published week; never applies it |
| `schedule_outcomes` | Mon 4am | `strategy_jobs.run_schedule_outcomes` — what each published week did, by daypart |
| `reservation_sync` | daily 5am | `reservation_feeds.run_reservation_sync(weekday=)` — reservation feeds into demand_signals for each restaurant whose draft is tomorrow; no provider is live, unconfigured restaurants are counted and skipped |
| `review_fetch` | 8am, 12pm, 4pm, 8pm (latest missed slot only) | `run_daily_fetch` — bounded pool, cursor in `job_cursors` |
| `ops_digest` | 8am | `ops.send_failure_digest` (job `ops_failure_digest`) — failures, overdue and stuck jobs, the backup, missed windows and the security lines since the last digest that was actually SENT; nothing to say → no email; raises `DigestNotSent` when it had something to say and did not send, and the day's claim is given back up to three times; pings the dead-man monitor |
| `operator_weekly_digest` | Mon 7am | `ops.send_operator_weekly_digest` — pipeline, churn risk, onboarding, failures, overdue jobs, AI cost, from `admin_ops.overview()` / `clients()` |
| `rec_ledger` | 8am | `rec_ledger.repair_sync_replays` (a one-off), `sync_existing` (older ledgers' answers; every tracker linked to its episode for good, its verdict — `unknown` included — and its abandonment carried, not windowed), `expire_stale` (14 days unanswered → ignored), `backfill_tags` (bounded; the `tags IS NULL` filter is its cursor) |
| `weekly_digest` | hourly → 9am *local* on the digest day | `run_weekly_digests` |
| `monthly_summary`, `quarterly_summary` | hourly → 9am *local* on the restaurant's own 1st (`local_due(day=1)`) | `run_monthly_summaries`, `run_quarterly_summaries` |
| `daily_alerts` | hourly → 10am *local* (`notify._gated_out`) | `run_daily_alert_checks` (the morning batch) |
| `onboarding` | hourly → 10am *local* | `run_onboarding_sequence` |
| `onboarding_nudges` | hourly → 11am *local* | `run_onboarding_nudges(local_hour=11)` — one email per missing setup step (Google, brand voice, first approval, the app), each step once |
| `contract_chase` | 10am | `billing_jobs.run_contract_chase` — the pay link again on days 2, 5 and 9 after signing to a client who has not paid |
| `provider_probes`, `dunning` | hourly | `provider_health.run_probes` (one authenticated, non-sending call per provider — Resend, Twilio, Anthropic, Stripe, Places, APNs; a provider that starts failing pages once), `billing_jobs.run_dunning` (owes the dunning email for a failed invoice attempt the webhook missed, stands down dunning for invoices since paid, then sends) |
| `issue_scan` | hourly → 10am *local* | `run_issue_scan` |
| `review_request_followups` | hourly | `run_review_request_followups` |
| `stale_inventory` | Mon 10am | `check_stale_inventory` |
| `inactive_clients` | Mon 11am | `check_inactive_clients`, `send_while_away_nudges` (job `while_away`, same claim) — the stale-inventory and inactive-client emails are still sent beside the weekly digest (candidate for future cleanup after additional verification) |
| `labor_reminders` | hourly → 9am *local* | `strategy_jobs.run_labor_reminders` — requests close to their date, a drafted week not sent |
| `review_request_nudge` | hourly → Monday morning *local* | `strategy_jobs.run_review_request_nudge` — at most weekly, resumable |
| `optin_invite` | 11am–`OPTIN_INVITE_LATEST_HOUR` | `guest_marketing.run_toast_optin_invites` |
| `campaign_attribution` | noon | `run_campaign_attribution` |
| `intraday` | every 20 min | `run_intraday_capture`, `run_pre_dinner_pulse`, `run_coverage_check`, `run_preshift_nudge`, `run_closing_summary`, `run_demand_opportunity` (each gates itself) — the restaurants are read once per slot (`strategy_jobs.slot_restaurants`); each job walks them from its own `job_cursors` cursor under a wall-clock bound (capture: 4 in flight, 10 min; the others 4 min), `claim="intraday"` on each run |
| `dsr_sweep` | every tick, claimed per 10-minute slot | `dsr.pipeline.run_sweep` — the nightly DSR for every live, `dsr_enabled`, POS-connected restaurant past its own *local* close: polls the POS close-day record every 10 min, collects each block (retrying awaiting ones 10/20/40 min until the deadline; the closeout never holds a night open), writes the narrative, finalises. At `dsr_deadline_hour` *local* (default 4am) only Sales decides: still missing → provisional, re-checked hourly for 48h and completed as a new version when sales land; in → final, with any other block still awaiting (Food's 5am item sync) labelled, never a new version on its own. Bounded (20 min) with a `job_cursors` cursor; each night claims `dsr:<rid>:<date>:v<n>` so it never double-runs. When a version is saved final or provisional it calls `dsr.deliver.on_terminal` (below) |
| `dsr_delivery` | every tick, claimed per 10-minute slot | `dsr.deliver.release_held` — the DSR pushes held through a restaurant's alert quiet hours, sent once they end (oldest release time first; bounded at 500 rows / 2 minutes; the held rows are the queue, each taken `held` → `sending` before it goes, so a second pass or a run-now finds nothing) |
| `business_metrics` | 11:50pm (a missed night, or a failed run's retry, before 6am) | `admin_ops.snapshot_business_metrics(day=)` — the day's MRR, accounts, signups, churn, active users and costs into `business_metrics_daily` (never pruned) and each paying account's churn-risk state |
| `prune_login_attempts` | daily | `run_prune_login_attempts` — drops `login_attempts` rows older than two days |
| every tick | — | Run-now requests first; `minute_duties` (also from the pulse during a long job): `marketing_publish.run_due_posts`, `delayed.run_due`, `issues.tick`, `notify.release_due_alerts`, `guest_email.run_newsletter_sends`, `guest_marketing.run_campaign_sends` (guest text campaigns still sending, cut off by a deploy, or waiting for 8:00 AM — bounded at 2 minutes a tick, the `guest_campaign_queue` rows are the cursor; a stale in-flight text is failed, never re-sent), `push.reap_push_outbox` and `webhooks.reap_webhook_outbox` (`push_outbox_reaper`, `webhook_outbox_reaper`: rows a restart or a full pool left queued or half-delivered go back to the pool, rows too old to matter expire as not sent) — eight duties, each captured on its own; `owed_sends` (`billing_jobs.run_owed_sends`: the billing email a signing or a Stripe event owes — receipts, dunning, the set-password welcome, pay reminders — each row claimed before its send, retried with backoff); `morning_brief` (`morning_brief.run_due`: a due-queue with a 4-minute bound and a cursor, each restaurant at its own brief hour, only restaurants `in_service`; a window that closed unsent is a missed window); `run_health_checks`; then `loop_completed_at` and the dead-man ping |

**Registered by the integration wave (9/29/26):** `business_metrics`, `value_figures`, `owed_sends`, `dunning`, `provider_probes`, `contract_chase`, `stripe_reconcile` and `onboarding_nudges` (rows above), and the two outbox reapers among the minute duties. Each shows "never run" on Operations → Jobs until its first window; its SLA is measured from the boot that first knew it (`job_expected_since`), not from the start of time.

`admin_ops.RUNNABLE_JOBS` is the admin console's "run now" map onto the same functions, built from `jobs_registry.JOBS`.

The **shift-scheduling** feature (an owner clicking "Generate optimized schedule") is an on-demand async job (`ops.async_jobs`, one per restaurant at a time — `ops.active_job`), run by `schedule_engine._run_schedule_job` — see `MODULE_OVERVIEW.md`'s Labor section. It is not a scheduled job; only the weekly draft (on the owner's draft day, Thursday by default) and the publish the day after are.

### Shift Quality Engine (`shift_quality.py`)

A pure evaluation layer with no I/O: `ShiftContext` (what happened) → per-dimension `DimensionResult` → the `DIMENSIONS` registry combines them into one 0–100 score. Three invariants hold everywhere in this file:
1. A dimension with no data returns `None`, never `0` — weights renormalize across whatever dimensions *do* have data.
2. A critical dimension under its floor **caps the shift at its own score** (never drags it further via an arbitrary penalty).
3. Confidence (how much data backed the score) is tracked separately from the score itself — a high-confidence 60 and a low-confidence 60 are reported differently.
`SUBSTANTIVE_DIMENSIONS` gates the "fatigue alone can't produce a score" rule. The what-if / swap evaluator (`_SwapIndex`) only considers same-role swaps and is bounded (`MAX_CANDIDATE_EVALUATIONS`) so it stays O(1)-ish per legality check rather than re-scanning the whole schedule.

## The day's jobs (workflow audit #19)

Owner-facing jobs are attempted **hourly** and gated per restaurant on its own local hour, once per local day: `weekly_digest` (9am), `monthly_summary` (9am on the restaurant's own 1st — the calendar gate is `local_due(day=1)`, lost once in Sep 2026 and pinned by a test since), `onboarding` (10am), the daily alert checks (10am, `notify._gated_out`) and `issue_scan` (10am). Infrastructure jobs keep their Chicago-time daily claims.

Every tick: `notify.release_due_alerts()` (alerts held through a rush). Every 20 minutes: `intraday_capture` (hourly per restaurant while open), `pre_dinner_pulse` (4pm local, one push when the day is materially off), `coverage_check` (scheduled staff not clocked in), `preshift_nudge` (the hour the owner picked, off by default).

## Strategic jobs (`strategy_jobs.py`, gated in `scheduler.scheduler_loop`)

- `loss_sync` — daily after `pos_sync` (3am CT+): comps/voids/refunds into `pos_loss_daily`; an unsupported POS is normal, not a failure.
- `outcome_evaluations` — daily 6am CT+: closes outcome trackers whose window ended (storing after-value, % change, the other changes in the window and the attribution grade, and accruing the window's measured days into `outcome_value_days`), marks met goals.
- `outcome_rechecks` — daily 6am CT+, after `outcome_evaluations` (`strategy_jobs.run_outcome_rechecks`): re-checks each measured move at `outcomes.RECHECK_DAYS` (a win that no longer holds stops counting in Delivered and stops accruing), then accrues measured dollars day by day while each move holds. Bounded and resumable (`_bounded_each`, cursor `outcome_rechecks_cursor` in `job_cursors`); each tracker's `accrued_through` is its own cursor, so a second run adds nothing. Sends nothing; runnable from the admin console (`admin_ops.RUNNABLE_JOBS`).
- `auto_draft_schedule` — hourly; each restaurant from 6am in its own zone on its own draft day (`restaurants.auto_draft_weekday`, `models.auto_draft_weekday`: Monday–Saturday, Thursday by default — owner 9/27/26), claimed once per restaurant per LOCAL date: drafts next week's schedule for `auto_draft_schedule=1` restaurants with no `external_scheduling_tool` and no schedule in the last 5 days. A draft in Schedule History only; the push is sent only when the job row says `done`. Bounded (40 minutes) with a `job_cursors` cursor, so a pass that runs out of time resumes where it stopped.
- `issue_scan` — hourly: bad reviews → issues where a manager is routed.
- `closing_summary` — at each restaurant's own close (a close at or after midnight is the same night's, owned by its business date — `time_utils.service_window`), after one last POS reading: tonight against a typical same weekday plus the close-out handover, to the brief's audience. Skipped in quiet hours and when there is nothing to say — and for every restaurant the nightly DSR runs for (`dsr_enabled` and a POS connected, `dsr.deliver.replaces_closing_summary`): its DSR notice replaces this one, one notification not two.
- **DSR delivery** (`dsr.deliver`, called by `dsr.pipeline` after a version is saved terminal — never able to fail or roll it back; errors go to `ops.capture` as `dsr_deliver`): every active owner login at the location (and the group owner's) gets the Owner DSR, every manager login the Manager DSR — `dsr.access.view_for`, content always `dsr.access.render`; employees, support and Cavnar admins get nothing. Email (`emails.send_dsr_email`, `report_shell`) goes at once; the push (`dsr`, P4, payload `{type: "dsr", business_date}`) is held through `alert_quiet_start`/`_end` and released by `dsr_delivery`. One notice per night, person and channel, at the first terminal version; a night that went out provisional gets exactly one "Updated" notice when a later version is final (a push still held then goes out once, from the final version). Claim-before-send in `dsr_deliveries`. A failed night notifies nobody — the pipeline already captured its errors (`job_failures`, job `dsr`) for the admin console. Only where `scheduler.scheduling_allowed()`: a laptop never sends.
- `demand_opportunity` — weekly (claimed on the ISO week, not the date): a reliably quiet weekday two days out, Marketing module only. It drafts a post (a `marketing_drafts` row) and no guest text: the push opens Marketing (`nav: "marketing"`), whose Opportunity Feed card for the night drafts the text in the Campaign Studio with the audience and target day set. A `guest_sms` draft it wrote before 9/28/26 had no send path (AUX-5); those still expire and still count toward the two-ignored-weeks stop.
- Every tick (and, for the first four, from the pulse during a long job): scheduled posts, `delayed.run_due()`, `issues.tick()` (held notifications, one escalation), `notify.release_due_alerts()`, and — on the loop thread only — `morning_brief.run_due()` (per-restaurant local hour, claimed per restaurant per day). The brief goes to every recipient in `morning_brief.recipients()` — owners and managers by default, per-login opt-out in `login_prefs` — each built from that person's own permissions (built once per distinct view) and pushed to that person's devices only, or emailed.

**Loop hazard (fixed Sep 19 2026):** never assign to a name inside `scheduler_loop` that is also a module helper it calls — Python makes it local for the whole function. `_due = run_due_posts(...)` did exactly that and every tick died on `UnboundLocalError`. `tests/test_scheduler_catchup.py` now drives a real tick.

## Notification system

Three delivery channels, one firing decision layer:
- **Email** (`notify.py`, `emails.py`) via Resend. Every sender returns an `emails.SendResult` (`.ok`, and a `.reason` of transient / refused / skipped / deferred) and logs against its restaurant; a caller acts on `.ok`, never on a truthy object. Suppressions have scopes and only widen; operator mail is never suppressed (`DATABASE_SCHEMA.md` → Email). Cavnar AI's own marketing carries the CAN-SPAM postal address (`CAVNAR_POSTAL_ADDRESS`) and a signed opt-out — without the address it is not sent, and the operator is told once a day.
- **SMS** (`notify.py`) via Twilio, signature-validated on inbound. Every attempt is an `sms_log` row (the number hashed, last four kept; the write waits at most `notify.SMS_LOG_BUSY_MS`, 2 seconds, for the lock, so a ledger row never holds up a page to the operator); every text asks Twilio for a status callback (`POST /webhooks/twilio/status`, signed) that moves the row forward only; account-level Twilio errors are captured at most once an hour; `send_sms` never texts a number that sent STOP (sign-in codes excepted) and `notify.textable` checks consent plus the platform STOP for every non-code text.
- **Push** (`push.py`) via APNs (JWT provider auth, environment-aware host). Every push is a `push_outbox` row per device before the pool takes it; a deploy mid-burst no longer loses the queue (`push.reap_push_outbox` re-drives it every tick, one of the minute duties; `webhooks.reap_webhook_outbox` does the same for customer webhooks).
- **Alert storms**: at `ALERT_STORM_PER_HOUR` (10) alerts in an hour a restaurant is capped until its next local midnight (`alert_storm_caps`); health alerts still go through; the operator is told once; an admin can lift it early.

**Two entry points, one delivery path.** `blast()` inside `fire_review_alerts` raises review alerts; `notify.raise_alert()` raises everything else (`check_daily_alerts`, `check_extra_daily_alerts`, `check_no_response_alerts`). Both check the gates — the daily ceiling (`_over_alert_ceiling`, `ALERT_HARD_CEILING_PER_DAY = 50`), the owner's `alert_max_per_day` (`_daily_alert_suppressed`), quiet hours (bypassed for true health alerts via `health_bypasses_quiet_hours`) — then hold through a rush (`rush_release_at` / `hold_alert`, released by `release_due_alerts` every tick) or hand off to `deliver_alert()`, which owns channel selection, logging and the webhook. Nothing may deliver around it.

**The morning batch.** `run_daily_alert_checks` opens `notify.begin_daily_batch()` before the three 10am checks and flushes after. Types in `DAILY_BATCH_TYPES` are collected across the whole pass and sent as ONE notification per restaurant, worst first, when more than one fired — eight separate notifications about the same week of trading is what teaches an owner to stop reading. Each folded type still writes its own `alert_log` row, so the 7-day repeat windows are unchanged; the `daily_briefing` wrapper is in `models.NON_ALERT_TYPES` and does not spend the cap.

**Priority.** `push.PRIORITY` maps every alert type to P0–P5 and is the single source for three things: whether the phone may break a Focus mode (`interruption-level`, P0/P1 only), how long APNs keeps retrying (`apns-expiration`), and how both notification centers rank and filter. Stored on `alert_log.priority` at write time so a later change to the map cannot rewrite history.

**Where a push lands, and what it can do** (friction audit #3 / #22, 9/25/26). `fire_push` adds `nav` to every payload (`push.nav_for`, the nav.py path: `review/<id>`, `action/<delayed_id>`, `request/shift-<id>`, `schedule/<id>`, `dsr/night/<date>`, else the section or module) and, for a review, `draft_ready` — read once per push against `models.BULK_PUBLISHABLE_SQL`, the same bar as Home's "Publish N replies". `_category` picks the buttons: `CAVNAR_REVIEW_DRAFTED` (Approve & post · Edit) only for a draft that may go out unread, `CAVNAR_UNDOABLE` (Undo · Review) for `schedule_publish_pending` / `order_send_pending` with a `delayed_action_id`, `CAVNAR_REQUEST` (Approve · Deny) for a `shift_request` carrying `request_id` + `request_kind` (`shift_requests._tell_managers`); otherwise `CAVNAR_REVIEW` (Reply), `CAVNAR_BRIEF` (Ask about this, which sends) or `CAVNAR_ISSUE` (no button — the tap opens it). The action buttons run in the background behind the phone's own unlock (`.authenticationRequired`), with the stored owner session (`PushManager.perform`); a failure, or an alert about another location than the one the phone is on, posts a local notification carrying the original payload so a tap opens it. A new-review alert fires before its reply is drafted (`scheduler.py` alerts, then drafts), so it usually arrives as `CAVNAR_REVIEW`; `no_response` and held alerts about drafted replies get Approve.

**Push failure classification** (`push._classify`) — the P0 of audit #19. `dead` (Apple says the token is gone) deletes it; `provider` (our key, our topic, a 5xx, a network drop) never touches the failure counter and raises one `ops.capture` per hour; an unclassified 4xx counts, and at `_AUTO_DISABLE_AFTER` the token is PARKED (`disabled_reason`), not deleted. A 403 re-mints the cached JWT. Before this, ten alerts sent during an expired `.p8` deleted every device at every restaurant.

Every fired alert is logged to `alert_log` — the durable record `ask_cavnar_tools._read_alerts` and the Ask Cavnar snapshot read back from — and so are the advisory notifications that push directly (`notify.record_notification`: the brief, the pulse, the closing summary, a drafted schedule, an issue, a coverage gap, an outcome win). Opens are recorded in `notification_opens`, which powers `notify.engagement_report` (a suggestion to the owner, never an automatic downgrade) and admin's open-rate panel. See `project_job_and_alert_architecture` — the 50/day ceiling and claim-before-work pattern exist specifically to stop one bad fetch from becoming 180 notifications, a real incident this system had.

## Auth

- **Web**: session cookie, `sessions` table (`auth.create_session`), `auth.login_required` decorator checks `get_current_user()`. CSRF token required on state-changing POSTs (`csrf.py`, `_csrf_fetch.html` include). Login throttling is durable (`security.login_throttled`, `login_attempts` table).
- **iOS**: Bearer token in `Authorization` header, same `sessions` table, `auth.mobile_login_required` decorator.
- **Staff**: PIN identity in the staff portal (`staff_routes.py`, `auth.staff_login_required`, `memberships` + `staff_portal_tokens`; pepper in `docs/ops/PIN_PEPPER_RUNBOOK.md`).
- **2FA**: SMS or email OTP (setup routes in `auth_routes.py`/`mobile_api.py`; `auth._billing_blocked` gates a lapsed account at the decorator), with `two_fa_backup_codes` as a hashed, single-use fallback. Codes go through `auth.deliver_two_fa_code`: texts on Twilio's OTP messaging service, falling back to email when the number has sent STOP or the text fails; the sign-in screens say what really happened (mobile login adds `code_sent`, `fell_back`, `delivery_error`).
- **Team access**: a restaurant can have multiple `users` rows; `role` distinguishes `owner` from teammate — only `owner` can invite/revoke team members (`auth.invite_team_member` / `revoke_team_member`).
- **Login history**: every successful login writes one `login_history` row (independent of `sessions`' hard-deletes on expiry/revoke; kept 90 days — pruned nightly by the retention registry and at boot by `auth.prune_login_history`, which reads the registry's own window and floor; each login's month is summarised into `engagement_monthly` first), giving a real "sign-in activity" audit trail.
- **Device trust / sessions list**: `trusted_devices`, `get_sessions_for_user` (with device de-dup), `revoke_other_sessions`.
- **Admin**: separate `auth.admin_required` decorator. `is_admin` logins write; the `support` role (`permissions.ROLE_SUPPORT`) reads the console and opens view-as but every write returns 403 (except view-as and its own 2FA enrolment); no client role reaches it. Since the fix round (9/29/26):
  - **An internal login's own second factor** (`users.two_fa_enabled` / `two_fa_method`, `user_backup_codes`) — asked on every sign-in path (web password, Google, Apple, mobile), answered from the login's own row, never the restaurant's switch. `auth.admin_second_factor_state` is `ok`, `enrol` (only when `ADMIN_REQUIRE_2FA=1`: the console sends the admin to `/admin/two-factor`), `verify` (a session from before enrolment is ended) or `error` (fails closed, 503). Google and Apple sign-ins by an enrolled admin are refused unless the device is remembered.
  - **Step-up**: `@auth.recent_auth_required()` under `@admin_required` refuses 403 `reauth_required` unless the password was typed within 15 minutes (`sessions.reauth_at`, stamped by a password sign-in and by `POST /admin/api/reauth`; five wrong passwords end the session). Applied to 32 routes — the login actions, the reset-link and welcome sends, freeze, the demo flag and delete, client delete, add-location, the four billing actions and every POS credential save or disconnect — and, through `auth.reauth_refusal`, to the part of three more that needs it (a settings save that changes billing, a module or the owner email; an offboarding step that acts; the review-account rotation). The list is in `docs/ops/SECURITY.md`.
  - **An expired or missing session**: `admin_required` answers 401 `{session_expired}` to anything that wants JSON (a write, or a GET under `/api/`, `/admin/api/` or `/mobile/api/`) and a 302 to the login to a page.
  - **Lifetimes**: an internal login's session lasts 12 hours absolute; a view-as session 2 hours absolute, never extended, carrying the acting admin (`sessions.acting_admin_id`) and `read_only` for support. Every write through a view-as is recorded (`view_as_write`) and the dashboard shows a banner naming the admin whenever the session is a view-as.
  - **The request ceiling**: 240 requests and 60 writes a minute per admin session (`security.admin_request_allowed`, process-local), 429 with `Retry-After`.
  - **Lockouts**: an internal login is locked per address (a stranger cannot lock the admin out from everywhere), with a separate cap of 100 failures a day across all addresses; a remembered device skips the account lock. Break-glass: `LOGIN_UNLOCK_USERNAMES` and `ADMIN_2FA_RESET_USERNAMES` at boot, `scripts/unlock_login.py`, the console's clear-lockout (`docs/ops/RECOVERY.md`).
  - **The seed**: `auth.ensure_admin_login` creates an admin only when none exists at all, only with an `ADMIN_PASSWORD` that passes the policy, on its own "Cavnar AI Admin" restaurant row.

## Deployment

Railway runs `railway.json`'s `startCommand` (gunicorn, one worker, four threads, a 120-second request timeout, and an access log that writes `method route-rule status ms bytes class rid` — never the raw path). The `Procfile` carries the same command since 9/29/26 (a test pins them equal); it is only picked up by a platform that has no `railway.json`. `healthcheckPath` is `/health` and `healthcheckTimeout` 120 seconds. **With a volume attached, Railway runs one deployment at a time**: the old container is stopped before the new one boots, so a boot that fails or a healthcheck that never answers 200 is DOWNTIME until a redeploy or a rollback — not "the previous container keeps serving". Python is pinned by `.python-version` (3.12.7); Railway (Nixpacks) and CI install `requirements.txt`, a lock generated from `requirements.in` (every package pinned, transitive ones included; CI installs it with `--no-deps` and runs `pip check`). The marketing site is a hand-deployed Cloudflare Worker (`wrangler.jsonc`, `public/` only). CI (`.github/workflows/ci.yml`) compiles every module, runs the colour and silent-handler lints and pytest, plus a non-blocking vulnerability scan of the lock; the iOS build is not in CI.

## The web process: boot, threads and supervision

**Boot** (`hosted_dashboard.py`, module level, so gunicorn runs it): `logging_setup.configure()` first; `models.require_volume()` — on Railway the boot is REFUSED with no `RAILWAY_VOLUME_MOUNT_PATH`, a missing or unwritable mount, or the database off the mount (`ALLOW_NO_VOLUME=1` overrides; a no-op locally); a requested restore (`db_restore`, `RESTORE_FROM`); `status_manager.assert_platform_not_emptied()` — a volume whose marker (`.cavnar-volume.json`) says it held client restaurants must still hold them, checked BEFORE `init_db` and the seeds could build an empty platform over it (`ALLOW_EMPTY_DATABASE=1` overrides); `init_db` and the `init_*` chain (any failure fails the boot — `DB init error:` in the log — rather than serving a half-migrated schema); the boot record (`boot_events`); the admin seed and the break-glass variables; the scheduler thread; the demo seed thread; then `_post_boot`: the reference schema `/health` compares against, `credentials.encrypt_existing()` (plaintext credentials re-saved encrypted once `CREDENTIAL_KEY` works), the volume marker, the boot marked ready, a crash-loop check (more than 3 boots of one deployment, or more than 3 that followed a crash, in an hour pages once), and the supervisor thread. At exit the supervisor flushes the minute still filling and the boot is stamped as ended cleanly, so the next boot knows how this one ended.

**Threads in the web process**: gunicorn's four request threads; the scheduler thread (with its pulse threads, the `intel` lane and the lease keeper); `platform-supervisor`, a `PlatformSupervisor` ticking every 30 seconds — it restarts a dead scheduler thread with the thread body `start_scheduler` uses, the lease keeper and then the loop (`scheduler._run_scheduler_thread`, through `hosted_dashboard._restart_scheduler_loop`), with backoff from 30 seconds doubling to ten minutes, never beside a live one (a thread alive but wedged is left to the heartbeat and the dead-man ping); flips the public status page's scheduler row while the heartbeat is stale (`status_manager.check_scheduler_liveness`); runs `ops.check_platform_sla(send=True)` every five minutes; persists the telemetry `http_layer` buffered, and hourly prunes it and refreshes the volume marker; the two bounded admin pools (`ops.run_admin_task` and `admin_routes._submit_admin_job`); the webhook and push delivery pools; and the threads a request starts — an owner's schedule generation, the Ask stream — which carry the requester's AI attribution (`ai_utils.attributed`), as the admin pools do. Every thread that dies is logged with its traceback (`logging_setup`).

**Logging.** One handler on the root logger, installed first: JSON lines on Railway (or `LOG_FORMAT=json`) — `level`, `logger`, `message`, `thread`, the request id, route and method, and anything bound for the restaurant or job, with the traceback rendered into the record — and readable lines locally. stdout is line-buffered. Sentry's release is the commit (`RAILWAY_GIT_COMMIT_SHA`). A job that fails is logged with its text redacted and a redacted `traceback` field (*Standard counts* above). `worker.py` installs the same handler first (`logging_setup.configure()`, bound `process=worker`), then runs `models.require_volume()`, before `init_db()`.

## The admin console (`admin_routes.py`, `admin_ops.py`, `admin_events.py`)

- **Reads** are built from one fleet build (`admin_ops._records_cached`): a 45-second memo on request threads only (10 seconds after a build with query errors), single-flight with one waiter; a request past that is refused 503 `busy` with `Retry-After` and `X-Admin-Busy`. Every write under `/admin` (on any blueprint) and every Stripe or DocuSign webhook drops the memo; `?fresh=1` accepts a memo at most 5 seconds old. Each payload says how old it is and what failed (`generated_at`, `cached`, `age_seconds`, `errors` / `query_errors`, `unavailable`, `windows`); a failed query is captured at most once per 10 minutes and shown as unknown, never zero. Off a request thread (the scheduler, the operator digests) every call builds fresh.
- **Issues** are resolved per OCCURRENCE (`admin_issue_resolutions.occurrence_at`): a newer occurrence reopens the issue; condition-level issues stay resolved while the condition lasts and lapse after 30 days; `scheduler`, `platform:error_rate`, `backup` and deletion requests cannot be resolved (409).
- **Support** reads everything but writes nothing; an `after_request` hook masks owner and login emails and phones, sign-in IPs and user agents, and Stripe ids in every `/admin/api/` JSON read answered to a support login (`X-Redacted: support`) — by key name, and within free text for emails, Stripe ids, phone numbers and IPv4 addresses — on the admin blueprint and the sales-audit blueprint's `/admin/api/audits*`. The legacy HTML client pages refuse support (403); the task poll and an AI call's text are admin-only.
- **Writes** are audited twice over: the request hook's row (after the response, only for a console login that passed CSRF, a phone in the stored body cut to its last four digits; refused attempts to `admin_audit_refused`) and, for every action that records itself, the one typed call `admin_events.record_admin_action` with actor, target, before/after and result.
- **Sends from an admin action** (reset links, welcome, test sends, value recap, "Run now" of a sending job, fetch now) are refused 409 on a backend where `scheduler.scheduling_allowed()` is false (`admin_routes._send_blocked`, `LOCAL_SENDS_REFUSED`) — a laptop holds production's Resend and Twilio keys. The contract resend (a DocuSign email) and the alert-contact test text use the same gate. The billing actions that email or change Stripe (card-update link, change plan, a mark-signed that sends — checked before it writes) are refused the same way (`admin_routes._live_actions_refused`); resend-payment's email stays queued (`held`); the offboarding Stripe cancel and DocuSign void answer 409 `local_backend`. Deliberately not gated: the admin's own two-factor code, the owner and login flows in `auth_routes`, and `/api/send-referral` (`docs/ops/SECURITY.md`).
- **Background work** runs on two bounded pools tracked in `async_jobs` and polled: `ops.run_admin_task` (fetch now, manual POS syncs; `/admin/api/tasks/<id>`) and the one admin job pool, `_submit_admin_job` (menu extraction, new-client setup, and — through `_start_admin_job` — drafting, redraft-all, the Instagram token refresh, the review-account seed; `/admin/api/admin-jobs/<id>`, `/admin/api/menu-extract/<id>`, `/admin/api/create-client/<id>`). A pool job's model and Places calls are the admin's, not the client's (`ops.run_admin_task`'s attribution, `admin_routes._admin_job_attribution`).
- **One welcome email** (`emails.send_welcome_with_set_password_link`) for the post-signing outbox, checkout provisioning and the console's Resend welcome, with a one-use set-password link good for 72 hours from the send (`models.SET_PASSWORD_LINK_HOURS`).

## Billing lifecycle (`webhook_routes.py`, `billing_jobs.py`, `models.py`)

- **Every billing-state write** goes through `webhook_routes._apply_state` / `_lock_accounts` and `models.update_restaurant`, which raise on failure: the event claim is released and Stripe redelivers, instead of a 200 over a lost write. Every change to billing status, pause reason and date, contract status, Stripe customer, tier or module flags is a `billing_status_history` row in the same transaction, attributed by `models.billing_context`.
- **Matching** is by subscription id, then metadata, then the invoice or charge on file, then customer — never by email; a stored customer id is never overwritten by an email match or an invoice; an ended subscription is never read as live, so a returning client's new checkout is not cancelled as a duplicate; a duplicate checkout's refund, invoices and cancellation change nothing (`stripe_duplicate_subscriptions`).
- **One subscription per group** (owner decision): sibling locations are covered by the paying location — their pay links and billing screens say who pays; state changes apply only to the locations a subscription covers (`subscription_scope`), except a dispute, which holds the whole group; MRR counts each subscription once.
- **Holds**: a dispute pauses the whole group with `pause_reason` `dispute`, a FULL refund only the location that paid (`refund`); later Stripe events and paid invoices never lift a hold (even on an account that has since churned); only the admin lift-hold route does. A self-serve pause (`self`) keeps working, and the owner's Resume refuses a hold with 409.
- **The post-signing outbox**: at signing (inside the step the DocuSign claim protects) the payment link and the one welcome (above; its 72-hour set-password link is minted at the send) are written to `owed_sends`, then sent at once, each claimed before the send and marked from the real `SendResult`; no password is ever reset. Anything a Stripe event owes (receipts — one per paid invoice —, dunning for attempts 1–3, the provisioning welcome) is only queued, and sent by the billing jobs.
- **Billing jobs** (`billing_jobs.py`; nothing sends where `scheduling_allowed()` is false): `run_owed_sends` drains due rows with backoff (6 tries over about two days, then `failed` + `ops.capture`); `run_dunning` is the safety net for missed payment-failed events and cancels queued dunning for invoices since paid; `run_contract_chase` resends the pay link on days 2, 5 and 9 after signing (with a 2-day grace so a deploy cannot mail old signers); `reconcile_stripe` refreshes the mirror nightly (bounded and resumable) and records mismatches in `billing_reconcile`, never changing billing status itself; `cancel_subscription` (the offboarding Stripe step) cancels every live subscription on the customer, refused for a location another's subscription covers and on a server that is not production. Scheduled as `owed_sends` (every tick), `dunning` (hourly), `contract_chase` (10am) and `stripe_reconcile` (3:30am) — the gates above.
- **An admin's billing-status override** (the client-settings save, with its required reason) is attributed `source='admin_override'` in `billing_status_history`; leaving `paused` clears `paused_until` and `pause_reason`, and an admin's pause is `pause_reason='admin'`, a hold only an admin lifts.
- **Contracts**: every envelope requests DocuSign reminders; a resend reuses the envelope unless it was declined or voided or the module list changed; declined / voided / delivered envelopes update `contract_status` for the current envelope only; trials never auto-expire — the console raises "signed N days ago, never paid" at 7 days (warning) and 30 (critical).

## Payments — Stripe

`stripe_customer_id` lives on `restaurants`. `emails.create_stripe_checkout` builds a Checkout Session for the setup fee + retainer at signup (amounts always read from `pricing.TIERS`, never hardcoded — see `PROJECT_CONTEXT.md`). `webhook_routes.py` verifies inbound Stripe webhooks (`stripe.Webhook.construct_event`) against `STRIPE_WEBHOOK_SECRET`, read on every request — an empty secret is refused 401 and counted, a bad signature 400 — and de-dupes by event id via `stripe_events_seen` before acting, so a Stripe retry can never double-process a payment event; the ledger row (`admin_events`, one per event id) is written after the duplicate claim. The subscription itself is mirrored locally (`stripe_subscriptions`, `stripe_invoices`) — see *Billing lifecycle* above; the admin console reads the mirror, never Stripe, except the one client's "live" billing read.

## Intel module

`competitor.py` / `competitor_intel_format.py` pull nearby competitor data (Google Places) into `competitor_snapshots`, diffed week over week. AI-visibility checking (`ai_visibility_runs` / `ai_visibility_query_runs`) asks Perplexity a fixed set of queries an actual customer might ask and records whether/how the restaurant is mentioned — reported as a **range** over the sample of queries, never a single false-precision number, and never blended with "is Cavnar AI set up for this client" (see `project_intel_module` invariants: a missing measurement is never 0, no unsourced claims about AI indexing).

## Cross-cutting: what to read before touching each area

| Touching... | Read first |
|---|---|
| Any route in `client_api.py` | Check whether `mobile_api.py` already has the logic — delegate via `_m()` |
| A new `restaurants` column | The "four-plus-one touch points" rule in `PROJECT_CONTEXT.md` |
| Labor / scheduling | `MODULE_OVERVIEW.md` Labor section + `shift_quality.py`'s module docstring |
| Ask Cavnar | `ask_cavnar_tools.py`'s `TOOLS` registry (kind: read/action/write) |
| Anything emailed | `emails.py` — every sender returns a `SendResult` and logs against its restaurant; one `RESEND_API_KEY` pattern to get right |
| Dashboard JS | ES5-only, even in comments — `tests/test_frontend_rules.py` |
| A color | Goes through a CSS variable, never a literal — `scripts/check_colors.py` |

## Intelligence engine

Two nightly jobs in the scheduler: `intelligence_features` at 3am (one
feature row per active restaurant per ISO week; worker pool, wall-clock
bound, cursor in `job_cursors`) and `intelligence_learning` at 4am
(feedback sync → pattern discovery → benchmarks → confidence log, reading
only the materialized `intel_*` tables). Request paths read materialized
rows only: Home attaches a confidence band to each recommendation, Ask has
two read tools and one context section, admin has `/admin/api/intelligence`.
Design and privacy rules: `INTELLIGENCE_ENGINE.md`.
