"""
ai_lane.py — the interactive AI lane: an owner's multi-call AI turn (an Ask
answer) run OFF the web request threads, its events read back by polling
(scale audit 10/19/26 A4-01, SEC-06).

An Ask answer is a tool loop of 2-6 model calls (~13 s measured). Streamed
over server-sent events it holds one of gunicorn's four request threads for
the whole loop, so Ask was capped at ASK_MAX_CONCURRENT (2) streams and two
interactive slots per process — "busy" from ~500 restaurants, unusable at
1,500. Here the turn runs on a small pool of its own and the client polls
for its events; no request thread waits on the model:

  start(restaurant_id, user_id, fn)   a Turn, or LaneBusy. fn(turn) runs on
                                      the lane and calls turn.put(event) for
                                      each event the stream would have sent
                                      (progress, sentence, answer, error) and
                                      turn.put(None) at the end
  read(turn_id, restaurant_id, user_id, after)
                                      (events after `after`, next, done) —
                                      None for a turn this login did not
                                      start (or one long gone)
  enabled()                           AI_LANE_ENABLED (default on)

FAIR BETWEEN RESTAURANTS. At most AI_LANE_PER_TENANT turns of one
restaurant run at once; a free worker takes the waiting turn whose
restaurant has the fewest running, then the one served longest ago, then
the oldest. AI_LANE_WORKERS bounds the lane (outbound model calls are
bounded further by the gateway's tokens a minute, model_gateway);
AI_LANE_QUEUE_MAX bounds what may wait, and a turn that waited
AI_LANE_QUEUE_WAIT_SECONDS without a worker is answered "busy". Nothing of
the turn changes: the same ask_with_tools, budgets, readiness gate,
validation and ledger; its calls take no interactive slot (they are not on
a request thread — those slots stay as the last-resort guard of the
request threads, ai_utils.FairSlots).

ONE PROCESS. Turns and their events live in this process's memory (a
poll lands on the same process: gunicorn runs --workers 1 — CLAUDE.md).
Like the other pools, the lane multiplies with workers; raising --workers
needs the poll pinned to its process or the events moved to the database
(docs/plans/POSTGRES_AND_WORKERS_PLAN.md). A turn's events are kept
TURN_TTL_SECONDS after it ends, then dropped.

The SSE stream stays: a client that does not ask to poll (the iOS builds in
the field) is streamed exactly as before, under ASK_MAX_CONCURRENT and the
interactive slots.
"""
import contextvars
import logging
import os
import threading
import time
import uuid

log = logging.getLogger("ai_lane")


def _int(name, default, lo=1):
    try:
        return max(lo, int(os.getenv(name, str(default)) or default))
    except ValueError:
        return default


LANE_WORKERS = _int("AI_LANE_WORKERS", 6)
LANE_PER_TENANT = _int("AI_LANE_PER_TENANT", 2)
LANE_QUEUE_MAX = _int("AI_LANE_QUEUE_MAX", 32)
LANE_QUEUE_WAIT_SECONDS = float(os.getenv("AI_LANE_QUEUE_WAIT_SECONDS", "20") or 20)
TURN_TTL_SECONDS = 600
TURNS_MAX = 2000
# How soon a client should poll again (the web client's own timer).
POLL_AFTER_MS = 600

BUSY_MESSAGE = "Cavnar AI is answering a lot of questions right now — try again in a moment."


def enabled() -> bool:
    return str(os.getenv("AI_LANE_ENABLED", "1")).strip().lower() not in ("0", "false", "no", "off")


class LaneBusy(RuntimeError):
    """The lane's queue is full (or this restaurant already has its share
    waiting): nothing was started."""


class Turn:
    """One turn's events, appended by the lane and read by polls."""

    def __init__(self, restaurant_id, user_id):
        self.id = uuid.uuid4().hex
        self.restaurant_id = restaurant_id
        self.user_id = user_id
        self.created = time.time()
        self.ended = None
        self.events = []
        self.done = False
        self.started = False
        self._lock = threading.Lock()

    def put(self, event):
        """Append one event; None ends the turn (as the stream's queue)."""
        with self._lock:
            if self.done:
                return
            if event is None:
                self.done, self.ended = True, time.time()
            else:
                self.events.append(event)

    def read(self, after=0):
        with self._lock:
            after = max(0, min(int(after or 0), len(self.events)))
            return list(self.events[after:]), len(self.events), self.done


class Sink:
    """What a turn's body puts its events into before it knows its Turn —
    the `events` of client_api's Ask body, bound to the Turn when the lane
    runs it (a queue.Queue's put, so the streamed and polled paths share
    one body)."""

    def __init__(self):
        self._turn = None
        self._buf = []

    def bind(self, turn):
        self._turn = turn
        buf, self._buf = self._buf, []
        for e in buf:
            turn.put(e)

    def put(self, event):
        if self._turn is None:
            self._buf.append(event)
        else:
            self._turn.put(event)


_turns = {}
_turns_lock = threading.Lock()


def _prune(now=None):
    now = now or time.time()
    with _turns_lock:
        for tid in [t for t, turn in _turns.items()
                    if (turn.ended and now - turn.ended > TURN_TTL_SECONDS)
                    or now - turn.created > TURN_TTL_SECONDS * 3]:
            _turns.pop(tid, None)
        while len(_turns) > TURNS_MAX:
            _turns.pop(next(iter(_turns)), None)


