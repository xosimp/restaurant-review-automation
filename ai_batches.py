"""
ai_batches.py — model calls nobody waits on, sent through Anthropic's
Message Batches API at half the list price (AI cost audit 10/7/26 #19).

A batch is answered asynchronously — usually inside an hour, at most 24 —
and every token in it (cache writes and reads included) is billed at 50% of
the synchronous rate. The nightly DSR narrative (dsr.pipeline) is the first
workflow: it is written while the owner is asleep, so nobody waits on it.

  enabled(workflow)              may this workflow batch here and now?
  submit(workflow, items)        gate each item as create_with_retry would,
                                 then one batches.create for the rest
  item(workflow, custom_id)      the item's row (its status), or None
  pending(workflow, custom_id)   True while its answer has not come back
  cancel(workflow, custom_id)    the caller fell back: the answer, when it
                                 lands, is ledgered and discarded
  open_items(workflow, rid)      the items still out (a fallback's list)
  run_collector()                the scheduled job (ai_batch_collect, every
                                 ~5 minutes): read the ended batches, ledger
                                 every result, hand each to its callback;
                                 then hand back each item still out past
                                 its own cutoff_at (BatchItemFailed
                                 "cutoff") for a synchronous call
  init_ai_batches()              boot DDL (models.init_db)

AN ITEM is {custom_id, restaurant_id, action, request, callback, context,
readiness}: `request` is exactly the keyword arguments create_with_retry
would send (model, max_tokens, system, messages, output_config, …),
`callback` is "module:function", reached lazily by name the way
pos.PROVIDERS is (so a module's import never runs at submit), and `context`
a small JSON-able dict handed back with the answer.

THE SAME GATES AS A SYNCHRONOUS CALL. Before anything is sent each item
passes what create_with_retry checks: the readiness gate (is_held), the
budget (ai_budget_exceeded, the stop recorded the same way) and the
provider breaker (breaker_open — read, never taking the probe slot, since a
batch is no probe of the synchronous path). A refused item is a zero-cost
'blocked' ledger row (log_blocked, the same reasons) and is never sent; its
callback is told at once, with the exception create_with_retry would have
raised (DataNotReady, AIBudgetExceeded, AIProviderDown). The request is
shaped the same way too (default effort and thinking, no temperature).
create_with_retry itself is not refactored for this: its body is where the
budget and concurrency work lands, and these are the same functions it
calls.

THE LEDGER. A result is one ai_usage row written through
ai_utils.log_ai_usage with batch=True — priced at
ai_utils.BATCH_PRICE_MULTIPLIER of list and stamped
ai_utils.BATCH_PRICE_VERSION — and one ai_calls trace through the same
_record_trace_safe a synchronous call uses, under the attribution captured
when the item was submitted (the sweep's 'scheduler', its correlation id).
The message handed to the callback carries `_cavnar_call_id` exactly as
create_with_retry's does, so extract_text, outcome_of, parse_json_reply and
mark_outcome work on it unchanged. An errored, expired or canceled request
is billed nothing: a zero-cost 'error' row, reason batch_<type>, and the
callback gets a BatchItemFailed so its caller can fall back.

WHO WINS. A caller that stops waiting (its own cutoff) calls cancel(): the
row goes cancelled, and when the answer lands it is ledgered (it was billed)
and discarded — the callback never runs. The collector claims an item
(submitted -> collecting) before its callback, so cancel() and a landing
answer can never both act; when every item of a batch is cancelled the
batch is cancelled at Anthropic too, so requests not yet run are not billed.

CALLBACKS RUN OFF THE LOOP (platform re-audit 10/7/26 #1). The collector
runs at the top of every scheduler tick, ahead of the DSR sweep, the
reminders and the briefs; a callback may make a synchronous model call
(a fallback on an errored, expired or cut-off item, the DSR's one-tier
escalation), and run inline it held the loop for as long as the model
took. So the loop thread only ledgers and claims: each callback is handed
to a small pool of daemon threads (CALLBACK_WORKERS), and the pass waits
at most CALLBACK_INLINE_WAIT_SECONDS for the ones it handed over — a
judged answer is stored in that time, so the DSR sweep right after still
finishes the night's narrative in the same tick; a slow fallback goes on
in the pool and the loop moves on. Bounded: past CALLBACK_MAX_PENDING
callbacks waiting or running the pass stops claiming, leaving the batch
in progress (items are idempotent by status, so the next pass picks up
the rest), and the pass checks its time bound per item, not per batch.
A callback a crash or a deploy cut short leaves its item `collecting`
(or `cut_off`): the sweep finds it STALE_COLLECTING_MINUTES later,
wherever its batch is, and tells its callback once more
(BatchItemFailed("stale"), or "cutoff" again) — a second cut-short call
closes it as failed, never a third.

ONLY WHERE THE SCHEDULER RUNS. A local backend holds a copy of real
restaurants and production's key; it never submits and never collects
(scheduler.scheduling_allowed). AI_BATCHES_ENABLED=0 switches the whole
pipeline off; AI_BATCHES_WORKFLOWS names the workflows that may batch.
"""
import importlib
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import zlib

log = logging.getLogger("ai_batches")

# Workflows that batch unless AI_BATCHES_WORKFLOWS names others: the nightly
# DSR narrative, and the learner's weekly shadow replays (ai_learning.
# shadow_arms — a cheaper tier on kept production requests, which nobody
# waits on and which would never be worth list price). AI cost audit 10/7/26
# added more nobody waits on, each with a synchronous fallback by a cutoff:
# the two daily root-cause reads (#58: the review diagnosis sent at 4am, the
# food-cost one once the morning's snapshots are in, the 6am pass calling
# synchronously for whatever has not landed — scheduler.run_*_diagnoses), the
# weekly competitor read (#59, competitor), the Tuesday recipe drafts (#60,
# recipes), the weekly digest's narrative written before the send (#61,
# reporter) and the quiet-night post drafts (#62, strategy_jobs). An explicit
# AI_BATCHES_WORKFLOWS replaces this list, so it must name every one to keep it.
DEFAULT_WORKFLOWS = ("dsr_narrative,shadow_arms,review_diagnosis,food_cost_diagnosis,competitor_insight,"
                     "recipe_draft,weekly_digest,quiet_night_post")
