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
   container.
2. The scheduler lease lives in that database (`ops.acquire_scheduler_lease`
   → `scheduler_lease` table), so each service would win its own lease and
   believe it was the only runner.
3. Setting `RUN_SCHEDULER_IN_WEB=0` on the web service would then leave the
   worker as the only scheduler — running every job against zero restaurants.
   Review fetches, digests, diagnoses and the nightly backup would stop on the
   real data, with nothing failing loudly.

A separate scheduler service only becomes possible once the database is over
the network (Postgres), not a file on a volume.

## What the original concern was, and what actually addresses it

Audit #17 (P0-2) listed three problems with the scheduler running as a thread
inside the gunicorn web process:

| Problem | Fixed by a same-container second process? | Real fix |
|---|---|---|
| Scheduler writes hold SQLite's single writer lock; requests wait | **No** — same file either way | Postgres |
| A job overrunning an hour makes later `now.hour ==` jobs skip for the day | **No** — it is the gating logic | **Done** (Sep 2026): catch-up gating in `scheduler.py` — `_due` / `_latest_slot` |
| Scheduler shares the web process: a web-worker restart kills a job mid-run; its Python work competes for the GIL | **Yes** | Run `worker.py` as a second process in the same service |

## Option: a second process in the SAME service

This does work, because both processes run in one container and see the one
volume. The lease still elects a single runner.

Start command (railway.json `deploy.startCommand`):

```
sh -c '(while true; do python worker.py; sleep 5; done) & exec gunicorn hosted_dashboard:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --keep-alive 5 --log-level info'
```

plus the variable `RUN_SCHEDULER_IN_WEB=0` on the service.

- The `while true` loop restarts the worker if it ever exits.
- `exec gunicorn` makes gunicorn the process Railway watches; if it dies the
  container restarts, taking the worker with it.
- **Rollback:** remove `RUN_SCHEDULER_IN_WEB` and restore the old start
  command. Either one alone is also safe — with the variable unset the web
  process runs its own scheduler thread too, and the lease lets only one work.

Honest value: modest. CPU for a whole billing period was ~19 vCPU-minutes, so
GIL contention is not a problem today; the gain is that a job is no longer
killed when gunicorn recycles its worker. Worth doing only alongside the
catch-up gating fix, which addresses the problem that actually loses work.

## Worker count — leave it at 1

Three limits live in process memory, so every gunicorn worker keeps its own
copy and N workers multiply each limit by N:

  * `auth_routes._login_attempts` — the login brute-force limiter
  * `client_api._order_send_last` — the supplier-email cooldown, so a double
    tap can email a supplier twice
  * `ai_utils._ai_call_log` — the per-user AI rate limit

Move those into the database first. There is no load reason to hurry: the Sep
2026 billing period showed ~19 vCPU-minutes of CPU, under a dollar of memory,
and a volume of roughly 60-70 MB including 14 days of backups.
