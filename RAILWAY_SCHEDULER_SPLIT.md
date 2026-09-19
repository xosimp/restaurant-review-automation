# Splitting the scheduler onto its own Railway service

Audit #17, P0-2. The web service runs `gunicorn --workers 1 --threads 4` and
the background scheduler runs as a **thread inside that same process**, so
three things share one runtime: request handling, every background job, and
SQLite's single writer.

## Why it matters

* A job holding the write lock makes request threads wait out a 30-second
  busy timeout.
* Jobs run sequentially in one loop, so a slow one delays every other.
* Daily jobs are gated on `now.hour`. **A job that overruns an hour means
  later hours are never observed and those jobs are silently skipped for the
  day.** At a thousand restaurants the per-cluster Sonnet pass in
  `run_review_diagnoses` can plausibly do that to the 08:00 review fetch.

## Why this is safe

The single-runner guarantee was never gunicorn's to give. `ops.acquire_
scheduler_lease()` is database-backed and elects exactly one runner across
however many processes exist — a second instance idles and takes over only if
the holder stops heartbeating. That is why running both during the migration
is fine, and why unsetting one variable is the rollback.

## Steps

1. **Deploy the current code.** `worker.py` and the `RUN_SCHEDULER_IN_WEB`
   flag ship inactive: the default is `1`, so the web process keeps running
   the scheduler exactly as it does today. Nothing changes yet.

2. **Create a second Railway service** from the same repo:
   - Start command: `python worker.py`
   - **Attach THE SAME VOLUME** as the web service, mounted at the same path.
     Railway exposes it as `RAILWAY_VOLUME_MOUNT_PATH`, which is what
     `models.DB_PATH` reads. A separate volume would give the worker its own
     empty SQLite file and the two would diverge silently — this is the one
     step that must not be got wrong.
   - Copy every environment variable from the web service.

3. **Watch both run.** The lease means only one is working. `/health` on the
   web service reports `scheduler_heartbeat_age_minutes`; the worker's logs
   show either "lease acquired — this process is now the runner" or
   "standing by".

4. **Set `RUN_SCHEDULER_IN_WEB=0` on the WEB service only.** The web process
   stops starting its scheduler thread and becomes purely a request server.

5. **Rollback, if needed:** remove `RUN_SCHEDULER_IN_WEB` from the web service
   and redeploy. The web process resumes the work and the worker idles.

## After the split

`/health` still returns 200 when the scheduler heartbeat is stale — that is
deliberate and unchanged (the web app being up is a different question from
the worker being up). It now becomes a genuine cross-service signal: a stale
heartbeat means the worker service is down, not that the web service is sick.

## Worker count — leave it at 1

This is NOT the same as the scheduler split, and it is not safe yet.

Three limits live in process memory, so every gunicorn worker keeps its own
copy and N workers multiply each limit by N:

  * `auth_routes._login_attempts` — the login brute-force limiter
  * `client_api._order_send_last` — the supplier-email cooldown, so a double
    tap can email a supplier twice
  * `ai_utils._ai_call_log` — the per-user AI rate limit

Move those into the database first. And there is no load reason to hurry:
Railway's usage page for the Sep 2026 billing period showed ~19 vCPU-minutes
of CPU in total, under a dollar of memory, and a volume of roughly 60-70 MB
that includes 14 days of backups. The scheduler split is the change that
relieves the real contention (the SQLite writer lock); more workers would add
writers, not remove them.