class _Lane:
    """The pool: workers started on first use, a fair pick of what waits."""

    def __init__(self, workers, per_tenant, queue_max):
        self.workers = workers
        self.per_tenant = per_tenant
        self.queue_max = queue_max
        self._pending = []            # [tenant, turn, fn, seq, queued_at]
        self._running = {}
        self._served = {}
        self._seq = 0
        self._cv = threading.Condition()
        self._threads = []

    def _ensure_threads(self):
        alive = [t for t in self._threads if t.is_alive()]
        self._threads = alive
        while len(self._threads) < self.workers:
            t = threading.Thread(target=self._loop, daemon=True, name=f"ai-lane-{len(self._threads)}")
            t.start()
            self._threads.append(t)

    def submit(self, tenant, turn, fn):
        with self._cv:
            if len(self._pending) >= self.queue_max:
                raise LaneBusy("the lane's queue is full")
            if sum(1 for p in self._pending if p[0] == tenant) >= max(self.per_tenant, 1) * 2:
                raise LaneBusy("this restaurant already has turns waiting")
            self._seq += 1
            self._pending.append([tenant, turn, fn, self._seq, time.time()])
            self._ensure_threads()
            self._cv.notify()

    def _pick(self):
        cands = [p for p in self._pending if self._running.get(p[0], 0) < self.per_tenant]
        if not cands:
            return None
        p = min(cands, key=lambda c: (self._running.get(c[0], 0), self._served.get(c[0], 0.0), c[3]))
        self._pending.remove(p)
        return p

    def _expire(self, now):
        """Turns that waited past LANE_QUEUE_WAIT_SECONDS: answered busy."""
        late = [p for p in self._pending if now - p[4] > LANE_QUEUE_WAIT_SECONDS]
        for p in late:
            self._pending.remove(p)
        return late

    def sweep(self):
        """Answer "busy" to every turn that waited too long — also run from
        a poll, so a lane whose every worker is busy still answers them."""
        with self._cv:
            late = self._expire(time.time())
        for p in late:
            p[1].put({"type": "error", "error": BUSY_MESSAGE})
            p[1].put(None)

    def _loop(self):
        while True:
            with self._cv:
                item = None
                while item is None:
                    for p in self._expire(time.time()):
                        p[1].put({"type": "error", "error": BUSY_MESSAGE})
                        p[1].put(None)
                    item = self._pick()
                    if item is None:
                        self._cv.wait(1.0)
                tenant = item[0]
                self._running[tenant] = self._running.get(tenant, 0) + 1
                self._served[tenant] = time.monotonic()
                if len(self._served) > 5000:
                    self._served.clear()
            turn, fn = item[1], item[2]
            try:
                turn.started = True
                # A fresh context for every turn: nothing one turn set
                # (an ai_context, a held-slot flag) leaks into the next.
                contextvars.Context().run(fn, turn)
            except Exception as e:
                log.warning("ai_lane: turn %s failed: %s", turn.id, e)
                turn.put({"type": "error", "error": "Couldn't get an answer right now — try again."})
            finally:
                turn.put(None)
                with self._cv:
                    n = self._running.get(tenant, 1) - 1
                    if n > 0:
                        self._running[tenant] = n
                    else:
                        self._running.pop(tenant, None)
                    self._cv.notify_all()

    def stats(self):
        with self._cv:
            return {"workers": self.workers, "per_tenant": self.per_tenant, "waiting": len(self._pending),
                    "running": sum(self._running.values()), "restaurants_running": len(self._running)}


_lane = None
_lane_lock = threading.Lock()


def _the_lane():
    global _lane
    with _lane_lock:
        if _lane is None:
            _lane = _Lane(LANE_WORKERS, LANE_PER_TENANT, LANE_QUEUE_MAX)
        return _lane


def start(restaurant_id, user_id, fn):
    """Queue `fn(turn)` on the lane for this restaurant; the Turn its events
    go to. Raises LaneBusy when nothing could be queued. `fn` should already
    carry who asked (ai_utils.attributed) — the lane thread has no request."""
    _prune()
    turn = Turn(restaurant_id, user_id)
    with _turns_lock:
        _turns[turn.id] = turn
    try:
        _the_lane().submit(restaurant_id, turn, fn)
    except LaneBusy:
        with _turns_lock:
            _turns.pop(turn.id, None)
        raise
    return turn


def read(turn_id, restaurant_id, user_id, after=0):
    """(events after `after`, next index, done) for a turn this login of
    this restaurant started; None for any other (an unknown id reads the
    same as someone else's)."""
    with _turns_lock:
        turn = _turns.get(str(turn_id or ""))
    if turn is None or turn.restaurant_id != restaurant_id or turn.user_id != user_id:
        return None
    if not turn.started and not turn.done and _lane is not None:
        _lane.sweep()
    return turn.read(after)


def stats():
    return _the_lane().stats() if _lane is not None else {"workers": LANE_WORKERS, "per_tenant": LANE_PER_TENANT,
                                                          "waiting": 0, "running": 0, "restaurants_running": 0}


def reset_for_tests():
    """Drop every turn (the lane's threads stay; they find nothing to do)."""
    with _turns_lock:
        _turns.clear()
