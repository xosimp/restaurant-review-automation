"""
The closing-time crunch, simulated on STAGING (scale audit 10/19/26): every seeded restaurant closes inside
the same window and its night runs - the POS sync, the ticket archive, the report figures, the report itself -
with the workers and bounds production uses. Prints, per stage, wall time, restaurants done, restaurants
left for the next night (hit_bound), and failures; and the job queue's depth while it drains.

Same guard as seed_staging.py: LOADTEST_STAGING=1 and no provider keys. The POS is the simulator; the
report's narrative model call is refused without a key (the report still writes its figures), which
measures everything but model latency - add it from the AI ledger's measured averages.

  LOADTEST_STAGING=1 DATABASE_URL=... python scripts/loadtest/nightly_sim.py --stages pos_sync,pos_archive,dsr
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from scripts.loadtest.seed_staging import _guard  # noqa: E402


def _stage(name, fn):
    t0 = time.time()
    try:
        out = fn()
        err = None
    except Exception as e:  # a stage that raises is a finding, not a crash of the simulation
        out, err = None, repr(e)
    return {"stage": name, "seconds": round(time.time() - t0, 1), "result": out, "error": err}


def main():
    _guard()
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default="pos_sync,pos_archive,loss_sync,dsr_backfill,dsr")
    a = ap.parse_args()
    import scheduler
    import strategy_jobs
    from dsr import pipeline
    stages = {
        "pos_sync": scheduler.run_toast_sync,
        "pos_archive": strategy_jobs.run_pos_archive,
        "loss_sync": strategy_jobs.run_loss_sync,
        "dsr_backfill": strategy_jobs.run_dsr_backfill,
        "dsr": pipeline.run_sweep,
    }
    report = []
    for name in [s.strip() for s in a.stages.split(",") if s.strip()]:
        r = _stage(name, stages[name])
        report.append(r)
        print(json.dumps(r, default=str), flush=True)
    try:
        import job_queue
        print(json.dumps({"queue": job_queue.stats()}, default=str))
    except Exception as e:
        print(json.dumps({"queue_error": repr(e)}))


if __name__ == "__main__":
    main()
