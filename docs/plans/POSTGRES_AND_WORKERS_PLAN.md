# Postgres, a job queue and more than one web worker (#96)

Plan, 9/29/26 (admin-console fix round, finding #96). Nothing here is built.
Updated 10/7/26 (AI cost audit #56) with the process-local state that round
added — the interactive AI slots, the AI background pools, the route-override
cache — and where each goes in the order of work.
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
| The admin request ceiling | `security._admin_hits` | 240 requests / 60 writes a minute becomes N × that | a `login_attempts`-style table, or the gateway |
| AI breakers | `ai_utils._breakers` | each worker trips and closes its own; "Reset breaker" reaches one | an `ai_breakers` row per provider, read with a short cache (the events are already in `ai_health_events`) |
| AI budget cache, blocked-call merge | `ai_utils._budget_cache`, `_blocked_memo` | stale spend per worker; blocked rows merged per worker, not per minute | read-through with a generation stamp; merge on an `INSERT … ON CONFLICT` key |
| The admin fleet memo | `admin_ops._fleet_state` | a write on one worker drops only its own memo: the other serves reads up to 45 s stale | the materialised health row (#97), or a generation counter in the database the memo checks |
| Bounded pools | `ASK_MAX_CONCURRENT` (`client_api._ASK_SLOTS`), Ask's read pool (`ask_cavnar.ASK_READ_WORKERS`, 10/7/26), `ops.run_admin_task`, `admin_routes._submit_admin_job` (the one admin job pool; `_AdminJobs` was folded into it), the webhook and push delivery pools, `admin_ops.heavy_slot`, the schedule generation pool and its slots (`schedule_engine.submit_generation`, `_GEN_SLOTS`, `SCHEDULE_GEN_WORKERS`; 10/3/26), the owner AI job pool (`ai_async`, `OWNER_AI_JOB_WORKERS`; AI cost audit 10/7/26 #57 — invoice and recipe-card reads, the Campaign Studio's drafts) | every bound becomes N × its value | the job queue below (a global bound is a count of claimed rows) |
| The in-memory telemetry | `http_layer` (`_samples`, `_inflight`, `_rollups`, `_sentry_last`) | each worker reports only its own "now"; persisted rollups add up correctly | nothing — label the live panels per worker |
| Claim fallback and page memory | `ops._claim_fallback`, `ops._page_memory`, `ops._running_jobs` | fallbacks for a database that cannot be written; `is_running` sees one process (`running_elsewhere` reads the database) | acceptable as fallbacks; keep |
| Refused-audit window | `admin_events._refused_window` | N × 20 rows per IP per 10 minutes | the table's own cap already bounds it |
| Caches | `client_api._insight_cache`, `_aivis_cache`, `_city_cache`, `models._tenant_names_cache`, `intelligence.dashboard._build_cache`, `status_manager._schema_ok`, `labor._NOTE_CACHE` (a front cache over `insight_store`) | duplicated work, no wrong answer | keep |
| The interactive AI slots (AI cost audit 10/7/26) | `ai_utils._INTERACTIVE_SLOTS` (`INTERACTIVE_AI_SLOTS`, default 2) | the "at most two owner-facing model calls in flight" bound becomes 2 × N, and the slot a waiting request is promised exists only on its own worker | a count of claimed rows (the job queue below), or `ai_rate_events`-style rows with a lease stamp |
| The AI background pools (10/7/26) | Ask's read pool (`ask_cavnar._READ_POOL`, `ASK_READ_WORKERS`), Ask's summary pool (`client_api._SUMMARY_POOL`, one thread), the orchestrator's shadow-review pool (`ai_orchestrator._SHADOW_POOL`, one thread), the learner's scoring pool (`ai_learning._SCORE_POOL`, one thread) | each one-thread pool becomes N threads; the shadow review's sampling rate is per worker, so the share of runs reviewed holds but the spend bound is N × | the job queue (kinds `ask_summary`, `shadow_review`, `shadow_score`) |
| The route-override cache (10/7/26) | `ai_orchestrator._OVERRIDES` (`OVERRIDE_CACHE_SECONDS`, 60) | an override applied or reverted in the console reaches the other workers up to 60 s later — the same window one worker already has | keep, or a generation stamp in `ai_route_overrides` the cache checks |

Not on the list, on purpose: the Places Details cache (`places_details_cache`,
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

1. **Now, still on SQLite**: move the admin ceiling and the AI breakers into
   the database; give the fleet memo a database generation (or build #97);
   elect the supervisor with a lease; make the interactive AI slots a
   database count (a row per call in flight with a lease stamp, so a dead
   worker's slot frees itself) — the one 10/7/26 bound an owner feels when it
   doubles, since two workers would let four owner-facing calls race the
   provider's rate limit. The one-thread AI pools (Ask's summary, the shadow
   review, the learner's scoring) can stay per worker until step 2: doubling
   them costs a little spend, never a wrong answer. Then two gunicorn workers
   in one container behind a flag, measured on `request_rollups` (latency by
   class, saturated minutes) and the write probe's wait (`/health`
   `database.write_ms`). `synchronous=NORMAL` (10/7/26 #92) shortens each
   commit, which is what two workers contend on.
2. **The job queue on SQLite** (the table and its claim), with the review
   fetch's AI work as its first consumer, then the AI pools above (the
   summary, the shadow reviews, the learner's scoring) as kinds on it, and
   the scheduler's sweep lane's jobs as enqueued work rather than a thread.
3. **Postgres** behind `models.get_conn` and a dialect helper, run as a
   shadow (dual-write the ledgers, compare) before the switch; migrations
   replace boot DDL; backups move to the provider's PITR plus a scrubbed
   off-site dump.
4. **Services**: the queue workers and the scheduler as their own Railway
   services once the database is networked; retire the volume.

**When**: before about 10,000 restaurants, or earlier when request latency
p95 by class, saturated minutes or the write probe's wait start to climb on
`/admin` → Engineering. None of these do today.