# The batches endpoints carry a whole request set up and a JSONL file back.
API_TIMEOUT_SECONDS = 60.0
# The collector's bounds per pass (CLAUDE.md: bounded; the jobs table is the
# queue, oldest first, so a pass that stops early leaves the rest for the next).
COLLECT_MAX_BATCHES = 50
COLLECT_MAX_SECONDS = 120
# An item claimed for its callback this long ago whose callback is not
# running in this process (a deploy killed the process mid-callback) is
# handed to its callback once more as BatchItemFailed("stale"), so its
# caller falls back; one already re-told is closed as failed — never a
# third time (platform re-audit 10/7/26 #7).
STALE_COLLECTING_MINUTES = 15
STALE_MAX_ITEMS = 10
# The callbacks' pool (platform re-audit 10/7/26 #1): daemon threads, so a
# callback in a synchronous model call never holds the process's exit (the
# stale sweep re-tells one cut short); two, so the night's DSR narrative is
# not queued behind a slow weekly fallback.
CALLBACK_WORKERS = max(1, int(os.getenv("AI_BATCH_CALLBACK_WORKERS", "2")))
# Past this many callbacks waiting or running, a pass stops claiming more.
CALLBACK_MAX_PENDING = 20
# How long a pass waits for the callbacks it handed over before it returns
# (the loop goes on; the rest finish in the pool).
CALLBACK_INLINE_WAIT_SECONDS = 20
# Items past their own cutoff handed back per collector pass (AI cost audit
# 10/7/26 #59-#62): each callback falls back to a synchronous call, so the
# pass is bounded; the rest are the next pass's, oldest cutoff first.
CUTOFF_MAX_ITEMS = 10

# Item states.
QUEUED = "queued"              # written; batches.create has not answered yet
SUBMITTED = "submitted"        # in a batch at Anthropic
BLOCKED = "blocked"            # refused by a gate, never sent; callback told
SUBMIT_FAILED = "submit_failed"  # batches.create failed; nothing was sent
COLLECTING = "collecting"      # claimed by the collector for its callback
DONE = "done"                  # answered; callback ran
FAILED = "failed"              # errored / expired / canceled; callback told
CANCELLED = "cancelled"        # the caller stopped waiting
CUT_OFF = "cut_off"            # past its cutoff_at: cancelled, its callback owed (-> cancelled)
DISCARDED = "discarded"        # answered after it was cancelled: ledgered, dropped
OPEN = (QUEUED, SUBMITTED)

# What submit() says about each item.
DISABLED = "disabled"
DUPLICATE = "duplicate"

# Anthropic's rule for a custom_id.
_CUSTOM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_BUDGET_MESSAGE = ("AI is paused — this account has reached its {over}. "
                   "Contact will@cavnar.ai if this looks wrong.")    # create_with_retry's own words


class BatchItemFailed(RuntimeError):
    """A batched request came back without an answer: errored, expired (not
    run within 24 hours), canceled, or lost from its batch's results. The
    callback gets it so its caller can fall back to a synchronous call."""

    def __init__(self, kind, detail=None):
        self.kind = kind
        self.detail = detail
        super().__init__(f"batch request {kind}" + (f": {detail}" if detail else ""))


# ── tables ──────────────────────────────────────────────────────────────────

