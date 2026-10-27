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

ONE PROCESS, OR THE DATABASE (audit #25 WS10). A turn's events live in
the memory of the process that runs it, and with one gunicorn web worker
(production today) every poll lands there — nothing else is written. With
more than one (pool_sizing.web_workers() > 1, or AI_LANE_SHARED_EVENTS=1)
a poll can land on a worker that never saw the turn — gunicorn's workers
share one socket, so a request cannot be routed to the process that owns
it — so every event is ALSO written to `ai_lane_events` as it is put
(shared_events): the owner still reads its memory; any other worker reads
the rows, by the same login of the same restaurant only. The lane's size
and its per-restaurant share are the container's bounds, split between the
workers (pool_sizing). A turn's events are kept TURN_TTL_SECONDS after it
ends, then dropped (the rows too: each start in shared mode deletes rows
past three times that, at most once a minute).

The SSE stream stays: a client that does not ask to poll (the iOS builds in
the field) is streamed exactly as before, under ASK_MAX_CONCURRENT and the
interactive slots.
"""
import contextvars
import json
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


def shared_events() -> bool:
    """Whether a turn's events are also written to the database, for a poll
    another gunicorn worker serves: AI_LANE_SHARED_EVENTS=1 / 0 decides;
    unset, on exactly when this process is one of several web workers."""
    raw = str(os.getenv("AI_LANE_SHARED_EVENTS", "") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    import pool_sizing
    return pool_sizing.web_workers() > 1


# ── the shared copy of a turn's events (multi-worker only) ──────────────────

_DDL = (
    """CREATE TABLE IF NOT EXISTS ai_lane_events (
        turn_id       TEXT NOT NULL,
        seq           INTEGER NOT NULL,
        restaurant_id INTEGER,
        user_key      TEXT NOT NULL,
        kind          TEXT NOT NULL,
        event_json    TEXT,
        created_at    REAL NOT NULL,
        PRIMARY KEY (turn_id, seq)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ai_lane_events_created ON ai_lane_events(created_at)",
)
# A test points the shared copy at its own database; None is the default one.
_EVENTS_DB = None
_prune_state = {"at": 0.0}
_PRUNE_EVERY_SECONDS = 60


def init_ai_lane(db_path=None):
    """The shared copy's table — at boot (models.init_db), never on a call."""
    import models
    conn = models.get_conn(db_path)
    try:
        for stmt in _DDL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def _user_key(user_id) -> str:
    """A login as the shared copy keys it: an owner's id, or a staff
    member's ("staff", membership id) — the same login reads the same key."""
    return json.dumps(user_id, default=str, separators=(",", ":"))


def _write_row(turn_id, seq, restaurant_id, user_key, kind, event=None):
    import models
    try:
        conn = models.get_conn(_EVENTS_DB)
        try:
            conn.execute("INSERT INTO ai_lane_events (turn_id, seq, restaurant_id, user_key, kind, event_json, "
                         "created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(turn_id, seq) DO NOTHING",
                         (turn_id, int(seq), restaurant_id, user_key, kind,
                          None if event is None else json.dumps(event, default=str), time.time()))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        # The owning process still has the event in memory; only a poll
        # another worker serves misses it. Said, never swallowed.
        log.warning("ai_lane: shared event not written (turn %s, seq %s): %s", turn_id, seq, e)
        try:
            import ops
            ops.capture(e, job="ai_lane_events", context=f"turn_id={turn_id} seq={seq}")
        except Exception as ce:
            log.warning("ai_lane: capture failed too: %s", ce)
        return False


def _drop_rows(turn_id, restaurant_id):
    import models
    try:
        conn = models.get_conn(_EVENTS_DB)
        try:
            conn.execute("DELETE FROM ai_lane_events WHERE turn_id=? AND restaurant_id=?", (turn_id, restaurant_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("ai_lane: shared rows of refused turn %s not dropped: %s", turn_id, e)


def _prune_rows(now=None):
    """Rows of turns long past (three times TURN_TTL_SECONDS), at most once a
    minute — the table holds minutes of turns, never history."""
    import models
    now = now or time.time()
    if now - _prune_state["at"] < _PRUNE_EVERY_SECONDS:
        return
    _prune_state["at"] = now
    try:
        conn = models.get_conn(_EVENTS_DB)
        try:
            conn.execute("DELETE FROM ai_lane_events WHERE created_at < ?", (now - TURN_TTL_SECONDS * 3,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("ai_lane: shared rows not pruned: %s", e)


def _read_rows(turn_id, restaurant_id, user_id, after=0):
    """A turn another process runs, read from its shared rows: (events after
    `after`, next index, done), or None when this login of this restaurant
    started no such turn (or it is long gone)."""
    import models
    try:
        conn = models.get_conn(_EVENTS_DB)
        try:
            rows = conn.execute("SELECT seq, kind, event_json FROM ai_lane_events WHERE turn_id=? "
                                "AND restaurant_id=? AND user_key=? ORDER BY seq",
                                (str(turn_id or ""), restaurant_id, _user_key(user_id))).fetchall()
        finally:
            conn.close()
    except Exception as e:
        log.warning("ai_lane: shared events of turn %s not read: %s", turn_id, e)
        return None
    if not rows:
        return None
    events, done = [], False
    for seq, kind, event_json in rows:
        if kind == "event":
            try:
                events.append(json.loads(event_json))
            except (TypeError, ValueError) as e:
                log.warning("ai_lane: shared event %s/%s unreadable: %s", turn_id, seq, e)
        elif kind == "end":
            done = True
    after = max(0, min(int(after or 0), len(events)))
    return events[after:], len(events), done


class LaneBusy(RuntimeError):
    """The lane's queue is full (or this restaurant already has its share
    waiting): nothing was started."""


class Turn:
    """One turn's events, appended by the lane and read by polls."""

    def __init__(self, restaurant_id, user_id, shared=False):
        self.id = uuid.uuid4().hex
        self.restaurant_id = restaurant_id
        self.user_id = user_id
        self.created = time.time()
        self.ended = None
        self.events = []
        self.done = False
        self.started = False
        # Also written to ai_lane_events as it goes (shared_events: more
        # than one web worker); the memory copy stays the owner's.
        self.shared = bool(shared)
        self._lock = threading.Lock()

    def put(self, event):
        """Append one event; None ends the turn (as the stream's queue)."""
        with self._lock:
            if self.done:
                return
            if event is None:
                self.done, self.ended = True, time.time()
                if self.shared:
                    _write_row(self.id, len(self.events), self.restaurant_id, _user_key(self.user_id), "end")
            else:
                if self.shared:
                    _write_row(self.id, len(self.events), self.restaurant_id, _user_key(self.user_id), "event",
                               event)
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
        self._sweeper = None

    def _ensure_threads(self):
        alive = [t for t in self._threads if t.is_alive()]
        self._threads = alive
        while len(self._threads) < self.workers:
            t = threading.Thread(target=self._loop, daemon=True, name=f"ai-lane-{len(self._threads)}")
            t.start()
            self._threads.append(t)

    def _ensure_sweeper(self):
        """A shared turn's polls may all land on other workers, so the poll
        that answers a too-long wait "busy" (sweep) may never come here: a
        thread does it every second instead — only for shared turns."""
        if self._sweeper is not None and self._sweeper.is_alive():
            return

        def run():
            while True:
                time.sleep(1.0)
                try:
                    self.sweep()
                except Exception as e:
                    log.warning("ai_lane: sweep failed: %s", e)
        self._sweeper = threading.Thread(target=run, daemon=True, name="ai-lane-sweeper")
        self._sweeper.start()

    def submit(self, tenant, turn, fn):
        with self._cv:
            if len(self._pending) >= self.queue_max:
                raise LaneBusy("the lane's queue is full")
            if sum(1 for p in self._pending if p[0] == tenant) >= max(self.per_tenant, 1) * 2:
                raise LaneBusy("this restaurant already has turns waiting")
            self._seq += 1
            self._pending.append([tenant, turn, fn, self._seq, time.time()])
            self._ensure_threads()
            if getattr(turn, "shared", False):
                self._ensure_sweeper()
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
            # AI_LANE_WORKERS and AI_LANE_PER_TENANT are the container's
            # bounds: each gunicorn web worker takes its share (pool_sizing;
            # one worker: all of it). The queue bound is this process's
            # memory: kept.
            import pool_sizing
            _lane = _Lane(pool_sizing.sized("ai_lane", LANE_WORKERS), pool_sizing.per_worker(LANE_PER_TENANT),
                          LANE_QUEUE_MAX)
        return _lane


def start(restaurant_id, user_id, fn):
    """Queue `fn(turn)` on the lane for this restaurant; the Turn its events
    go to. Raises LaneBusy when nothing could be queued. `fn` should already
    carry who asked (ai_utils.attributed) — the lane thread has no request."""
    _prune()
    shared = shared_events()
    turn = Turn(restaurant_id, user_id, shared=shared)
    if shared:
        _prune_rows()
        # The turn exists for a poll on any worker before its first event.
        _write_row(turn.id, -1, restaurant_id, _user_key(user_id), "open")
    with _turns_lock:
        _turns[turn.id] = turn
    try:
        _the_lane().submit(restaurant_id, turn, fn)
    except LaneBusy:
        with _turns_lock:
            _turns.pop(turn.id, None)
        if shared:
            _drop_rows(turn.id, restaurant_id)
        raise
    return turn


def read(turn_id, restaurant_id, user_id, after=0):
    """(events after `after`, next index, done) for a turn this login of
    this restaurant started; None for any other (an unknown id reads the
    same as someone else's)."""
    with _turns_lock:
        turn = _turns.get(str(turn_id or ""))
    if turn is None and shared_events():
        # Another worker runs it (or ran it): its shared rows.
        return _read_rows(turn_id, restaurant_id, user_id, after)
    if turn is None or turn.restaurant_id != restaurant_id or turn.user_id != user_id:
        return None
    if not turn.started and not turn.done and _lane is not None:
        _lane.sweep()
    return turn.read(after)


def stats():
    if _lane is not None:
        return _the_lane().stats()
    import pool_sizing
    return {"workers": pool_sizing.per_worker(LANE_WORKERS), "per_tenant": pool_sizing.per_worker(LANE_PER_TENANT),
            "waiting": 0, "running": 0, "restaurants_running": 0}


def reset_for_tests():
    """Drop every turn (the lane's threads stay; they find nothing to do)."""
    with _turns_lock:
        _turns.clear()
