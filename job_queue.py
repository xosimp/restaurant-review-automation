"""
job_queue.py — the durable job queue (scheduler audit 10/10/26: RM-7,
SCALE-1, SCALE-2, SCALE-10, SCALE-13, SCALE-24, SCALE-25, ARCH-4, ARCH-22).

Every unit of background work that used to be a thread in the web process —
an owner's schedule generation, the weekly auto-draft, the nightly learners'
per-restaurant passes — is a ROW here first, claimed with a lease, run by
whichever consumer has room, checkpointed at its stage boundaries, and
resumed by the next consumer when the process running it dies (a deploy).

THE ROW (`jobs`): kind, group (`grp`: which concurrency pool it draws on),
restaurant_id and its fairness tenant (`tenant_key`: the organization when the
restaurant belongs to one, so a 40-location group shares one turn with a
single restaurant), priority_class (interactive | scheduled | bulk),
not_before, deadline, attempts / max_attempts, lease_owner / lease_until,
idempotency_key (UNIQUE: enqueueing the same work twice returns the first
row), stage_checkpoint (JSON: the last stage reached and what a resume
needs), external_id (the `async_jobs` id the owner polls), the result.

CLAIM (claim): one write transaction (`BEGIN IMMEDIATE` on SQLite; on
Postgres the dialect layer turns it into an advisory transaction lock —
db_dialect), so two consumers can never take one row:
  * a group's global concurrency — a COUNT of its rows running with a live
    lease, in the database, so N processes share one bound (the in-memory
    GenerationSlots multiplied with every process — SCALE-24, ARCH-22).
    The generation group's bound is QUEUE_GENERATION_CONCURRENCY, capped by
    what the model gateway's token budget can carry (model_gateway);
  * at most one running job per restaurant in a group (the per-tenant cap);
  * class order: interactive (an owner waiting, p95 < 2 min to start), then
    scheduled (earliest deadline first: the auto-draft's "drafted by" time),
    then bulk (learners);
  * within a class, weighted fair queueing across tenants (`job_tenants`
    virtual time — a tenant's weight grows with its locations, slowly, so a
    group is served more than a single but never starves one) gated by a
    token bucket per tenant per class: a tenant that spent its burst waits
    behind tenants that have not, unless nobody else is waiting (the queue
    is work-conserving).
A running row whose lease ran out (its process died) is claimable again —
`attempts` counts tries, and a job past `max_attempts` fails.

RUN (run_one): the handler for the row's kind is reached BY NAME (HANDLERS —
"module:function", resolved lazily; registry-driven dispatch, CLAUDE.md),
under the attribution the job was enqueued with (ai_utils.ai_context — the
owner who pressed stays the actor on every model call, #148), with
`current()` naming the job for the code underneath (checkpoint(), the
generation's slot skip, the model gateway's class). A lease keeper renews
the lease while it runs.

CONSUMERS. mode():
  "inprocess" (the default) — the web process consumes its own queue on
    bounded pools (kick(); one per group) — one process, as production has
    always run, but every unit of work is a durable row;
  "worker" (QUEUE_WORKER=1) — worker.py consumes, as a second process in the
    SAME Railway service (docs/ops/RAILWAY_SCHEDULER_SPLIT.md — never a
    separate service: the volume cannot be shared). The web process only
    enqueues, and consumes after all if no worker has beaten its heartbeat
    for WORKER_STALE_SECONDS (a worker that never started never strands an
    owner's press);
  "off" (JOB_QUEUE=0) — the old in-process paths, as before this module.

DRAIN (shutdown): on SIGTERM / exit a consumer stops claiming and hands its
running rows back (state queued, lease cleared) so the next process resumes
them at once from their checkpoints, instead of waiting out the lease.
"""
import concurrent.futures
import contextlib
import contextvars
import json
import logging
import math
import os
import socket
import threading
import time
import uuid

log = logging.getLogger("job_queue")


def get_conn(db_path=None):
    """Resolved through models at call time, so a test's patched database is
    the one used (CLAUDE.md: never a bound copy of get_conn)."""
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


# ── configuration ───────────────────────────────────────────────────────────

CLASSES = ("interactive", "scheduled", "bulk")
_CLASS_RANK = {c: i for i, c in enumerate(CLASSES)}

# kind -> (group, default class, max attempts). A kind not listed runs in the
# bulk group at the bulk class.
KINDS = {
    "schedule_generate": ("generation", "interactive", 3),
    "auto_draft": ("generation", "scheduled", 2),
    "learner": ("bulk", "bulk", 2),
    # The owner's notes and staff notes read into typed rules when they are
    # SAVED, one batched call (owner_rules.queue_read — blind re-audit
    # 10/11/26 AI-22): never in a generation's critical path.
    "owner_words_read": ("bulk", "scheduled", 2),
    # The learning ablation, off the owner's wait (blind re-audit 10/11/26
    # ARCH-14): research data, one try.
    "schedule_ablation": ("bulk", "bulk", 1),
}