_DDL = (
    # One row per batch sent to Anthropic.
    """CREATE TABLE IF NOT EXISTS ai_batch_jobs (
        batch_id       TEXT PRIMARY KEY,
        workflow       TEXT NOT NULL,
        status         TEXT NOT NULL DEFAULT 'in_progress',
        n_items        INTEGER NOT NULL DEFAULT 0,
        n_succeeded    INTEGER NOT NULL DEFAULT 0,
        n_errored      INTEGER NOT NULL DEFAULT 0,
        n_expired      INTEGER NOT NULL DEFAULT 0,
        n_canceled     INTEGER NOT NULL DEFAULT 0,
        submitted_at   TEXT NOT NULL DEFAULT (datetime('now')),
        ended_at       TEXT,
        checked_at     TEXT,
        collected_at   TEXT,
        error          TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ai_batch_jobs_status ON ai_batch_jobs(status, submitted_at)",
    "CREATE INDEX IF NOT EXISTS idx_ai_batch_jobs_submitted ON ai_batch_jobs(submitted_at)",
    # One row per request. request_z is the request as sent (compressed), kept
    # only until its answer is ledgered and traced, then nulled; usage_json
    # holds the token counts (the ledger row, usage_id, is the record).
    """CREATE TABLE IF NOT EXISTS ai_batch_items (
        workflow       TEXT NOT NULL,
        custom_id      TEXT NOT NULL,
        batch_id       TEXT,
        restaurant_id  INTEGER,
        action         TEXT,
        model          TEXT,
        callback       TEXT NOT NULL,
        context_json   TEXT,
        request_z      BLOB,
        status         TEXT NOT NULL,
        reason         TEXT,
        result_type    TEXT,
        stop_reason    TEXT,
        outcome        TEXT,
        error          TEXT,
        usage_json     TEXT,
        cost_usd       REAL,
        call_id        TEXT,
        usage_id       INTEGER,
        "trigger"      TEXT,
        actor_user_id  INTEGER,
        correlation_id TEXT,
        callback_error TEXT,
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        submitted_at   TEXT,
        claimed_at     TEXT,
        collected_at   TEXT,
        cutoff_at      TEXT,
        PRIMARY KEY (workflow, custom_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ai_batch_items_batch ON ai_batch_items(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_ai_batch_items_created ON ai_batch_items(created_at)",
)
# Added after the table shipped (AI cost audit 10/7/26 #59-#62): the time
# (naive UTC) past which an item still out is cancelled and its callback
# told BatchItemFailed("cutoff"), so its caller writes it synchronously.
_ITEM_COLUMNS = (("cutoff_at", "TEXT"),)
_INDEXES_AFTER = ("CREATE INDEX IF NOT EXISTS idx_ai_batch_items_cutoff ON ai_batch_items(status, cutoff_at)",)


def init_ai_batches(db_path=None):
    """Boot DDL (models.init_db, beside init_ai_ops) — never on a call path."""
    conn = _conn(db_path)
    try:
        for sql in _DDL:
            conn.execute(sql)
        have = {r[1] for r in conn.execute("PRAGMA table_info(ai_batch_items)").fetchall()}
        for col, decl in _ITEM_COLUMNS:
            if col not in have:
                conn.execute(f"ALTER TABLE ai_batch_items ADD COLUMN {col} {decl}")
        for sql in _INDEXES_AFTER:
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


def _conn(db_path=None):
    # Resolved through models at call time (CLAUDE.md, bound imports), so a
    # test's patch of models.get_conn / DB_PATH reaches it.
    import models
    return models.get_conn(db_path or models.DB_PATH)


def _now():
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _stamp(dt):
    """A datetime (aware or naive UTC) as the tables' naive UTC text, or None."""
    from datetime import datetime, timezone
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ── switches ────────────────────────────────────────────────────────────────

def workflows():
    """The workflows allowed to batch (AI_BATCHES_WORKFLOWS, comma-separated)."""
    raw = os.getenv("AI_BATCHES_WORKFLOWS", DEFAULT_WORKFLOWS)
    return {w.strip() for w in str(raw).split(",") if w.strip()}


def _switched_on():
    return os.getenv("AI_BATCHES_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def _scheduling_allowed():
    import scheduler
    return scheduler.scheduling_allowed()


def enabled(workflow):
    """Whether `workflow` may batch here, now: the kill switch on, the
    workflow listed, and this the production scheduler's host — a local
    backend never submits (it holds production's key and a copy of real
    restaurants) and its callers take the synchronous path."""
    return _switched_on() and workflow in workflows() and _scheduling_allowed()


def _client():
    from ai_utils import get_client
    return get_client(timeout=API_TIMEOUT_SECONDS)


def _resolve(callback):
    """The callback function for "module:function" — reached by name, lazily."""
    mod, _sep, fn = str(callback or "").partition(":")
    if not mod or not fn:
        raise ValueError(f"callback must be 'module:function', got {callback!r}")
    target = getattr(importlib.import_module(mod), fn, None)
    if not callable(target):
        raise ValueError(f"callback {callback!r} is not a function")
    return target


# ── the request, shaped and gated as create_with_retry would ────────────────

# Keyword arguments create_with_retry takes for itself, never sent.
_NOT_SENT = ("restaurant_id", "action", "readiness", "retries", "backoff", "deadline", "stream", "timeout")


def _shape(request):
    """The request as create_with_retry would send it: the default effort and
    thinking for the model, no `temperature` (the SDK rejects it), none of
    its own arguments."""
    import ai_utils
    kwargs = {k: v for k, v in dict(request or {}).items() if k not in _NOT_SENT}
    kwargs.pop("temperature", None)
    effort = ai_utils.default_effort(kwargs.get("model"), kwargs)
    if effort:
        kwargs["output_config"] = dict(kwargs.get("output_config") or {}, effort=effort)
    if "thinking" not in kwargs:
        thinking = ai_utils.default_thinking(kwargs.get("model"), kwargs)
        if thinking is not None:
            kwargs["thinking"] = thinking
    return kwargs


def _gate(restaurant_id, action, model, readiness, attribution):
    """The exception create_with_retry would raise before sending, with its
    zero-cost 'blocked' ledger row — or None when the item may go."""
    import ai_utils
    if ai_utils.is_held(readiness):
        ai_utils.log_blocked(restaurant_id, action, model, "data_not_ready",
                             detail=str(readiness.get("reason") or readiness.get("decision"))[:200], **attribution)
        return ai_utils.DataNotReady(readiness)
    # One positional argument, as create_with_retry calls it (tests replace
    # it); it reads the trigger from the attribution in force itself.
    over = ai_utils.ai_budget_exceeded(restaurant_id)
    if over:
        ai_utils._record_budget_stop(over, restaurant_id)
        ai_utils.log_blocked(restaurant_id, action, model, "budget", detail=over, **attribution)
        return ai_utils.AIBudgetExceeded(_BUDGET_MESSAGE.format(over=over))
    if ai_utils.breaker_open("anthropic"):
        ai_utils.log_blocked(restaurant_id, action, model, "breaker", detail="the provider breaker is open",
                             **attribution)
        return ai_utils.AIProviderDown(ai_utils._PROVIDER_DOWN_MESSAGE)
    return None


def _item_view(row):
    """What a callback receives about its item."""
    d = dict(row)
    try:
        ctx = json.loads(d.get("context_json") or "{}")
    except (TypeError, ValueError):
        ctx = {}
    return {"workflow": d.get("workflow"), "custom_id": d.get("custom_id"), "batch_id": d.get("batch_id"),
            "restaurant_id": d.get("restaurant_id"), "action": d.get("action"), "model": d.get("model"),
            "context": ctx, "call_id": d.get("call_id"), "status": d.get("status")}


def _invoke(row, message=None, error=None):
    """Run the item's callback under the attribution it was submitted with;
    returns the error text when it raised (captured, never re-raised — one
    caller's bug must not stop the batch's other answers)."""
    import ai_utils
    d = dict(row)
    try:
        fn = _resolve(d.get("callback"))
        attr = {k: d.get(k) for k in ("trigger", "actor_user_id", "correlation_id") if d.get(k) is not None}
        with ai_utils.ai_context(restaurant_id=d.get("restaurant_id"), action=d.get("action"), **attr):
            if message is not None and d.get("call_id"):
                # A validation row or a stored read made by the callback links
                # to this call, as it would after create_with_retry.
                ai_utils._note_last_call(d.get("call_id"), d.get("restaurant_id"), d.get("action") or "unspecified")
            fn(_item_view(d), message=message, error=error)
        return None
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="ai_batch_callback",
                        context=f"{d.get('workflow')}:{d.get('custom_id')} callback={d.get('callback')}")
        except Exception:
            pass
        return f"{type(e).__name__}: {str(e)[:300]}"


def submit(workflow, items, client=None):
    """Send `items` as one batch. Returns {custom_id: outcome}: "submitted",
    "blocked" (a gate refused it; its callback has been told), "disabled"
    (enabled() is false — nothing written, the caller goes synchronous),
    "duplicate" (that custom_id was already used for this workflow) or
    "submit_failed" (batches.create failed; nothing was sent). Raises
    ValueError for an item that breaks the contract (a bad custom_id or a
    callback that does not resolve) — a programming error, found at submit
    rather than a day later.

    Two optional item keys (AI cost audit 10/7/26 #59-#62): `cutoff_at` (a
    datetime, or naive-UTC text) — still out past it, the item is cancelled
    by the collector's cutoff sweep and its callback told
    BatchItemFailed("cutoff"), so the caller writes it synchronously; and
    `correlation_id` — the item's own ledger group (a workflow run's id)
    where several runs share one batch, in place of the one in force."""
    import ai_utils
    items = list(items or [])
    if not items:
        return {}
    if not enabled(workflow):
        return {str(it.get("custom_id")): DISABLED for it in items}
    for it in items:
        if not _CUSTOM_ID_RE.match(str(it.get("custom_id") or "")):
            raise ValueError(f"custom_id must match {_CUSTOM_ID_RE.pattern}: {it.get('custom_id')!r}")
        _resolve(it.get("callback"))
    trigger, actor, corr = ai_utils._attribution()
    attribution = {"trigger": trigger, "actor_user_id": actor, "correlation_id": corr}
    out, requests, blocked = {}, [], []
    conn = _conn()
    try:
        for it in items:
            cid = str(it["custom_id"])
            request = _shape(it.get("request"))
            model = request.get("model", "unknown")
            rid, action = it.get("restaurant_id"), it.get("action") or "unspecified"
            item_corr = it.get("correlation_id") or corr
            error = _gate(rid, action, model, it.get("readiness"), dict(attribution, correlation_id=item_corr))
            cutoff = it.get("cutoff_at")
            cutoff = cutoff[:19].replace("T", " ") if isinstance(cutoff, str) else _stamp(cutoff)
            cur = conn.execute(
                'INSERT OR IGNORE INTO ai_batch_items (workflow, custom_id, restaurant_id, action, model, callback, '
                'context_json, request_z, status, reason, "trigger", actor_user_id, correlation_id, cutoff_at) '
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (workflow, cid, rid, action, model, it["callback"],
                 json.dumps(it.get("context") or {}, default=str),
                 None if error else zlib.compress(json.dumps(request, default=str).encode("utf-8")),
                 BLOCKED if error else QUEUED, (type(error).__name__ if error else None),
                 trigger, actor, (str(item_corr)[:80] if item_corr else None), cutoff))
            conn.commit()
            if cur.rowcount != 1:
                out[cid] = DUPLICATE
                continue
            if error is not None:
                out[cid] = BLOCKED
                blocked.append((cid, error))
                continue
            requests.append({"custom_id": cid, "params": request})
    finally:
        conn.close()
    for cid, error in blocked:
        row = item(workflow, cid)
        if row:
            err = _invoke(row, error=error)
            if err:
                _update(workflow, cid, callback_error=err)
    if not requests:
        return out
    ids = [r["custom_id"] for r in requests]
    try:
        batch = (client or _client()).messages.batches.create(requests=requests)
        batch_id = str(getattr(batch, "id"))
    except Exception as e:
        reason = ai_utils.classify_error(e)
        for cid in ids:
            _update(workflow, cid, status=SUBMIT_FAILED, reason=reason, error=f"{type(e).__name__}: {str(e)[:300]}",
                    request_z=None)
            out[cid] = SUBMIT_FAILED
        try:
            import ops
            ops.capture(e, job="ai_batch_submit", context=f"{workflow}: {len(ids)} item(s)")
        except Exception:
            pass
        return out
    conn = _conn()
    try:
        conn.execute("INSERT OR IGNORE INTO ai_batch_jobs (batch_id, workflow, status, n_items, submitted_at) "
                     "VALUES (?,?,?,?,?)", (batch_id, workflow, "in_progress", len(ids), _now()))
        conn.executemany("UPDATE ai_batch_items SET batch_id=?, status=?, submitted_at=? "
                         "WHERE workflow=? AND custom_id=? AND status=?",
                         [(batch_id, SUBMITTED, _now(), workflow, cid, QUEUED) for cid in ids])
        conn.commit()
    finally:
        conn.close()
    for cid in ids:
        out[cid] = SUBMITTED
    return out


def _update(workflow, custom_id, **fields):
    if not fields:
        return
    conn = _conn()
    try:
        cols = ", ".join(f'"{k}"=?' for k in fields)
        conn.execute(f"UPDATE ai_batch_items SET {cols} WHERE workflow=? AND custom_id=?",
                     (*fields.values(), workflow, custom_id))
        conn.commit()
    finally:
        conn.close()


# ── the caller's side ───────────────────────────────────────────────────────

def item(workflow, custom_id):
    """The item's row as a dict (status, reason, batch_id, call_id, cost…),
    or None."""
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM ai_batch_items WHERE workflow=? AND custom_id=?",
                           (workflow, str(custom_id))).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def pending(workflow, custom_id):
    """True while the item is out and its answer has not come back — the
    caller's "keep waiting?" (against its own cutoff)."""
    row = item(workflow, custom_id)
    return bool(row) and row["status"] in OPEN


def open_items(workflow, restaurant_id=None):
    """The custom_ids of `workflow`'s items still out (queued or submitted),
    for one restaurant or all — what a fallback pass cancels before it
    calls synchronously."""
    conn = _conn()
    try:
        sql = "SELECT custom_id FROM ai_batch_items WHERE workflow=? AND status IN (?,?)"
        args = [workflow, *OPEN]
        if restaurant_id is not None:
            sql += " AND restaurant_id=?"
            args.append(restaurant_id)
        return [r["custom_id"] for r in conn.execute(sql + " ORDER BY created_at", args).fetchall()]
    finally:
        conn.close()


def cancel(workflow, custom_id, client=None):
    """The caller stopped waiting (its cutoff passed and it went
    synchronous). True when the item was still out: its answer, if it ever
    lands, is ledgered and discarded, never handed to the callback. False
    when the collector already has it (or it is finished) — the caller
    should read what the callback did. When no item of the batch is still
    wanted the batch is cancelled at Anthropic as well (best effort), so the
    requests it has not run yet are not billed."""
    return _stop_waiting(workflow, custom_id, client, CANCELLED, "the caller stopped waiting")


def _stop_waiting(workflow, custom_id, client, status, reason):
    """cancel()'s body: the item out (queued or submitted) -> `status`,
    atomically; the batch cancelled at Anthropic when nothing in it is
    still wanted. The cutoff sweep moves an item to CUT_OFF the same way
    (its callback is owed), stamping claimed_at for the stale sweep."""
    conn = _conn()
    try:
        cur = conn.execute("UPDATE ai_batch_items SET status=?, reason=?, collected_at=?, "
                           "claimed_at=CASE WHEN ?=? THEN ? ELSE claimed_at END "
                           "WHERE workflow=? AND custom_id=? AND status IN (?,?)",
                           (status, reason, _now(), status, CUT_OFF, _now(), workflow, str(custom_id), *OPEN))
        conn.commit()
        if cur.rowcount != 1:
            return False
        row = conn.execute("SELECT batch_id FROM ai_batch_items WHERE workflow=? AND custom_id=?",
                           (workflow, str(custom_id))).fetchone()
        batch_id = row["batch_id"] if row else None
        wanted = conn.execute("SELECT COUNT(*) FROM ai_batch_items WHERE batch_id=? AND status IN (?,?)",
                              (batch_id, *OPEN)).fetchone()[0] if batch_id else 1
    finally:
        conn.close()
    if batch_id and not wanted and _scheduling_allowed():
        try:
            (client or _client()).messages.batches.cancel(batch_id)
            c = _conn()
            try:
                c.execute("UPDATE ai_batch_jobs SET status='canceling' WHERE batch_id=? AND status='in_progress'",
                          (batch_id,))
                c.commit()
            finally:
                c.close()
        except Exception as e:
            log.warning("ai_batches: cancelling batch %s failed: %s", batch_id, e)
    return True


# ── the collector ───────────────────────────────────────────────────────────

def _usage_counts(message):
    u = getattr(message, "usage", None)
    return {"input_tokens": int(getattr(u, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
            "cache_write_tokens": int(getattr(u, "cache_creation_input_tokens", 0) or 0),
            "cache_read_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0)}


def _latency_ms(row, ended_at):
    """Submission to the batch's end — a batch's "latency" is how long its
    caller waited, which is what the ledger's latency means elsewhere."""
    from datetime import datetime, timezone
    try:
        start = datetime.fromisoformat(str(row.get("submitted_at"))[:19].replace(" ", "T"))
        end = ended_at if isinstance(ended_at, datetime) else datetime.utcnow()
        if end.tzinfo is not None:
            end = end.astimezone(timezone.utc).replace(tzinfo=None)
        return max(0, int((end - start).total_seconds() * 1000))
    except (TypeError, ValueError):
        return None


def _ledger(row, result_type, message, error_text, ended_at):
    """The item's ai_usage row and ai_calls trace, written the way
    create_with_retry writes them. Returns (call_id, usage_id, cost, outcome)."""
    import ai_utils
    d = dict(row)
    call_id = ai_utils._new_call_id()
    attribution = {"trigger": d.get("trigger"), "actor_user_id": d.get("actor_user_id"),
                   "correlation_id": d.get("correlation_id")}
    try:
        kwargs = json.loads(zlib.decompress(d["request_z"]).decode("utf-8")) if d.get("request_z") else {}
    except Exception:
        kwargs = {}
    kwargs.setdefault("model", d.get("model"))
    model = d.get("model") or kwargs.get("model") or "unknown"
    rid, action = d.get("restaurant_id"), d.get("action") or "unspecified"
    latency = _latency_ms(d, ended_at)
    usage_id, cost, outcome = None, 0.0, "error"
    if result_type == "succeeded" and message is not None:
        outcome = ai_utils.outcome_of(message)
        u = _usage_counts(message)
        cost = ai_utils._estimate_cost(model, u["input_tokens"], u["output_tokens"], u["cache_write_tokens"],
                                       u["cache_read_tokens"], batch=True)
        try:
            usage_id = ai_utils.log_ai_usage(
                rid, action, model, u["input_tokens"], u["output_tokens"],
                cache_write_tokens=u["cache_write_tokens"], cache_read_tokens=u["cache_read_tokens"],
                latency_ms=latency, outcome=outcome, stop_reason=getattr(message, "stop_reason", None),
                attempts=1, request_id=getattr(message, "id", None), call_id=call_id, cost_usd=cost, batch=True,
                effort=ai_utils._effort_of(kwargs), **attribution)
        except Exception as e:
            log.warning("ai_batches: ledger row not written for %s: %s", d.get("custom_id"), e)
        ai_utils._record_trace_safe(call_id, kwargs, message, rid, action, outcome, attribution,
                                    attempts=1, latency_ms=latency, usage_id=usage_id)
        try:
            setattr(message, "_cavnar_call_id", call_id)
        except Exception:
            pass
    else:
        # Not run, or refused by the API: billed nothing, still a row — the
        # console sees every outcome (#48, #52).
        try:
            usage_id = ai_utils.log_ai_usage(
                rid, action, model, 0, 0, status="error", outcome="error", reason=f"batch_{result_type}",
                error=(error_text or f"batch request {result_type}")[:400], latency_ms=latency, attempts=1,
                call_id=call_id, cost_usd=0.0, batch=True, **attribution)
        except Exception as e:
            log.warning("ai_batches: ledger row not written for %s: %s", d.get("custom_id"), e)
        ai_utils._record_trace_safe(call_id, kwargs, None, rid, action, "error", attribution,
                                    attempts=1, latency_ms=latency, usage_id=usage_id)
    return call_id, usage_id, cost, outcome


def _claim(workflow, custom_id):
    """submitted -> collecting, atomically: the one moment the collector and
    a caller's cancel() can race, and only one of them wins it."""
    conn = _conn()
    try:
        cur = conn.execute("UPDATE ai_batch_items SET status=?, claimed_at=? "
                           "WHERE workflow=? AND custom_id=? AND status=?",
                           (COLLECTING, _now(), workflow, custom_id, SUBMITTED))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _discard(workflow, custom_id):
    """cancelled -> discarded (the answer landed after its caller fell back)."""
    conn = _conn()
    try:
        cur = conn.execute("UPDATE ai_batch_items SET status=? WHERE workflow=? AND custom_id=? AND status=?",
                           (DISCARDED, workflow, custom_id, CANCELLED))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _error_text(result):
    err = getattr(getattr(result, "error", None), "error", None) or getattr(result, "error", None)
    kind = getattr(err, "type", None)
    msg = getattr(err, "message", None)
    return (f"{kind}: {msg}" if kind or msg else None)




# ── the callbacks' pool (platform re-audit 10/7/26 #1) ─────────────────────
#
# The loop thread ledgers and claims; a callback — which may call a model
# synchronously — runs here. Daemon threads on one queue; `_cb_inflight`
# names every item handed over and not finished in this process (the stale
# sweep leaves those alone, and a pass waits only for its own).

_cb_queue = queue.Queue()
_cb_cond = threading.Condition()
_cb_inflight = set()
_cb_threads = []


def _callback_worker():
    while True:
        key, fn = _cb_queue.get()
        try:
            fn()
        except Exception as e:                    # _invoke captures the callback's own; never kill the worker
            log.warning("ai_batches: callback %s:%s failed outside its capture: %s", key[0], key[1], e)
        finally:
            with _cb_cond:
                _cb_inflight.discard(key)
                _cb_cond.notify_all()


def _callbacks_pending():
    with _cb_cond:
        return len(_cb_inflight)


def _handed_over(workflow, custom_id):
    with _cb_cond:
        return (workflow, str(custom_id)) in _cb_inflight


def _dispatch(workflow, custom_id, fn):
    """Hand fn (one item's callback and the write that closes it) to the
    pool. False when that item's callback is already waiting or running
    here."""
    key = (workflow, str(custom_id))
    with _cb_cond:
        if key in _cb_inflight:
            return False
        _cb_inflight.add(key)
        _cb_threads[:] = [t for t in _cb_threads if t.is_alive()]
        while len(_cb_threads) < CALLBACK_WORKERS:
            t = threading.Thread(target=_callback_worker, daemon=True,
                                 name=f"ai-batch-callback-{len(_cb_threads) + 1}")
            t.start()
            _cb_threads.append(t)
    _cb_queue.put((key, fn))
    return True


def _wait_for(keys, seconds):
    """Wait at most `seconds` for these handed-over callbacks to finish.
    The keys of those still going."""
    end = time.monotonic() + max(0.0, seconds)
    with _cb_cond:
        while True:
            left = [k for k in keys if k in _cb_inflight]
            remaining = end - time.monotonic()
            if not left or remaining <= 0:
                return left
            _cb_cond.wait(remaining)


def _run_callback(workflow, custom_id, error=None, message=None, finish=None):
    """On the pool: the item's callback (its row read fresh, as ledgered),
    then finish(err) — the status write that closes the item."""
    import contextlib
    try:
        import logging_setup
        log_ctx = logging_setup.context(job="ai_batch_callback")
    except Exception:
        log_ctx = contextlib.nullcontext()
    with log_ctx:
        row = item(workflow, custom_id)
        if row is None:
            return
        err = _invoke(row, message=message, error=error)
        if finish is not None:
            finish(err)


def _finish_collected(workflow, custom_id, final, err):
    """collecting -> done / failed, only while it is still collecting."""
    conn = _conn()
    try:
        conn.execute("UPDATE ai_batch_items SET status=?, callback_error=? WHERE workflow=? AND custom_id=? "
                     "AND status=?", (final, err, workflow, custom_id, COLLECTING))
        conn.commit()
    finally:
        conn.close()


def _finish_cut_off(workflow, custom_id, err):
    """cut_off -> cancelled once its callback has been told — discarded when
    its answer landed meanwhile (ledgered by _collect_one, never handed
    over)."""
    conn = _conn()
    try:
        conn.execute("UPDATE ai_batch_items SET status=CASE WHEN result_type IS NOT NULL THEN ? ELSE ? END, "
                     "reason='past its cutoff', callback_error=? WHERE workflow=? AND custom_id=? AND status=?",
                     (DISCARDED, CANCELLED, err, workflow, custom_id, CUT_OFF))
        conn.commit()
    finally:
        conn.close()


def _collect_one(workflow, custom_id, result, ended_at):
    """One result: ledger it, then hand it to its callback on the pool (or
    discard it). Returns the item's new status — COLLECTING while its
    callback is owed — or None when there was nothing to do."""
    row = item(workflow, custom_id)
    if row is None:
        log.warning("ai_batches: a result for an unknown item %s:%s", workflow, custom_id)
        return None
    rtype = getattr(result, "type", None) or "errored"
    message = getattr(result, "message", None) if rtype == "succeeded" else None
    error_text = None if rtype == "succeeded" else (_error_text(result) if rtype == "errored" else None)
    if row["status"] == CANCELLED:
        if not _discard(workflow, custom_id):
            return None
        status = DISCARDED
    elif row["status"] == CUT_OFF and not row.get("result_type"):
        # Past its cutoff, its fallback owed or running: the answer that
        # landed anyway was billed, so it is ledgered — and dropped
        # (_finish_cut_off closes it as discarded).
        status = CUT_OFF
    elif row["status"] == SUBMITTED:
        if not _claim(workflow, custom_id):
            return None                      # cancelled a moment ago; the next pass discards it
        status = COLLECTING
    else:
        return None                          # already collected by an earlier pass
    call_id, usage_id, cost, outcome = _ledger(row, rtype, message, error_text, ended_at)
    fields = dict(result_type=rtype, outcome=outcome, call_id=call_id, usage_id=usage_id, cost_usd=round(cost, 8),
                  stop_reason=(getattr(message, "stop_reason", None) if message is not None else None),
                  error=error_text, usage_json=(json.dumps(_usage_counts(message)) if message is not None else None),
                  collected_at=_now(), request_z=None)
    _update(workflow, custom_id, **fields)
    if status in (DISCARDED, CUT_OFF):
        return status
    if rtype == "succeeded" and message is not None:
        final, kw = DONE, {"message": message}
    else:
        final, kw = FAILED, {"error": BatchItemFailed(rtype, error_text)}
    _dispatch(workflow, custom_id, lambda: _run_callback(
        workflow, custom_id, finish=lambda err: _finish_collected(workflow, custom_id, final, err), **kw))
    return COLLECTING


def _collect_batch(client, job, batch, started, handed):
    """The results of one ended batch, each ledgered and its callback handed
    to the pool (named in `handed`); then the job row closed.
    (complete, items collected): incomplete when the pass's time bound
    passed or the pool is full — the job stays in progress and the next
    pass reads its results again (an item already collected is skipped by
    its status)."""
    batch_id, workflow = job["batch_id"], job["workflow"]
    ended_at = getattr(batch, "ended_at", None)
    counts = getattr(batch, "request_counts", None)
    seen, n = set(), 0

    def one(cid, result):
        status = _collect_one(workflow, cid, result, ended_at)
        if status == COLLECTING:
            handed.append((workflow, cid))
        return 0 if status is None else 1
    for res in client.messages.batches.results(batch_id):
        # Per item, not per batch (#1): one batch of hundreds must not
        # hold the loop past the pass's bound.
        if time.monotonic() - started > COLLECT_MAX_SECONDS or _callbacks_pending() >= CALLBACK_MAX_PENDING:
            return False, n
        cid = str(getattr(res, "custom_id", "") or "")
        seen.add(cid)
        n += one(cid, getattr(res, "result", None))
    # An item the results never named (should not happen): its caller is
    # told, so it falls back rather than waiting on nothing.
    conn = _conn()
    try:
        left = [r["custom_id"] for r in conn.execute(
            "SELECT custom_id FROM ai_batch_items WHERE batch_id=? AND status IN (?,?,?)",
            (batch_id, SUBMITTED, CANCELLED, CUT_OFF)).fetchall()]
    finally:
        conn.close()
    for cid in left:
        if cid not in seen:
            n += one(cid, _Missing())
    conn = _conn()
    try:
        conn.execute("UPDATE ai_batch_jobs SET status='collected', ended_at=?, collected_at=?, checked_at=?, "
                     "n_succeeded=?, n_errored=?, n_expired=?, n_canceled=? WHERE batch_id=?",
                     (_stamp(ended_at) or _now(), _now(), _now(),
                      int(getattr(counts, "succeeded", 0) or 0), int(getattr(counts, "errored", 0) or 0),
                      int(getattr(counts, "expired", 0) or 0), int(getattr(counts, "canceled", 0) or 0), batch_id))
        conn.commit()
    finally:
        conn.close()
    return True, n


class _Missing:
    type = "missing"


def _sweep_cutoffs(client, started, out, handed):
    """Items still out past their own cutoff_at (AI cost audit 10/7/26
    #59-#62): each moved to CUT_OFF (cancelled: its answer, if it ever
    lands, is ledgered and dropped) and its callback told
    BatchItemFailed("cutoff") on the pool, so its caller writes it
    synchronously — off the loop thread (platform re-audit 10/7/26 #1).
    Oldest cutoff first, at most CUTOFF_MAX_ITEMS a pass and inside the
    collector's time bound."""
    conn = _conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT workflow, custom_id FROM ai_batch_items WHERE status IN (?,?) AND cutoff_at IS NOT NULL "
            "AND cutoff_at <= ? ORDER BY cutoff_at LIMIT ?", (*OPEN, _now(), CUTOFF_MAX_ITEMS + 1)).fetchall()]
    finally:
        conn.close()
    if len(rows) > CUTOFF_MAX_ITEMS:
        rows, out["hit_bound"] = rows[:CUTOFF_MAX_ITEMS], True
    for r in rows:
        if time.monotonic() - started > COLLECT_MAX_SECONDS or _callbacks_pending() >= CALLBACK_MAX_PENDING:
            out["hit_bound"] = True
            break
        wf_, cid = r["workflow"], str(r["custom_id"])
        if not _stop_waiting(wf_, cid, client, CUT_OFF, "past its cutoff"):
            continue                       # the collector took it a moment ago
        if _dispatch(wf_, cid, _told(wf_, cid, BatchItemFailed("cutoff", "not answered by its cutoff"), CUT_OFF)):
            handed.append((wf_, cid))
        out["cut_off"] = out.get("cut_off", 0) + 1


def _told(workflow, custom_id, error, status):
    """The pool's job for an item whose callback is told `error`: then
    closed from `status` (cut_off -> cancelled, collecting -> failed)."""
    if status == CUT_OFF:
        def finish(err):
            _finish_cut_off(workflow, custom_id, err)
    else:
        def finish(err):
            _finish_collected(workflow, custom_id, FAILED, err)
    return lambda: _run_callback(workflow, custom_id, error=error, finish=finish)


_STALE_REASON = "stale: told again after the collector stopped mid-callback"


def _sweep_stale(out, handed):
    """Items whose callback was owed and never finished — a crash or a
    deploy mid-callback — wherever their batch is (platform re-audit
    10/7/26 #7: the old check ran only when its batch was collected, which
    an ended batch never is again). Claimed more than
    STALE_COLLECTING_MINUTES ago and not in this process's pool: told once
    more (a collected item BatchItemFailed("stale"), a cut-off one
    "cutoff" again) with claimed_at re-stamped; one already told again is
    closed with no third call."""
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(minutes=STALE_COLLECTING_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT workflow, custom_id, status, reason FROM ai_batch_items WHERE status IN (?,?) "
            "AND claimed_at IS NOT NULL AND claimed_at < ? ORDER BY claimed_at LIMIT ?",
            (COLLECTING, CUT_OFF, cutoff, STALE_MAX_ITEMS)).fetchall()]
    finally:
        conn.close()
    for r in rows:
        wf_, cid, status = r["workflow"], str(r["custom_id"]), r["status"]
        if _handed_over(wf_, cid):
            continue                       # still waiting or running here: not stale
        conn = _conn()
        try:
            if r.get("reason") == _STALE_REASON:
                conn.execute("UPDATE ai_batch_items SET status=?, callback_error=? WHERE workflow=? "
                             "AND custom_id=? AND status=?",
                             (CANCELLED if status == CUT_OFF else FAILED,
                              "the collector stopped mid-callback twice", wf_, cid, status))
                conn.commit()
                out["stale_closed"] = out.get("stale_closed", 0) + 1
                continue
            cur = conn.execute("UPDATE ai_batch_items SET claimed_at=?, reason=? WHERE workflow=? AND custom_id=? "
                               "AND status=? AND claimed_at < ?", (_now(), _STALE_REASON, wf_, cid, status, cutoff))
            conn.commit()
            if cur.rowcount != 1:
                continue
        finally:
            conn.close()
        error = (BatchItemFailed("cutoff", "not answered by its cutoff") if status == CUT_OFF
                 else BatchItemFailed("stale", "the collector stopped before its callback finished"))
        if _dispatch(wf_, cid, _told(wf_, cid, error, status)):
            handed.append((wf_, cid))
        out["stale"] = out.get("stale", 0) + 1


def _capture_sweep(e, what="cutoff sweep"):
    try:
        import ops
        ops.capture(e, job="ai_batch_collect", context=what)
    except Exception:
        pass


def run_collector(client=None):
    """The scheduled job (ai_batch_collect, every ~5 minutes): every batch
    still out, oldest first, asked whether it has ended; an ended one's
    results ledgered and their callbacks handed to the pool — never run on
    the loop thread (platform re-audit 10/7/26 #1). Bounded by
    COLLECT_MAX_BATCHES, by COLLECT_MAX_SECONDS checked per item and by
    CALLBACK_MAX_PENDING; the jobs table is the queue, so a pass cut short
    leaves the rest for the next. Then each item still out past its own
    cutoff is handed back for a synchronous call (_sweep_cutoffs, counted
    in `cut_off`) and each callback a crash cut short is told again
    (_sweep_stale, #7, counted in `stale`). The pass waits at most
    CALLBACK_INLINE_WAIT_SECONDS for the callbacks it handed over
    (`callbacks_running`: those still going when it returned). Only on the
    production scheduler's host. Returns the standard counts: attempted =
    batches that had ended, skipped = batches still running."""
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False, "items": 0, "cut_off": 0}
    if not _scheduling_allowed():
        return dict(out, reason="not the production scheduler host")
    conn = _conn()
    try:
        jobs = [dict(r) for r in conn.execute(
            "SELECT * FROM ai_batch_jobs WHERE status IN ('in_progress','canceling') "
            "ORDER BY submitted_at, batch_id LIMIT ?", (COLLECT_MAX_BATCHES + 1,)).fetchall()]
    finally:
        conn.close()
    started = time.monotonic()
    handed = []
    if len(jobs) > COLLECT_MAX_BATCHES:
        jobs, out["hit_bound"] = jobs[:COLLECT_MAX_BATCHES], True
    if jobs:
        client = client or _client()
    for job in jobs:
        if time.monotonic() - started > COLLECT_MAX_SECONDS or _callbacks_pending() >= CALLBACK_MAX_PENDING:
            out["hit_bound"] = True
            break
        try:
            batch = client.messages.batches.retrieve(job["batch_id"])
            status = getattr(batch, "processing_status", None)
            if status != "ended":
                c = _conn()
                try:
                    c.execute("UPDATE ai_batch_jobs SET checked_at=?, status=? WHERE batch_id=?",
                              (_now(), "canceling" if status == "canceling" else job["status"], job["batch_id"]))
                    c.commit()
                finally:
                    c.close()
                out["skipped"] += 1
                continue
            out["attempted"] += 1
            complete, n = _collect_batch(client, job, batch, started, handed)
            out["ok"] += 1
            out["items"] += n
            if not complete:
                # Left in progress: the next pass reads it again.
                out["hit_bound"] = True
                c = _conn()
                try:
                    c.execute("UPDATE ai_batch_jobs SET checked_at=? WHERE batch_id=?", (_now(), job["batch_id"]))
                    c.commit()
                finally:
                    c.close()
                break
        except Exception as e:
            out["attempted"] += 1
            out["failed"] += 1
            try:
                import ops
                ops.capture(e, job="ai_batch_collect", context=f"batch_id={job['batch_id']}")
            except Exception:
                pass
            c = _conn()
            try:
                c.execute("UPDATE ai_batch_jobs SET checked_at=?, error=? WHERE batch_id=?",
                          (_now(), f"{type(e).__name__}: {str(e)[:300]}", job["batch_id"]))
                c.commit()
            finally:
                c.close()
    # After the results: an answer that landed this pass is never cut off.
    try:
        _sweep_cutoffs(client, started, out, handed)
    except Exception as e:
        _capture_sweep(e)
    try:
        _sweep_stale(out, handed)
    except Exception as e:
        _capture_sweep(e, "stale sweep")
    # A judged answer is stored within moments, so the DSR sweep right
    # after this job still finishes the night in the same tick; a slow
    # fallback keeps running in the pool and the loop goes on.
    left = _wait_for(handed, CALLBACK_INLINE_WAIT_SECONDS)
    if left:
        out["callbacks_running"] = len(left)
    return out
