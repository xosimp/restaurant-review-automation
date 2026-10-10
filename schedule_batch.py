"""
schedule_batch.py — the unattended auto-draft through Message Batches (scheduler
audit 10/10/26: RM-10, AI-15, SCALE-12, SCALE-22).

The auto-draft (strategy_jobs.run_auto_draft_schedules) is written while the
owner is not looking — they review it later that day — yet it paid list price
and held one of the two generation slots for every minute the model wrote.
Here its model call goes through ai_batches at half the price, holding no
slot while it waits:

  SUBMIT (submit_auto_draft, from strategy_jobs._draft_one before it claims
  a job). The generation is built exactly as a press of Generate would build
  it — schedule_engine._build_schedule_result, every input, the route, the
  manager plan, the request — up to its first model call, which is CAPTURED
  instead of sent (capture(): labor's call site raises CallCaptured with the
  request it would have sent). A generation that makes no call (a code
  draft) is not batched: it is cheap and runs now. The captured request is
  one ai_batches item (workflow "labor_schedule", the policy's batch flag
  governs it) with a cutoff (BATCH_CUTOFF_HOURS).

  ANSWER (on_auto_draft, ai_batches' callback). The ordinary auto-draft
  runs (strategy_jobs._draft_one: its job, its slot, its push), with the
  batched answer handed to the first call that asks for the same dates on
  the same model and output schema (answering()): the inputs may have moved
  a little since the submit, and the week is the one the model wrote for
  them then. Any other call — a slice the first answer left, a retry, the
  gate's rewrite — and a first call that does not match go out as usual,
  synchronously. The slot is held only for the code's work after the model.

  NO ANSWER (errored, expired, past its cutoff — BatchItemFailed): the
  auto-draft runs synchronously, as it did before. A gate's refusal
  (budget, readiness, the provider breaker) writes nothing: the synchronous
  call would have been refused too.

A batched answer's usage is marked `batch` (schedule_model_calls.usage_json),
so schedule_metrics prices it at ai_utils.BATCH_PRICE_MULTIPLIER; ai_batches
ledgers it in ai_usage at the batch rate when it lands.
"""
import contextvars
import hashlib
import json
import threading
import uuid

WORKFLOW = "labor_schedule"
CALLBACK = "schedule_batch:on_auto_draft"
# How long the auto-draft waits for its batch before it writes the week
# synchronously: the owner reads it later the same day.
BATCH_CUTOFF_HOURS = 6


class CallCaptured(BaseException):
    """Raised at labor's call site while capturing: the request the first
    model call would have sent. A BaseException, so no `except Exception`
    between the call site and submit_auto_draft swallows it."""

    def __init__(self, request):
        super().__init__("schedule call captured for a batch")
        self.request = request


# None: calls go out as usual. {"mode": "capture"} or {"mode": "answer",
# "key": ..., "message": ..., "used": False, "lock": Lock}.
_HOOK = contextvars.ContextVar("schedule_batch_hook", default=None)


def _dates_of(request):
    try:
        import schedule_prompt as sp
        return sorted(sp.request_dates((request.get("messages") or [{}])[0].get("content")))
    except Exception:
        return []


