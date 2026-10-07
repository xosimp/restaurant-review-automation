"""ai_orchestrator — runs every AI workflow the same way (AI orchestration
design, owner-approved 10/7/26).

One run of a workflow:

    result = ai_orchestrator.generate(
        "labor_insight", rid,
        attempt=lambda route, notes: <one model call on `route`>,
        check=lambda result: Verdict(...),         # deterministic: rules, schema, facts
        review=..., subject="labor:2026-10-07", unattended=False)

  1. reads the workflow's POLICY (ai_workflows: ladder, triggers, reviewer,
     caps — the code's defaults with the console's override),
  2. calls `attempt` on the first rung of the ladder (the cheapest tier able
     to do the job), under one correlation id so every model call the run
     makes is one ledger group,
  3. checks the result with the workflow's own deterministic `check`,
  4. on a check that failed with a TRIGGER the policy escalates on, and while
     the run is inside its caps (calls, dollars, deadline), calls `attempt`
     again one rung up, with the check's reasons as `notes` for the prompt,
  5. where the policy names a reviewer and no human approves first, asks the
     reviewer (a Haiku rubric, ai_reviewer) before returning; a flag is a
     trigger like any other, one round only,
  6. records the run (ai_runs): every rung tried and why, the verdicts, the
     reviewer's score, cost and latency from the ledger, and later what the
     owner did with it (record_outcome) — the quality signal the learner
     (ai_learning) ranks routes by.

It never calls a model itself: `attempt` does, through ai_utils.
create_with_retry, which keeps every per-call guard (budgets, breakers,
timeouts, retries, the readiness gate, the ledger). The orchestrator adds the
per-RUN guards the platform lacked: a ladder that only climbs on evidence,
one escalation by default, a dollar and call cap per run, and a depth guard —
a workflow may run inside another (Ask's marketing tool) but never deeper
than MAX_DEPTH, so no chain of workflows can loop.

What it returns is the BEST result it saw: the passing one, else the last
attempt with its failing verdict, so the caller serves its own honest
fallback exactly as it did before (fixed copy, "couldn't", 422). Missing data
(DataNotReady) is never retried here: it propagates, and the caller holds.
"""
import contextvars
import json
import logging
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass, field

import ai_workflows as wf

log = logging.getLogger("ai_orchestrator")

MAX_DEPTH = 2
OVERRIDE_CACHE_SECONDS = 60
# Retention for ai_runs / ai_run_requests / ai_route_recommendations lives in
# ops._RETENTION_DAYS, the one registry (#72).

_RUN = contextvars.ContextVar("cavnar_ai_run", default=None)


def _conn(db_path=None):
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


