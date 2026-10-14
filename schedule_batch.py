"""
schedule_batch.py — the unattended auto-draft through Message Batches (scheduler
audit 10/10/26: RM-10, AI-15, SCALE-12, SCALE-22; blind re-audit 10/11/26
ARCH-15).

The auto-draft (strategy_jobs.run_auto_draft_schedules) is written while the
owner is not looking — they review it later that day — yet it paid list price
and held one of the two generation slots for every minute the model wrote.
Here its model calls go through ai_batches at half the price, holding no slot
while they wait:

  PLAN (plan_requests, from submit_auto_draft before the pass claims a job).
  The generation is built exactly as a press of Generate would build it —
  schedule_engine._build_schedule_result, every input, the route, the
  manager plan — up to its first round of model calls: the independent calls
  the engine sends together (one per department part, a part's first date
  slices — schedule_engine._take_batch). Under planning() each of them is
  LISTED at labor's call site instead of sent (plan_call; labor raises
  CallPlanned, an ordinary Exception the engine's dispatch collects with the
  round's other calls), and the engine returns the list ({"planned_calls"})
  instead of a week. A generation that makes no call (a code draft) plans
  none and is not batched: it is cheap and runs now.

  SUBMIT. Every planned call is one ai_batches item of one group (custom ids
  "<group>-<i>", the group in each item's context with its call key and the
  prompt's hash), each with a cutoff (BATCH_CUTOFF_HOURS). If any item could
  not be submitted the ones that were are cancelled and the draft runs now.

  ANSWER (on_auto_draft, ai_batches' callback, once per item). Each answer
  is stored as a schedule_model_calls row of the group (job_id = the group,
  its call key, outcome "batch_answer", its usage marked `batch`); a failed
  item is stored as "batch_failed". When the group's last item lands, the
  ordinary auto-draft runs (strategy_jobs._draft_one: its job, its slot, its
  push) under schedule_resume.resuming(group): every call whose key matches
  a stored answer reads it — the same resume mechanism a deploy uses — and
  any other call (a retry, a split, a later round, the gate's rewrite) goes
  out as usual. Answers written from inputs that have since moved are said
  on the draft (schedule_resume.inputs_moved, ARCH-25).

  NO ANSWER (errored, expired, past its cutoff — BatchItemFailed): that
  call goes out synchronously when the draft runs. A gate's refusal
  (budget, readiness, the provider breaker) of any item writes nothing: the
  synchronous call would have been refused too.

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
# The schedule_model_calls outcomes a group's items leave.
ANSWERED, FAILED, BLOCKED = "batch_answer", "batch_failed", "batch_blocked"


class CallPlanned(Exception):
    """Raised at labor's call site while planning: the request this call
    would have sent. An ordinary Exception: the engine's dispatch collects it
    with the round's other calls (it used to be a BaseException that tunnelled
    past cleanup written as `except Exception` and captured only the first
    call — blind re-audit 10/11/26 ARCH-15)."""

    def __init__(self, request):
        super().__init__("schedule call planned for a batch")
        self.request = request


# None: calls go out as usual (or read a resumed try's stored answers).
# {"mode": "plan", "requests": [...], "lock": Lock} while planning;
# {"mode": "answer", ...} — a queue row from before ARCH-15 carrying one answer.
_HOOK = contextvars.ContextVar("schedule_batch_hook", default=None)


def _dates_of(request):
    try:
        import schedule_prompt as sp
        return sorted(sp.request_dates((request.get("messages") or [{}])[0].get("content")))
    except Exception:
        return []


def call_key(request) -> str:
    """What a stored answer must match to be used for a call: the model, the
    dates the call writes and its output schema (a changed roster changes the
    schema's enums, and that answer is then not this call's)."""
    req = dict(request or {})
    schema = ((req.get("output_config") or {}).get("format") or {})
    blob = json.dumps({"model": req.get("model"), "dates": _dates_of(req), "schema": schema}, sort_keys=True,
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def plan_call(request) -> bool:
    """labor's call site asks this before anything is sent: True while
    planning — the request is listed, and the caller raises CallPlanned."""
    hook = _HOOK.get()
    if not hook or hook.get("mode") != "plan":
        return False
    with hook["lock"]:
        hook["requests"].append(dict(request or {}))
    return True


def answer_for(request):
    """labor's call site asks this before it sends: a stored answer for this
    call (a group's batched answer or a resumed try's — schedule_resume, by
    call key, each once), else None — send it."""
    hook = _HOOK.get()
    if hook and hook.get("mode") == "answer":
        # A queue row from before ARCH-15 carrying its one batched answer.
        with hook["lock"]:
            if not hook.get("used") and call_key(request) == hook.get("key"):
                hook["used"] = True
                msg = hook.get("message")
                try:
                    msg._cavnar_batch = True
                except Exception:
                    pass
                return msg
    import schedule_resume
    return schedule_resume.stored_answer(request)


class _Scope:
    def __init__(self, value):
        self.value, self.token = value, None

    def __enter__(self):
        self.token = _HOOK.set(self.value)
        return self.value

    def __exit__(self, *exc):
        _HOOK.reset(self.token)
        return False


def planning():
    """`with planning() as plan:` — every model call of the generation's
    first round is listed in plan["requests"], none sent."""
    return _Scope({"mode": "plan", "requests": [], "lock": threading.Lock()})


def answering(key, message):
    """`with answering(key, message):` — the call matching `key` reads
    `message` instead of calling the model, once. Kept for an auto-draft
    queue row enqueued before ARCH-15 with its one answer in the payload."""
    return _Scope({"mode": "answer", "key": key, "message": message, "used": False, "lock": threading.Lock()})


def plan_requests(restaurant_id, week_start=None) -> list:
    """The model requests the restaurant's next draft would send first, all
    of them (the round of independent calls), or [] when it would send none
    (a code draft) or could not be built."""
    import schedule_engine as se
    try:
        with planning() as plan:
            out = se._build_schedule_result(restaurant_id, week_start=week_start)
    except Exception as e:
        print(f"[schedule_batch] restaurant {restaurant_id}: no request to batch ({e!r})")
        return []
    if not isinstance(out, dict) or out.get("planned_calls") is None:
        return []
    return list(out["planned_calls"]) or list(plan["requests"])


def submit_auto_draft(r, period=None, week_start=None, target=None, attempt=None):
    """The auto-draft's planned calls sent as one group of batch items.
    "submitted" when every one went (the answers finish the draft,
    on_auto_draft); "blocked" when a gate refused any (nothing will be
    drafted — the synchronous calls would have been refused too); None when
    the draft must run now (batches off here, nothing to batch, a submit
    failed — the items that did go are cancelled). Never raises."""
    try:
        import ai_batches
        if not ai_batches.enabled(WORKFLOW):
            return None
        from datetime import datetime, timedelta
        import data_health
        import schedule_output
        requests = plan_requests(r.id, week_start=week_start)
        if not requests:
            return None
        group = f"sd-{int(r.id)}-{uuid.uuid4().hex[:12]}"
        cutoff = datetime.utcnow() + timedelta(hours=BATCH_CUTOFF_HOURS)
        items = []
        for i, request in enumerate(requests):
            items.append({
                "custom_id": f"{group}-{i}", "restaurant_id": r.id, "action": "labor_schedule", "request": request,
                "readiness": request.get("readiness") or data_health.NOT_APPLICABLE, "callback": CALLBACK,
                "cutoff_at": cutoff,
                "context": {"group": group, "n": len(requests), "i": i, "key": call_key(request),
                            "prompt_sig": schedule_output.prompt_sig(request), "period": period,
                            "week_start": week_start, "target": target, "attempt": attempt}})
        res = ai_batches.submit(WORKFLOW, items)
        status = [res.get(it["custom_id"]) for it in items]
        if all(s == ai_batches.SUBMITTED for s in status):
            return "submitted"
        if any(s == ai_batches.BLOCKED for s in status):
            return "blocked"
        for it, s in zip(items, status):
            if s == ai_batches.SUBMITTED:
                ai_batches.cancel(WORKFLOW, it["custom_id"])
        return None
    except Exception as e:
        import ops
        ops.capture(e, job="auto_draft_schedule", context=f"restaurant_id={getattr(r, 'id', None)} batch submit")
        return None


def _store_item(rid, ctx, message=None, outcome=ANSWERED, error=None):
    """One item of a group, stored as a schedule_model_calls row of the
    group: its answer (read by call key when the draft runs) or why none."""
    import schedule_output
    usage, text, stop, model = {}, None, None, None
    if message is not None:
        import labor
        from ai_utils import extract_text
        text = extract_text(message)
        stop = getattr(message, "stop_reason", None)
        model = getattr(message, "model", None)
        usage = dict(labor._usage_of(message), batch=True, prompt_sig=ctx.get("prompt_sig"),
                     call_id=getattr(message, "_cavnar_call_id", None))
    return schedule_output.record_call(rid, {}, answer=text, model=model, stop_reason=stop, outcome=outcome,
                                       error=error, usage=usage, job_id=ctx.get("group"),
                                       call_key=ctx.get("key"))


def _group_state(group, n):
    """(settled, blocked) for a group: how many of its n items are stored,
    and whether a gate refused any."""
    import schedule_output
    conn = schedule_output.get_conn(None)
    try:
        rows = conn.execute("SELECT outcome FROM schedule_model_calls WHERE job_id=?", (str(group),)).fetchall()
    finally:
        conn.close()
    outcomes = [r[0] for r in rows]
    return len(outcomes) >= int(n or 1), BLOCKED in outcomes


def on_auto_draft(item, message=None, error=None):
    """ai_batches' callback, once per item of a group: the item stored; when
    it is the group's last, the auto-draft runs with every stored answer read
    by call key (schedule_resume.resuming(group)). A gate's refusal of any
    item drafts nothing."""
    import ai_batches
    import ops
    rid = (item or {}).get("restaurant_id")
    ctx = (item or {}).get("context") or {}
    if not rid:
        return
    group = ctx.get("group")
    if not group:
        return _on_legacy_item(rid, ctx, message, error)
    if error is not None and not isinstance(error, ai_batches.BatchItemFailed):
        _store_item(rid, ctx, outcome=BLOCKED, error=error)
        return
    if error is None and message is not None:
        _store_item(rid, ctx, message=message)
    else:
        _store_item(rid, ctx, outcome=FAILED, error=error or "no answer")
    settled, blocked = _group_state(group, ctx.get("n"))
    if not settled or blocked:
        return
    _run_group(rid, ctx, group)


def _run_group(rid, ctx, group):
    import ops
    from models import get_restaurant
    r = get_restaurant(rid)
    if r is None or not (getattr(r, "auto_draft_schedule", 0) and getattr(r, "module_labor", 0)):
        return
    import strategy_jobs as sj
    import schedule_engine as se
    target = ctx.get("week_start") or ctx.get("target")
    if target:
        # The owner may have made the week while the batch was out: theirs
        # stands, and the answers are not used.
        conn = sj.get_conn(sj.DB_PATH)
        try:
            if sj._week_has_schedule(conn, rid, target):
                return
        finally:
            conn.close()
    import job_queue
    if job_queue.enabled():
        # The draft is a job_queue row like any auto-draft (RM-7), keyed so
        # the group's last two callbacks never queue it twice.
        try:
            sj.enqueue_auto_draft(r, period=ctx.get("period"), target=ctx.get("target"), attempt=ctx.get("attempt"),
                                  batch=False, answers_job=group)
        except Exception as e:
            ops.capture(e, job="auto_draft_schedule", context=f"restaurant_id={rid} (batch enqueue)")
        return
    if not ops.claim_marker("auto_draft_batch_ran", group):
        return
    counts = {"drafted": 0, "skipped": 0, "failed": 0}

    def _bump(k):
        counts[k] = counts.get(k, 0) + 1
    import schedule_resume
    try:
        with schedule_resume.resuming(group):
            sj._draft_one(r, sj.DB_PATH, se, _bump, period=ctx.get("period"), target=ctx.get("target"),
                          attempt=ctx.get("attempt"), batch=False)
    except Exception as e:
        ops.capture(e, job="auto_draft_schedule", context=f"restaurant_id={rid} (batch)")


def _on_legacy_item(rid, ctx, message, error):
    """An item submitted before ARCH-15 (one per draft, no group): its
    answer rides the queue row as it did."""
    import ai_batches
    import ops
    if error is not None and not isinstance(error, ai_batches.BatchItemFailed):
        return
    from models import get_restaurant
    r = get_restaurant(rid)
    if r is None or not (getattr(r, "auto_draft_schedule", 0) and getattr(r, "module_labor", 0)):
        return
    import strategy_jobs as sj
    import job_queue
    if not job_queue.enabled():
        import schedule_engine as se
        counts = {}

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
        return
    answer = None
    if error is None and message is not None:
        try:
            from ai_utils import extract_text
            import labor
            answer = {"key": ctx.get("key"), "text": extract_text(message),
                      "stop_reason": getattr(message, "stop_reason", None),
                      "model": getattr(message, "model", None), "usage": labor._usage_of(message),
                      "call_id": getattr(message, "_cavnar_call_id", None)}
        except Exception as e:
            ops.capture(e, job="auto_draft_schedule", context=f"restaurant_id={rid} (batch answer unread)")
    try:
        sj.enqueue_auto_draft(r, period=ctx.get("period"), target=ctx.get("target"), attempt=ctx.get("attempt"),
                              batch=False, answer=answer)
    except Exception as e:
        ops.capture(e, job="auto_draft_schedule", context=f"restaurant_id={rid} (batch enqueue)")