# kind -> "module:function": what runs a claimed row (reached by name).
HANDLERS = {
    "schedule_generate": "schedule_engine:run_queued_generation",
    "auto_draft": "strategy_jobs:run_queued_auto_draft",
    "learner": "strategy_jobs:run_queued_learner",
    "owner_words_read": "owner_rules:run_queued_read",
    "schedule_ablation": "schedule_scorecard:run_queued_ablation",
}
# kind -> "module:function" told when a queued row expires unstarted (its
# deadline passed): what it held is handed back.
EXPIRE_HOOKS = {
    "auto_draft": "strategy_jobs:on_auto_draft_expired",
    "schedule_generate": "schedule_engine:on_queued_generation_expired",
}

LEASE_SECONDS = int(os.getenv("QUEUE_LEASE_SECONDS", "120"))
RENEW_SECONDS = max(5, LEASE_SECONDS // 4)
WORKER_STALE_SECONDS = int(os.getenv("QUEUE_WORKER_STALE_SECONDS", "90"))
POLL_SECONDS = float(os.getenv("QUEUE_POLL_SECONDS", "2"))
CANDIDATES = 500
# Seconds before a failed try is tried again: try n waits RETRY_BACKOFF[n-1].
RETRY_BACKOFF = (30, 120, 600)
# Per-tenant token buckets: (burst, refill per hour) by class.
BUCKETS = {
    "interactive": (float(os.getenv("QUEUE_INTERACTIVE_BURST", "6")),
                    float(os.getenv("QUEUE_INTERACTIVE_PER_HOUR", "6"))),
    "scheduled": (float(os.getenv("QUEUE_SCHEDULED_BURST", "2")),
                  float(os.getenv("QUEUE_SCHEDULED_PER_HOUR", "2"))),
    "bulk": (float(os.getenv("QUEUE_BULK_BURST", "30")), float(os.getenv("QUEUE_BULK_PER_HOUR", "120"))),
}
# Finished rows are kept this long (ops._RETENTION_DAYS "jobs").
RETAIN_DAYS = 14


def _int_env(name, default):
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def group_concurrency(grp) -> int:
    """How many rows of `grp` may run at once across EVERY process (a count
    of claimed rows, never a per-process pool). The generation group is also
    held to what the organisation's token budget can carry (model_gateway.
    max_concurrent_generations — no cap when the budget is not configured)."""
    if grp == "generation":
        import schedule_engine
        n = _int_env("QUEUE_GENERATION_CONCURRENCY", schedule_engine.SCHEDULE_GEN_WORKERS)
        try:
            import model_gateway
            cap = model_gateway.max_concurrent_generations()
            if cap:
                n = max(1, min(n, int(cap)))
        except Exception:
            pass
        return n
    return _int_env("QUEUE_BULK_CONCURRENCY", 2)


def mode() -> str:
    """"off" (JOB_QUEUE=0: the old direct paths), "worker" (QUEUE_WORKER=1:
    worker.py consumes) or "inprocess" (the default: this process consumes)."""
    if os.getenv("JOB_QUEUE", "1").strip().lower() in ("0", "false", "off", "no"):
        return "off"
    if os.getenv("QUEUE_WORKER", "").strip().lower() in ("1", "true", "on", "yes"):
        return "worker"
    return "inprocess"


def enabled() -> bool:
    return mode() != "off"


# ── schema (boot only) ──────────────────────────────────────────────────────

_DDL = (
    """CREATE TABLE IF NOT EXISTS jobs (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        kind             TEXT NOT NULL,
        grp              TEXT NOT NULL,
        restaurant_id    INTEGER,
        tenant_key       TEXT,
        priority_class   TEXT NOT NULL,
        state            TEXT NOT NULL DEFAULT 'queued',
        payload_json     TEXT,
        idempotency_key  TEXT UNIQUE,
        external_id      TEXT,
        not_before       REAL NOT NULL DEFAULT 0,
        deadline         REAL,
        attempts         INTEGER NOT NULL DEFAULT 0,
        max_attempts     INTEGER NOT NULL DEFAULT 3,
        lease_owner      TEXT,
        lease_until      REAL,
        stage_checkpoint TEXT,
        result_json      TEXT,
        error            TEXT,
        enqueued_at      REAL NOT NULL,
        started_at       REAL,
        finished_at      REAL,
        created_at       TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(grp, state, not_before)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_rid ON jobs(restaurant_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_external ON jobs(external_id)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at)",
    # The fairness state per tenant and class: a token bucket and the
    # weighted-fair-queueing virtual time.
    """CREATE TABLE IF NOT EXISTS job_tenants (
        tenant_key   TEXT NOT NULL,
        klass        TEXT NOT NULL,
        tokens       REAL NOT NULL,
        refilled_at  REAL NOT NULL,
        vtime        REAL NOT NULL DEFAULT 0,
        served_at    REAL,
        PRIMARY KEY (tenant_key, klass)
    )""",
    # Who consumes: one row per consumer process, its heartbeat.
    """CREATE TABLE IF NOT EXISTS job_workers (
        worker_id    TEXT PRIMARY KEY,
        role         TEXT NOT NULL,
        pid          INTEGER,
        host         TEXT,
        started_at   REAL NOT NULL,
        heartbeat_at REAL NOT NULL,
        draining     INTEGER NOT NULL DEFAULT 0
    )""",
    # Who pressed Generate for a running generation (iOS parity #16): pushed
    # when the draft lands, whichever process finishes it (ARCH-22).
    """CREATE TABLE IF NOT EXISTS generation_watchers (
        job_id       TEXT NOT NULL,
        user_id      INTEGER NOT NULL,
        may_publish  INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (job_id, user_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_generation_watchers_created ON generation_watchers(created_at)",
)


def init_job_queue(db_path=None):
    """The queue's tables — at boot (models.init_db), never on a call."""
    conn = get_conn(db_path)
    try:
        for stmt in _DDL:
            conn.execute(stmt)
        # A running row waiting on the model gateway's next minute (re-audit
        # 10/11/26 SCALE-25): until this time it does not count against its
        # group's concurrency (parked).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "parked_until" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN parked_until REAL")
        conn.commit()
    finally:
        conn.close()


@contextlib.contextmanager
def parked(seconds, db_path=None):
    """`with parked(30): sleep(30)` — the running queue job of this thread
    gives up its group slot while it waits (the model gateway's wait for the
    next minute, SCALE-25): claim() does not count it toward the group's
    concurrency until `seconds` from now, so another generation can do its
    code work instead of the slot sitting idle. Its lease, its tenant's
    one-at-a-time rule and everything else are unchanged; the slot is taken
    back when the wait ends (a claim made meanwhile may run one over the
    bound for that wait — the bound is the model's tokens, which the waiting
    call is not spending). Outside a queue job it does nothing."""
    job = current()
    if job is None or job.get("id") is None:
        yield
        return
    def _set(value):
        try:
            conn = get_conn(db_path)
            try:
                conn.execute("UPDATE jobs SET parked_until=? WHERE id=? AND state='running'", (value, int(job["id"])))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            log.warning("job %s not parked: %s", job.get("id"), e)
    grp = job.get("grp")
    _set(time.time() + float(seconds or 0))
    with _pools_lock:
        _parked_n[grp] = _parked_n.get(grp, 0) + 1
    try:
        try:
            kick(grp, db_path=db_path)          # let a waiting row use the slot
        except Exception as e:
            log.warning("no drain started beside parked job %s: %s", job.get("id"), e)
        yield
    finally:
        with _pools_lock:
            _parked_n[grp] = max(0, _parked_n.get(grp, 0) - 1)
        _set(None)


# ── the job a thread is running ─────────────────────────────────────────────

class Job(dict):
    """A claimed row: a dict of its columns with the payload and checkpoint
    decoded (`payload`, `checkpoint`), plus `worker_id`."""

    @property
    def id(self):
        return self["id"]

    @property
    def payload(self):
        return self.get("payload") or {}

    @property
    def checkpoint(self):
        return self.get("checkpoint") or {}

    @property
    def resumed(self) -> bool:
        """Whether a previous try of this row got somewhere (a deploy killed
        it, or it was handed back at shutdown): its checkpoint names a stage."""
        return bool((self.get("checkpoint") or {}).get("stage")) or int(self.get("attempts") or 0) > 1


_CURRENT = contextvars.ContextVar("job_queue_current", default=None)


def current():
    """The Job this thread is running for the queue, or None."""
    return _CURRENT.get()


def in_group(grp) -> bool:
    j = _CURRENT.get()
    return j is not None and j.get("grp") == grp


# ── enqueue ─────────────────────────────────────────────────────────────────

def _tenant_key(restaurant_id, conn):
    if restaurant_id is None:
        return "platform"
    try:
        row = conn.execute("SELECT organization_id FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        if row and row["organization_id"]:
            return f"org:{int(row['organization_id'])}"
    except Exception:
        pass
    return f"r:{int(restaurant_id)}"


def enqueue(kind, payload=None, restaurant_id=None, priority_class=None, idempotency_key=None, external_id=None,
            not_before=None, deadline=None, max_attempts=None, wake=True, db_path=None):
    """(row id, created): one unit of work as a row. With `idempotency_key`
    a second enqueue of the same work returns the first row (created False)
    whatever its state. `not_before` / `deadline` are time.time() values; a
    row still queued past its deadline expires (EXPIRE_HOOKS) instead of
    running late. Wakes this process's consumers (kick) unless `wake` is
    False."""
    grp, klass, attempts = KINDS.get(kind, ("bulk", "bulk", 2))
    klass = priority_class if priority_class in CLASSES else klass
    now = time.time()
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            row = conn.execute("SELECT id FROM jobs WHERE idempotency_key=?", (str(idempotency_key),)).fetchone()
            if row:
                conn.rollback()
                return int(row["id"]), False
        cur = conn.execute(
            "INSERT INTO jobs (kind, grp, restaurant_id, tenant_key, priority_class, state, payload_json, "
            "idempotency_key, external_id, not_before, deadline, attempts, max_attempts, enqueued_at) "
            "VALUES (?,?,?,?,?, 'queued', ?,?,?,?,?, 0, ?, ?)",
            (kind, grp, restaurant_id, _tenant_key(restaurant_id, conn), klass,
             json.dumps(payload or {}, default=str), idempotency_key, external_id,
             float(not_before or 0), (float(deadline) if deadline else None),
             int(max_attempts or attempts), now))
        conn.commit()
        qid = int(cur.lastrowid)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    if wake:
        kick(grp, db_path=db_path)
    return qid, True


def enqueue_many(kind, items, priority_class=None, wake=True, db_path=None) -> int:
    """Many rows of one kind in one write transaction — a nightly fan-out of
    thousands of restaurants is one commit, not thousands. Each item is
    {payload, restaurant_id, idempotency_key?, not_before?, deadline?}; an
    item whose key is already queued (or ran) is skipped. Returns how many
    rows were created."""
    grp, klass, attempts = KINDS.get(kind, ("bulk", "bulk", 2))
    klass = priority_class if priority_class in CLASSES else klass
    now = time.time()
    created = 0
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        tenants = {}
        for it in items:
            rid = it.get("restaurant_id")
            if rid not in tenants:
                tenants[rid] = _tenant_key(rid, conn)
            cur = conn.execute(
                "INSERT INTO jobs (kind, grp, restaurant_id, tenant_key, priority_class, state, payload_json, "
                "idempotency_key, external_id, not_before, deadline, attempts, max_attempts, enqueued_at) "
                "VALUES (?,?,?,?,?, 'queued', ?,?,?,?,?, 0, ?, ?) ON CONFLICT(idempotency_key) DO NOTHING",
                (kind, grp, rid, tenants[rid], klass, json.dumps(it.get("payload") or {}, default=str),
                 it.get("idempotency_key"), it.get("external_id"), float(it.get("not_before") or 0),
                 (float(it["deadline"]) if it.get("deadline") else None), int(attempts), now))
            created += cur.rowcount or 0
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    if wake and created:
        kick(grp, db_path=db_path)
    return created


# ── claim ───────────────────────────────────────────────────────────────────

def _bucket(conn, tenant, klass, now):
    burst, per_hour = BUCKETS.get(klass, BUCKETS["bulk"])
    row = conn.execute("SELECT tokens, refilled_at, vtime FROM job_tenants WHERE tenant_key=? AND klass=?",
                       (tenant, klass)).fetchone()
    if row is None:
        return burst, 0.0
    tokens = min(burst, float(row["tokens"]) + (now - float(row["refilled_at"])) * per_hour / 3600.0)
    return tokens, float(row["vtime"] or 0)


def _tenant_weight(conn, tenant) -> float:
    """A tenant's share: an organization with n locations weighs
    1 + log2(n), at most 4 — served more than a single restaurant, never so
    much that singles starve behind it."""
    if not str(tenant).startswith("org:"):
        return 1.0
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM restaurants WHERE organization_id=?",
                         (int(str(tenant)[4:]),)).fetchone()["n"]
    except Exception:
        n = 1
    return min(4.0, 1.0 + math.log2(max(1, int(n or 1))))


def _choose(conn, cands, now):
    """The row to run among claimable `cands` (all one group): the best
    class present, then — interactive and bulk — the tenant with tokens and
    the lowest virtual time, its oldest row; scheduled — earliest deadline
    first, ties by tenant fairness."""
    best_rank = min(_CLASS_RANK.get(c["priority_class"], 2) for c in cands)
    klass = CLASSES[best_rank]
    pool = [c for c in cands if _CLASS_RANK.get(c["priority_class"], 2) == best_rank]
    tenants = {}
    for c in pool:
        t = c["tenant_key"] or "platform"
        if t not in tenants:
            tokens, vt = _bucket(conn, t, klass, now)
            tenants[t] = (tokens >= 1.0, vt)
    if klass == "scheduled":
        def key(c):
            ok, vt = tenants[c["tenant_key"] or "platform"]
            return (0 if ok else 1, c["deadline"] if c["deadline"] is not None else float("inf"), vt, c["id"])
    else:
        def key(c):
            ok, vt = tenants[c["tenant_key"] or "platform"]
            return (0 if ok else 1, vt, c["id"])
    return min(pool, key=key), klass


def _serve(conn, tenant, klass, now):
    """Spend a token and advance the tenant's virtual time by 1/weight."""
    burst, per_hour = BUCKETS.get(klass, BUCKETS["bulk"])
    tokens, vt = _bucket(conn, tenant, klass, now)
    floor = conn.execute("SELECT MIN(vtime) AS v FROM job_tenants WHERE klass=?", (klass,)).fetchone()
    base = max(vt, float((floor["v"] if floor else 0) or 0))
    vt2 = base + 1.0 / _tenant_weight(conn, tenant)
    conn.execute(
        "INSERT INTO job_tenants (tenant_key, klass, tokens, refilled_at, vtime, served_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(tenant_key, klass) DO UPDATE SET tokens=excluded.tokens, refilled_at=excluded.refilled_at, "
        "vtime=excluded.vtime, served_at=excluded.served_at",
        (tenant, klass, max(0.0, tokens - 1.0), now, vt2, now))


def claim(worker_id, groups=("generation", "bulk"), db_path=None):
    """The next row this consumer may run, claimed (state running, a lease,
    attempts + 1) — or None when nothing is claimable within the bounds
    above. Rows expired unstarted on the way are handed to their kind's
    EXPIRE_HOOKS after the commit."""
    if _draining.is_set() or (os.getenv("MAINTENANCE_READ_ONLY") or "").strip().lower() in ("1", "true", "yes", "on"):
        return None                       # draining, or the cutover's write freeze
    now = time.time()
    expired, picked = [], None
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for grp in groups:
            live = conn.execute("SELECT restaurant_id, parked_until FROM jobs WHERE grp=? AND state='running' "
                                "AND lease_until>?", (grp, now)).fetchall()
            # A row parked on the model gateway holds no slot (SCALE-25); it
            # still holds its restaurant's one-at-a-time place below.
            if sum(1 for r in live if not (r["parked_until"] and r["parked_until"] > now)) >= group_concurrency(grp):
                continue
            busy = {r["restaurant_id"] for r in live if r["restaurant_id"] is not None}
            rows = conn.execute(
                "SELECT * FROM jobs WHERE grp=? AND ((state='queued' AND not_before<=?) "
                "OR (state='running' AND lease_until<=?)) ORDER BY id LIMIT ?",
                (grp, now, now, CANDIDATES)).fetchall()
            cands = []
            for r in rows:
                r = dict(r)
                if r["state"] == "queued" and r["deadline"] is not None and r["deadline"] < now and not r["attempts"]:
                    conn.execute("UPDATE jobs SET state='expired', finished_at=?, error=? WHERE id=? "
                                 "AND state='queued'", (now, "not started before its deadline", r["id"]))
                    expired.append(r)
                    continue
                if r["state"] == "running" and int(r["attempts"] or 0) >= int(r["max_attempts"] or 1):
                    # Its process died on its last try: failed, said.
                    conn.execute("UPDATE jobs SET state='failed', finished_at=?, error=COALESCE(error, ?), "
                                 "lease_owner=NULL WHERE id=?",
                                 (now, "its process stopped on its last try", r["id"]))
                    expired.append(dict(r, state="failed"))
                    continue
                if r["restaurant_id"] is not None and r["restaurant_id"] in busy:
                    continue
                cands.append(r)
            if not cands:
                continue
            chosen, klass = _choose(conn, cands, now)
            n = conn.execute(
                "UPDATE jobs SET state='running', attempts=attempts+1, lease_owner=?, lease_until=?, "
                "started_at=COALESCE(started_at, ?) WHERE id=? AND (state='queued' OR "
                "(state='running' AND lease_until<=?))",
                (worker_id, now + LEASE_SECONDS, now, chosen["id"], now)).rowcount
            if n == 1:
                _serve(conn, chosen["tenant_key"] or "platform", klass, now)
                picked = conn.execute("SELECT * FROM jobs WHERE id=?", (chosen["id"],)).fetchone()
                break
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    for r in expired:
        _expired(r)
    if picked is None:
        return None
    job = Job(dict(picked))
    job["payload"] = _loads(job.get("payload_json"))
    job["checkpoint"] = _loads(job.get("stage_checkpoint"))
    job["worker_id"] = worker_id
    return job


def _loads(text):
    try:
        return json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}


def _resolve(target):
    import importlib
    mod, _sep, fn = str(target or "").partition(":")
    return getattr(importlib.import_module(mod), fn)


def _expired(row):
    hook = EXPIRE_HOOKS.get(row.get("kind"))
    if not hook:
        return
    try:
        _resolve(hook)(Job(dict(row, payload=_loads(row.get("payload_json")),
                                checkpoint=_loads(row.get("stage_checkpoint")))))
    except Exception as e:
        import ops
        ops.capture(e, job="job_queue", context=f"expire hook {row.get('kind')} #{row.get('id')}")


# ── the running row ─────────────────────────────────────────────────────────

def renew(job_id, worker_id, db_path=None) -> bool:
    """Push the lease out; False when this consumer no longer holds it."""
    conn = get_conn(db_path)
    try:
        n = conn.execute("UPDATE jobs SET lease_until=? WHERE id=? AND lease_owner=? AND state='running'",
                         (time.time() + LEASE_SECONDS, int(job_id), worker_id)).rowcount
        conn.commit()
        return n == 1
    finally:
        conn.close()


def checkpoint(stage, db_path=None, **data) -> bool:
    """Record that the job this thread runs reached `stage` (with what a
    resume needs, JSON-able): merged into its stage_checkpoint, the stage
    appended to its `stages`. A no-op outside a queue job. Never raises."""
    job = _CURRENT.get()
    if job is None:
        return False
    try:
        cp = dict(job.get("checkpoint") or {})
        stages = list(cp.get("stages") or [])
        if not stages or stages[-1] != stage:
            stages.append(stage)
        cp.update(data)
        cp["stage"], cp["stages"], cp["at"] = stage, stages[-40:], time.time()
        job["checkpoint"] = cp
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE jobs SET stage_checkpoint=? WHERE id=? AND state='running'",
                         (json.dumps(cp, default=str), int(job["id"])))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        log.warning("checkpoint %s not kept: %s", stage, e)
        return False


def complete(job_id, worker_id, result=None, db_path=None) -> bool:
    conn = get_conn(db_path)
    try:
        n = conn.execute("UPDATE jobs SET state='done', result_json=?, finished_at=?, lease_owner=NULL, "
                         "lease_until=NULL WHERE id=? AND lease_owner=? AND state='running'",
                         (json.dumps(result, default=str) if result is not None else None, time.time(),
                          int(job_id), worker_id)).rowcount
        conn.commit()
        return n == 1
    finally:
        conn.close()


def fail(job_id, worker_id, error, retry=True, db_path=None) -> str:
    """A try that raised: queued again after RETRY_BACKOFF while tries are
    left (and `retry`), else failed. Returns the new state."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT attempts, max_attempts FROM jobs WHERE id=?", (int(job_id),)).fetchone()
        if row is None:
            return "gone"
        again = retry and int(row["attempts"] or 0) < int(row["max_attempts"] or 1)
        if again:
            wait = RETRY_BACKOFF[min(int(row["attempts"] or 1), len(RETRY_BACKOFF)) - 1]
            conn.execute("UPDATE jobs SET state='queued', not_before=?, error=?, lease_owner=NULL, lease_until=NULL "
                         "WHERE id=? AND lease_owner=?",
                         (time.time() + wait, str(error)[:500], int(job_id), worker_id))
        else:
            conn.execute("UPDATE jobs SET state='failed', error=?, finished_at=?, lease_owner=NULL, lease_until=NULL "
                         "WHERE id=? AND lease_owner=?", (str(error)[:500], time.time(), int(job_id), worker_id))
        conn.commit()
        return "queued" if again else "failed"
    finally:
        conn.close()


def release(job_id, worker_id, db_path=None) -> bool:
    """Hand a running row back (a draining consumer): queued now, its lease
    cleared, this try not counted against its attempts — the next consumer
    resumes it from its checkpoint at once."""
    conn = get_conn(db_path)
    try:
        n = conn.execute("UPDATE jobs SET state='queued', lease_owner=NULL, lease_until=NULL, not_before=0, "
                         "attempts=MAX(0, attempts-1) WHERE id=? AND lease_owner=? AND state='running'",
                         (int(job_id), worker_id)).rowcount
        conn.commit()
        return n == 1
    finally:
        conn.close()


def cancel(external_id=None, job_id=None, reason="cancelled", db_path=None) -> int:
    """Cancel a row still queued (by its async job id, or its own id). A
    running one is stopped by its own checks (the async job row), never
    here."""
    conn = get_conn(db_path)
    try:
        if job_id is not None:
            n = conn.execute("UPDATE jobs SET state='cancelled', error=?, finished_at=? WHERE id=? AND state='queued'",
                             (reason, time.time(), int(job_id))).rowcount
        else:
            n = conn.execute("UPDATE jobs SET state='cancelled', error=?, finished_at=? WHERE external_id=? "
                             "AND state='queued'", (reason, time.time(), str(external_id))).rowcount
        conn.commit()
        return n or 0
    finally:
        conn.close()


def promote(external_id, db_path=None) -> str:
    """An owner's press joined the generation `external_id` (re-audit
    10/7/26 #1): a queued scheduled row goes on as interactive — ahead of
    the other drafts, its deadline (the auto-draft's give-up) lifted.
    "waiting" (queued, promoted), "running" (already running — nothing to
    do) or "pending" (no row: not queued here)."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT id, state FROM jobs WHERE external_id=? AND state IN ('queued','running') "
                           "ORDER BY id DESC LIMIT 1", (str(external_id),)).fetchone()
        if row is None:
            return "pending"
        conn.execute("UPDATE jobs SET priority_class='interactive', deadline=NULL WHERE id=?", (row["id"],))
        conn.commit()
        return "running" if row["state"] == "running" else "waiting"
    finally:
        conn.close()


def row_for(external_id, db_path=None):
    """The newest queue row for an async job id, as a dict, or None."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE external_id=? ORDER BY id DESC LIMIT 1",
                           (str(external_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def resumable_external_ids(db_path=None) -> list:
    """The async job ids whose queue row will still run (queued, or running
    with tries left): the boot sweep leaves those pending (ops.sweep_stale_jobs)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT external_id FROM jobs WHERE external_id IS NOT NULL AND "
                            "(state='queued' OR (state='running' AND attempts < max_attempts))").fetchall()
        return [r["external_id"] for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


# ── running a row ───────────────────────────────────────────────────────────

_local_futures = {}
_local_lock = threading.Lock()


def local_future(job_id):
    """A Future resolved when row `job_id` finishes in THIS process (the
    in-process consumer); a row another process runs never resolves it."""
    with _local_lock:
        fut = _local_futures.get(int(job_id))
        if fut is None:
            fut = _local_futures[int(job_id)] = concurrent.futures.Future()
        return fut


def _settle_local(job_id, result=None, error=None):
    with _local_lock:
        fut = _local_futures.pop(int(job_id), None)
    if fut is not None and not fut.done():
        if error is not None:
            fut.set_exception(error)
        else:
            fut.set_result(result)


class _LeaseKeeper(threading.Thread):
    def __init__(self, job):
        super().__init__(daemon=True, name=f"job-lease-{job['id']}")
        self.job, self.stop = job, threading.Event()
        self.lost = False

    def run(self):
        while not self.stop.wait(RENEW_SECONDS):
            try:
                if not renew(self.job["id"], self.job["worker_id"]):
                    self.lost = True
                    return
            except Exception as e:
                log.warning("lease renewal failed for job %s: %s", self.job["id"], e)


def run_one(job):
    """Run a claimed row through its handler: done, or failed / queued
    again. The handler's return is the row's result. Returns it."""
    with _held_lock:
        _held[int(job["id"])] = job
    keeper = _LeaseKeeper(job)
    keeper.start()
    token = _CURRENT.set(job)
    attribution = (job.get("payload") or {}).get("attribution") or {}
    try:
        import ai_utils
        ctx = ai_utils.ai_context(**{k: v for k, v in attribution.items()
                                     if k in ("trigger", "actor_user_id", "correlation_id")}) \
            if attribution else contextlib.nullcontext()
    except Exception:
        ctx = contextlib.nullcontext()
    try:
        handler = _resolve(HANDLERS[job["kind"]])
        with ctx:
            result = handler(job)
        complete(job["id"], job["worker_id"], result if isinstance(result, (dict, list)) else None)
        _settle_local(job["id"], result)
        return result
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        try:
            import ops
            ops.capture(e, job=f"queue:{job.get('kind')}",
                        context=f"job #{job.get('id')} restaurant_id={job.get('restaurant_id')}")
        except Exception:
            pass
        fail(job["id"], job["worker_id"], e, retry=not getattr(e, "no_retry", False))
        _settle_local(job["id"], error=e)
        return None
    finally:
        _CURRENT.reset(token)
        keeper.stop.set()
        with _held_lock:
            _held.pop(int(job["id"]), None)


def drain(groups, worker_id, max_jobs=None, db_path=None) -> int:
    """Claim and run rows of `groups` until none is claimable (or `max_jobs`
    ran). Returns how many ran."""
    n = 0
    while max_jobs is None or n < max_jobs:
        job = claim(worker_id, groups=groups, db_path=db_path)
        if job is None:
            break
        run_one(job)
        n += 1
    return n


# ── consumers ───────────────────────────────────────────────────────────────

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
_draining = threading.Event()
_held = {}
_held_lock = threading.Lock()
_pools = {}
_active = {}
_parked_n = {}              # grp -> rows of this process parked on the model gateway
_pools_lock = threading.Lock()


def worker_alive(db_path=None) -> bool:
    """Whether a worker.py consumer has beaten its heartbeat recently."""
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT MAX(heartbeat_at) AS h FROM job_workers WHERE role='worker' AND draining=0"
                               ).fetchone()
        finally:
            conn.close()
        return bool(row and row["h"] and float(row["h"]) > time.time() - WORKER_STALE_SECONDS)
    except Exception:
        return False