# ── schema (boot only: models.init_db calls init_ai_orchestration) ──────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_runs (
    run_id TEXT PRIMARY KEY,
    parent_run_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT,
    restaurant_id INTEGER,
    workflow TEXT NOT NULL,
    agent TEXT,
    policy_version TEXT,
    overridden INTEGER DEFAULT 0,
    subject TEXT,
    "trigger" TEXT,
    unattended INTEGER DEFAULT 0,
    start_tier TEXT,
    final_tier TEXT,
    final_model TEXT,
    attempts INTEGER DEFAULT 0,
    escalations INTEGER DEFAULT 0,
    steps_json TEXT,
    status TEXT,
    verdict TEXT,
    reasons TEXT,
    reviewer TEXT,
    reviewer_score REAL,
    reviewer_notes TEXT,
    latency_ms INTEGER,
    cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    context_json TEXT,
    outcome TEXT,
    outcome_quality REAL,
    outcome_detail TEXT,
    outcome_at TEXT,
    shadow_of TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_runs_wf_created ON ai_runs(workflow, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_runs_subject ON ai_runs(workflow, restaurant_id, subject);
CREATE INDEX IF NOT EXISTS idx_ai_runs_created ON ai_runs(created_at);

CREATE TABLE IF NOT EXISTS ai_route_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT (datetime('now')),
    workflow TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT,
    proposed_json TEXT,
    evidence_json TEXT,
    status TEXT DEFAULT 'open',
    decided_by TEXT,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_route_recs_status ON ai_route_recommendations(status, workflow);
CREATE INDEX IF NOT EXISTS idx_ai_route_recs_created ON ai_route_recommendations(created_at);

CREATE TABLE IF NOT EXISTS ai_run_requests (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    workflow TEXT,
    restaurant_id INTEGER,
    request_z BLOB,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_ai_run_requests_wf ON ai_run_requests(workflow, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_run_requests_created ON ai_run_requests(created_at);

CREATE TABLE IF NOT EXISTS ai_route_overrides (
    workflow TEXT PRIMARY KEY,
    override_json TEXT NOT NULL,
    previous_json TEXT,
    updated_by TEXT,
    updated_at TEXT DEFAULT (datetime('now')),
    reason TEXT
);
"""


def init_ai_orchestration(db_path=None):
    conn = _conn(db_path)
    try:
        conn.executescript(_SCHEMA)
        # Run cost is read from the ledger by correlation id (one run, one
        # id). ai_usage is created by ai_utils at boot just before this; on
        # a database where it is not there yet the index waits for the next boot.
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_correlation ON ai_usage(correlation_id)")
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()
    _OVERRIDES.clear()


# ── overrides (the console's edits) ────────────────────────────────────────

_OVERRIDES = {}
_OVERRIDES_LOCK = threading.Lock()


def overrides(db_path=None) -> dict:
    """{workflow: override dict}, cached OVERRIDE_CACHE_SECONDS. Never raises
    (an unreadable table means no overrides — the code's defaults run)."""
    key = db_path or "default"
    now = time.time()
    with _OVERRIDES_LOCK:
        hit = _OVERRIDES.get(key)
        if hit and now - hit[0] < OVERRIDE_CACHE_SECONDS:
            return hit[1]
    out = {}
    try:
        conn = _conn(db_path)
        try:
            for r in conn.execute("SELECT workflow, override_json FROM ai_route_overrides").fetchall():
                try:
                    out[r["workflow"]] = json.loads(r["override_json"] or "{}") or {}
                except (TypeError, ValueError):
                    continue
        finally:
            conn.close()
    except Exception as e:
        log.debug("ai_route_overrides unreadable: %s", e)
    with _OVERRIDES_LOCK:
        _OVERRIDES[key] = (now, out)
    return out


def set_override(workflow, override, actor=None, reason=None, db_path=None) -> dict:
    """Store the console's override for `workflow` (validated against the
    default policy — ValueError on a bad field), keeping the one it replaces
    so a revert is one step. Returns {"before", "after"} for the audit.
    `override` None or {} removes it."""
    base = wf.POLICIES.get(workflow)
    if base is None:
        raise ValueError(f"unknown workflow {workflow!r}")
    if override:
        wf.apply_override(base, override)       # raises on anything invalid
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT override_json FROM ai_route_overrides WHERE workflow=?", (workflow,)).fetchone()
        before = json.loads(row["override_json"]) if row and row["override_json"] else None
        if override:
            conn.execute(
                "INSERT INTO ai_route_overrides (workflow, override_json, previous_json, updated_by, updated_at, reason) "
                "VALUES (?,?,?,?,datetime('now'),?) ON CONFLICT(workflow) DO UPDATE SET "
                "override_json=excluded.override_json, previous_json=ai_route_overrides.override_json, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at, reason=excluded.reason",
                (workflow, json.dumps(override, sort_keys=True), json.dumps(before) if before else None,
                 str(actor or "")[:80] or None, (reason or "")[:300] or None))
        else:
            conn.execute("DELETE FROM ai_route_overrides WHERE workflow=?", (workflow,))
        conn.commit()
    finally:
        conn.close()
    with _OVERRIDES_LOCK:
        _OVERRIDES.clear()
    return {"before": before, "after": override or None}


def override_rows(db_path=None) -> list:
    conn = _conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT workflow, override_json, previous_json, updated_by, updated_at, reason "
            "FROM ai_route_overrides ORDER BY workflow").fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


# ── a run ──────────────────────────────────────────────────────────────────

@dataclass
class Verdict:
    """A deterministic check's answer about one attempt. `trigger` names what
    failed (a key of ai_workflows.TRIGGERS, or one of NEVER_TRIGGERS, which
    never escalates); `reasons` are short, owner-safe sentences handed to the
    next attempt as notes. `score` (0-1) is optional — a reviewer's."""
    ok: bool = True
    trigger: str | None = None
    reasons: list = field(default_factory=list)
    score: float | None = None
    label: str | None = None      # the validation engine's verdict word, when there is one

    @classmethod
    def passed(cls, label="pass", score=None):
        return cls(ok=True, label=label, score=score)

    @classmethod
    def failed(cls, trigger, *reasons, label=None):
        return cls(ok=False, trigger=trigger, reasons=[str(r) for r in reasons if r], label=label)


@dataclass
class RunResult:
    run_id: str
    workflow: str
    result: object = None
    verdict: Verdict = field(default_factory=Verdict)
    tier: str | None = None
    model: str | None = None
    attempts: int = 0
    escalations: int = 0
    status: str = "ok"          # ok | failed | held | capped | error | refused

    @property
    def ok(self):
        return self.status == "ok" and bool(self.verdict.ok)


class RunRefused(RuntimeError):
    """A workflow started deeper than MAX_DEPTH inside other workflows."""


def current_run() -> dict | None:
    """The run in force on this thread (a copy), or None."""
    cur = _RUN.get()
    return dict(cur) if cur else None


def new_run_id(workflow):
    return f"run:{workflow[:24]}:{uuid.uuid4().hex[:12]}"


def _spent(run_id, db_path=None):
    """(cost_usd, input_tokens, output_tokens, calls) the ledger holds for
    this run's correlation id."""
    try:
        conn = _conn(db_path)
        try:
            r = conn.execute("SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(input_tokens),0), "
                             "COALESCE(SUM(output_tokens),0), COUNT(*) FROM ai_usage WHERE correlation_id=?",
                             (run_id,)).fetchone()
        finally:
            conn.close()
        return float(r[0] or 0), int(r[1] or 0), int(r[2] or 0), int(r[3] or 0)
    except Exception:
        return 0.0, 0, 0, 0


def _record(rr: RunResult, pol, *, restaurant_id, subject, unattended, start_tier, steps, started,
            reviewer, reviewer_score, reviewer_notes, context, parent, trigger, db_path, shadow_of=None):
    cost, tin, tout, _calls = _spent(rr.run_id, db_path)
    try:
        conn = _conn(db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO ai_runs (run_id, parent_run_id, restaurant_id, workflow, agent, "
                "policy_version, overridden, subject, \"trigger\", unattended, start_tier, final_tier, final_model, "
                "attempts, escalations, steps_json, status, verdict, reasons, reviewer, reviewer_score, "
                "reviewer_notes, latency_ms, cost_usd, input_tokens, output_tokens, context_json, finished_at, "
                "shadow_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),?)",
                (rr.run_id, parent, restaurant_id, rr.workflow, pol.agent, wf.POLICY_VERSION,
                 1 if rr.workflow in overrides(db_path) else 0, (str(subject)[:120] if subject else None),
                 trigger, 1 if unattended else 0, start_tier, rr.tier, rr.model, rr.attempts, rr.escalations,
                 json.dumps(steps)[:4000], rr.status, rr.verdict.label or ("pass" if rr.verdict.ok else "fail"),
                 json.dumps(rr.verdict.reasons[:6])[:1000] if rr.verdict.reasons else None,
                 reviewer, reviewer_score, (reviewer_notes or "")[:500] or None,
                 int((time.time() - started) * 1000), round(cost, 6), tin, tout,
                 json.dumps(context)[:2000] if context else None, shadow_of))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("ai_runs row not written (%s): %s", rr.workflow, e)


def _trigger_now():
    try:
        import ai_utils
        return ai_utils._attribution()[0]
    except Exception:
        return None


def generate(workflow, restaurant_id, attempt, check=None, *, review=None, start=None, subject=None,
             unattended=False, deadline=None, context=None, db_path=None) -> RunResult:
    """Run `workflow` once (module docstring). `attempt(route, notes)` makes
    one model call on `route` (an ai_workflows.Route: route.apply(kwargs) sets
    the model and its thinking) and returns the caller's result; `notes` is a
    list of reasons the previous rung failed ([] on the first). `check(result)`
    returns a Verdict. `review(result, mode)` returns a Verdict for a reviewer
    mode ("haiku_gate"/"haiku_shadow"); omitted, no model reviewer runs.
    `start` is the rung the workflow's own pre-router chose (a tier on its
    ladder). `deadline` (time.time()) bounds the whole run. Exceptions from
    `attempt` propagate after the run is recorded — the caller's existing
    error handling stays what it was."""
    pol = wf.policy(workflow, db_path)
    parent = _RUN.get()
    depth = (parent or {}).get("depth", 0) + 1
    run_id = new_run_id(workflow)
    started = time.time()
    caps = pol.caps
    if deadline is None:
        deadline = started + float(caps.seconds)
    else:
        deadline = min(float(deadline), started + float(caps.seconds)) if pol.delivery == "interactive" \
            else float(deadline)
    rr = RunResult(run_id=run_id, workflow=workflow)
    steps = []
    reviewer_mode = (pol.reviewer_unattended or pol.reviewer) if unattended else pol.reviewer
    reviewer_score = reviewer_notes = None
    trigger = _trigger_now()
    if depth > MAX_DEPTH:
        rr.status = "refused"
        _record(rr, pol, restaurant_id=restaurant_id, subject=subject, unattended=unattended, start_tier=start,
                steps=[{"refused": f"depth {depth} > {MAX_DEPTH}"}], started=started, reviewer=None,
                reviewer_score=None, reviewer_notes=None, context=context,
                parent=(parent or {}).get("run_id"), trigger=trigger, db_path=db_path)
        raise RunRefused(f"{workflow} would run {depth} workflows deep (max {MAX_DEPTH})")
    n_rungs = wf.rungs(pol, start)
    # The rung a pre-router moved the run to; None when it starts at the bottom
    # (the learner's escalation rate reads runs that started there).
    start = start if start and start in pol.ladder and start != pol.ladder[0] else None
    max_steps = max(1, min(n_rungs, 1 + int(pol.max_escalations)))
    token = _RUN.set({"run_id": run_id, "workflow": workflow, "depth": depth,
                      "restaurant_id": restaurant_id, "keep_request": _sampled_for_replay(workflow),
                      "seq": 0})
    import ai_utils
    best = None
    notes = []
    error = None
    try:
        with ai_utils.ai_context(correlation_id=run_id, restaurant_id=restaurant_id):
            for step in range(max_steps):
                if step:
                    cost, _i, _o, calls = _spent(run_id, db_path)
                    if calls >= caps.calls or cost >= caps.usd or time.time() >= deadline:
                        rr.status = "capped"
                        steps.append({"capped": {"calls": calls, "usd": round(cost, 4),
                                                 "late": time.time() >= deadline}})
                        break
                route = wf.route_for(pol, step, start)
                t0 = time.time()
                try:
                    result = attempt(route, list(notes))
                except Exception as e:
                    steps.append({"tier": route.tier, "model": route.model, "error": type(e).__name__,
                                  "ms": int((time.time() - t0) * 1000)})
                    rr.attempts += 1
                    rr.tier, rr.model = route.tier, route.model
                    error = e
                    break
                rr.attempts += 1
                rr.tier, rr.model = route.tier, route.model
                v = check(result) if check else Verdict.passed()
                if not isinstance(v, Verdict):
                    v = Verdict.passed() if v in (True, None) else Verdict.failed("validation_refuse", str(v))
                # The reviewer reads only what the rules engine passed.
                if v.ok and reviewer_mode == "haiku_gate" and review is not None:
                    try:
                        rv = review(result, "haiku_gate")
                    except Exception as e:
                        log.warning("reviewer failed (%s): %s", workflow, e)
                        rv = None
                    if isinstance(rv, Verdict):
                        reviewer_score, reviewer_notes = rv.score, "; ".join(rv.reasons)[:500] or None
                        if not rv.ok:
                            v = Verdict.failed("reviewer_flag", *rv.reasons, label="reviewer_flag")
                steps.append({"tier": route.tier, "model": route.model, "ok": bool(v.ok),
                              "trigger": v.trigger, "label": v.label, "ms": int((time.time() - t0) * 1000)})
                # The newest attempt is the one returned: it carries the
                # notes of every rung before it.
                best = (result, v)
                if v.ok:
                    break
                escalate = (v.trigger in pol.escalate_on and v.trigger not in wf.NEVER_TRIGGERS
                            and step + 1 < max_steps)
                if not escalate:
                    break
                rr.escalations += 1
                notes = list(v.reasons) or [wf.TRIGGERS.get(v.trigger, "the last attempt failed its check")]
    finally:
        _RUN.reset(token)
    if best is not None:
        rr.result, rr.verdict = best
        if rr.verdict.ok:
            rr.status = "ok"
        elif rr.status != "capped":
            rr.status = "failed"
    if error is not None and best is None:
        # Nothing came back: missing data is a hold (the caller says what is
        # missing), anything else an error the caller handles as before.
        # An escalated attempt that raised keeps the earlier attempt's result.
        held = isinstance(error, getattr(ai_utils, "DataNotReady", ()))
        rr.status = "held" if held else "error"
        rr.verdict = Verdict.failed("data_missing" if held else None, type(error).__name__)
    _record(rr, pol, restaurant_id=restaurant_id, subject=subject, unattended=unattended, start_tier=start,
            steps=steps, started=started, reviewer=reviewer_mode if reviewer_mode not in ("rules", "none") else None,
            reviewer_score=reviewer_score, reviewer_notes=reviewer_notes, context=context,
            parent=(parent or {}).get("run_id"), trigger=trigger, db_path=db_path)
    # A shadow reviewer scores a sample of passing runs off the request path.
    if (rr.ok and review is not None and reviewer_mode in ("owner", "rules", "haiku_shadow")
            and pol.shadow_rate > 0 and random.random() < pol.shadow_rate):
        _shadow_review(run_id, rr.result, review, db_path)
    if error is not None and best is None:
        raise error
    return rr


# ── kept requests (the learner's shadow arms replay them) ───────────────────
#
# A sample of runs keeps each model request it made — redacted like the
# ai_calls trace, compressed — so a candidate route can be replayed on real
# inputs later (ai_learning.shadow_arms, through Message Batches at half
# price) without asking anyone. The schedule keeps its own full store
# (schedule_model_calls); a tool loop cannot be replayed one call at a time.
REPLAY_SAMPLE_RATE = float(os.getenv("AI_REPLAY_SAMPLE_RATE", "0.05"))
NOT_REPLAYABLE = ("labor_schedule", "ask_cavnar", "weekly_plan", "ai_review", "invoice_extract",
                  "recipe_photo")


def _sampled_for_replay(workflow):
    return workflow not in NOT_REPLAYABLE and REPLAY_SAMPLE_RATE > 0 and random.random() < REPLAY_SAMPLE_RATE


def _redacted_request(kwargs):
    import ai_utils
    keep = {k: kwargs[k] for k in ("model", "max_tokens", "system", "messages", "output_config", "tools",
                                   "tool_choice") if k in kwargs}

    def scrub(v):
        if isinstance(v, str):
            return ai_utils.redact_pii(v)
        if isinstance(v, list):
            return [scrub(x) for x in v]
        if isinstance(v, dict):
            return {k: (scrub(x) if k in ("text", "content", "system", "messages") or isinstance(x, (list, dict))
                        else x) for k, x in v.items()}
        return v
    keep["messages"] = scrub(keep.get("messages") or [])
    if isinstance(keep.get("system"), (str, list)):
        keep["system"] = scrub(keep["system"])
    return keep


def keep_request(kwargs, db_path=None):
    """Called by ai_utils.create_with_retry before a call is sent: when the
    run in force was sampled for replay, store the request. Never raises."""
    cur = _RUN.get()
    if not cur or not cur.get("keep_request"):
        return
    try:
        import zlib
        cur["seq"] = int(cur.get("seq") or 0) + 1
        blob = zlib.compress(json.dumps(_redacted_request(kwargs), default=str).encode("utf-8", "replace"))
        conn = _conn(db_path)
        try:
            conn.execute("INSERT OR REPLACE INTO ai_run_requests (run_id, seq, workflow, restaurant_id, request_z) "
                         "VALUES (?,?,?,?,?)", (cur["run_id"], cur["seq"], cur["workflow"],
                                               cur.get("restaurant_id"), blob))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug("request not kept (%s): %s", cur.get("workflow"), e)


def kept_requests(workflow, limit=20, db_path=None) -> list:
    """[(run_id, request dict)] — the newest kept first-attempt requests."""
    import zlib
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT run_id, restaurant_id, request_z FROM ai_run_requests WHERE workflow=? AND seq=1 "
                            "ORDER BY created_at DESC LIMIT ?", (workflow, int(limit))).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            out.append((r["run_id"], r["restaurant_id"], json.loads(zlib.decompress(r["request_z"]).decode("utf-8"))))
        except Exception:
            continue
    return out


_SHADOW_POOL = None
_SHADOW_LOCK = threading.Lock()


def _shadow_review(run_id, result, review, db_path):
    """Score a passing run with the reviewer in the background and write the
    score on its row — never on the request path, never shown to anyone."""
    global _SHADOW_POOL
    import ai_utils
    if os.getenv("AI_SHADOW_REVIEW", "1") == "0":
        return
    with _SHADOW_LOCK:
        if _SHADOW_POOL is None:
            from concurrent.futures import ThreadPoolExecutor
            _SHADOW_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-shadow")

    def _run():
        try:
            v = review(result, "haiku_shadow")
        except Exception as e:
            log.info("shadow review failed (%s): %s", run_id, e)
            return
        if not isinstance(v, Verdict):
            return
        try:
            conn = _conn(db_path)
            try:
                conn.execute("UPDATE ai_runs SET reviewer='haiku_shadow', reviewer_score=?, reviewer_notes=? "
                             "WHERE run_id=?", (v.score, "; ".join(v.reasons)[:500] or None, run_id))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            log.info("shadow score not stored (%s): %s", run_id, e)
    try:
        _SHADOW_POOL.submit(ai_utils.context_runner(ai_utils.attributed(_run)))
    except Exception as e:
        log.info("shadow review not queued: %s", e)


# ── outcomes: what the owner did with it ────────────────────────────────────

OUTCOMES = {
    "accepted": 1.0,     # used as written (approved, published, sent, kept)
    "edited": None,      # used after an edit — quality from the edit's size
    "rejected": 0.0,     # discarded, regenerated, rewritten from scratch
    "corrected": 0.2,    # the owner said it was wrong
    "ignored": None,     # never opened or acted on — no quality signal
}


def edit_quality(before: str, after: str) -> float:
    """1.0 for an untouched text down to 0.0 for a rewrite: the share of the
    draft that survived the owner's edit (difflib ratio)."""
    import difflib
    a, b = str(before or ""), str(after or "")
    if not a and not b:
        return 1.0
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 3)


def record_outcome(workflow, restaurant_id, subject, outcome, quality=None, detail=None, run_id=None,
                   db_path=None) -> bool:
    """File what the owner did with a workflow's output against the run that
    wrote it — the newest run for (workflow, restaurant, subject) when no
    run id is given. Never raises; False when no run matched."""
    if outcome not in OUTCOMES:
        log.debug("unknown outcome %r", outcome)
        return False
    q = quality if quality is not None else OUTCOMES[outcome]
    try:
        conn = _conn(db_path)
        try:
            if not run_id:
                row = conn.execute("SELECT run_id FROM ai_runs WHERE workflow=? AND restaurant_id IS ? AND subject=? "
                                   "AND shadow_of IS NULL ORDER BY created_at DESC LIMIT 1",
                                   (workflow, restaurant_id, str(subject)[:120])).fetchone()
                run_id = row["run_id"] if row else None
            if not run_id:
                return False
            cur = conn.execute("UPDATE ai_runs SET outcome=?, outcome_quality=?, outcome_detail=?, "
                               "outcome_at=datetime('now') WHERE run_id=?",
                               (outcome, q, (str(detail)[:300] if detail else None), run_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        log.warning("outcome not recorded (%s): %s", workflow, e)
        return False


def subject_run(workflow, restaurant_id, subject, db_path=None):
    """The newest run for a subject, as a dict, or None."""
    try:
        conn = _conn(db_path)
        try:
            row = conn.execute("SELECT * FROM ai_runs WHERE workflow=? AND restaurant_id IS ? AND subject=? "
                               "ORDER BY created_at DESC LIMIT 1",
                               (workflow, restaurant_id, str(subject)[:120])).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception:
        return None


def attach(run_id, subject=None, context=None, db_path=None) -> bool:
    """Name a finished run's subject after the fact — a draft whose id
    exists only once the caller stores it (a marketing draft's draft_ref) —
    and/or merge `context` into its context_json (a task-sheet starter's
    offered lines, read back when the owner adds them). Never raises."""
    if not run_id or (subject is None and not context):
        return False
    try:
        conn = _conn(db_path)
        try:
            if subject is not None:
                conn.execute("UPDATE ai_runs SET subject=? WHERE run_id=?", (str(subject)[:120], run_id))
            if context:
                row = conn.execute("SELECT context_json FROM ai_runs WHERE run_id=?", (run_id,)).fetchone()
                try:
                    cur = json.loads(row["context_json"]) if row and row["context_json"] else {}
                except (TypeError, ValueError):
                    cur = {}
                cur = dict(cur if isinstance(cur, dict) else {}, **context)
                conn.execute("UPDATE ai_runs SET context_json=? WHERE run_id=?", (json.dumps(cur)[:2000], run_id))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        log.info("run not annotated (%s): %s", run_id, e)
        return False


def run_context(workflow, restaurant_id, subject, db_path=None) -> dict:
    """The context_json of the subject's newest run, as a dict ({} when none)."""
    row = subject_run(workflow, restaurant_id, subject, db_path=db_path) or {}
    try:
        out = json.loads(row.get("context_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def record_review(workflow, restaurant_id, subject, verdict, mode="haiku_gate", db_path=None) -> bool:
    """File a reviewer's verdict given AFTER the run, on the subject's newest
    run — the auto-approve gate reads a reply drafted hours earlier, just
    before it would post it unread. Never raises; False when no run matched."""
    if not isinstance(verdict, Verdict):
        return False
    row = subject_run(workflow, restaurant_id, subject, db_path=db_path)
    if not row:
        return False
    try:
        conn = _conn(db_path)
        try:
            conn.execute("UPDATE ai_runs SET reviewer=?, reviewer_score=?, reviewer_notes=? WHERE run_id=?",
                         (mode, verdict.score, ("; ".join(verdict.reasons)[:500] or None), row["run_id"]))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        log.info("review not filed (%s): %s", workflow, e)
        return False


def verdict_from_validation(v) -> Verdict:
    """A Verdict from a response_validation verdict object or word: refuse →
    validation_refuse, withhold → validation_withhold, pass / caveat → ok."""
    word = getattr(v, "verdict", None) or getattr(v, "decision", None) or (v if isinstance(v, str) else None)
    reasons = []
    for attr in ("reasons", "details", "hits"):
        val = getattr(v, attr, None)
        if val:
            reasons = [str(x) for x in (val if isinstance(val, (list, tuple)) else [val])][:4]
            break
    if word in ("refuse", "refused"):
        return Verdict.failed("validation_refuse", *reasons, label="refuse")
    if word in ("withhold", "withheld"):
        return Verdict.failed("validation_withhold", *reasons, label="withhold")
    return Verdict.passed(label=word or "pass")
