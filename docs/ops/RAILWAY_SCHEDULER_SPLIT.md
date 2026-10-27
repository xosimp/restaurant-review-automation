# The scheduler and Railway: what can and cannot be split

## Do NOT create a second Railway service for the scheduler

An earlier version of this file said to run `worker.py` as its own Railway
service attached to the same volume. **That is impossible on Railway, and
following it would silently stop every scheduled job.** Railway's volume docs:

> "Each service can only have a single volume" · "Replicas cannot be used with
> volumes" · "we prevent multiple deployments from being active and mounted to
> the same service"

What would actually happen:

1. The worker service would have no volume, so `models.DB_PATH` falls back to
   `./reviews.db` — a new, **empty** database inside the worker's own
   container. (Since 9/29/26 the web process refuses to boot on Railway
   without its volume — `models.require_volume()`, `ALLOW_NO_VOLUME=1`
   overrides — and `worker.py` calls it too; see below.)
2. The scheduler lease lives in that database (`ops.acquire_scheduler_lease`
   → `scheduler_lease` table), so each service would win its own lease and
   believe it was the only runner.
3. Setting `RUN_SCHEDULER_IN_WEB=0` on the web service would then leave the
   worker as the only scheduler — running every job against zero restaurants.
   Review fetches, digests, diagnoses and the nightly backup would stop on the
   real data, with nothing failing loudly.

A separate scheduler service only becomes possible once the database is over
the network (Postgres), not a file on a volume (`docs/plans/POSTGRES_AND_WORKERS_PLAN.md`).

## The deploy sequence

With a volume attached, **Railway runs one deployment at a time**: the old
container is stopped before the new one boots. So:

- a boot that fails (the volume guard, an emptied database, a migration that
  raises, a refused `RESTORE_FROM`), or a `/health` that does not answer 200
  within `healthcheckTimeout` (**120 seconds**, `railway.json`), is
  **downtime until a redeploy or a rollback** — not "the previous container
  keeps serving";
- `/health` answers 200 for a DEGRADED platform on purpose (a stale backup, a
  stale scheduler, low disk), so a deploy that fixes one of those is never
  blocked by it; it answers 500 only when this deployment should not take
  traffic;
- the reference schema `/health` compares against is built at boot, not in
  the first healthcheck, so the check answers inside the window;
- the new process takes the scheduler lease within minutes: the old one
  released it at exit (`ops.shutdown_scheduler`), or its kept lease goes
  stale after 4 minutes (below).

`railway.json` and the `Procfile` carry the same start command (a test pins
them equal); Railway reads `railway.json`.

## The lease: one runner, and how the next one takes over

