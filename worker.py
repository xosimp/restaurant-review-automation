"""
worker.py — the scheduler, as its own process.

Audit #17 found the background scheduler running as a THREAD inside the single
gunicorn web process (`--workers 1 --threads 4`), which put three problems on
one runtime:

  * A job holding SQLite's single writer lock blocks request threads, which
    then wait out a 30-second busy timeout.
  * Every job runs sequentially in one loop, so a slow one delays the rest.
  * Worse, the daily jobs are gated on `now.hour`. A job that overruns an hour
    means later hours are never observed and those jobs are silently SKIPPED
    for the day — at a thousand restaurants, the per-cluster Sonnet pass in
    run_review_diagnoses can plausibly do that to the 08:00 review fetch.

A separate PROCESS fixes only the kind of problem where the scheduler shares
the web process, so a gunicorn worker recycle kills a job mid-run. It does
not relieve the SQLite writer lock (same file either way). (The hour-skipping
above is gone: the loop's gates catch up — scheduler._due — since audit #17.)
Running more than one process is safe because the single-runner guarantee is
`ops.acquire_scheduler_lease()`, which is database-backed; a lease keeper
renews it every minute, so a standby takes over within minutes of the
holder's process ending (#134).

DEPLOYMENT
----------
Run this as a SECOND PROCESS IN THE SAME Railway service as the web app, never
as a separate service. Railway volumes cannot be shared between services
("Each service can only have a single volume"), so a separate service would
open an empty ./reviews.db of its own, win its own lease in it, and — once the
web process stopped scheduling — leave every real job unrun. See
docs/ops/RAILWAY_SCHEDULER_SPLIT.md for the exact start command and rollback.

THE JOB QUEUE (scheduler audit 10/10/26 RM-7, SCALE-1, SCALE-10, SCALE-25)
--------------------------------------------------------------------------
With QUEUE_WORKER=1 this process is also the job queue's consumer
(job_queue.run_worker): schedule generations — owners' presses and the
auto-draft — and the nightly learners' per-restaurant passes run HERE, off
the web process's four request threads and its GIL; the web process only
enqueues (and consumes after all if this process's heartbeat goes stale, so
a press is never stranded). Without it the web process consumes its own
queue, as one process. On SIGTERM the rows this process is running are handed
back to the queue (job_queue.shutdown) before the lease is released, so the
next process resumes them from their checkpoints at once.

`worker.py --queue` (blind re-audit 10/11/26 SCALE-17, SCALE-18) is the
queue consumer alone — no scheduler — and what gunicorn's master starts
beside the web process by default (gunicorn.conf.py). It waits for the web
process's boot (its heartbeat: the schema is migrated — two processes never
run init_db at once), consumes until it has run QUEUE_WORKER_MAX_JOBS rows
or its resident memory passes QUEUE_WORKER_MAX_RSS_MB, then exits 0 so the
supervisor starts a fresh process (max-jobs-per-child: a 250-person
generation left 1.4 GB resident for good). On SIGTERM it hands its running
rows back first.
"""
import os
import logging
import signal
import sys
import threading
import time

# Configured in main() by logging_setup, the web process's handler: JSON lines
# on Railway (with the job and restaurant on each), text locally (#38).
log = logging.getLogger("worker")

_stopping = False
_STOP = threading.Event()


def _handle_stop(signum, _frame):
    """Railway sends SIGTERM on redeploy. Release the lease, then exit, so
    the next instance picks the work up on its next tick. Exiting alone did
    not do that: a lease only lapses once its heartbeat is stale, so the new
    instance idled behind it. ops.shutdown_scheduler marks this process as
    exiting BEFORE it releases, so the loop, a pulse or the lease keeper —
    daemon threads still running — cannot take the lease back (#161)."""
    global _stopping
    _stopping = True
    log.info("received signal %s — shutting down", signum)
    _STOP.set()
    try:
        # Running queue rows go back first, so the next process resumes them
        # from their checkpoints instead of waiting out their leases.
        import job_queue
        job_queue.shutdown()
    except Exception:
        log.exception("could not hand the running queue jobs back")
    try:
        import ops
        ops.shutdown_scheduler()
    except Exception:
        log.exception("could not release the scheduler lease")
    sys.exit(0)