def this_process_consumes(db_path=None) -> bool:
    """Whether the web process consumes the queue: in-process mode, or
    worker mode with no live worker (an owner's press is never stranded)."""
    m = mode()
    if m == "inprocess":
        return True
    if m == "worker":
        return not worker_alive(db_path)
    return False


def kick(grp, db_path=None, force=False):
    """Start a drain of `grp` on this process's pool when it consumes and a
    pool thread is free. Returns the drain's Future, or None."""
    if _draining.is_set() or not (force or this_process_consumes(db_path)):
        return None
    with _pools_lock:
        size = group_concurrency(grp)
        # Room for as many again while rows wait on the model gateway
        # (parked, SCALE-25): a parked row's thread is asleep, so a drain may
        # start beside it — claim() still holds the group to its bound.
        pool = _pools.get(grp)
        if pool is None or pool._max_workers != 2 * size:
            pool = _pools[grp] = concurrent.futures.ThreadPoolExecutor(max_workers=2 * size,
                                                                       thread_name_prefix=f"queue-{grp}")
        if _active.get(grp, 0) >= size + min(size, _parked_n.get(grp, 0)):
            return None
        _active[grp] = _active.get(grp, 0) + 1

    def _go():
        try:
            return drain((grp,), WORKER_ID, db_path=db_path)
        finally:
            with _pools_lock:
                _active[grp] = max(0, _active.get(grp, 0) - 1)
    return pool.submit(_go)