`ops.acquire_scheduler_lease()` elects exactly one runner across processes,
in the database. Since 9/29/26 (#134):

- **The lease keeper** (`scheduler._LEASE_KEEPER`, started with the scheduler
  thread by `scheduler._run_scheduler_thread`, and by `worker.py`) renews the
  lease every `ops.LEASE_RENEW_SECONDS` (**60 s**) and marks it `kept`.
- **Takeover**: a KEPT lease silent for `ops.LEASE_OWNER_GONE_SECONDS`
  (**240 s**) belongs to a process that is gone, and the next process takes
  it; an unkept lease (an older process, a script) keeps the
  `SCHEDULER_LEASE_STALE_SECONDS` window (**30 minutes**).
- **A wedged runner loses it**: the keeper stops renewing while the loop is
  stuck in a job past twice its bound (`jobs_registry` `max_minutes`), so a
  standby takes over instead of a stuck loop holding the lease forever.
- **Shutdown**: `ops.shutdown_scheduler()` — registered at exit by
  `scheduler.start_scheduler` (`scheduler.register_shutdown`: a
  `threading._register_atexit` hook, so it runs BEFORE Python joins the
  thread pools, plus `atexit` — platform re-audit 10/7/26 #13), and called by
  `worker.py` on SIGTERM — marks the process as exiting BEFORE it releases
  the lease, so the loop, a pulse or the keeper (daemon threads still
  running) cannot take it back.
- A process that newly takes the lease closes the runs its predecessor left
  open (`ops.close_orphaned_runs`) and clears the job it was inside, so the
  Jobs page does not read the new runner as wedged.
- The `intel` lane (the weekly competitor and AI-visibility sweeps, beside the
  loop) renews the lease on its own while it runs, and never stamps the
  loop's heartbeat.

## What the original concern was, and what actually addresses it

Audit #17 (P0-2) listed three problems with the scheduler running as a thread
inside the gunicorn web process:

| Problem | Fixed by a same-container second process? | Real fix |
|---|---|---|
| Scheduler writes hold SQLite's single writer lock; requests wait | **No** — same file either way | Postgres |
| A job overrunning an hour makes later `now.hour ==` jobs skip for the day | **No** — it is the gating logic | **Done** (Sep 2026): catch-up gating in `scheduler.py` — `_due` / `_latest_slot`; since 9/29/26 the weekly Intel sweeps run on a lane beside the loop, and a window that closed unsent is recorded (`missed_windows`) |
| Scheduler shares the web process: a web-worker restart kills a job mid-run; its Python work competes for the GIL | **Yes** | Run `worker.py` as a second process in the same service |

In the web process the scheduler thread is also watched: the
`PlatformSupervisor` thread restarts it with backoff if it dies (never beside
a live one), with the same thread body `start_scheduler` uses — the lease
keeper, then the loop (`scheduler._run_scheduler_thread`, through
`hosted_dashboard._restart_scheduler_loop`) — so a restarted loop keeps
renewing its lease instead of looking abandoned after 4 minutes.

## Option: `SCHEDULER_PROCESS=1` (one variable, the start command unchanged)

Since the scale audit (10/19/26 INF-13) gunicorn's master can run the
scheduler as its own process in the web container, the way it already runs
the queue worker: set **`SCHEDULER_PROCESS=1`** on the service and redeploy.
`gunicorn.conf.py` then supervises `worker.py --scheduler` (restarted with a
growing pause whenever it exits, SIGTERM'd with the queue worker at a
deploy), and `RUN_SCHEDULER_IN_WEB` defaults to 0 so the web process starts
no scheduler thread — the loop, its lanes and pools leave the web process's
GIL and its four request threads. `worker.py --scheduler` never runs
`init_db`: like the queue worker it waits for a web boot since the container
started (the schema is migrated), then runs the lease keeper and the loop.
Off by default; measure first (request p95 during the long jobs — scale
audit A1 §5 #7). **Rollback:** unset `SCHEDULER_PROCESS` and redeploy (an
explicit `RUN_SCHEDULER_IN_WEB` still wins either way; the lease lets only
one loop work).

## Option: a second process in the SAME service

This does work, because both processes run in one container and see the one
volume. The lease still elects a single runner.

Start command (`railway.json` `deploy.startCommand`):

```
sh -c '(while true; do python worker.py; sleep 5; done) & exec gunicorn hosted_dashboard:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --keep-alive 5 --log-level info'
```

plus the access-log arguments today's command carries (`--access-logfile -`
and its `--access-logformat`, copied from `railway.json` — their quotes need
escaping inside `sh -c`), the same command in the `Procfile` (a test pins the
two equal), and the variable `RUN_SCHEDULER_IN_WEB=0` on the service.

- The `while true` loop restarts the worker if it ever exits.
- `exec gunicorn` makes gunicorn the process Railway watches; if it dies the
  container restarts, taking the worker with it.
- **Rollback:** remove `RUN_SCHEDULER_IN_WEB` and restore the old start
  command. Either one alone is also safe — with the variable unset the web
  process runs its own scheduler thread too, and the lease lets only one work.

**`worker.py`'s boot.** It does what the web boot does first — since the
integration wave (9/29/26) worker.py calls both at the top of main(), before
the schema: `logging_setup.configure()` (the JSON log, bound
`process=worker`) and `models.require_volume()` (refuse to run on Railway
without the volume). Then it runs `models.boot_schema()` — the web boot's
whole schema chain, `init_db()`, `ensure_columns()` and the `init_*` steps
(release-safety audit 10/26/26, A05-07) — refuses
unless `scheduler.scheduling_allowed()`, closes orphaned runs, starts the
lease keeper and runs `scheduler_loop()` (restarting the loop, never
exiting, if it raises), and on SIGTERM calls `ops.shutdown_scheduler()`. The web process's
supervisor, boot records and `/health` do not exist in the worker: its
liveness is the scheduler heartbeat and the external dead-man ping.

Honest value: modest. CPU for a whole billing period was ~19 vCPU-minutes, so
GIL contention is not a problem today; the gain is that a job is no longer
killed when gunicorn recycles its worker. Worth doing only alongside the
catch-up gating fix, which addresses the problem that actually loses work.

## The job queue's consumer in the same process (10/10/26)

Since the scheduler audit of 10/10/26 (RM-7, SCALE-1, SCALE-10, SCALE-13,
SCALE-25) schedule generation — owners' presses and the auto-draft — and the
nightly learners' per-restaurant passes are rows in the durable `jobs`
table (`job_queue.py`), not threads. Who runs them:

- **Default (`QUEUE_WORKER` unset or `1`; since the blind re-audit of
  10/11/26, SCALE-17, SCALE-18):** the queue worker process consumes.
  gunicorn's master reads `gunicorn.conf.py` (gunicorn's default config file
  in the working directory — the start command in `railway.json` and the
  `Procfile` is unchanged) and, in `when_ready`, starts `worker.py --queue`
  in the SAME container and restarts it whenever it exits. `worker.py
  --queue` is the consumer alone — no scheduler: it waits for the web
  process's boot heartbeat (the schema is migrated; two processes never run
  `init_db` at once), runs `job_queue.run_worker`, and exits 0 after
  `QUEUE_WORKER_MAX_JOBS` rows (20) or past `QUEUE_WORKER_MAX_RSS_MB` (1500)
  so the supervisor starts a fresh process — a 250-person generation used to
  leave 1.4 GB resident in the web process for good, and two of them pushed
  a 1 ms request to a 137 ms p95 under the one GIL. The generation CPU (the
  repair loop, the solver, the optimizer) and the model waits leave the web
  process and its four request threads; the web process only enqueues. If
  no worker has beaten its heartbeat (`job_workers`) for
  `QUEUE_WORKER_STALE_SECONDS` (90 s), the web process consumes after all —
  a press is never stranded behind a worker that is not running, and a
  local `python3 hosted_dashboard.py` (no gunicorn, no worker) consumes
  in-process. At the master's exit (`on_exit`, a deploy's SIGTERM) the queue
  worker is SIGTERMed and given 25 seconds to hand its rows back.