def main():
    # The web process's logging (JSON on Railway, tracebacks rendered), with
    # every line saying it came from here.
    import logging_setup
    logging_setup.configure()
    logging_setup.bind(process="worker")

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    # On Railway the database must be on the volume (#108), as the web boot
    # requires: a worker with no volume would schedule against an empty
    # database of its own. Raises, so the process refuses to start.
    import models
    models.require_volume()

    # Same boot the web process does: the schema must exist before any job
    # touches it, and this process may well start first.
    from models import init_db, ensure_columns
    init_db()
    ensure_columns()
    log.info("schema ready")

    # The job queue's consumer, when this process is the worker
    # (QUEUE_WORKER=1): generations and learners run here, not in the web
    # process. Started before the scheduler check: an owner's press is
    # consumed wherever this runs (it sends only what the press asked for).
    import job_queue
    consumer = None
    if job_queue.mode() == "worker":
        job_queue.register_shutdown()
        consumer = threading.Thread(target=job_queue.run_worker, kwargs={"stop": _STOP}, daemon=True,
                                    name="queue-worker")
        consumer.start()
        log.info("job queue consumer started (QUEUE_WORKER=1)")

    # Imported after init_db so a job module reading schema at import time
    # sees a migrated database.
    from scheduler import scheduler_loop, scheduling_allowed, _LEASE_KEEPER
    if not scheduling_allowed():
        log.error("not on Railway — refusing to run the scheduler (set ALLOW_LOCAL_SCHEDULER=1 "
                  "to override; jobs send real email and SMS)")
        if consumer is not None:
            _STOP.wait()
        return

    # Runs a previous process left open are closed as interrupted (#150),
    # and the lease is renewed every minute and advertised as kept (#134).
    import ops
    ops.close_orphaned_runs()
    _LEASE_KEEPER.start()
    log.info("starting scheduler loop (lease-elected — a second instance idles)")
    while not _stopping:
        try:
            scheduler_loop()          # only returns if it raises
        except SystemExit:
            raise
        except Exception as e:
            # scheduler_loop has its own per-tick try/except, so reaching here
            # means something broke outside a tick. Restarting the loop is
            # strictly better than exiting into a Railway restart storm.
            log.error("scheduler loop exited unexpectedly: %s", e)
            try:
                import ops
                ops.capture(e, job="worker", context="scheduler_loop exited")
            except Exception:
                pass
            time.sleep(30)


QUEUE_WORKER_MAX_JOBS = int(os.getenv("QUEUE_WORKER_MAX_JOBS", "20") or 0)
QUEUE_WORKER_MAX_RSS_MB = float(os.getenv("QUEUE_WORKER_MAX_RSS_MB", "1500") or 0)
# How long the queue worker waits for the web process's boot.
BOOT_WAIT_SECONDS = 600


def _handle_queue_stop(signum, _frame):
    log.info("queue worker received signal %s — handing its rows back", signum)
    _STOP.set()
    try:
        import job_queue
        job_queue.shutdown()
    except Exception:
        log.exception("could not hand the running queue jobs back")
    sys.exit(0)


def _web_booted(since) -> bool:
    """Whether a web process has booted since `since` (its job_queue
    heartbeat, written after init_db)."""
    try:
        import models
        conn = models.get_conn()
        try:
            row = conn.execute("SELECT MAX(started_at) AS s FROM job_workers WHERE role='web'").fetchone()
        finally:
            conn.close()
        return bool(row and row["s"] and float(row["s"]) >= since - 5)
    except Exception:
        return False


def main_queue():
    """The queue consumer alone (gunicorn.conf.py's child). Returns the exit
    code: 0 after a planned recycle or a stop."""
    import logging_setup
    logging_setup.configure()
    logging_setup.bind(process="queue-worker")
    signal.signal(signal.SIGTERM, _handle_queue_stop)
    signal.signal(signal.SIGINT, _handle_queue_stop)
    import models
    models.require_volume()
    started = time.time()
    while not _web_booted(started):
        if _STOP.is_set():
            return 0
        if time.time() - started > BOOT_WAIT_SECONDS:
            log.error("no web process booted in %ss — the queue worker exits (the supervisor retries)",
                      BOOT_WAIT_SECONDS)
            return 1
        _STOP.wait(2)
    import job_queue
    job_queue.register_shutdown()
    log.info("queue worker consuming (recycles after %s jobs or %s MB)", QUEUE_WORKER_MAX_JOBS,
             QUEUE_WORKER_MAX_RSS_MB)
    out = job_queue.run_worker(stop=_STOP, max_jobs=QUEUE_WORKER_MAX_JOBS or None,
                               max_rss_mb=QUEUE_WORKER_MAX_RSS_MB or None)
    log.info("queue worker done (%s)", out)
    return 0


if __name__ == "__main__":
    # The entry point loads the .env beside it (hosted_dashboard does the
    # same); modules never do it on import.
    try:
        import pathlib as _pl
        from dotenv import load_dotenv as _ld
        _ld(_pl.Path(__file__).parent / ".env")
    except Exception:
        pass
    if "--queue" in sys.argv[1:]:
        sys.exit(main_queue())
    main()
