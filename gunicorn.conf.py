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
import signal
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_state = {"proc": None, "stop": False, "draining": [], "sched": None, "drain_started": None, "terminated": set()}
_lock = threading.RLock()   # re-entrant: _begin_drain may run in a signal handler on the main thread
# When this container's master started: before the web worker it forks
# boots. The queue worker waits for a web process booted SINCE THEN (the
# schema is migrated), not since its own start — a recycled worker starts
# long after the web process booted and waited out its 600 s forever, so the
# web process silently took the queue back (scale audit 10/19/26 INF-4).
_MASTER_STARTED = time.time()
# Seconds a SIGTERM'd queue worker is given to hand its rows back.
_DRAIN_SECONDS = 25
# The deploy's shutdown budget (release-safety audit 10/26/26 A05-12; blind
# re-audit 10/27/26 B5 P2 / B8 P3). Railway sends SIGTERM and SIGKILLs
# RAILWAY_DEPLOYMENT_DRAINING_SECONDS later — a service setting whose
# DEFAULT IS 0. The queue worker (and a scheduler process) are told at the
# SIGTERM itself (_begin_drain, installed by when_ready) and hand their rows
# back WHILE the web worker spends graceful_timeout finishing its requests
# and running its atexit (lease release, telemetry flush, boot stop); the web
# worker stops claiming queue rows at the same moment (post_worker_init).
# The two used to run one after the other (20 + 25 = 45 s, so graceful was
# cut from gunicorn's 30 to 20 and killed 30-60 s Ask requests); in parallel
# the budget is the longer of the two plus a margin — SHUTDOWN_SECONDS,
# published to the web worker (CAVNAR_SHUTDOWN_SECONDS), which logs loudly
# at boot and reports on /health when the drain is shorter
# (config.drain_budget_problem). docs/ops/RECOVERY.md "Deploys": set the
# drain to DEPLOY_DRAIN_SECONDS.
DEPLOY_DRAIN_SECONDS = 60
graceful_timeout = 30
_MARGIN_SECONDS = 5
SHUTDOWN_SECONDS = max(graceful_timeout, _DRAIN_SECONDS) + _MARGIN_SECONDS


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


def publish_worker_count(server):
    """The master's real web worker count (the start command's --workers),
    in the environment every web worker inherits: each bounded pool takes
    its share of the container's bound (pool_sizing.web_workers — audit #25
    WS10). One worker, as today: every pool keeps its whole bound."""
    try:
        n = max(1, int(server.cfg.workers))
    except (AttributeError, TypeError, ValueError):
        n = 1
    os.environ["CAVNAR_WEB_WORKERS"] = str(n)
    return n


def _live_children():
    with _lock:
        return [p for p in [_state["proc"], _state.get("sched")] + list(_state["draining"])
                if p is not None and p.poll() is None]


def _begin_drain():
    """The deploy's SIGTERM, in the master, before gunicorn's own handling:
    no supervisor restarts anything from here, and every queue worker (the
    current one and any still draining for a recycle) and a scheduler
    process get their SIGTERM NOW — they hand rows back and release the
    lease in parallel with the web worker's graceful window. Idempotent;
    signal-safe enough (os.kill per child, no waiting)."""
    _state["stop"] = True
    if _state["drain_started"] is None:
        _state["drain_started"] = time.time()
    for proc in _live_children():
        if proc.pid in _state["terminated"]:
            continue
        _state["terminated"].add(proc.pid)
        try:
            proc.terminate()
        except OSError:                  # it exited in between: nothing to tell
            pass


def install_drain_on_sigterm():
    """Chain _begin_drain in front of the master's SIGTERM handler (when_ready
    runs in the master's main thread after gunicorn installed its own)."""
    prev = signal.getsignal(signal.SIGTERM)

    def _on_term(signum, frame):
        _begin_drain()
        if callable(prev):
            prev(signum, frame)
    signal.signal(signal.SIGTERM, _on_term)
    return _on_term


def post_worker_init(worker):
    """In each web worker, after gunicorn installed its signal handlers: at
    the deploy's SIGTERM the worker stops claiming queue rows (job_queue.
    stop_claiming) — the queue worker is handing its rows back at that
    moment, and this process must not pick them up in its graceful window
    only to be killed holding them. Then gunicorn's own handler runs."""
    prev = signal.getsignal(signal.SIGTERM)

    def _on_term(signum, frame):
        jq = sys.modules.get("job_queue")
        if jq is not None and hasattr(jq, "stop_claiming"):
            jq.stop_claiming()           # sets an Event: nothing in it to fail
        if callable(prev):
            prev(signum, frame)
    signal.signal(signal.SIGTERM, _on_term)
    return _on_term


def when_ready(server):
    publish_worker_count(server)
    # What the shutdown needs, for the web worker's boot check and /health
    # (config.drain_budget_problem), and the SIGTERM hand-off above.
    os.environ["CAVNAR_SHUTDOWN_SECONDS"] = str(SHUTDOWN_SECONDS)
    install_drain_on_sigterm()
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
    _begin_drain()                     # a no-op for the children the SIGTERM already told
    procs = _live_children()
    # The children's _DRAIN_SECONDS run from when they were told (the
    # SIGTERM), not from now — they have been handing back all along.
    deadline = (_state["drain_started"] or time.time()) + _DRAIN_SECONDS
    for proc in procs:
        try:
            proc.wait(timeout=max(0.1, deadline - time.time()))
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
