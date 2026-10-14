"""
gunicorn.conf.py — read by gunicorn from the working directory (its default
config file), so the start command in railway.json / the Procfile is
unchanged.

THE QUEUE WORKER PROCESS (blind re-audit 10/11/26 SCALE-17, SCALE-18,
SCALE-31). Schedule generations, auto-drafts and the nightly learners are
job_queue rows. They used to be consumed by threads inside the one gunicorn
web worker: two 250-person generations pushed a 1 ms request to a 137 ms p95
(one GIL for every page, Studio drag and /health) and left 1.4 GB resident
in the web process for good. gunicorn's master now starts `worker.py
--queue` beside the web worker, in the SAME container (the volume cannot be
shared with another service — docs/ops/RAILWAY_SCHEDULER_SPLIT.md), and
restarts it whenever it exits: it exits on purpose after
QUEUE_WORKER_MAX_JOBS rows or past QUEUE_WORKER_MAX_RSS_MB, so its memory is
returned (max-jobs-per-child). The web process consumes only while no
worker heartbeat is fresh (job_queue.this_process_consumes).

QUEUE_WORKER=0 (or QUEUE_WORKER_PROCESS=0, when a worker.py runs some other
way) starts nothing here: the web process consumes, as before.

The master imports nothing of the app (job_queue reads only the
environment), and the --workers count stays 1 (CLAUDE.md).
"""
import os
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_state = {"proc": None, "stop": False}
# Seconds a SIGTERM'd queue worker is given to hand its rows back.
_DRAIN_SECONDS = 25


def _supervise(log):
    """Run `worker.py --queue` until the master exits: at once again after a
    planned recycle (exit 0), with a growing pause after a crash."""
    pause = 1.0
    while not _state["stop"]:
        started = time.time()
        try:
            proc = subprocess.Popen([sys.executable, os.path.join(_HERE, "worker.py"), "--queue"], cwd=_HERE)
        except Exception as e:
            log.error("queue worker could not start: %s", e)
            time.sleep(30)
            continue
        _state["proc"] = proc
        rc = proc.wait()
        if _state["stop"]:
            return
        if rc == 0:
            pause = 1.0
            log.info("queue worker recycled; starting a fresh one")
            time.sleep(0.5)
            continue
        log.warning("queue worker exited with %s; restarting", rc)
        pause = 1.0 if time.time() - started > 300 else min(60.0, pause * 2)
        time.sleep(pause)


def when_ready(server):
    try:
        sys.path.insert(0, _HERE)
        import job_queue
        wanted = job_queue.worker_process_wanted()
    except Exception as e:
        server.log.error("queue worker not started (job_queue unreadable: %s)", e)
        return
    if not wanted:
        server.log.info("queue worker not started (QUEUE_WORKER=0 or QUEUE_WORKER_PROCESS=0): "
                        "the web process consumes the queue")
        return
    threading.Thread(target=_supervise, args=(server.log,), daemon=True, name="queue-worker-supervisor").start()
    server.log.info("queue worker supervisor started (worker.py --queue)")


def on_exit(server):
    """The deploy's SIGTERM: the queue worker hands its running rows back
    (job_queue.shutdown) before the container goes."""
    _state["stop"] = True
    proc = _state["proc"]
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=_DRAIN_SECONDS)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
