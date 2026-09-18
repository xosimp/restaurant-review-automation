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

Splitting the scheduler onto its own Railway service fixes all three without
touching a line of job code, because the single-runner guarantee was never
gunicorn's to give: `ops.acquire_scheduler_lease()` is database-backed and
already elects exactly one runner across however many processes exist.

DEPLOYMENT
----------
Create a second Railway service from this same repo with:

    startCommand:  python worker.py
    volume:        THE SAME VOLUME as the web service, mounted at the same
                   path (RAILWAY_VOLUME_MOUNT_PATH). Both processes open the
                   same SQLite file; a second volume would be a second,
                   silently diverging database.
    env:           identical to the web service.

Then set RUN_SCHEDULER_IN_WEB=0 on the WEB service, so it stops starting its
own scheduler thread.

The order is safe either way round: run both for a while and the lease means
only one is actually working — the other idles and takes over if the holder
stops heartbeating. That is also the rollback: unset the variable and the web
process resumes the work.
"""
import logging
import os
import signal
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [worker] %(message)s",
)
log = logging.getLogger(__name__)

_stopping = False


def _handle_stop(signum, _frame):
    """Railway sends SIGTERM on redeploy. Log it and let the process exit so
    the lease lapses promptly and the next deploy picks the work up, rather
    than the new instance waiting out a stale heartbeat."""
    global _stopping
    _stopping = True
    log.info("received signal %s — shutting down", signum)
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    # Same boot the web process does: the schema must exist before any job
    # touches it, and this process may well start first.
    from models import init_db, ensure_columns
    init_db()
    ensure_columns()
    log.info("schema ready")

    # Imported after init_db so a job module reading schema at import time
    # sees a migrated database.
    from scheduler import scheduler_loop

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


if __name__ == "__main__":
    main()
