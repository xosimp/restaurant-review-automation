# Postgres, a job queue and more than one web worker (#96)

Plan, 9/29/26 (admin-console fix round, finding #96).

**Status, 10/10/26 (scheduler audit, workstream H2 — RM-7, RM-13, RM-14,
SCALE-1/2/10/13/20/21/24/25/32, ARCH-4/22/34).** Step 2 (the job queue) and
step 3 up to the cutover (Postgres behind `models.get_conn`, the copy and its
verification, the normalized schedule model as a shadow, blobs in object
storage) are BUILT. What runs in production is unchanged until the switches
are set: the queue is consumed in the web process (`QUEUE_WORKER=1` moves it
to `worker.py`), the shadow is off (`PG_SHADOW_WRITE`), blobs stay inline
(`BLOB_STORE`), and SQLite is the database until `DATABASE_URL` names a
Postgres — the cutover is Will's step, in `docs/ops/POSTGRES_CUTOVER.md`.
Built:

- **`job_queue`** — one `jobs` table (kind, group, tenant, priority class
  interactive / scheduled / bulk, not_before, deadline, attempts, lease,
  idempotency key, stage checkpoint). Claimed in one write transaction (an
  advisory transaction lock on Postgres): a group's concurrency is a count of
  claimed rows across every process, one generation runs per restaurant,
  presses go before drafts (scheduled by earliest deadline), tenants share
  each class by weighted fair queueing gated by a token bucket. Consumers:
  the web process by default, `worker.py` with `QUEUE_WORKER=1` (the web
  process consumes after all if the worker's heartbeat goes stale). FIRST
  CONSUMER: schedule generation (owners' presses and the auto-draft), then
  the nightly learners' per-restaurant passes (`learner` rows).
- **Resume across a deploy** (`schedule_resume`): a running row is handed
  back at SIGTERM (or its lease runs out); the next try finishes from the
  saved checkpoint, or runs again reading every model answer its earlier
  try paid for (`schedule_model_calls.job_id`, `call_key`). The boot sweep
  no longer fails a job the queue will resume.
- **`model_gateway`** — every Anthropic call classed; the organisation's
  tokens a minute allocated by class (`GATEWAY_ORG_OTPM` / `ITPM`), a
  tenant's tokens a minute, generation concurrency held to the budget,
  Message Batches for the unattended classes, per-restaurant AI ceilings,
  and the provider breakers shared through `ai_breakers`.
- **`db_dialect`** — Postgres behind `models.get_conn` when `DATABASE_URL`
  names one: the SQL translated per statement (placeholders, dates,
  upserts, `BEGIN IMMEDIATE` as an advisory lock, PRAGMAs answered,
  sqlite3's errors) — `scripts/pg_sql_audit.py` lists the handful of
  statements it cannot carry (SQLite triggers, ported in
  `migrations/postgres/0002_triggers.sql`; a few `strftime` / GLOB / PRAGMA
  sites).
- **`pg_copy` + `scripts/run_pg_copy.py`** — the schema (month-partitioned fact
  tables, deferred foreign keys), the copy (every value its column's type,
  what could not be counted), the verification (counts, key min/max/sum),
  and the Postgres -> SQLite export the nightly backup uses on Postgres.
- **`pg_shadow`** — the normalized schedule model (`sched`: weeks with the
  working and published pointers, immutable revisions, shifts by
  `shift_id` on timestamptz with a no-double-booking exclusion on
  published revisions, the event log) written from an outbox beside
  SQLite, compared before the switch.
- **`blob_store`** — model-call and quality blobs to object storage.
- **Versions as differences from a snapshot** (SCALE-34) and the off-site
  copy in parts past 512 MiB (SCALE-20).
Updated 10/7/26 (AI cost audit #56) with the process-local state that round
added — the interactive AI slots, the AI background pools, the route-override
cache — and where each goes in the order of work; and again 10/7/26 (iOS
parity round) with the generation watchers, the Studio cache and the push
pool's provider alarms and native deliveries.
It is grounded in the code at `fixround-0929`; re-derive every number with the
command beside it before acting on it.

## Where we are

- **One container, one gunicorn worker, four threads** (`railway.json`:
  `--workers 1 --threads 4`), one SQLite file in WAL mode on a Railway volume.
  The scheduler is a thread in the same process, elected by a lease row in the
  same file (`ops.acquire_scheduler_lease`); `worker.py` can run it as a
  second process in the same container, never as a second service
  (`docs/ops/RAILWAY_SCHEDULER_SPLIT.md`).
- **No load reason yet.** The Sep 2026 billing period was ~19 vCPU-minutes of
  CPU. The admin console's own measurement (#97) is the first thing that
  breaks with scale: 28–30 s per fleet read at 5,000 synthetic restaurants,
  now held off by a 45-second memo.
- **What already moved into the database** (so it no longer multiplies with
  workers): the login brute-force limiter (`login_attempts`), the
  supplier-order cooldown (`client_api._order_send_last` is gone —
  `ops.claim_cooldown` rows), the AI rate limiter's window (`ai_rate_events`;
  `ai_utils._ai_call_log` is only its fallback), run-now requests
  (`job_run_requests`), async admin work (`async_jobs`), and three durable
  outboxes that already have the shape of a queue — `push_outbox`,
  `webhook_outbox`, `owed_sends` (a row first, claimed before the send, marked
  from the real result, re-driven by a reaper).

## What still blocks a second gunicorn worker

Process-local state a second worker would duplicate (the inventory:
`rg -n "^_[a-z_]+\s*=\s*(\{\}|\[\]|set\(\)|dict\(\)|deque|_OrderedKeySet|threading\.)" --glob '*.py' --glob '!tests/**'`):

| State | Where | What N workers do to it | Move it to |
|---|---|---|---|
| The admin request ceiling | `security._admin_hits` | 240 requests / 60 writes a minute becomes N × that | **moved 10/11/26** (blind re-audit ARCH-26): `admin_request_hits`, a sliding-window counter per session and minute; the dict is only its fallback |
| AI breakers | `ai_utils._breakers` | **moved 10/10/26**: each process keeps its own counts, but every opening, closing and reset is the `ai_breakers` row, read back by every process within `AI_BREAKER_SYNC_SECONDS` | done |
| AI budget cache, blocked-call merge | `ai_utils._budget_cache`, `_blocked_memo` | stale spend per worker; blocked rows merged per worker, not per minute | read-through with a generation stamp; merge on an `INSERT … ON CONFLICT` key |
| The admin fleet memo | `admin_ops._fleet_state` | a write on one worker drops only its own memo: the other serves reads up to 45 s stale | **moved 10/11/26**: a generation counter in the database the memo checks (`job_cursors['admin_fleet_gen']`, moved by every admin write) |
| Bounded pools | `ASK_MAX_CONCURRENT` (`client_api._ASK_SLOTS`), Ask's read pool (`ask_cavnar.ASK_READ_WORKERS`, 10/7/26), `ops.run_admin_task`, `admin_routes._submit_admin_job` (the one admin job pool; `_AdminJobs` was folded into it), the webhook and push delivery pools, `admin_ops.heavy_slot`, the schedule generation pool and its slots (**moved 10/10/26**: a generation is a `job_queue` row and its slot a count of claimed rows — `QUEUE_GENERATION_CONCURRENCY` across every process; since 10/11/26 the only path — `_GEN_SLOTS` and `JOB_QUEUE=0` are gone — and consumed by the queue worker process beside gunicorn, which recycles itself; a row waiting on the model holds no CPU slot), (`ASK_MAX_CONCURRENT` and `INTERACTIVE_AI_SLOTS` guard each process's own request threads: right per worker by design) the owner AI job pool (`ai_async`, `OWNER_AI_JOB_WORKERS`; AI cost audit 10/7/26 #57 — invoice and recipe-card reads, the Campaign Studio's drafts), Home's snapshot rebuild pool (`home_snapshots`, `HOME_SNAPSHOT_WORKERS`; scale audit 10/19/26 FE-01), the interactive lane (`ai_lane`, `AI_LANE_WORKERS`; scale audit 10/19/26 A4-01 — polled Ask turns; its turns' events are in the process's memory, so a second worker also needs the poll pinned to its process or the events in the database) | every bound becomes N × its value | the job queue below (a global bound is a count of claimed rows) |
| The in-memory telemetry | `http_layer` (`_samples`, `_inflight`, `_rollups`, `_sentry_last`) | each worker reports only its own "now"; persisted rollups add up correctly | nothing — label the live panels per worker |
| Claim fallback and page memory | `ops._claim_fallback`, `ops._page_memory`, `ops._running_jobs` | fallbacks for a database that cannot be written; `is_running` sees one process (`running_elsewhere` reads the database) | acceptable as fallbacks; keep |
| Refused-audit window | `admin_events._refused_window` | N × 20 rows per IP per 10 minutes | the table's own cap already bounds it |
| Caches | `client_api._insight_cache`, `_aivis_cache`, `_city_cache`, `models._tenant_names_cache`, `intelligence.dashboard._build_cache`, `status_manager._schema_ok`, `labor._NOTE_CACHE` (a front cache over `insight_store`) | duplicated work, no wrong answer | keep |
| The interactive AI slots (AI cost audit 10/7/26) | `ai_utils._INTERACTIVE_SLOTS` (`INTERACTIVE_AI_SLOTS`, default 2) | the "at most two owner-facing model calls in flight" bound becomes 2 × N, and the slot a waiting request is promised exists only on its own worker | a count of claimed rows (the job queue below), or `ai_rate_events`-style rows with a lease stamp |
| The schedule model-call pool (scheduler fix round 10/10/26, C2) | `schedule_engine._call_pool` (`SCHEDULE_CALL_POOL_WORKERS`, default `SCHEDULE_GEN_WORKERS` x `SCHEDULE_PARALLEL_CALLS` = 6) — one pool per process for every generation's calls sent together (it was a new pool per batch, a bound per generation) | the "at most six schedule model streams at once" bound becomes 6 x N, one per worker | the model gateway's generation concurrency (`model_gateway.max_concurrent_generations`, claimed `jobs` rows) already bounds generations across processes; the streams per generation stay a per-process pool until calls are queue rows of their own |
| The AI background pools (10/7/26) | Ask's read pool (`ask_cavnar._READ_POOL`, `ASK_READ_WORKERS`), Ask's summary pool (`client_api._SUMMARY_POOL`, one thread), the orchestrator's shadow-review pool (`ai_orchestrator._SHADOW_POOL`, one thread), the learner's scoring pool (`ai_learning._SCORE_POOL`, one thread) | each one-thread pool becomes N threads; the shadow review's sampling rate is per worker, so the share of runs reviewed holds but the spend bound is N × | the job queue (kinds `ask_summary`, `shadow_review`, `shadow_score`) |
| The route-override cache (10/7/26) | `ai_orchestrator._OVERRIDES` (`OVERRIDE_CACHE_SECONDS`, 60) | an override applied or reverted in the console reaches the other workers up to 60 s later — the same window one worker already has | keep, or a generation stamp in `ai_route_overrides` the cache checks |
| The generation watchers (iOS parity round 10/7/26) | was `schedule_engine._GEN_WATCHERS` (`watch_generation` / `take_generation_watchers`) — who pressed Generate for each running job, pushed `schedule_drafted` when it lands | **moved 10/10/26**: the `generation_watchers` table keyed on the async job id, taken (read and deleted in one transaction) when the job finishes, wherever it ran — a restart no longer forgets who was waiting | done |
| The Studio's kept inputs (10/2/26; reviewed 10/10/26, SCALE-24: its key is `models.schedule_inputs_version`, read from the database, so a second worker can never serve a stale week — only re-sweep once) | `schedule_engine._studio_cache`, `_studio_live` (keyed on `models.schedule_inputs_version`; an LRU sized in bytes since 10/14/26 — `STUDIO_CACHE_BUDGET_MB` 512, each week measured as kept and its scorer as it fills, `studio_cache_stats()` its fill, hits, misses and evictions (blind re-audit 10/11/26 SCALE-22) — `STUDIO_CACHE_PER_RESTAURANT` 3, `STUDIO_CACHE_SECONDS` 900 as a backstop, one `_studio_lock` — scheduler audit 10/10/26 SCALE-8, BUGSLIFE-23), and each prepared week's last served answers for the re-score's deltas (`quality_payload`, SCALE-29) | each worker re-sweeps on its own first drag; the key moves the moment the inputs do, so nothing is stored stale; a delta's base another worker served is answered whole | keep (a cache) |
| The pre-publish review's kept answers (schedule re-audit 10/11/26 TRUST-26) | `prepublish._CACHE` (`CACHE_SECONDS` 30, `CACHE_MAX` 64; the SAVED week's review only — keyed on the week's version and state, `models.schedule_inputs_version`, the dismissals and overrides recorded, the reader and the day; `prepublish.invalidate` on an answer) | each worker computes the review once on its own; nothing stale past the key | keep (a cache) |
| The Crew stage's kept matrix (10/10/26) | `crew_matrix._matrix_cache` (`MATRIX_CACHE_SECONDS` 600, `MATRIX_CACHE_SIZE` 512; keyed on `models.schedule_inputs_version`, the week's draft and the newest POS day — scheduler audit SCALE-27) | each worker builds it on its own first view; the key moves when anything it reads does | keep (a cache) |
| Push provider alarms and the delivery ceiling | `push._provider_alarms` (one capture per APNs failure reason an hour), `push._queued` / the overflow queue (`_MAX_PUSH_QUEUED`) — the alerts, and since 10/7/26 the silent widget pushes and Live Activity updates too (`push._submit_native`) | N × one capture an hour per reason; N × the queue ceiling | the push outbox already holds every alert (`push_outbox`); the ceiling becomes a count of claimed rows with the job queue |

Not on the list, on purpose (iOS parity round, 10/7/26): the silent
widget push's throttle (`push.fire_silent`, one per restaurant, reason and
audience per `SILENT_MIN_MINUTES`) is an `ops.claim_cooldown` row, so it
holds across workers; `live_activities` keeps no process state — every
activity's token and state are rows (`live_activity_tokens`), and its
deliveries ride the push pool above; `insight_refresh` starts its reads on
the owner AI pool (`ai_async`, already listed) and joins a running read by
its `async_jobs` row (`ops.claim_async_job`), so a second worker joins the
same job rather than starting another.

Also not on the list, on purpose: the Places Details cache (`places_details_cache`,
AI cost audit 10/7/26 #13) and the context packets (`context_sections`,
`restaurant_context`) are rows in the database, so every worker reads the
same ones; the Message Batches queue (`ai_batch_jobs`, `ai_batch_items`) is a
table claimed with a compare-and-set (`ai_batches._claim`), already the job
queue's shape. The scheduler's lanes (`scheduler._LANES`: intel, ai and, from
10/7/26 #54, sweep — the 4-6am restaurant sweeps) and their wake-up event
(`scheduler._SWEEP_DONE`) live only in the process that holds the scheduler
lease, so they never multiply with web workers; in `worker.py` they move with
the scheduler.

The PlatformSupervisor would also run once per worker: two SLA checks every
five minutes (pages are de-duplicated by the cooldown row, so one page) and
two telemetry flushes (additive upserts — correct). Make it elect itself with
a lease like the scheduler's before running N workers.

## What blocks a second container or a separate worker service

SQLite on a Railway volume: "Each service can only have a single volume",
"Replicas cannot be used with volumes". Two containers cannot open one file.
The scheduler lease, every claim, every outbox and every rate limit live in
that file. So the order is fixed: networked database first, then services.

## Postgres: what the port touches

Counts at `fixround-0929` (`rg -F -c "<pattern>" --glob '*.py' --glob '!tests/**'`, non-test code):

| SQLite construct | Occurrences / files | Postgres |
|---|---|---|
| `get_conn(` | 1,527 / 136 | one factory in `models.get_conn`; keep the call-time wrapper pattern so tests can still patch it |
| `datetime('now'…)` | 698 / 84 | `now()` / `now() - interval …`; a SQL-dialect helper, not 698 hand edits |
| `strftime(` | 397 / 98 | `to_char` / `date_trunc` — most are stamp formatting that can move to Python |
| `ON CONFLICT` | 80 / 43 | the same syntax, mostly |
| `rowid` (incl. `lastrowid`) | 92 / 39 | `RETURNING id`; the retention chunked delete uses `rowid IN (…LIMIT)` — use the primary key |
| `PRAGMA` | 62 / 20 | removed (WAL, `foreign_keys`, `table_info` → `information_schema`, `integrity_check`, `optimize`, `schema_version` for the health cache) |
| `julianday(` | 49 / 11 | `extract(epoch from …)` |
| `INSERT OR IGNORE` / `OR REPLACE` | 39 / 14, 16 / 11 | `ON CONFLICT DO NOTHING` / `DO UPDATE` |
| `BEGIN IMMEDIATE` | 35 / 13 | `SELECT … FOR UPDATE` / advisory locks — the claim-before-work and compare-and-set paths depend on it (claims, the lease, owed-send claims, retention) |
| `sqlite_master` | 17 / 6 | `information_schema` (retention's "table exists", the scrub plan, the reference schema, `delete_restaurant`'s table walk) |

Also: the boot DDL (`CREATE TABLE IF NOT EXISTS` + guarded `ALTER` in two
lists) becomes numbered migrations; the backup (SQLite's online backup API,
the local snapshot, the scrubbed Fernet copy, `db_restore`'s file swap, the
restore drill) becomes `pg_dump` / point-in-time recovery with the scrub run on
a restored copy; `/health`'s write probe, schema check and volume marker are
rewritten; the test suite's template-database copy (`conftest._db_template`)
becomes a per-worker schema or transaction rollback.

## A job queue

Today the "queues" are threads in the web process. The pattern that works is
already in the code three times (`owed_sends`, `push_outbox`,
`webhook_outbox`): a row per unit of work, claimed with a lock-until stamp,
marked from the real result, retried with backoff, reaped after a restart.
Generalise it into one `job_queue` table (kind, payload, restaurant, state,
attempts, next_attempt_at, locked_until, result) and move onto it, in order:
the review-fetch AI work (#98, `REVIEW_FETCH_QUEUE_PLAN.md`), the admin
pools (fetch now, POS sync, menu extraction, drafting), then the outboxes.
On Postgres the claim is `SELECT … FOR UPDATE SKIP LOCKED`; on SQLite it is
the existing `UPDATE … WHERE state='queued' AND locked_until < now()`
compare-and-set. Workers are then a process (and later a service) that runs
the queue, beside the scheduler that only enqueues.

## Order of work

1. **Now, still on SQLite**: move the admin ceiling into the database; give
   the fleet memo a database generation (or build #97); elect the
   supervisor with a lease; make the interactive AI slots a database count.
   (The AI breakers moved 10/10/26.)
2. **The job queue — BUILT 10/10/26**, with schedule generation as its first
   consumer (not the review fetch: generation is what breaks first — owner
   presses wait past the 15-minute client wait at ~150-300 restaurants,
   SCALE-1) and the auto-draft and the nightly learners on it. Next kinds:
   the review fetch's AI work (#98), the AI pools (the summary, the shadow
   reviews, the learner's scoring), the admin pools, then the outboxes. To
   take the CPU and the model waits out of the web process, set
   `QUEUE_WORKER=1` with the start command in
   `docs/ops/RAILWAY_SCHEDULER_SPLIT.md`.
3. **Postgres — BUILT up to the cutover 10/10/26; run against a real Postgres since 10/14/26** (CI job `postgres`; the boot, the copy, every static statement PREPAREd, 37 database-heavy test files — blind re-audit 10/11/26 SCALE-5 … SCALE-12): `db_dialect` behind
   `models.get_conn`, `scripts/run_pg_copy.py` (copy + verify), the normalized
   schedule model as a shadow (`PG_SHADOW_WRITE`), blobs in object storage
   (`BLOB_STORE`); migrations replace boot DDL for the new model; backups on
   Postgres are the provider's point-in-time recovery plus the nightly
   export to SQLite that the existing scrub, encryption and off-site copy
   run on. The cutover — provision, copy, verify, switch, rollback — is
   `docs/ops/POSTGRES_CUTOVER.md`.
4. **Services**: once the database is networked, the queue workers and the
   scheduler as their own Railway services (the scheduler only enqueues,
   elected by an advisory lock); the web tier with N replicas; retire the
   volume. Workers autoscaled on queue depth (`job_queue.stats`).

**When** — re-based on what breaks first (scheduler audit 10/10/26,
SCALE-21; scale.md *What breaks first*), not "about 10,000 restaurants",
which was 30-50x too late:

| Step | Before | Why | Trigger to watch |
|---|---|---|---|
| Queue consumed by `worker.py` (`QUEUE_WORKER=1`) | **~150 restaurants** | owner presses queue past the 15-minute client wait at ~150-300 (2 slots, 5-30 minutes a week); the generation CPU holds the web process's GIL | `job_queue.stats()` `wait_p95_seconds` for the interactive class over 120 s (the target: p95 under two minutes to start) |
| Postgres shadow on, then the cutover | **before the RPOWER rollout (~15-25 POS stores; scale audit 10/19/26 DB-01 — the ~300 below predates the ticket archive)** | the 5 GB volume and the backup's 2-3x free space hold tens to ~100 restaurant-years of schedules; the Studio cache, the AI caps and press latency all fail by ~300 | the volume over 60% (`/health` disk, Railway's volume metric); a backup whose off-site copy took parts (multipart) means the snapshot passed 512 MiB |
| Services + autoscaled workers | ~1,000-2,500 | the GIL-bound CPU (~262 CPU-hours a week at 9,000), auto-draft throughput (~900-1,200 opted in) | generation queue depth at the auto-draft's deadline; saturated minutes on `/admin` → Engineering |
| Warehouse + change data capture (Stage 4) | a few thousand | learners scanning the fleet nightly | learner rows still queued at 8am |

The latency triggers (`request_rollups` p95 by class, the write probe's
wait) still apply, but they do not see queue wait — that is the new trigger
above.