- **`QUEUE_WORKER=0`:** no queue worker is started; the web process consumes
  its own queue on bounded pools (the rollback: a variable change redeploys
  the service on Railway, and the new master starts no worker).
- **`QUEUE_WORKER_PROCESS=0`:** worker mode, but gunicorn starts nothing —
  for a `worker.py` run some other way (the `sh -c` command above, which
  also runs the scheduler loop; in worker mode plain `worker.py` consumes
  the queue too).
- `JOB_QUEUE=0` is retired (ARCH-10): the queue is the one path — the
  in-process generation pool and its `GenerationSlots`, and the learners
  inline in the scheduler thread, are gone.

Slots: a generation row holds one of the group's CPU slots
(`QUEUE_GENERATION_CONCURRENCY`; by default the container's cores over
`SCHEDULE_CPSAT_WORKERS`, held to its memory at ~1.4 GB a generation —
`job_queue.default_generation_slots`, scale audit 10/19/26 A5-F2/F7; it was
a fixed 2) only while it does code work; while it waits on a model answer it gives the slot
back (`job_queue.model_wait`, entered by `ai_utils.create_with_retry` around
every attempt) and counts only against `QUEUE_GENERATION_MAX_RUNNING`
(default 3 x the slots; under `GATEWAY_ORG_OTPM`, never more streams than
the budget carries). One slot is kept for owners' presses and one for the
auto-draft while drafts wait (`QUEUE_INTERACTIVE_RESERVED`,
`QUEUE_SCHEDULED_RESERVED`) — the drafts' slot only while presses keep two,
or for a draft within an hour of its deadline (scale audit 10/19/26 A5-F4). A
row back from the model waits ('await') ahead
of any new claim.