def call_key(request) -> str:
    """What a batched answer must match to be used for a call: the model, the
    dates the call writes and its output schema (a changed roster changes the
    schema's enums, and that answer is then not this call's)."""
    req = dict(request or {})
    schema = ((req.get("output_config") or {}).get("format") or {})
    blob = json.dumps({"model": req.get("model"), "dates": _dates_of(req), "schema": schema}, sort_keys=True,
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def answer_for(request):
    """labor's call site asks this before it sends (labor.
    generate_optimized_schedule): None — send it; a message — the batched
    answer, used once; or it raises CallCaptured while capturing."""
    hook = _HOOK.get()
    if not hook:
        return None
    if hook.get("mode") == "capture":
        raise CallCaptured(dict(request or {}))
    if hook.get("mode") == "answer":
        with hook["lock"]:
            if hook.get("used") or call_key(request) != hook.get("key"):
                return None
            hook["used"] = True
        msg = hook.get("message")
        try:
            msg._cavnar_batch = True
        except Exception:
            pass
        return msg
    return None


class _Scope:
    def __init__(self, value):
        self.value, self.token = value, None

    def __enter__(self):
        self.token = _HOOK.set(self.value)
        return self.value

    def __exit__(self, *exc):
        _HOOK.reset(self.token)
        return False


def capture():
    """`with capture():` — the first model call raises CallCaptured."""
    return _Scope({"mode": "capture"})


def answering(key, message):
    """`with answering(key, message):` — the call matching `key` reads
    `message` instead of calling the model, once."""
    return _Scope({"mode": "answer", "key": key, "message": message, "used": False, "lock": threading.Lock()})


def capture_request(restaurant_id, week_start=None):
    """The first model request the restaurant's next draft would send, or
    None when it would send none (a code draft) or could not be built."""
    import schedule_engine as se
    try:
        with capture():
            se._build_schedule_result(restaurant_id, week_start=week_start)
    except CallCaptured as cap:
        return cap.request
    except Exception as e:
        print(f"[schedule_batch] restaurant {restaurant_id}: no request to batch ({e!r})")
        return None
    return None


def submit_auto_draft(r, period=None, week_start=None, target=None, attempt=None):
    """The auto-draft's first model call sent as one batch item. "submitted"
    when it went (the answer finishes the draft, on_auto_draft); "blocked"
    when a gate refused it (nothing will be drafted — the synchronous call
    would have been refused too); None when the draft must run now (batches
    off here, nothing to batch, the submit failed). Never raises."""
    try:
        import ai_batches
        if not ai_batches.enabled(WORKFLOW):
            return None
        from datetime import datetime, timedelta
        import data_health
        request = capture_request(r.id, week_start=week_start)
        if not request:
            return None
        cid = f"sd-{int(r.id)}-{uuid.uuid4().hex[:12]}"
        res = ai_batches.submit(WORKFLOW, [{
            "custom_id": cid, "restaurant_id": r.id, "action": "labor_schedule", "request": request,
            "readiness": request.get("readiness") or data_health.NOT_APPLICABLE, "callback": CALLBACK,
            "cutoff_at": datetime.utcnow() + timedelta(hours=BATCH_CUTOFF_HOURS),
            "context": {"key": call_key(request), "period": period, "week_start": week_start, "target": target,
                        "attempt": attempt}}])
        status = res.get(cid)
        if status == ai_batches.SUBMITTED:
            return "submitted"
        if status == ai_batches.BLOCKED:
            return "blocked"
        return None
    except Exception as e:
        import ops
        ops.capture(e, job="auto_draft_schedule", context=f"restaurant_id={getattr(r, 'id', None)} batch submit")
        return None


def on_auto_draft(item, message=None, error=None):
    """ai_batches' callback: the auto-draft finished with the batched answer
    (or, with no answer, synchronously). A gate's refusal drafts nothing."""
    import ai_batches
    import ops
    rid = (item or {}).get("restaurant_id")
    ctx = (item or {}).get("context") or {}
    if not rid:
        return
    if error is not None and not isinstance(error, ai_batches.BatchItemFailed):
        return
    from models import get_restaurant
    r = get_restaurant(rid)
    if r is None or not (getattr(r, "auto_draft_schedule", 0) and getattr(r, "module_labor", 0)):
        return
    import strategy_jobs as sj
    import schedule_engine as se
    target = ctx.get("week_start") or ctx.get("target")
    if target:
        # The owner may have made the week while the batch was out: theirs
        # stands, and the answer is not used.
        conn = sj.get_conn(sj.DB_PATH)
        try:
            if sj._week_has_schedule(conn, rid, target):
                return
        finally:
            conn.close()
    counts = {"drafted": 0, "skipped": 0, "failed": 0}

    def _bump(k):
        counts[k] = counts.get(k, 0) + 1
    try:
        if error is not None or message is None:
            sj._draft_one(r, sj.DB_PATH, se, _bump, period=ctx.get("period"), target=ctx.get("target"),
                          attempt=ctx.get("attempt"), batch=False)
        else:
            with answering(ctx.get("key"), message):
                sj._draft_one(r, sj.DB_PATH, se, _bump, period=ctx.get("period"), target=ctx.get("target"),
                              attempt=ctx.get("attempt"), batch=False)
    except Exception as e:
        ops.capture(e, job="auto_draft_schedule", context=f"restaurant_id={rid} (batch)")
