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

A separate PROCESS fixes only the third kind of problem — the scheduler sharing
the web process, so a gunicorn worker recycle kills a job mid-run. It does
not relieve the SQLite writer lock (same file either way) or the hour-skipping
(that is the `now.hour ==` gating in scheduler.py). Running more than one
process is safe because the single-runner guarantee is
`ops.acquire_scheduler_lease()`, which is database-backed.

DEPLOYMENT
----------
Run this as a SECOND PROCESS IN THE SAME Railway service as the web app, never
as a separate service. Railway volumes cannot be shared between services
("Each service can only have a single volume"), so a separate service would
open an empty ./reviews.db of its own, win its own lease in it, and — once the
web process stopped scheduling — leave every real job unrun. See
docs/ops/RAILWAY_SCHEDULER_SPLIT.md for the exact start command and rollback.
"""
import logging
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
    """Railway sends SIGTERM on redeploy. Release the lease, then exit, so
    the next instance picks the work up on its next tick. Exiting alone did
    not do that: a lease only lapses once its heartbeat is
    SCHEDULER_LEASE_STALE_SECONDS old, so the new instance idled behind it."""
    global _stopping
    _stopping = True
    log.info("received signal %s — shutting down", signum)
    try:
        import ops
        ops.release_scheduler_lease()
    except Exception:
        log.exception("could not release the scheduler lease")
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
    from scheduler import scheduler_loop, scheduling_allowed
    if not scheduling_allowed():
        log.error("not on Railway — refusing to run the scheduler (set ALLOW_LOCAL_SCHEDULER=1 "
                  "to override; jobs send real email and SMS)")
        return

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