On a deploy's SIGTERM the process hands its running rows back to the queue
(`job_queue.shutdown`, before `ops.shutdown_scheduler` in `worker.py`, and at
interpreter exit in the web process); the next process resumes each from its
checkpoint — a generation that had saved its draft finishes with it, one
that had not reads back every model answer it already paid for
(`schedule_resume`) — and the boot sweep no longer fails a job the queue
will resume. A process killed without SIGTERM loses only its lease
(`QUEUE_LEASE_SECONDS`, 120 s): the next consumer then claims the row.

Turning the queue worker off: `QUEUE_WORKER=0` and a redeploy — the web
process consumes again; the worker's rows are handed back at its SIGTERM or
after their lease.

A separate worker SERVICE is still impossible on the volume; it becomes
possible after the Postgres cutover (`docs/ops/POSTGRES_CUTOVER.md`), when the
queue, the lease and the heartbeat live in a networked database.

## Worker count — leave it at 1

The two limits this section used to name have moved into the database: the
supplier-email cooldown (`client_api._order_send_last`) is gone — it is
`ops.claim_cooldown` rows now — and the AI rate limiter's window is the
`ai_rate_events` table, with `ai_utils._ai_call_log` kept only as the
fallback when that table cannot be reached. The login brute-force limiter
was already durable (`security.py`'s `login_attempts` table;
`auth_routes._login_attempts` is only its fail-closed fallback).

Process-local state still multiplies with N workers (the full inventory and
the order to move it are in `docs/plans/POSTGRES_AND_WORKERS_PLAN.md`):

  * (moved 10/11/26, blind re-audit ARCH-26) `security._admin_hits` — the
    admin request ceiling (240 requests / 60 writes a minute per session) is
    counted in `admin_request_hits` (a sliding-window counter over minute
    buckets); the dict is only its fallback
  * (moved 10/10/26) `ai_utils._breakers` — every opening, closing and
    reset is the shared `ai_breakers` row now, read back within seconds
  * (moved 10/11/26, ARCH-26) the admin console's fleet memo
    (`admin_ops._fleet_state`) — an admin write moves the shared generation
    (`job_cursors['admin_fleet_gen']`), so every worker's memo is dropped,
    not only the writer's
  * (keyed on the database 10/11/26) the Studio's live check and the
    requirement model cache — on `schedule_engine.shifts_stamp`, so an
    upload or a sync is seen by every worker at once
  * the bounded pools on outbound work (the two admin pools —
    `ops.run_admin_task` and the one admin job pool,
    `admin_routes._submit_admin_job` — the webhook and push delivery pools,
    the owner AI pool and the AI background pools) — each bound would be N
    times its value. `ASK_MAX_CONCURRENT` and `INTERACTIVE_AI_SLOTS` are per
    process by design: each guards that process's own request threads
  * `http_layer`'s in-memory latency window and in-flight gauge — each
    worker reports only its own traffic (the persisted per-minute rollups
    add up correctly)

There is no load reason to hurry: the Sep 2026 billing period showed ~19
vCPU-minutes of CPU, under a dollar of memory, and a volume of roughly
60-70 MB including 14 days of backups.
