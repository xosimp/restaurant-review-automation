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
`init_db()`: `logging_setup.configure()` (the JSON log, bound
`process=worker`) and `models.require_volume()` (refuse to run on Railway
without the volume). Then it runs `init_db()` + `ensure_columns()`, refuses
unless `scheduler.scheduling_allowed()`, closes orphaned runs, starts the
lease keeper and runs `scheduler_loop()` (restarting the loop, never
exiting, if it raises), and on SIGTERM calls `ops.shutdown_scheduler()`. The web process's
supervisor, boot records and `/health` do not exist in the worker: its
liveness is the scheduler heartbeat and the external dead-man ping.

Honest value: modest. CPU for a whole billing period was ~19 vCPU-minutes, so
GIL contention is not a problem today; the gain is that a job is no longer
killed when gunicorn recycles its worker. Worth doing only alongside the
catch-up gating fix, which addresses the problem that actually loses work.

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

  * `security._admin_hits` — the admin request ceiling (240 requests / 60
    writes a minute per session) would be N times that
  * `ai_utils._breakers` — each worker trips and resets its own provider
    breakers ("Reset breaker" reaches only the worker that served it)
  * the admin console's fleet memo (`admin_ops._fleet_state`) — a write on one
    worker drops only that worker's memo, so the other serves a read up to
    45 seconds stale
  * the bounded pools (the two admin pools — `ops.run_admin_task` and the
    one admin job pool, `admin_routes._submit_admin_job` — the webhook and
    push delivery pools, `ASK_MAX_CONCURRENT`, `INTERACTIVE_AI_SLOTS`) — each
    bound would be N times its value
  * `http_layer`'s in-memory latency window and in-flight gauge — each
    worker reports only its own traffic (the persisted per-minute rollups
    add up correctly)

There is no load reason to hurry: the Sep 2026 billing period showed ~19
vCPU-minutes of CPU, under a dollar of memory, and a volume of roughly
60-70 MB including 14 days of backups.