def tick(db_path=None) -> dict:
    """The minute duty (scheduler._minute_duties) in the consuming process:
    rows a drain could not take when they arrived (every slot busy, a
    resumed row whose lease ran out) are started. Standard counts."""
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    if not this_process_consumes(db_path):
        out["skipped"] = 1
        return out
    heartbeat("web", db_path=db_path)
    for grp in ("generation", "bulk"):
        if claimable(grp, db_path=db_path):
            out["attempted"] += 1
            if kick(grp, db_path=db_path) is not None:
                out["ok"] += 1
    return out


def claimable(grp, db_path=None) -> bool:
    now = time.time()
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT 1 FROM jobs WHERE grp=? AND ((state='queued' AND not_before<=?) OR "
                           "(state='running' AND lease_until<=?)) LIMIT 1", (grp, now, now)).fetchone()
        return row is not None
    finally:
        conn.close()


def heartbeat(role="web", db_path=None) -> None:
    try:
        conn = get_conn(db_path)
        try:
            now = time.time()
            conn.execute("INSERT INTO job_workers (worker_id, role, pid, host, started_at, heartbeat_at, draining) "
                         "VALUES (?,?,?,?,?,?, ?) ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at=excluded."
                         "heartbeat_at, draining=excluded.draining",
                         (WORKER_ID, role, os.getpid(), socket.gethostname(), now, now,
                          1 if _draining.is_set() else 0))
            conn.execute("DELETE FROM job_workers WHERE heartbeat_at < ?", (time.time() - 86400,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("queue heartbeat not written: %s", e)


def run_worker(groups=("generation", "bulk"), stop=None, threads=None, db_path=None):
    """worker.py's consumer: `threads` per group (the group's concurrency by
    default) claiming in a loop, a heartbeat every RENEW_SECONDS, waking on
    POLL_SECONDS. Returns when `stop` is set (or at shutdown)."""
    stop = stop or threading.Event()
    heartbeat("worker", db_path=db_path)

    def _loop(grp):
        while not stop.is_set() and not _draining.is_set():
            try:
                ran = drain((grp,), WORKER_ID, max_jobs=1, db_path=db_path)
            except Exception as e:
                log.error("queue consumer %s failed: %s", grp, e)
                ran = 0
            if not ran:
                stop.wait(POLL_SECONDS)

    workers = []
    for grp in groups:
        for i in range(threads or group_concurrency(grp)):
            t = threading.Thread(target=_loop, args=(grp,), daemon=True, name=f"queue-worker-{grp}-{i}")
            t.start()
            workers.append(t)
    while not stop.is_set() and not _draining.is_set():
        heartbeat("worker", db_path=db_path)
        stop.wait(RENEW_SECONDS)
    for t in workers:
        t.join(timeout=1)


def shutdown(db_path=None) -> int:
    """Drain at exit (a deploy's SIGTERM): stop claiming, hand every row
    this process runs back to the queue so the next process resumes it from
    its checkpoint at once. Returns how many were handed back."""
    _draining.set()
    heartbeat(db_path=db_path)
    with _held_lock:
        held = list(_held.values())
    n = 0
    for job in held:
        try:
            if release(job["id"], job["worker_id"], db_path=db_path):
                n += 1
        except Exception as e:
            log.warning("job %s not handed back: %s", job.get("id"), e)
    if n:
        log.info("handed %d running job(s) back to the queue", n)
    return n


_shutdown_registered = False


def register_shutdown():
    """shutdown() at interpreter exit, before the pools' threads are joined
    (the scheduler's own pattern — scheduler.register_shutdown)."""
    global _shutdown_registered
    if _shutdown_registered:
        return
    _shutdown_registered = True
    try:
        threading._register_atexit(shutdown)
    except Exception:
        import atexit
        atexit.register(shutdown)


def start_inprocess(db_path=None) -> None:
    """The web boot: heartbeat as a web consumer, register the drain, and
    start whatever is already claimable (a row a deploy interrupted resumes
    here)."""
    if not enabled():
        return
    register_shutdown()
    heartbeat("web", db_path=db_path)
    if this_process_consumes(db_path):
        for grp in ("generation", "bulk"):
            kick(grp, db_path=db_path)


def reset_for_tests():
    """Forget this process's consumer state (pools are left to finish)."""
    _draining.clear()
    with _pools_lock:
        _active.clear()
    with _local_lock:
        _local_futures.clear()


# ── measures (the scale triggers: docs/plans/POSTGRES_AND_WORKERS_PLAN.md) ──

def stats(window_seconds=3600, db_path=None) -> dict:
    """{groups: {grp: {queued, running, oldest_queued_seconds}}, wait_p95_seconds
    (enqueue to start, rows started in the window, interactive class),
    failed, consumers: {web, worker}} — the queue-wait p95 is one of the
    triggers for the next scale stage."""
    now = time.time()
    conn = get_conn(db_path)
    try:
        out = {"groups": {}, "mode": mode()}
        for r in conn.execute("SELECT grp, state, COUNT(*) AS n, MIN(enqueued_at) AS oldest FROM jobs "
                              "WHERE state IN ('queued','running') GROUP BY grp, state").fetchall():
            g = out["groups"].setdefault(r["grp"], {"queued": 0, "running": 0, "oldest_queued_seconds": None})
            g[r["state"]] = int(r["n"])
            if r["state"] == "queued" and r["oldest"]:
                g["oldest_queued_seconds"] = round(now - float(r["oldest"]), 1)
        waits = sorted(float(r["w"]) for r in conn.execute(
            "SELECT started_at - enqueued_at AS w FROM jobs WHERE started_at IS NOT NULL AND started_at>? "
            "AND priority_class='interactive'", (now - window_seconds,)).fetchall())
        out["wait_p95_seconds"] = round(waits[min(len(waits) - 1, int(math.ceil(0.95 * len(waits))) - 1)], 1) \
            if waits else None
        out["wait_samples"] = len(waits)
        out["failed"] = int(conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE state='failed' AND finished_at>?",
                                         (now - window_seconds,)).fetchone()["n"])
        out["consumers"] = {r["role"]: int(r["n"]) for r in conn.execute(
            "SELECT role, COUNT(*) AS n FROM job_workers WHERE heartbeat_at>? GROUP BY role",
            (now - WORKER_STALE_SECONDS,)).fetchall()}
        return out
    finally:
        conn.close()
