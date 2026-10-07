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
  run_collector()                the scheduled job (ai_batch_collect, every
                                 ~5 minutes): read the ended batches, ledger
                                 every result, hand each to its callback
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

ONLY WHERE THE SCHEDULER RUNS. A local backend holds a copy of real
restaurants and production's key; it never submits and never collects
(scheduler.scheduling_allowed). AI_BATCHES_ENABLED=0 switches the whole
pipeline off; AI_BATCHES_WORKFLOWS names the workflows that may batch.
"""
import importlib
import json
import logging
import os
import re
import sqlite3
import time
import zlib

log = logging.getLogger("ai_batches")

# Workflows that batch unless AI_BATCHES_WORKFLOWS names others.
DEFAULT_WORKFLOWS = "dsr_narrative"
# The batches endpoints carry a whole request set up and a JSONL file back.
API_TIMEOUT_SECONDS = 60.0
# The collector's bounds per pass (CLAUDE.md: bounded; the jobs table is the
# queue, oldest first, so a pass that stops early leaves the rest for the next).
COLLECT_MAX_BATCHES = 50
COLLECT_MAX_SECONDS = 120
# An item claimed for its callback this long ago by a pass that never
# finished it (a deploy killed the process mid-callback) is closed as
# failed — never handed to its callback twice.
STALE_COLLECTING_MINUTES = 15

# Item states.
QUEUED = "queued"              # written; batches.create has not answered yet
SUBMITTED = "submitted"        # in a batch at Anthropic
BLOCKED = "blocked"            # refused by a gate, never sent; callback told
SUBMIT_FAILED = "submit_failed"  # batches.create failed; nothing was sent
COLLECTING = "collecting"      # claimed by the collector for its callback
DONE = "done"                  # answered; callback ran
FAILED = "failed"              # errored / expired / canceled; callback told
CANCELLED = "cancelled"        # the caller stopped waiting
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
        PRIMARY KEY (workflow, custom_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ai_batch_items_batch ON ai_batch_items(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_ai_batch_items_created ON ai_batch_items(created_at)",
)


def init_ai_batches(db_path=None):
    """Boot DDL (models.init_db, beside init_ai_ops) — never on a call path."""
    conn = _conn(db_path)
    try:
        for sql in _DDL:
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
    rather than a day later."""
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
            error = _gate(rid, action, model, it.get("readiness"), attribution)
            cur = conn.execute(
                'INSERT OR IGNORE INTO ai_batch_items (workflow, custom_id, restaurant_id, action, model, callback, '
                'context_json, request_z, status, reason, "trigger", actor_user_id, correlation_id) '
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (workflow, cid, rid, action, model, it["callback"],
                 json.dumps(it.get("context") or {}, default=str),
                 None if error else zlib.compress(json.dumps(request, default=str).encode("utf-8")),
                 BLOCKED if error else QUEUED, (type(error).__name__ if error else None),
                 trigger, actor, (str(corr)[:80] if corr else None)))
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


def cancel(workflow, custom_id, client=None):
    """The caller stopped waiting (its cutoff passed and it went
    synchronous). True when the item was still out: its answer, if it ever
    lands, is ledgered and discarded, never handed to the callback. False
    when the collector already has it (or it is finished) — the caller
    should read what the callback did. When no item of the batch is still
    wanted the batch is cancelled at Anthropic as well (best effort), so the
    requests it has not run yet are not billed."""
    conn = _conn()
    try:
        cur = conn.execute("UPDATE ai_batch_items SET status=?, reason='the caller stopped waiting', collected_at=? "
                           "WHERE workflow=? AND custom_id=? AND status IN (?,?)",
                           (CANCELLED, _now(), workflow, str(custom_id), *OPEN))
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
                **attribution)
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


def _collect_one(workflow, custom_id, result, ended_at):
    """One result: ledger it, then hand it to its callback (or discard it).
    Returns the item's new status, or None when there was nothing to do."""
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
    if status == DISCARDED:
        _update(workflow, custom_id, **fields)
        return DISCARDED
    _update(workflow, custom_id, **fields)
    row = item(workflow, custom_id)
    if rtype == "succeeded" and message is not None:
        err = _invoke(row, message=message)
        final = DONE
    else:
        err = _invoke(row, error=BatchItemFailed(rtype, error_text))
        final = FAILED
    _update(workflow, custom_id, status=final, callback_error=err)
    return final


def _close_stale(batch_id):
    """Items a killed pass claimed and never finished: failed, not re-run."""
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(minutes=STALE_COLLECTING_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn()
    try:
        conn.execute("UPDATE ai_batch_items SET status=?, callback_error='the collector stopped mid-callback' "
                     "WHERE batch_id=? AND status=? AND claimed_at < ?", (FAILED, batch_id, COLLECTING, cutoff))
        conn.commit()
    finally:
        conn.close()


def _collect_batch(client, job, batch):
    """Every result of one ended batch; then the job row closed."""
    batch_id, workflow = job["batch_id"], job["workflow"]
    ended_at = getattr(batch, "ended_at", None)
    counts = getattr(batch, "request_counts", None)
    seen = set()
    for res in client.messages.batches.results(batch_id):
        cid = str(getattr(res, "custom_id", "") or "")
        seen.add(cid)
        _collect_one(workflow, cid, getattr(res, "result", None), ended_at)
    # An item the results never named (should not happen): its caller is
    # told, so it falls back rather than waiting on nothing.
    conn = _conn()
    try:
        left = [r["custom_id"] for r in conn.execute(
            "SELECT custom_id FROM ai_batch_items WHERE batch_id=? AND status IN (?,?)",
            (batch_id, SUBMITTED, CANCELLED)).fetchall()]
    finally:
        conn.close()
    for cid in left:
        if cid not in seen:
            _collect_one(workflow, cid, _Missing(), ended_at)
    _close_stale(batch_id)
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


class _Missing:
    type = "missing"


def run_collector(client=None):
    """The scheduled job (ai_batch_collect, every ~5 minutes): every batch
    still out, oldest first, asked whether it has ended; an ended one's
    results ledgered and handed to their callbacks. Bounded by
    COLLECT_MAX_BATCHES and COLLECT_MAX_SECONDS; the jobs table is the queue,
    so a pass cut short leaves the rest for the next. Only on the production
    scheduler's host. Returns the standard counts: attempted = batches that
    had ended, skipped = batches still running."""
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False, "items": 0}
    if not _scheduling_allowed():
        return dict(out, reason="not the production scheduler host")
    conn = _conn()
    try:
        jobs = [dict(r) for r in conn.execute(
            "SELECT * FROM ai_batch_jobs WHERE status IN ('in_progress','canceling') "
            "ORDER BY submitted_at, batch_id LIMIT ?", (COLLECT_MAX_BATCHES + 1,)).fetchall()]
    finally:
        conn.close()
    if not jobs:
        return out
    if len(jobs) > COLLECT_MAX_BATCHES:
        jobs, out["hit_bound"] = jobs[:COLLECT_MAX_BATCHES], True
    client = client or _client()
    started = time.monotonic()
    for job in jobs:
        if time.monotonic() - started > COLLECT_MAX_SECONDS:
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
            _collect_batch(client, job, batch)
            out["ok"] += 1
            out["items"] += int(job.get("n_items") or 0)
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
    return out
