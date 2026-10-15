"""
schedule_batch.py — the unattended auto-draft through Message Batches.

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
  on the draft (schedule_resume.inputs_moved).

  NO ANSWER (errored, expired, past its cutoff — BatchItemFailed): that
  call goes out synchronously when the draft runs. A gate's refusal
  (budget, readiness, the provider breaker) of any item writes nothing: the
  synchronous call would have been refused too.

A batched answer's usage is marked `batch` (schedule_model_calls.usage_json),
so schedule_metrics prices it at ai_utils.BATCH_PRICE_MULTIPLIER; ai_batches
ledgers it in ai_usage at the batch rate when it lands.
"""
import uuid

# The call site's hooks (plan_call, answer_for, CallPlanned, call_key) live in
# schedule_output, below labor; re-exported here.
from schedule_output import (  # noqa: F401
    _HOOK, CallPlanned, _dates_of, _Scope, answer_for, answering, call_key, plan_call, planning)
import logging

log = logging.getLogger(__name__)

from schedule_context import degraded as _degraded  # noqa: E402  (the one degradation event)

WORKFLOW = "labor_schedule"
CALLBACK = "schedule_batch:on_auto_draft"
# How long the auto-draft waits for its batch before it writes the week
# synchronously: the owner reads it later the same day.
BATCH_CUTOFF_HOURS = 6
# The schedule_model_calls outcomes a group's items leave.
ANSWERED, FAILED, BLOCKED = "batch_answer", "batch_failed", "batch_blocked"


def plan_requests(restaurant_id, week_start=None) -> list:
    """The model requests the restaurant's next draft would send first, all
    of them (the round of independent calls), or [] when it would send none
    (a code draft) or could not be built."""
    import schedule_engine as se
    import learning_stats as _ls_scope
    try:
        # The week's learning_v2 arm holds for this build only.
        with planning() as plan, _ls_scope.learning_scope():
            out = se._build_schedule_result(restaurant_id, week_start=week_start)
    except Exception as e:
        _degraded(f"restaurant {restaurant_id}: no request to batch ({e!r})", e)
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
    # The draft is a job_queue row like any auto-draft (the one path
    # since), keyed so the group's last two callbacks never queue it twice.
    try:
        sj.enqueue_auto_draft(r, period=ctx.get("period"), target=ctx.get("target"), attempt=ctx.get("attempt"),
                              batch=False, answers_job=group)
    except Exception as e:
        ops.capture(e, job="auto_draft_schedule", context=f"restaurant_id={rid} (batch enqueue)")


def _on_legacy_item(rid, ctx, message, error):
    """An item submitted before the batched answers (one per draft, no group): its
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
