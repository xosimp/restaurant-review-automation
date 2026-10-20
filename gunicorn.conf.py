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

A recycling worker says so on a pipe when it stops claiming
(QUEUE_WORKER_HANDOFF_FD), and its replacement starts then, beside it, while
it finishes its running rows (scale audit 10/19/26 A5-F3). Each worker is
told when this container started (QUEUE_WORKER_BOOT_SINCE), so after a
recycle it does not wait for a web boot that already happened (INF-4).

QUEUE_WORKER=0 (or QUEUE_WORKER_PROCESS=0, when a worker.py runs some other
way) starts nothing here: the web process consumes, as before.

SCHEDULER_PROCESS=1 (off by default — INF-13) also starts `worker.py
--scheduler`: the scheduler loop in its own process, off the web process's
GIL, restarted whenever it exits.

The master imports nothing of the app (job_queue reads only the
environment), and the --workers count stays 1 (CLAUDE.md).
"""
import os
import select
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_state = {"proc": None, "stop": False, "draining": [], "sched": None}
_lock = threading.Lock()
# When this container's master started: before the web worker it forks
# boots. The queue worker waits for a web process booted SINCE THEN (the
# schema is migrated), not since its own start — a recycled worker starts
# long after the web process booted and waited out its 600 s forever, so the
# web process silently took the queue back (scale audit 10/19/26 INF-4).
_MASTER_STARTED = time.time()
# Seconds a SIGTERM'd queue worker is given to hand its rows back.
_DRAIN_SECONDS = 25


def _queue_command():
    return [sys.executable, os.path.join(_HERE, "worker.py"), "--queue"]


def _spawn():
    """Start one queue worker. Returns (proc, read end of its handoff pipe):
    the worker writes "draining" there when it stops claiming for a recycle
    (worker._announce_draining)."""
    r, w = os.pipe()
    env = dict(os.environ, QUEUE_WORKER_BOOT_SINCE=f"{_MASTER_STARTED:.3f}", QUEUE_WORKER_HANDOFF_FD=str(w))
    try:
        proc = subprocess.Popen(_queue_command(), cwd=_HERE, env=env, pass_fds=(w,))
    except Exception:
        os.close(r)
        raise
    finally:
        os.close(w)
    return proc, r


def _await(proc, r):
    """Wait until `proc` exits (its return code) or says it is draining for
    a recycle (None)."""
    readable = True
    try:
        while True:
            if readable:
                ready, _w, _x = select.select([r], [], [], 1.0)
                if ready:
                    data = os.read(r, 64)
                    if data.startswith(b"draining"):
                        return None
                    if not data:          # closed: the worker exited or gave the pipe up
                        readable = False
            else:
                try:
                    return proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
            rc = proc.poll()
            if rc is not None:
                return rc
    finally:
        os.close(r)


def _reap(proc):
    try:
        proc.wait()
    finally:
        with _lock:
            if proc in _state["draining"]:
                _state["draining"].remove(proc)


def _supervise(log):
    """Run `worker.py --queue` until the master exits: at once again after a
    planned recycle, with a growing pause after a crash. A recycling worker
    says so when it stops claiming, and its replacement starts THEN, beside
    it, while it lets its running rows finish (scale audit 10/19/26 A5-F3:
    the queue took no new row until the slowest one ended — up to a model
    wait). claim() counts claimed rows across processes, so two consumers
    never exceed a group's slots."""
    pause = 1.0
    while not _state["stop"]:
        started = time.time()
        try:
            proc, r = _spawn()
        except Exception as e:
            log.error("queue worker could not start: %s", e)
            time.sleep(30)
            continue
        _state["proc"] = proc
        rc = _await(proc, r)
        if _state["stop"]:
            return
        if rc is None:
            with _lock:
                _state["draining"].append(proc)
            threading.Thread(target=_reap, args=(proc,), daemon=True, name="queue-worker-reaper").start()
            log.info("queue worker draining for a recycle; starting its replacement now")
            pause = 1.0
            continue
        if rc == 0:
            pause = 1.0
            log.info("queue worker recycled; starting a fresh one")
            time.sleep(0.5)
            continue
        log.warning("queue worker exited with %s; restarting", rc)
        pause = 1.0 if time.time() - started > 300 else min(60.0, pause * 2)
        time.sleep(pause)


def scheduler_process_wanted() -> bool:
    """SCHEDULER_PROCESS=1 (off by default — scale audit 10/19/26 INF-13):
    the scheduler loop runs as its own process beside the web worker, in
    this container, instead of as a thread sharing the web process's GIL
    with its four request threads. hosted_dashboard then starts no
    scheduler thread (RUN_SCHEDULER_IN_WEB defaults to 0); the lease still
    elects one runner."""
    return (os.getenv("SCHEDULER_PROCESS") or "").strip().lower() in ("1", "true", "yes", "on")


def _supervise_scheduler(log):
    """Run `worker.py --scheduler` until the master exits, restarting it
    with a growing pause when it exits."""
    pause = 1.0
    while not _state["stop"]:
        started = time.time()
        try:
            env = dict(os.environ, QUEUE_WORKER_BOOT_SINCE=f"{_MASTER_STARTED:.3f}")
            proc = subprocess.Popen([sys.executable, os.path.join(_HERE, "worker.py"), "--scheduler"],
                                    cwd=_HERE, env=env)
        except Exception as e:
            log.error("scheduler process could not start: %s", e)
            time.sleep(30)
            continue
        _state["sched"] = proc
        rc = proc.wait()
        if _state["stop"]:
            return
        log.warning("scheduler process exited with %s; restarting", rc)
        pause = 1.0 if time.time() - started > 300 else min(60.0, pause * 2)
        time.sleep(pause)


def when_ready(server):
    # Before the web worker forks, so it inherits it: the platform check
    # warns when the queue worker this master supervises goes silent
    # (ops.queue_problems — scale audit 10/19/26 INF-4).
    if scheduler_process_wanted():
        os.environ["SCHEDULER_PROCESS_SUPERVISED"] = "1"
        threading.Thread(target=_supervise_scheduler, args=(server.log,), daemon=True,
                         name="scheduler-process-supervisor").start()
        server.log.info("scheduler process supervisor started (worker.py --scheduler, SCHEDULER_PROCESS=1)")
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
    os.environ["QUEUE_WORKER_SUPERVISED"] = "1"
    threading.Thread(target=_supervise, args=(server.log,), daemon=True, name="queue-worker-supervisor").start()
    server.log.info("queue worker supervisor started (worker.py --queue)")


def on_exit(server):
    """The deploy's SIGTERM: every queue worker — the current one and any
    still draining for a recycle — hands its running rows back
    (job_queue.shutdown), and a scheduler process releases its lease, before
    the container goes."""
    _state["stop"] = True
    with _lock:
        procs = [p for p in [_state["proc"], _state.get("sched")] + list(_state["draining"])
                 if p is not None and p.poll() is None]
    for proc in procs:
        try:
            proc.terminate()
        except Exception:
            pass
    deadline = time.time() + _DRAIN_SECONDS
    for proc in procs:
        try:
            proc.wait(timeout=max(0.1, deadline - time.time()))
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
