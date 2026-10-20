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

CLAIM (claim): a plain read first — nothing claimable, or no room, returns
without the write lock (blind re-audit 10/11/26 SCALE-32) — then one write
transaction (`BEGIN IMMEDIATE` on SQLite; on Postgres the dialect layer
turns it into an advisory transaction lock — db_dialect), so two consumers
can never take one row:
  * a group's global bounds, COUNTs of its rows running with a live lease,
    in the database, so N processes share them (SCALE-24, ARCH-22): its CPU
    slots (group_concurrency — QUEUE_GENERATION_CONCURRENCY for generation,
    capped by what the model gateway's token budget can carry) and its rows
    in flight (max_running). A row waiting on a model answer gives its CPU
    slot back for the wait (model_wait, SCALE-1);
  * at most one running job per restaurant in a group (the per-tenant cap);
  * class order: interactive (an owner waiting, p95 < 2 min to start), then
    scheduled (earliest deadline first: the auto-draft's "drafted by" time),
    then bulk (learners) — each class read on its own over the WHOLE queue
    (SCALE-2: a window of the oldest 500 rows hid a press behind any
    backlog) — with a CPU slot kept for owners' presses and one for the
    auto-draft while drafts wait (reservations, SCALE-28);
  * within a class, weighted fair queueing across tenants (`job_tenants`
    virtual time, ranked never below the class's system virtual time —
    SCALE-4; an organization weighs n for per-restaurant work and
    1 + log2(n), at most 4, for presses — SCALE-3) gated by a token bucket
    per tenant per class (per restaurant for the scheduled class): a tenant
    that spent its burst waits behind tenants that have not, unless nobody
    else is waiting (the queue is work-conserving).
A running row whose lease ran out (its process died) is claimable again,
ahead of new rows of its class — `attempts` counts tries, and a job past
`max_attempts` fails.

RUN (run_one): the handler for the row's kind is reached BY NAME (HANDLERS —
"module:function", resolved lazily; registry-driven dispatch, CLAUDE.md),
under the attribution the job was enqueued with (ai_utils.ai_context — the
owner who pressed stays the actor on every model call, #148), with
`current()` naming the job for the code underneath (checkpoint(), the
generation's slot skip, the model gateway's class). A lease keeper renews
the lease while it runs. A row whose lease this process LOST (another
consumer took it, it was handed back at shutdown, or renewals failed for a
whole LEASE_SECONDS so any consumer may now resume it) stops at its next
stage boundary or model call (LeaseLost — blind re-audit 10/20/26 R1-05 /
R9-13): it ran on beside its reclaimer, paying for one week twice.

CONSUMERS. mode():
  "worker" (the default; blind re-audit 10/11/26 SCALE-17) — the queue
    worker process consumes: `worker.py --queue`, started and supervised by
    gunicorn's master (gunicorn.conf.py) in the SAME container, so the
    generation CPU and its memory leave the web process's GIL; it recycles
    itself after QUEUE_WORKER_MAX_JOBS rows or QUEUE_WORKER_MAX_RSS_MB
    (docs/ops/RAILWAY_SCHEDULER_SPLIT.md — never a separate service: the
    volume cannot be shared). The web process only enqueues, and consumes
    after all if no worker has beaten its heartbeat for
    WORKER_STALE_SECONDS (a worker that never started never strands an
    owner's press; a local run with no worker consumes in-process);
  "inprocess" (QUEUE_WORKER=0) — the web process consumes its own queue on
    bounded pools (kick(); one per group) and no worker is started.
The queue is the one path (ARCH-10): the JOB_QUEUE=0 in-memory generation
pool and its GenerationSlots are gone.

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

import db_dialect
import uuid

log = logging.getLogger("job_queue")


def get_conn(db_path=None):
    """Resolved through models at call time, so a test's patched database is
    the one used (CLAUDE.md: never a bound copy of get_conn)."""
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


# ── configuration ───────────────────────────────────────────────────────────

CLASSES = ("interactive", "scheduled", "bulk")
# The consumer groups, each with its own slots: "generation" (schedule
# generations, CPU), "bulk" (the learners and other CPU work), "pos_io"
# (network-bound POS reads — scale audit 10/19/26 INF-6: the nightly archive
# rows held one of bulk's two CPU slots while they waited on RPOWER, so the
# learners queued behind them), "notify" (rows that tell a person something
# on a clock — a published week's staff notices, the auto-publish check, a
# widget wake: blind re-audit 10/20/26 R1-08, they queued behind thousands
# of nightly learner rows on bulk's two CPU slots).
# "maintenance" (scale re-audit 10/20/26 R8-03): the platform's own long
# work — the nightly backup's export and off-site copy, the monthly restore
# drill — one row at a time, in the queue worker process: off the
# scheduler loop and out of the web process (it ran inline on the loop, in
# the web process, for an hour at ~40 GB).
# "studio" (blind re-audit 10/20/26 R5-03): the Schedule Studio's heavy reads
# (studio_offload) off the web process's request threads, never behind a
# generation or a learner.
GROUPS = ("generation", "bulk", "pos_io", "notify", "maintenance", "studio")
_CLASS_RANK = {c: i for i, c in enumerate(CLASSES)}

# kind -> (group, default class, max attempts). A kind not listed runs in the
# bulk group at the bulk class.
KINDS = {
    "schedule_generate": ("generation", "interactive", 3),
    "auto_draft": ("generation", "scheduled", 2),
    "learner": ("bulk", "bulk", 2),
    # An emailed invoice: CPU work (PDFs, a model read), so bulk's slots —
    # but the scheduled class, served ahead of every learner row (blind
    # re-audit 10/20/26 R1-08: it queued behind the nightly fan-outs).
    "email_intake": ("bulk", "scheduled", 4),
    # The nightly POS archive, one row per restaurant (strategy_jobs.
    # run_pos_archive; bar audit 10/14/26 R2).
    "pos_archive": ("pos_io", "bulk", 2),
    # A store just connected: its history archived newest-first, then its
    # first sync — off the request thread, at most the pos_io group's
    # concurrency of stores at once (strategy_jobs.run_queued_pos_onboard;
    # scale audit 10/19/26 ONB-6 / ONB-4).
    "pos_onboard": ("pos_io", "bulk", 2),
    # An owner's "Send this week now": the POS schedule push, off the web
    # request thread (pos_schedule_push.run_queued_push; blind re-audit
    # 10/20/26 R3-07 — it held one of the four request threads on a 30-second
    # RPOWER POST). One try: the push row keeps its own retries.
    "pos_schedule_push": ("pos_io", "interactive", 1),
    # The owner's notes and staff notes read into typed rules when they are
    # SAVED, one batched call (owner_rules.queue_read — blind re-audit
    # 10/11/26 AI-22): never in a generation's critical path.
    "owner_words_read": ("bulk", "scheduled", 2),
    # The learning ablation, off the owner's wait (blind re-audit 10/11/26
    # ARCH-14): research data, one try.
    "schedule_ablation": ("bulk", "bulk", 1),
    # The Monday bar price push, one row per restaurant due at its own hour
    # (bar_signals.run_price_push; FC/Bar audit 10/16/26 SCALE-07).
    "bar_price_push": ("bulk", "bulk", 2),
    # A widget-refresh wake sent at its restaurant's own offset (push.
    # fire_silent; scale audit 10/19/26 FE-12): one try, it carries nothing
    # the app's next refresh won't. The notify group (R1-08).
    "silent_push": ("notify", "bulk", 1),
    # A Send's schedule notices its inline window did not reach (scale
    # audit 10/19/26 A5-F14; schedule_publish.queue_delivery). The notify
    # group (R1-08): staff told about their week never wait on learners.
    "schedule_notices": ("notify", "bulk", 2),
    # One restaurant's auto-publish check (scale audit 10/19/26 A5-F14;
    # scheduler.run_auto_publish_schedules fans out). The notify group: it
    # must run inside its restaurant's local window (R1-08).
    "auto_publish": ("notify", "bulk", 2),
    # A scheduled job the loop hands to the queue worker (jobs_registry
    # `queue`; scheduler.queue_scheduled_job — scale re-audit 10/20/26
    # R8-03): run there through ops.run_job under its own name.
    "scheduled_job": ("maintenance", "scheduled", 2),
    # A Studio read the client polls for (studio_offload, R5-03): one try —
    # a failed or expired row is run by the poll itself. The bulk class's
    # per-tenant fairness, never the presses' interactive bucket.
    "studio_compute": ("studio", "bulk", 1),
}

# kind -> "module:function": what runs a claimed row (reached by name).
HANDLERS = {
    "schedule_generate": "schedule_engine:run_queued_generation",
    "auto_draft": "strategy_jobs:run_queued_auto_draft",
    "learner": "strategy_jobs:run_queued_learner",
    "email_intake": "email_intake:run_queued",
    "pos_archive": "strategy_jobs:run_queued_pos_archive",
    "pos_onboard": "strategy_jobs:run_queued_pos_onboard",
    "pos_schedule_push": "pos_schedule_push:run_queued_push",
    "owner_words_read": "owner_rules:run_queued_read",
    "schedule_ablation": "schedule_job:run_queued_ablation",
    "bar_price_push": "bar_signals:run_queued_price_push",
    "silent_push": "push:run_queued_silent",
    "schedule_notices": "schedule_publish:run_queued_notices",
    "auto_publish": "scheduler:run_queued_auto_publish",
    "scheduled_job": "scheduler:run_queued_scheduled_job",
    "studio_compute": "studio_offload:run_queued",
}
# kind -> "module:function" told when a queued row expires unstarted (its
# deadline passed): what it held is handed back.
EXPIRE_HOOKS = {
    "auto_draft": "strategy_jobs:on_auto_draft_expired",
    "schedule_generate": "schedule_engine:on_queued_generation_expired",
    # An auto-publish check no slot took before the window closed: the
    # owner who turned auto-publish on is told the week did not go (blind
    # re-audit 10/20/26 R5-07 — it expired silently).
    "auto_publish": "scheduler:on_auto_publish_expired",
    # A nightly learner row that never started inside its day: recorded as
    # a missed run, never a silent skip (blind re-audit 10/20/26 R1-08).
    "learner": "strategy_jobs:on_learner_expired",
}

LEASE_SECONDS = int(os.getenv("QUEUE_LEASE_SECONDS", "120"))
RENEW_SECONDS = max(5, LEASE_SECONDS // 4)
WORKER_STALE_SECONDS = int(os.getenv("QUEUE_WORKER_STALE_SECONDS", "90"))
POLL_SECONDS = float(os.getenv("QUEUE_POLL_SECONDS", "2"))
# The scheduled class's earliest-deadline window (blind re-audit 10/11/26
# SCALE-2): rows past it have later deadlines, so EDF would not pick them.
# Interactive and bulk rows are never windowed: every tenant's head is seen.
CANDIDATES = 200
# Seconds before a failed try is tried again: try n waits RETRY_BACKOFF[n-1].
RETRY_BACKOFF = (30, 120, 600)
# Per-tenant token buckets: (burst, refill per hour) by class. The scheduled
# class's bucket is per RESTAURANT, never per organization (SCALE-3: a
# 40-location group shared one 2-token bucket and its auto-drafts expired
# behind every single restaurant).
BUCKETS = {
    "interactive": (float(os.getenv("QUEUE_INTERACTIVE_BURST", "6")),
                    float(os.getenv("QUEUE_INTERACTIVE_PER_HOUR", "6"))),
    "scheduled": (float(os.getenv("QUEUE_SCHEDULED_BURST", "2")),
                  float(os.getenv("QUEUE_SCHEDULED_PER_HOUR", "2"))),
    "bulk": (float(os.getenv("QUEUE_BULK_BURST", "30")), float(os.getenv("QUEUE_BULK_PER_HOUR", "120"))),
}
# Finished rows are kept this long (ops._RETENTION_DAYS "jobs").
RETAIN_DAYS = 14
# A tenant's fairness row unserved this long is forgotten (SCALE-4): it
# would re-enter at the system virtual time with a full bucket anyway.
TENANT_IDLE_DAYS = 7
# The weighted-fair-queueing clock of each class: one job_tenants row.
SYSTEM_TENANT = "~system"


def _int_env(name, default):
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _int_env0(name, default):
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def group_concurrency(grp) -> int:
    """How many rows of `grp` may be doing CODE work at once across EVERY
    process (a count of claimed rows, never a per-process pool) — its CPU
    slots. A row waiting on a model answer gives its slot back while it
    waits (model_wait — blind re-audit 10/11/26 SCALE-1) and counts only
    against max_running. The organisation's token budget caps the rows
    STREAMING a model answer (model_gateway.max_concurrent_generations,
    held in model_gateway.admit), never these CPU slots: folded in here it
    throttled code-only generations that stream nothing (scale audit
    10/19/26 A5-F8)."""
    if grp == "generation":
        # Sized to the container by default (blind re-audit 10/11/26
        # SCALE-16; scale audit 10/19/26 A5-F2/F7): its cores over each
        # solve's threads — more solves at once than the cores carry end on
        # the wall clock instead of their work budget (A5-F6) — and its
        # memory over what one generation takes. It was also held to a fixed
        # 2 (SCHEDULE_GEN_WORKERS), so a bigger container's cores sat idle;
        # SCHEDULE_GEN_WORKERS is now only an operator's ceiling. An explicit
        # QUEUE_GENERATION_CONCURRENCY is the operator's and is kept.
        n = _int_env("QUEUE_GENERATION_CONCURRENCY", default_generation_slots())
        _log_capacity_once(n)
        return n
    if grp == "pos_io":
        # Network-bound rows: their slots are not cores. RPOWER's own budget
        # is its per-token bucket (rpower), not this count.
        return _int_env("QUEUE_POS_IO_CONCURRENCY", 4)
    if grp == "notify":
        # Sends and short checks (R1-08): slots of their own, so a backlog
        # of learner rows never holds a staff notice.
        return _int_env("QUEUE_NOTIFY_CONCURRENCY", 2)
    if grp == "maintenance":
        # One at a time, always: the backup and the drill share the volume's
        # free space and must never run side by side (R8-03).
        return 1
    if grp == "studio":
        # Seconds-long Studio reads (studio_offload): CPU, short.
        return _int_env("QUEUE_STUDIO_CONCURRENCY", 2)
    return _int_env("QUEUE_BULK_CONCURRENCY", 2)


# What one generation holds at its peak on top of the container's baseline
# (scale audit 10/19/26 A5-F7, MEASURED-LOCAL): the parent's growth plus its
# CP-SAT solve process — about 1.0-1.4 GB at 60-250 people. The figure for a
# row whose roster size is unknown, and the static clamp on the slot count;
# a row that carries its roster's estimate (`mem_mb`) is admitted by it.
GEN_SLOT_MB = float(os.getenv("SCHEDULE_GEN_SLOT_MB", "") or 1400)
# What one generation holds at its peak by roster size (blind re-audit
# 10/20/26 R5-06, MEASURED-LOCAL with scripts/run_schedule_benchmark.py on
# the solver arm: parent RSS + solve-process RSS): 60 people 844 MB, 120
# 1,044, 250 1,350, 500 3,798. A flat 1,400 MB admitted six 500-person
# generations into 22.8 GB that hold 3.8 GB each.
GEN_MEMORY_BY_ROSTER = ((0, 800.0), (60, 844.0), (120, 1044.0), (250, 1350.0), (500, 3798.0))
GEN_MEMORY_MARGIN = 1.15
# Cores one generation keeps busy on average (R5-08, MEASURED-LOCAL: CPU /
# wall 1.9 at 60 people, 1.7 at 120, 1.5 at 250, 1.1 at 500 — most of a
# generation is single-threaded Python around short CP-SAT solves). The
# slots were the cores over each solve's 4 threads, so half the container
# idled while a burst of big rosters queued; the memory admission above is
# the real bound, and the platform check names solves the wall clock cut.
GEN_CORES_PER_SLOT = float(os.getenv("SCHEDULE_GEN_CORES_PER_SLOT", "") or 2)


def generation_memory_mb(roster) -> float:
    """The peak MB one generation of a `roster`-person restaurant holds
    (GEN_MEMORY_BY_ROSTER, interpolated, extrapolated past its last point
    on the last segment's slope, times GEN_MEMORY_MARGIN); GEN_SLOT_MB when
    the roster is unknown."""
    try:
        n = float(roster)
    except (TypeError, ValueError):
        return GEN_SLOT_MB
    if n < 0:
        return GEN_SLOT_MB
    pts = GEN_MEMORY_BY_ROSTER
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if n <= x1:
            mb = y0 + (y1 - y0) * (n - x0) / float(x1 - x0)
            break
    else:
        (x0, y0), (x1, y1) = pts[-2], pts[-1]
        mb = y1 + (y1 - y0) * (n - x1) / float(x1 - x0)
    return round(mb * GEN_MEMORY_MARGIN, 1)


def _row_memory_mb(row) -> float:
    """A generation row's estimate (its payload's `mem_mb`), else GEN_SLOT_MB."""
    raw = row.get("payload_json") if isinstance(row, dict) else None
    try:
        mb = float((json.loads(raw) if raw else {}).get("mem_mb") or 0)
    except (TypeError, ValueError, AttributeError):
        mb = 0.0
    return mb if mb > 0 else GEN_SLOT_MB


def _memory_admits(conn, grp, chosen, now) -> bool:
    """Whether `chosen` fits the container's memory beside the generation
    rows running now (R5-06): their estimates plus its own within the limit
    less GEN_MEMORY_RESERVE_MB. Always when nothing runs (one at least), when
    the container has no limit, and for any group but generation."""
    if grp != "generation":
        return True
    lim = memory_limit_mb()
    if lim is None:
        return True
    rows = conn.execute("SELECT id, payload_json FROM jobs WHERE grp=? AND state='running' AND lease_until>? "
                        "AND id<>?", (grp, now, int(chosen["id"]))).fetchall()
    if not rows:
        return True
    held = sum(_row_memory_mb(dict(r)) for r in rows)
    return held + _row_memory_mb(chosen) <= lim - GEN_MEMORY_RESERVE_MB
# What the container holds before any generation runs: the web process and
# the queue worker's own baseline.
GEN_MEMORY_RESERVE_MB = float(os.getenv("SCHEDULE_GEN_MEMORY_RESERVE_MB", "") or 1536)


def default_generation_slots() -> int:
    """The generation group's CPU slots when the operator set none: the
    cores' slots (cpu_generation_slots), held to what the memory carries
    (memory_generation_slots, when the container has a limit) and to
    SCHEDULE_GEN_WORKERS when one is set. At least one."""
    n = cpu_generation_slots()
    mem = memory_generation_slots()
    if mem:
        n = min(n, mem)
    try:
        import schedule_engine
        ceiling = int(schedule_engine.SCHEDULE_GEN_WORKERS or 0)
    except Exception:
        ceiling = 0
    if ceiling > 0:
        n = min(n, ceiling)
    return max(1, int(n))


def memory_limit_mb():
    """The container's memory limit in MB — cgroup v2 memory.max, else v1
    memory.limit_in_bytes — or None when it has none (or it cannot be
    read): the host's memory is not the container's."""
    raw = None
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path) as f:
                raw = f.read().strip()
            break
        except Exception:
            continue
    if not raw or raw == "max":
        return None
    try:
        mb = float(raw) / 1048576.0
    except ValueError:
        return None
    # cgroup v1 writes a page-rounded 2^63 for "no limit".
    if mb <= 0 or mb >= float(1 << 40):
        return None
    return mb


def memory_generation_slots():
    """Generations the container's memory carries at once: its limit less
    GEN_MEMORY_RESERVE_MB, over GEN_SLOT_MB a generation; at least one.
    None without a limit."""
    lim = memory_limit_mb()
    if lim is None:
        return None
    return max(1, int((lim - GEN_MEMORY_RESERVE_MB) // max(1.0, GEN_SLOT_MB)))


def _solve_threads() -> int:
    try:
        import schedule_cpsat
        return max(1, int(schedule_cpsat.WORKERS))
    except Exception:
        return 4


def generation_capacity() -> dict:
    """What the generation group is sized from, for the platform check and
    the boot line: {cores, memory_mb, cpu_slots, memory_slots, slots,
    solve_threads, oversubscribed} — oversubscribed when the slots' solver
    threads outnumber the cores (solves then end on the wall clock, not
    their work budget: A5-F6)."""
    per = _solve_threads()
    cores = cpu_cores()
    slots = group_concurrency("generation")
    mem = memory_limit_mb()
    return {"cores": cores, "memory_mb": int(mem) if mem else None, "cpu_slots": cpu_generation_slots(),
            "memory_slots": memory_generation_slots(), "slots": slots, "solve_threads": per,
            "cores_per_slot": GEN_CORES_PER_SLOT,
            "oversubscribed": slots * GEN_CORES_PER_SLOT > cores}


_CAPACITY_LOGGED = []


def _log_capacity_once(slots) -> None:
    """The generation slots this process sized to, said once per process —
    a warning when their solver threads outnumber the cores (an operator's
    QUEUE_GENERATION_CONCURRENCY past them: A5-F6)."""
    if _CAPACITY_LOGGED:
        return
    _CAPACITY_LOGGED.append(slots)
    try:
        cores, mem = cpu_cores(), memory_limit_mb()
        line = (f"generation slots {slots}: {cores} cores / {GEN_CORES_PER_SLOT:g} a generation, memory "
                f"{int(mem) if mem else 'unlimited'} MB (each generation admitted by its roster's estimate, "
                f"{int(GEN_SLOT_MB)} MB when unknown)")
        if slots * GEN_CORES_PER_SLOT > cores:
            log.warning(line + " — more generations than the cores carry: solves will end on the wall clock")
        else:
            log.info(line)
    except Exception:
        pass


def cpu_cores() -> int:
    """The cores this container may use: the cgroup's CPU quota (v2
    cpu.max, else v1 cfs quota/period) when one is set, else the CPUs this
    process may run on — os.cpu_count() alone reads the HOST's cores."""
    quota = None
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            q, p = (f.read().split() + ["100000"])[:2]
        if q != "max":
            quota = float(q) / float(p)
    except Exception:
        try:
            with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as f:
                q = float(f.read().strip())
            with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as f:
                p = float(f.read().strip())
            if q > 0 and p > 0:
                quota = q / p
        except Exception:
            quota = None
    try:
        n = len(os.sched_getaffinity(0))
    except Exception:
        n = os.cpu_count() or 1
    if quota:
        n = min(n, max(1, int(quota)))
    return max(1, int(n))


def cpu_generation_slots() -> int:
    """Generations the cores carry at once: the cores over what one
    generation keeps busy (GEN_CORES_PER_SLOT, measured — R5-08; it was each
    solve's 4 threads), at least one."""
    return max(1, int(cpu_cores() // max(0.5, GEN_CORES_PER_SLOT)))


def _gateway_streams():
    try:
        import model_gateway
        return model_gateway.max_concurrent_generations()
    except Exception:
        return None


def rows_in_phase(grp, phase, exclude_id=None, db_path=None) -> int:
    """Rows of `grp` running with a live lease in `phase` ('model': waiting
    on a model answer) — what model_gateway's stream cap counts (A5-F8)."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE grp=? AND state='running' AND lease_until>? "
                           "AND phase=? AND id<>?", (grp, time.time(), phase,
                                                     int(exclude_id) if exclude_id is not None else -1)).fetchone()
        return int(row["n"] or 0)
    finally:
        conn.close()


def max_running(grp) -> int:
    """How many rows of `grp` may be claimed at once across every process,
    code work and model waits together (SCALE-1): the CPU slots plus the
    rows streaming a model answer. QUEUE_<GROUP>_MAX_RUNNING, else three
    times the CPU slots for the generation group (twice for bulk); under a
    configured token budget, never more streams than it carries. This is
    also the size of a consumer's thread pool for the group."""
    slots = group_concurrency(grp)
    if grp == "maintenance":
        return slots                      # no model waits: never more in flight than its one slot
    n = _int_env(f"QUEUE_{str(grp).upper()}_MAX_RUNNING", slots * (3 if grp == "generation" else 2))
    if grp == "generation":
        cap = _gateway_streams()
        if cap:
            n = min(n, slots + int(cap))
    return max(slots, n)


def reservations(grp) -> dict:
    """The CPU slots a class keeps for itself in `grp` (SCALE-28): one for
    owners' presses always (a press never waits behind drafts), and one for
    the auto-draft while drafts are waiting (a draft always progresses).
    QUEUE_INTERACTIVE_RESERVED / QUEUE_SCHEDULED_RESERVED; each at most the
    slots less one, so with one slot there is no reservation, only order.
    The drafts' slot is held against presses only while it leaves them
    PRESS_MIN_SLOTS, or a draft is within SCHEDULED_URGENT_SECONDS of its
    deadline (_admits — scale audit 10/19/26 A5-F4)."""
    slots = group_concurrency(grp)
    if grp != "generation" or slots <= 1:
        return {}
    return {"interactive": min(_int_env0("QUEUE_INTERACTIVE_RESERVED", 1), slots - 1),
            "scheduled": min(_int_env0("QUEUE_SCHEDULED_RESERVED", 1), slots - 1)}


# Presses keep at least this many CPU slots while drafts wait (scale audit
# 10/19/26 A5-F4): with two slots the drafts' reservation left an owners'
# burst one slot all morning. Below it the drafts' slot is held only for a
# draft about to expire.
PRESS_MIN_SLOTS = 2
SCHEDULED_URGENT_SECONDS = int(os.getenv("QUEUE_SCHEDULED_URGENT_SECONDS", "3600"))


def mode() -> str:
    """Who consumes the queue. "worker" (the default, QUEUE_WORKER unset or
    1 — blind re-audit 10/11/26 SCALE-17): the queue worker process
    (worker.py --queue, started beside gunicorn by gunicorn.conf.py)
    consumes, and the web process consumes only while no worker has beaten
    its heartbeat (this_process_consumes — a press is never stranded, and a
    local `python3 hosted_dashboard.py` with no worker consumes in-process
    as before). "inprocess" (QUEUE_WORKER=0): the web process consumes its
    own queue and no worker process is started — the rollback.

    The queue is the one path for generations, auto-drafts and the nightly
    learners (ARCH-10): the JOB_QUEUE=0 in-memory paths are gone."""
    if os.getenv("QUEUE_WORKER", "").strip().lower() in ("0", "false", "off", "no"):
        return "inprocess"
    return "worker"


def worker_process_wanted() -> bool:
    """Whether gunicorn's master starts the queue worker process
    (gunicorn.conf.py): in worker mode, unless QUEUE_WORKER_PROCESS=0 (a
    worker.py started some other way — the documented sh -c command)."""
    return mode() == "worker" and os.getenv("QUEUE_WORKER_PROCESS", "1").strip().lower() not in (
        "0", "false", "off", "no")


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
        phase            TEXT,
        result_json      TEXT,
        error            TEXT,
        enqueued_at      REAL NOT NULL,
        started_at       REAL,
        finished_at      REAL,
        created_at       TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(grp, state, not_before)",
    # The claim's per-class reads (blind re-audit 10/11/26 SCALE-2, SCALE-32):
    # each tenant's head of a class, the scheduled class by deadline, and the
    # rows past their deadline — each a SEARCH over its own class, never a
    # scan of the oldest rows of every class.
    "CREATE INDEX IF NOT EXISTS idx_jobs_class_tenant ON jobs(grp, priority_class, state, tenant_key, id)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_class_deadline ON jobs(grp, priority_class, state, deadline)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_deadline ON jobs(grp, state, deadline)",
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


# ── a lease this process lost (blind re-audit 10/20/26 R1-05 / R9-13) ───────
#
# The lease keeper used to note a lost lease and nothing read it: a row whose
# renewals failed for LEASE_SECONDS (a Postgres restart) was resumed by
# another consumer — or by this very process — while its first copy ran on,
# two model spends and two writers for one week. A lost row now stops at its
# next stage boundary (checkpoint) or model call (model_wait), and a
# side effect only one consumer may cause asks ensure_lease() first.

class LeaseLost(BaseException):
    """The queue row this thread runs is no longer this process's.

    A BaseException, deliberately (ai_utils.StoredReadMiss's reason): every
    handler body ends in broad `except Exception` blocks that would report
    the job failed — into the owner's async row, beside the consumer now
    running it. run_one catches it and leaves the row to its new holder."""


_lost = set()                 # ids of rows this process no longer holds (lost or handed back)
_lost_lock = threading.Lock()


def _mark_lost(job_id) -> None:
    with _lost_lock:
        _lost.add(int(job_id))


def lease_lost(job=None) -> bool:
    """Whether this process lost the lease of `job` (default: the row this
    thread runs). False outside a queue job."""
    job = job if job is not None else _CURRENT.get()
    if job is None or job.get("id") is None:
        return False
    if job.get("lease_lost"):                # set with the mark by _LeaseKeeper._lose
        return True
    with _lost_lock:
        return int(job["id"]) in _lost


def raise_if_lost(job=None) -> None:
    """LeaseLost when the row this thread runs is no longer this process's."""
    if lease_lost(job):
        raise LeaseLost(f"job {(job or _CURRENT.get() or {}).get('id')}: its lease was lost")


def ensure_lease(db_path=None) -> None:
    """Before a side effect only one consumer may cause (a send, a paid
    call): confirm against the database that this process still holds the
    row it runs — a renewal now. Raises LeaseLost when it does not (or the
    database cannot say: the row is then left to whoever resumes it). A
    no-op outside a queue job."""
    job = _CURRENT.get()
    if job is None or not job.get("worker_id"):
        return
    raise_if_lost(job)
    try:
        held = renew(job["id"], job["worker_id"], db_path=db_path)
    except Exception as e:
        log.warning("job %s: lease not confirmed (%s) — stopping", job.get("id"), e)
        held = False
    if not held:
        _mark_lost(job["id"])
        raise LeaseLost(f"job {job.get('id')}: this consumer no longer holds its lease")


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
            not_before=None, deadline=None, max_attempts=None, wake=True, db_path=None, requeue_states=()):
    """(row id, created): one unit of work as a row. With `idempotency_key`
    a second enqueue of the same work returns the first row (created False)
    whatever its state — unless that state is one of `requeue_states` (an
    `expired` auto-draft, which ran nothing: scale audit 10/19/26 A5-F12),
    when the old row keeps its record under a retired key and the work is
    queued again. `not_before` / `deadline` are time.time() values; a row
    still queued past its deadline expires (EXPIRE_HOOKS) instead of
    running late. Wakes this process's consumers (kick) unless `wake` is
    False."""
    grp, klass, attempts = KINDS.get(kind, ("bulk", "bulk", 2))
    klass = priority_class if priority_class in CLASSES else klass
    now = time.time()
    conn = get_conn(db_path)
    try:
        # Enqueues of one kind serialise with each other (its idempotency
        # keys), never with claims or other kinds (SCALE-12).
        db_dialect.begin_write(conn, "jobs", "enqueue", kind)
        if idempotency_key:
            row = conn.execute("SELECT id, state FROM jobs WHERE idempotency_key=?",
                               (str(idempotency_key),)).fetchone()
            if row and row["state"] in (requeue_states or ()):
                conn.execute("UPDATE jobs SET idempotency_key=? WHERE id=?",
                             (f"{idempotency_key}~{row['state']}:{int(row['id'])}", int(row["id"])))
            elif row:
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
    _wake.set()
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
        db_dialect.begin_write(conn, "jobs", "enqueue", kind)
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
    if created:
        _wake.set()
    if wake and created:
        kick(grp, db_path=db_path)
    return created


# ── claim ───────────────────────────────────────────────────────────────────
#
# Blind re-audit 10/11/26 (SCALE-2, SCALE-3, SCALE-4, SCALE-28, SCALE-32).
# The claim used to rank the OLDEST 500 rows of a group by id, then apply
# class priority among them: behind 9,000 queued auto-drafts an owner's
# press was not claimed in 2,000 claims. Each class is now read on its own,
# best class first, with indexed reads over the WHOLE queue:
#   * interactive and bulk — every tenant's oldest claimable row (one
#     GROUP BY over idx_jobs_class_tenant), ordered in SQL by (its bucket
#     has a token, its effective virtual time, id): weighted fair queueing
#     over every tenant with work, never a window of the oldest rows;
#   * scheduled — earliest deadline first over its own deadline index
#     (CANDIDATES rows: any row past them has a later deadline), each
#     restaurant's own bucket deciding (SCALE-3);
#   * a running row whose lease ran out (its process died) is resumed first
#     within its class: it holds checkpoints and paid model answers.
# A class is admitted by the group's CPU slots, its stream bound and the
# other class's reservation (_admits). With nothing claimable the claim
# returns after a plain read, never taking the write lock (SCALE-32).

# The classes whose token bucket is the RESTAURANT's, never its
# organization's: the auto-drafts (SCALE-3) and owners' presses (blind
# re-audit 10/20/26 R5-05 — a 40-location group's managers shared one
# 6-an-hour bucket, so its 7th press of an hour ranked behind every single
# restaurant's). The organization stays the fairness tenant (virtual time).
PER_LOCATION_BUCKETS = ("scheduled", "interactive")


def _bucket_key(row, klass):
    """Whose token bucket a row spends: the restaurant's for the classes in
    PER_LOCATION_BUCKETS, else its fairness tenant's."""
    if klass in PER_LOCATION_BUCKETS and row.get("restaurant_id") is not None:
        return f"r:{int(row['restaurant_id'])}"
    return row.get("tenant_key") or "platform"


def _bucket_sql(klass) -> str:
    """_bucket_key as SQL over a jobs row (no alias), for _fair_head."""
    if klass in PER_LOCATION_BUCKETS:
        return ("CASE WHEN restaurant_id IS NULL THEN COALESCE(tenant_key, 'platform') "
                "ELSE 'r:' || CAST(restaurant_id AS TEXT) END")
    return "COALESCE(tenant_key, 'platform')"


def _bucket(conn, key, klass, now):
    """(tokens now, stored virtual time or None) of one job_tenants row; a
    tenant never seen has a full bucket and no virtual time."""
    burst, per_hour = BUCKETS.get(klass, BUCKETS["bulk"])
    row = conn.execute("SELECT tokens, refilled_at, vtime FROM job_tenants WHERE tenant_key=? AND klass=?",
                       (key, klass)).fetchone()
    if row is None:
        return burst, None
    tokens = min(burst, float(row["tokens"]) + (now - float(row["refilled_at"])) * per_hour / 3600.0)
    return tokens, float(row["vtime"] or 0)


def _system_vtime(conn, klass) -> float:
    """The class's system virtual time (self-clocked fair queueing, SCALE-4):
    the start tag of the last row served. A tenant whose own virtual time
    is behind it — idle a while, or new — is ranked AT it, never below: a
    long-idle tenant used to keep its old small tag and beat every busy
    tenant until it caught up, and the busiest restaurants (orgs, weekly
    planners) lost every contended claim for good."""
    row = conn.execute("SELECT vtime FROM job_tenants WHERE tenant_key=? AND klass=?",
                       (SYSTEM_TENANT, klass)).fetchone()
    return float(row["vtime"] or 0) if row else 0.0


def _tenant_weight(conn, tenant, klass="interactive") -> float:
    """A tenant's share. An organization with n locations weighs n for
    per-restaurant work — the scheduled auto-drafts and the bulk learners,
    each location a restaurant's worth of work (SCALE-3: capped at 4, a
    40-location group was served after every single restaurant and its
    drafts expired) — and 1 + log2(n), at most 4, for owners' presses:
    served more than a single restaurant, never so much that singles
    starve behind it."""
    if not str(tenant).startswith("org:"):
        return 1.0
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM restaurants WHERE organization_id=?",
                         (int(str(tenant)[4:]),)).fetchone()["n"]
    except Exception:
        n = 1
    n = max(1, int(n or 1))
    if klass in ("scheduled", "bulk"):
        return float(n)
    return min(4.0, 1.0 + math.log2(n))


def _put_tenant(conn, key, klass, tokens, now, vtime):
    conn.execute(
        "INSERT INTO job_tenants (tenant_key, klass, tokens, refilled_at, vtime, served_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(tenant_key, klass) DO UPDATE SET tokens=excluded.tokens, refilled_at=excluded.refilled_at, "
        "vtime=excluded.vtime, served_at=excluded.served_at",
        (key, klass, float(tokens), now, float(vtime), now))


def _serve(conn, row, klass, now):
    """Spend a token from the row's bucket and advance its tenant's virtual
    time: start = max(own, system), finish = start + 1/weight; the system
    virtual time moves to the start served (never back)."""
    burst = BUCKETS.get(klass, BUCKETS["bulk"])[0]
    tenant = row.get("tenant_key") or "platform"
    bkey = _bucket_key(row, klass)
    system = _system_vtime(conn, klass)
    tokens, vt = _bucket(conn, tenant, klass, now)
    start = max(system, vt if vt is not None else system)
    finish = start + 1.0 / _tenant_weight(conn, tenant, klass)
    if bkey == tenant:
        _put_tenant(conn, tenant, klass, max(0.0, tokens - 1.0), now, finish)
    else:
        # An organization's location: the tenant keeps the virtual time, the
        # location its own bucket.
        _put_tenant(conn, tenant, klass, burst, now, finish)
        btokens, _bvt = _bucket(conn, bkey, klass, now)
        _put_tenant(conn, bkey, klass, max(0.0, btokens - 1.0), now, 0.0)
    _put_tenant(conn, SYSTEM_TENANT, klass, 0.0, now, max(system, start))


# `+not_before` (a unary plus, valid on SQLite and Postgres alike) keeps the
# planner off idx_jobs_claim for these reads: with no ANALYZE statistics it
# chose that index's range on not_before over the per-class index and then
# sorted every queued row of the group (2.7 ms at 9,000 rows, against 0.1).

def _busy_clause(busy):
    """SQL excluding the restaurants that already run a row of the group."""
    ids = sorted(int(x) for x in busy if x is not None)
    if not ids:
        return "", []
    return " AND (restaurant_id IS NULL OR restaurant_id NOT IN (" + ",".join("?" * len(ids)) + "))", ids


def _fair_head(conn, grp, klass, now, busy):
    """The row weighted fair queueing serves next among EVERY tenant with a
    claimable row of `klass` (interactive, bulk): each tenant's oldest row,
    ranked by (its bucket has a token, its effective virtual time, id).
    Work-conserving: a tenant past its burst still runs when nobody else
    waits."""
    _burst, per_hour = BUCKETS.get(klass, BUCKETS["bulk"])
    system = _system_vtime(conn, klass)
    excl, ids = _busy_clause(busy)
    # Each BUCKET's oldest row (a restaurant's for a per-location class, so
    # every location of an organization is a candidate with its own tokens),
    # its tokens read from its bucket (b) and its virtual time from its
    # fairness tenant (t) — the organization's, shared by its locations.
    bk = _bucket_sql(klass)
    row = conn.execute(
        "SELECT j.* FROM (SELECT " + bk + " AS bk, MIN(id) AS head FROM jobs "
        "WHERE grp=? AND priority_class=? AND state='queued' AND +not_before<=?" + excl +
        " GROUP BY " + bk + ") h JOIN jobs j ON j.id = h.head "
        "LEFT JOIN job_tenants b ON b.tenant_key = h.bk AND b.klass = ? "
        "LEFT JOIN job_tenants t ON t.tenant_key = COALESCE(j.tenant_key, 'platform') AND t.klass = ? "
        "ORDER BY CASE WHEN b.tenant_key IS NULL OR b.tokens + (? - b.refilled_at) * ? / 3600.0 >= 1 "
        "THEN 0 ELSE 1 END, CASE WHEN t.vtime IS NULL OR t.vtime < ? THEN ? ELSE t.vtime END, j.id LIMIT 1",
        (grp, klass, now, *ids, klass, klass, now, float(per_hour), system, system)).fetchone()
    return dict(row) if row else None


def _edf_head(conn, grp, now, busy):
    """The scheduled row to serve: earliest deadline first (rows with no
    deadline last), the first whose restaurant's bucket has a token — ties
    on the deadline go to the tenant with the lower virtual time — else,
    work-conserving, the earliest."""
    excl, ids = _busy_clause(busy)
    base = ("SELECT * FROM jobs WHERE grp=? AND priority_class='scheduled' AND state='queued' "
            "AND +not_before<=?" + excl)
    rows = [dict(r) for r in conn.execute(base + " AND deadline IS NOT NULL ORDER BY deadline, id LIMIT ?",
                                          (grp, now, *ids, CANDIDATES)).fetchall()]
    if len(rows) < CANDIDATES:
        rows += [dict(r) for r in conn.execute(base + " AND deadline IS NULL ORDER BY id LIMIT ?",
                                               (grp, now, *ids, CANDIDATES - len(rows))).fetchall()]
    if not rows:
        return None
    system = _system_vtime(conn, "scheduled")
    tokens = {}

    def has_token(c):
        k = _bucket_key(c, "scheduled")
        if k not in tokens:
            tokens[k] = _bucket(conn, k, "scheduled", now)[0]
        return tokens[k] >= 1.0
    for r in rows:
        if has_token(r):
            tied = [c for c in rows if c["deadline"] == r["deadline"] and has_token(c)]
            if len(tied) <= 1:
                return r

            def vt_of(c):
                _t, v = _bucket(conn, c.get("tenant_key") or "platform", "scheduled", now)
                return (max(system, v if v is not None else system), c["id"])
            return min(tied, key=vt_of)
    return rows[0]


def _occupancy(conn, grp, now):
    """(rows running with a live lease, {class: rows holding or awaiting a
    CPU slot}, {restaurant ids running}) of `grp`."""
    rows = conn.execute("SELECT restaurant_id, priority_class, phase, parked_until FROM jobs WHERE grp=? "
                        "AND state='running' AND lease_until>?", (grp, now)).fetchall()
    held = {}
    for r in rows:
        # Waiting on a model answer (model_wait) or parked on the gateway's
        # next minute (parked, SCALE-25): no CPU slot held.
        if (r["phase"] or "") != "model" and not (r["parked_until"] and r["parked_until"] > now):
            held[r["priority_class"]] = held.get(r["priority_class"], 0) + 1
    return len(rows), held, {r["restaurant_id"] for r in rows if r["restaurant_id"] is not None}


def _has_work(conn, grp, klass, now) -> bool:
    return conn.execute("SELECT 1 FROM jobs WHERE grp=? AND priority_class=? AND state='queued' AND +not_before<=? "
                        "LIMIT 1", (grp, klass, now)).fetchone() is not None


def _has_urgent_draft(conn, grp, now) -> bool:
    """A scheduled row waiting that expires within SCHEDULED_URGENT_SECONDS."""
    return conn.execute("SELECT 1 FROM jobs WHERE grp=? AND priority_class='scheduled' AND state='queued' "
                        "AND +not_before<=? AND deadline IS NOT NULL AND deadline<=? LIMIT 1",
                        (grp, now, now + SCHEDULED_URGENT_SECONDS)).fetchone() is not None


def _admits(conn, grp, klass, total, held, now) -> bool:
    """Whether a row of `klass` may start in `grp` now: the group's rows in
    flight under max_running, and its CPU slots — less what another class
    keeps for itself (reservations: owners' presses always, the auto-draft
    while drafts wait) — not all held (SCALE-28). The drafts' slot is kept
    from a press only while presses still have PRESS_MIN_SLOTS, or when a
    draft is about to expire (scale audit 10/19/26 A5-F4: on two slots it
    halved an owners' burst all morning)."""
    if total >= max_running(grp):
        return False
    slots = group_concurrency(grp)
    kept = 0
    for other, n in reservations(grp).items():
        if other == klass or n <= 0:
            continue
        if other == "interactive":
            holds = True
        elif other == "scheduled" and slots - n < PRESS_MIN_SLOTS:
            holds = _has_urgent_draft(conn, grp, now)
        else:
            holds = _has_work(conn, grp, other, now)
        if holds:
            kept += max(0, n - held.get(other, 0))
    return sum(held.values()) + kept < slots


def _maybe_claimable(conn, groups, now) -> bool:
    """A plain read (no write lock) of whether a claim could take anything
    (SCALE-32): some group has a claimable row and room for it, or has a
    queued row past its deadline to expire."""
    for grp in groups:
        if conn.execute("SELECT 1 FROM jobs WHERE grp=? AND state='queued' AND attempts=0 AND deadline IS NOT NULL "
                        "AND deadline<? LIMIT 1", (grp, now)).fetchone():
            return True
        if not conn.execute("SELECT 1 FROM jobs WHERE grp=? AND ((state='queued' AND not_before<=?) OR "
                            "(state='running' AND lease_until<=?)) LIMIT 1", (grp, now, now)).fetchone():
            continue
        total, held, _busy = _occupancy(conn, grp, now)
        if total < max_running(grp) and sum(held.values()) < group_concurrency(grp):
            return True
    return False


def _expire_overdue(conn, grp, now) -> list:
    """Queued rows of `grp` that never started before their deadline:
    expired (their EXPIRE_HOOKS run after the commit). Bounded per claim."""
    out = []
    for r in conn.execute("SELECT * FROM jobs WHERE grp=? AND state='queued' AND attempts=0 AND deadline IS NOT NULL "
                          "AND deadline<? ORDER BY deadline LIMIT ?", (grp, now, CANDIDATES)).fetchall():
        conn.execute("UPDATE jobs SET state='expired', finished_at=?, error=? WHERE id=? AND state='queued'",
                     (now, "not started before its deadline", r["id"]))
        out.append(dict(r))
    return out


def _dead_rows(conn, grp, now, expired) -> list:
    """Running rows whose lease ran out (their process died): a row on its
    last try is failed (said); the rest are resumable, oldest first. A row
    THIS process still runs is never one (R1-05: its lease ran out because
    renewals failed, not because it stopped) — it is resumed once its
    handler has stopped at its next boundary (LeaseLost)."""
    out = []
    # A row THIS process is still running is never "dead" to it (R5-12):
    # after a stall past the lease another thread of the same process (the
    # same worker id, so the lease keeper's renewals kept succeeding) took it
    # and ran it beside the original.
    with _held_lock:
        mine = set(_held)
    for r in conn.execute("SELECT * FROM jobs WHERE grp=? AND state='running' AND lease_until<=? ORDER BY id LIMIT ?",
                          (grp, now, CANDIDATES)).fetchall():
        r = dict(r)
        if int(r["id"]) in mine:
            continue
        if int(r["attempts"] or 0) >= int(r["max_attempts"] or 1):
            conn.execute("UPDATE jobs SET state='failed', finished_at=?, error=COALESCE(error, ?), "
                         "lease_owner=NULL, phase=NULL WHERE id=?",
                         (now, "its process stopped on its last try", r["id"]))
            expired.append(dict(r, state="failed"))
            continue
        out.append(r)
    return out


def frozen() -> str:
    """Why no row may be claimed now, or "" (scale re-audit 10/20/26 R8-05):
    the Postgres cutover's write freeze (MAINTENANCE_READ_ONLY), a restore in
    progress (RESTORE_FROM — the runbook promises nothing runs against a
    database nobody has looked at yet; the scheduler stood down but the
    queue went on claiming schedule notices, auto-publishes and generations),
    or a database that went back in time under the running service
    (restore_guard — a Railway Postgres restore, R8-04). config.
    scheduling_allowed is not the gate: it is false on every laptop, where
    the queue must still run an owner's press."""
    if (os.getenv("MAINTENANCE_READ_ONLY") or "").strip().lower() in ("1", "true", "yes", "on"):
        return "maintenance_read_only"
    if (os.getenv("RESTORE_FROM") or "").strip():
        return "restore_from"
    try:
        import config
        if config.restore_frozen():
            return "restore_detected"
    except Exception:
        pass
    return ""


def claim(worker_id, groups=GROUPS, db_path=None):
    """The next row this consumer may run, claimed (state running, a lease,
    attempts + 1, holding a CPU slot) — or None when nothing is claimable
    within the bounds above. Rows expired unstarted on the way are handed
    to their kind's EXPIRE_HOOKS after the commit."""
    if _draining.is_set() or frozen():
        return None                       # draining, a write freeze, a restore, or a restored database
    now = time.time()
    expired, picked = [], None
    conn = get_conn(db_path)
    try:
        if not _maybe_claimable(conn, groups, now):
            return None
        # A group's bounds (its slots, its rows in flight, one running job a
        # restaurant) are checked and taken under that group's lock: claims
        # of other groups, enqueues and every other writer go on
        # (SCALE-12; on SQLite, BEGIN IMMEDIATE as before).
        db_dialect.begin_write(conn, lock_now=False)
        for grp in groups:
            db_dialect.lock(conn, "jobs", "claim", grp)
            expired += _expire_overdue(conn, grp, now)
            dead = _dead_rows(conn, grp, now, expired)
            total, held, busy = _occupancy(conn, grp, now)
            for klass in CLASSES:
                if not _admits(conn, grp, klass, total, held, now):
                    continue
                chosen = next((r for r in dead if r["priority_class"] == klass
                               and (r["restaurant_id"] is None or r["restaurant_id"] not in busy)), None)
                if chosen is None:
                    chosen = _edf_head(conn, grp, now, busy) if klass == "scheduled" else \
                        _fair_head(conn, grp, klass, now, busy)
                if chosen is None:
                    continue
                if not _memory_admits(conn, grp, chosen, now):
                    # Its roster's footprint does not fit beside what runs
                    # (R5-06): it waits for one to finish.
                    continue
                n = conn.execute(
                    "UPDATE jobs SET state='running', attempts=attempts+1, lease_owner=?, lease_until=?, phase=NULL, "
                    "started_at=COALESCE(started_at, ?) WHERE id=? AND (state='queued' OR "
                    "(state='running' AND lease_until<=?))",
                    (worker_id, now + LEASE_SECONDS, now, chosen["id"], now)).rowcount
                if n == 1:
                    _serve(conn, chosen, klass, now)
                    picked = conn.execute("SELECT * FROM jobs WHERE id=?", (chosen["id"],)).fetchone()
                    break
            if picked is not None:
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


def prune_tenants(db_path=None) -> int:
    """Forget fairness rows unserved for TENANT_IDLE_DAYS (SCALE-4): such a
    tenant re-enters at the system virtual time with a full bucket, exactly
    as if the row were there. The class clocks are kept."""
    conn = get_conn(db_path)
    try:
        n = conn.execute("DELETE FROM job_tenants WHERE tenant_key<>? AND served_at IS NOT NULL AND served_at<?",
                         (SYSTEM_TENANT, time.time() - TENANT_IDLE_DAYS * 86400)).rowcount
        conn.commit()
        return n or 0
    finally:
        conn.close()


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
    appended to its `stages`. A no-op outside a queue job. Raises only
    LeaseLost — a stage boundary is where a row this process no longer
    holds stops (R1-05); a bookkeeping failure never raises."""
    job = _CURRENT.get()
    if job is None:
        return False
    raise_if_lost(job)
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
            # Only while this consumer holds it: a lost copy's stage never
            # overwrites what its reclaimer reached.
            owner = job.get("worker_id")
            conn.execute("UPDATE jobs SET stage_checkpoint=? WHERE id=? AND state='running'"
                         + (" AND lease_owner=?" if owner else ""),
                         (json.dumps(cp, default=str), int(job["id"])) + ((owner,) if owner else ()))
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
                         "lease_until=NULL, phase=NULL WHERE id=? AND lease_owner=? AND state='running'",
                         (json.dumps(result, default=str) if result is not None else None, time.time(),
                          int(job_id), worker_id)).rowcount
        conn.commit()
        return n == 1
    finally:
        conn.close()
        _slot_freed()


def fail(job_id, worker_id, error, retry=True, db_path=None) -> str:
    """A try that raised: queued again after RETRY_BACKOFF while tries are
    left (and `retry`), else failed. Returns the new state. An error that
    carries `retry_after` seconds (a provider's Retry-After, an open
    breaker — rpower.RPowerTryLater) waits at least that long instead.

    Such a wait is the provider's, not the row's failure (blind re-audit
    10/20/26 R3-13): on a row with a deadline it does not use up a try — a
    two-try POS archive row died in any outage longer than two breaker
    cooldowns — and the deadline, not the attempt count, bounds it (a row
    still queued past it expires)."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT attempts, max_attempts, deadline FROM jobs WHERE id=?",
                           (int(job_id),)).fetchone()
        if row is None:
            return "gone"
        try:
            told = float(getattr(error, "retry_after", 0) or 0)
        except (TypeError, ValueError):
            told = 0.0
        waited_out = (retry and told > 0 and row["deadline"] is not None
                      and time.time() + min(told, 6 * 3600.0) < float(row["deadline"]))
        again = retry and (waited_out or int(row["attempts"] or 0) < int(row["max_attempts"] or 1))
        if again:
            wait = RETRY_BACKOFF[max(1, min(int(row["attempts"] or 1), len(RETRY_BACKOFF))) - 1]
            wait = max(wait, min(told, 6 * 3600.0))
            conn.execute("UPDATE jobs SET state='queued', not_before=?, error=?, lease_owner=NULL, lease_until=NULL, "
                         "phase=NULL, attempts=CASE WHEN ? = 1 AND attempts > 0 THEN attempts - 1 ELSE attempts END "
                         "WHERE id=? AND lease_owner=?",
                         (time.time() + wait, str(error)[:500], 1 if waited_out else 0, int(job_id), worker_id))
        else:
            conn.execute("UPDATE jobs SET state='failed', error=?, finished_at=?, lease_owner=NULL, lease_until=NULL, "
                         "phase=NULL WHERE id=? AND lease_owner=?",
                         (str(error)[:500], time.time(), int(job_id), worker_id))
        conn.commit()
        return "queued" if again else "failed"
    finally:
        conn.close()
        _slot_freed()


def release(job_id, worker_id, db_path=None) -> bool:
    """Hand a running row back (a draining consumer): queued now, its lease
    cleared, this try not counted against its attempts — the next consumer
    resumes it from its checkpoint at once."""
    conn = get_conn(db_path)
    try:
        n = conn.execute("UPDATE jobs SET state='queued', lease_owner=NULL, lease_until=NULL, not_before=0, "
                         "phase=NULL, attempts=MAX(0, attempts-1) WHERE id=? AND lease_owner=? AND state='running'",
                         (int(job_id), worker_id)).rowcount
        conn.commit()
        return n == 1
    finally:
        conn.close()
        _slot_freed()


# ── a model answer is not code work (blind re-audit 10/11/26 SCALE-1) ──────
#
# An owner's press held one of the generation group's two slots for the
# whole model stream — 4 to 8 minutes of waiting on the network at the
# measured 120 tok/s with 26-55k thinking tokens — so the platform managed
# about 15-20 presses an hour, whatever the CPU was doing. A row now gives
# its CPU slot back while it waits on a model answer (`phase` 'model';
# ai_utils.create_with_retry enters model_wait around every attempt, so a
# generation's draft calls, its critic and its note readings all do) and
# takes one back before its code runs on ('await' while it waits: a row
# coming back goes ahead of any new claim, a press ahead of a draft). The
# rows in flight are bounded by max_running; the CPU slots by
# group_concurrency. Time a generation waits for its slot back is excused
# from its clock (SLOT_WAIT_HOOKS — schedule_engine.GenerationClock.excuse).

SLOT_WAIT_HOOKS = []
_slot_cv = threading.Condition()
_wake = threading.Event()


def _slot_freed():
    """A CPU slot may have come free in this process: wake the rows waiting
    for one, and the worker loops waiting to claim."""
    with _slot_cv:
        _slot_cv.notify_all()
    _wake.set()


class _Phase:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = 0


def _set_phase(job, phase, db_path=None) -> bool:
    conn = get_conn(db_path)
    try:
        n = conn.execute("UPDATE jobs SET phase=? WHERE id=? AND lease_owner=? AND state='running'",
                         (phase, int(job["id"]), job["worker_id"])).rowcount
        conn.commit()
        return n == 1
    finally:
        conn.close()


def _take_slot(job, db_path=None) -> bool:
    """One try at taking a CPU slot back: True when this row holds one now
    (or no longer holds its lease — nothing to wait for)."""
    grp, klass, now = job.get("grp"), job.get("priority_class"), time.time()
    conn = get_conn(db_path)
    try:
        db_dialect.begin_write(conn, "jobs", "claim", grp)     # a slot is the group's, as in claim
        mine = conn.execute("SELECT phase FROM jobs WHERE id=? AND lease_owner=? AND state='running'",
                            (int(job["id"]), job["worker_id"])).fetchone()
        if mine is None:
            conn.rollback()
            return True
        rows = conn.execute("SELECT id, priority_class, phase, parked_until FROM jobs WHERE grp=? "
                            "AND state='running' AND lease_until>?", (grp, now)).fetchall()
        busy = sum(1 for r in rows if not r["phase"] and not (r["parked_until"] and r["parked_until"] > now))
        # A press coming back goes ahead of a draft coming back.
        ahead = klass != "interactive" and any(r["phase"] == "await" and r["priority_class"] == "interactive"
                                               and r["id"] != int(job["id"]) for r in rows)
        if busy < group_concurrency(grp) and not ahead:
            conn.execute("UPDATE jobs SET phase=NULL WHERE id=?", (int(job["id"]),))
            conn.commit()
            return True
        conn.rollback()
        return False
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _reacquire(job, db_path=None) -> float:
    """Wait for a CPU slot of the row's group (phase 'await' meanwhile, so
    no new claim takes the slot first). Returns the seconds waited. A
    draining consumer does not wait: its rows are being handed back."""
    started = time.time()
    try:
        _set_phase(job, "await", db_path=db_path)
    except Exception as e:
        log.warning("job %s: phase not recorded: %s", job.get("id"), e)
        return 0.0
    while not _draining.is_set():
        try:
            if _take_slot(job, db_path=db_path):
                break
        except Exception as e:
            log.warning("job %s: slot not taken back (%s) — running on", job.get("id"), e)
            break
        with _slot_cv:
            _slot_cv.wait(min(1.0, POLL_SECONDS))
    return time.time() - started


@contextlib.contextmanager
def model_wait(db_path=None):
    """Around a wait on a model answer inside a queue job: the row's CPU
    slot is given back for the wait (another row is woken to use it) and
    taken back after it. Parallel calls of one job share one wait: the slot
    goes at the first and comes back after the last. Outside a queue job,
    nothing. Never fails the call it wraps over bookkeeping."""
    job = _CURRENT.get()
    if job is None or not job.get("worker_id") or not job.get("grp"):
        yield
        return
    # A row this process no longer holds pays for no model answer (R1-05):
    # its reclaimer is paying for the same one.
    raise_if_lost(job)
    ph = getattr(job, "_phase", None)
    if ph is None:
        ph = job._phase = _Phase()
    with ph.lock:
        ph.calls += 1
        if ph.calls == 1:
            try:
                if _set_phase(job, "model", db_path=db_path):
                    _slot_freed()
                    kick(job["grp"], db_path=db_path)
            except Exception as e:
                log.warning("job %s: slot not given back for the model wait: %s", job.get("id"), e)
    try:
        yield
    finally:
        waited = 0.0
        with ph.lock:
            ph.calls -= 1
            if ph.calls == 0:
                waited = _reacquire(job, db_path=db_path)
        if waited > 0.5:
            for hook in list(SLOT_WAIT_HOOKS):
                try:
                    hook(waited)
                except Exception as e:
                    log.warning("slot wait hook failed: %s", e)


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


def queue_position(external_id, db_path=None):
    """(state, rows ahead) of the newest row for an async job id: rows ahead
    are the group's rows queued before it in its class or a better one (at
    most 1,000 counted; 0 once it runs) — what a pending poll says and how
    long it waits (blind re-audit 10/20/26 R5-03/R5-08). (None, None) when
    the queue does not run it."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT id, grp, state, priority_class FROM jobs WHERE external_id=? ORDER BY id DESC "
                           "LIMIT 1", (str(external_id),)).fetchone()
        if row is None:
            return None, None
        if row["state"] != "queued":
            return row["state"], 0
        better = [c for c in CLASSES if _CLASS_RANK[c] <= _CLASS_RANK.get(row["priority_class"], 2)]
        n = conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM jobs WHERE grp=? AND state='queued' AND id<? "
                         "AND priority_class IN (" + ",".join("?" * len(better)) + ") LIMIT 1000) q",
                         (row["grp"], int(row["id"]), *better)).fetchone()[0]
        return "queued", int(n or 0)
    except Exception as e:
        log.warning("queue position unread for %s: %s", external_id, e)
        return None, None
    finally:
        conn.close()


def poll_after_ms(state, ahead) -> int:
    """How long a pending poll waits before asking again: 2 s while it runs,
    longer the more rows are ahead of it (at most 15 s)."""
    if state != "queued" or not ahead:
        return 2000
    return int(min(15000, 2000 + 100 * int(ahead)))


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


def raise_if_lease_lost() -> None:
    """LeaseLost when the row this thread runs is no longer this process's
    (the name the generation's stage boundaries call; raise_if_lost)."""
    raise_if_lost()


class _LeaseKeeper(threading.Thread):
    """Renews a running row's lease every RENEW_SECONDS. The lease is LOST
    (R1-05) when a renewal finds another holder — or none — or when no
    renewal has succeeded for LEASE_SECONDS: the database then sees an
    expired lease any consumer may resume. Either way the row is marked
    (_lost) and its handler stops at its next boundary (LeaseLost)."""

    def __init__(self, job):
        super().__init__(daemon=True, name=f"job-lease-{job['id']}")
        self.job, self.stop = job, threading.Event()
        self.lost = False

    def _lose(self, why):
        self.lost = True
        self.job["lease_lost"] = True
        _mark_lost(self.job["id"])
        log.error("job %s (%s): lease lost — %s; it stops at its next stage boundary",
                  self.job.get("id"), self.job.get("kind"), why)

    def run(self):
        last_ok = time.monotonic()
        while not self.stop.wait(RENEW_SECONDS):
            try:
                if not renew(self.job["id"], self.job["worker_id"]):
                    self._lose("another consumer holds it, or it was handed back")
                    return
                last_ok = time.monotonic()
            except Exception as e:
                log.warning("lease renewal failed for job %s: %s", self.job["id"], e)
                if time.monotonic() - last_ok >= LEASE_SECONDS:
                    self._lose(f"no renewal for {LEASE_SECONDS}s")
                    return


def run_one(job):
    """Run a claimed row through its handler: done, or failed / queued
    again. The handler's return is the row's result. Returns it."""
    with _held_lock:
        _held[int(job["id"])] = job
    with _lost_lock:
        _lost.discard(int(job["id"]))        # a fresh claim of a row an earlier copy lost
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
        if not complete(job["id"], job["worker_id"], result if isinstance(result, (dict, list)) else None):
            log.warning("job %s (%s) finished after its lease was lost — its result is its reclaimer's to "
                        "write", job.get("id"), job.get("kind"))
        _settle_local(job["id"], result)
        return result
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(e, LeaseLost) or keeper.lost or lease_lost(job):
            # Not this process's row any more (R1-05 / R5-12): neither failed
            # nor retried here — the consumer holding it (or the next claim of
            # its expired lease) resumes it from its checkpoint. A caller in
            # this process waiting on it is answered.
            log.warning("job %s (%s) stopped: %s", job.get("id"), job.get("kind"), e)
            _settle_local(job["id"], error=e)
            return None
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
        with _lost_lock:
            _lost.discard(int(job["id"]))


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
    if mode() == "inprocess":
        return True
    return not worker_alive(db_path)


def kick(grp, db_path=None, force=False):
    """Start a drain of `grp` on this process's pool when it consumes and a
    pool thread is free. Returns the drain's Future, or None."""
    if _draining.is_set() or not (force or this_process_consumes(db_path)):
        return None
    with _pools_lock:
        # As many threads as rows may be in flight: a row waiting on a model
        # answer (model_wait) or parked on the gateway's next minute (parked,
        # SCALE-25) keeps its thread but not its CPU slot, so a drain may
        # start beside it; claim() holds the group to its CPU slots and to
        # max_running.
        size = max_running(grp)
        pool = _pools.get(grp)
        if pool is None or pool._max_workers != size:
            pool = _pools[grp] = concurrent.futures.ThreadPoolExecutor(max_workers=size,
                                                                       thread_name_prefix=f"queue-{grp}")
        if _active.get(grp, 0) >= size:
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
    # The web row is beaten every minute even while the worker consumes
    # (scale audit 10/19/26 INF-4): it was beaten only here, after this
    # return, so a day after the boot a worker's heartbeat pruned it and a
    # recycled queue worker found no web boot to wait for.
    heartbeat("web", db_path=db_path)
    if not this_process_consumes(db_path):
        out["skipped"] = 1
        return out
    for grp in GROUPS:
        if claimable(grp, db_path=db_path):
            out["attempted"] += 1
            if kick(grp, db_path=db_path) is not None:
                out["ok"] += 1
    try:
        prune_tenants(db_path=db_path)
    except Exception as e:
        log.warning("queue tenants not pruned: %s", e)
    return out


def claimable(grp, db_path=None) -> bool:
    """Whether a claim of `grp` could take a row now (a plain read)."""
    conn = get_conn(db_path)
    try:
        return _maybe_claimable(conn, (grp,), time.time())
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


def rss_mb():
    """This process's resident memory now, in MB (Linux /proc; elsewhere the
    peak, which is what a recycle bound needs anyway). None when unknown."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except Exception:
        pass
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / 1e6 if os.uname().sysname == "Darwin" else peak / 1e3
    except Exception:
        return None


def run_worker(groups=GROUPS, stop=None, threads=None, db_path=None, max_jobs=None,
               max_rss_mb=None, count_groups=None, on_recycle=None) -> str:
    """worker.py's consumer: `threads` per group (max_running by default —
    a row waiting on a model answer keeps its thread, not its CPU slot)
    claiming in a loop, a heartbeat every RENEW_SECONDS, woken by a slot
    coming free here or on POLL_SECONDS. Returns "stopped" when `stop` is
    set (or at shutdown) — or "recycle" once it has run `max_jobs` rows or
    its resident memory passed `max_rss_mb` (blind re-audit 10/11/26
    SCALE-17/SCALE-18: a 250-person generation left 1.4 GB resident for
    good): it stops claiming, lets its running rows finish, and returns, so
    worker.py's supervisor starts a fresh process. `count_groups`: only rows
    of these groups count toward `max_jobs` (worker.py: generations — scale
    audit 10/19/26 A5-F3); None counts every row. `on_recycle` is called once
    when the recycle starts (worker.py tells the supervisor, which starts
    the replacement while this process drains)."""
    stop = stop or threading.Event()
    recycle = threading.Event()
    ran_total = [0]
    count_lock = threading.Lock()
    heartbeat("worker", db_path=db_path)

    def _recycle():
        with count_lock:
            if recycle.is_set():
                return
            recycle.set()
        _wake.set()                 # idle consumers notice the recycle now, not after their poll

        if on_recycle is not None:
            try:
                on_recycle()
            except Exception as e:
                log.warning("on_recycle failed: %s", e)

    def _loop(grp):
        counts = count_groups is None or grp in count_groups
        while not stop.is_set() and not _draining.is_set() and not recycle.is_set():
            try:
                ran = drain((grp,), WORKER_ID, max_jobs=1, db_path=db_path)
            except Exception as e:
                log.error("queue consumer %s failed: %s", grp, e)
                ran = 0
            if ran:
                due = False
                with count_lock:
                    if counts:
                        ran_total[0] += ran
                    due = bool(max_jobs and ran_total[0] >= int(max_jobs))
                if due or (max_rss_mb and (rss_mb() or 0) > float(max_rss_mb)):
                    _recycle()
            else:
                _wake.clear()
                # A recycle set before the clear is seen here; one set after
                # it sets _wake too (_recycle), so the wait ends at once.
                if recycle.is_set() or stop.is_set():
                    break
                _wake.wait(POLL_SECONDS)

    workers = []
    for grp in groups:
        for i in range(threads or max_running(grp)):
            t = threading.Thread(target=_loop, args=(grp,), daemon=True, name=f"queue-worker-{grp}-{i}")
            t.start()
            workers.append(t)
    while not stop.is_set() and not _draining.is_set():
        heartbeat("worker", db_path=db_path)
        if recycle.is_set() and not any(t.is_alive() for t in workers):
            log.info("queue worker recycling after %d job(s), %s MB resident", ran_total[0], rss_mb())
            return "recycle"
        # A heartbeat every RENEW_SECONDS; a stop or a recycle noticed within a second.
        for _tick in range(max(1, int(RENEW_SECONDS))):
            if stop.is_set() or recycle.is_set():
                break
            stop.wait(1.0)
        if recycle.is_set():
            stop.wait(0.2)
    for t in workers:
        t.join(timeout=1)
    return "stopped"


def shutdown(db_path=None) -> int:
    """Drain at exit (a deploy's SIGTERM): stop claiming, hand every row
    this process runs back to the queue so the next process resumes it from
    its checkpoint at once. Returns how many were handed back. Each row is
    marked lost BEFORE it is handed back (R9-13): its handler thread, still
    running until the process exits, stops at its next boundary instead of
    sending or paying beside the next process's resume."""
    _draining.set()
    heartbeat(db_path=db_path)
    with _held_lock:
        held = list(_held.values())
    n = 0
    for job in held:
        _mark_lost(job["id"])
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
    register_shutdown()
    heartbeat("web", db_path=db_path)
    if this_process_consumes(db_path):
        for grp in GROUPS:
            kick(grp, db_path=db_path)


def reset_for_tests():
    """Forget this process's consumer state (pools are left to finish)."""
    _draining.clear()
    with _lost_lock:
        _lost.clear()
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
        # The wait p95 of every class, and what is running in each phase
        # (blind re-audit 10/11/26 SCALE-1/SCALE-28): the autoscale signal.
        by_class = {}
        for r in conn.execute("SELECT priority_class AS k, started_at - enqueued_at AS w FROM jobs "
                              "WHERE started_at IS NOT NULL AND started_at>?", (now - window_seconds,)).fetchall():
            by_class.setdefault(r["k"], []).append(float(r["w"]))
        out["wait_p95_by_class"] = {k: round(sorted(v)[min(len(v) - 1, int(math.ceil(0.95 * len(v))) - 1)], 1)
                                    for k, v in by_class.items() if v}
        out["phases"] = {}
        for r in conn.execute("SELECT grp, COALESCE(phase, 'cpu') AS ph, COUNT(*) AS n FROM jobs "
                              "WHERE state='running' AND lease_until>? GROUP BY grp, COALESCE(phase, 'cpu')",
                              (now,)).fetchall():
            out["phases"].setdefault(r["grp"], {})[r["ph"]] = int(r["n"])
        # Deadline misses per tenant (SCALE-3): who the queue failed to serve
        # in time, not just how many.
        out["expired_by_tenant"] = {r["t"]: int(r["n"]) for r in conn.execute(
            "SELECT tenant_key AS t, COUNT(*) AS n FROM jobs WHERE state='expired' AND finished_at>? "
            "GROUP BY tenant_key ORDER BY COUNT(*) DESC LIMIT 10", (now - window_seconds,)).fetchall()}
        # Per kind (scale audit 10/19/26 INF-6, INF-7): what is waiting, how
        # long the oldest row that MAY run has waited (from its not_before —
        # a row spread to later is not a backlog), and what failed or expired
        # in the last day. The bulk backlog alert reads the groups' figure.
        out["kinds"] = {}

        def _kind(kind, grp):
            return out["kinds"].setdefault(kind, {"grp": grp, "queued": 0, "running": 0,
                                                  "oldest_ready_seconds": None, "failed_24h": 0, "expired_24h": 0})
        for r in conn.execute(
                "SELECT kind, grp, state, COUNT(*) AS n, MIN(CASE WHEN not_before <= ? THEN "
                "(CASE WHEN not_before > enqueued_at THEN not_before ELSE enqueued_at END) END) AS ready "
                "FROM jobs WHERE state IN ('queued','running') GROUP BY kind, grp, state", (now,)).fetchall():
            k = _kind(r["kind"], r["grp"])
            k[r["state"]] += int(r["n"])
            if r["state"] == "queued" and r["ready"] is not None:
                age = round(now - float(r["ready"]), 1)
                k["oldest_ready_seconds"] = max(k["oldest_ready_seconds"] or 0, age)
                g = out["groups"].setdefault(r["grp"], {"queued": 0, "running": 0, "oldest_queued_seconds": None})
                g["oldest_ready_seconds"] = max(g.get("oldest_ready_seconds") or 0, age)
        for r in conn.execute("SELECT kind, state, COUNT(*) AS n FROM jobs WHERE state IN ('failed','expired') "
                              "AND finished_at>? GROUP BY kind, state", (now - 86400,)).fetchall():
            _kind(r["kind"], KINDS.get(r["kind"], ("bulk",))[0])[r["state"] + "_24h"] = int(r["n"])
        # The queue worker process (INF-4): seconds since it last beat its
        # heartbeat (None: none on record), and since this web process booted.
        row = conn.execute("SELECT MAX(heartbeat_at) AS h FROM job_workers WHERE role='worker'").fetchone()
        out["worker_heartbeat_age_seconds"] = round(now - float(row["h"]), 1) if row and row["h"] else None
        row = conn.execute("SELECT MAX(started_at) AS s FROM job_workers WHERE role='web'").fetchone()
        out["web_started_age_seconds"] = round(now - float(row["s"]), 1) if row and row["s"] else None
        return out
    finally:
        conn.close()
