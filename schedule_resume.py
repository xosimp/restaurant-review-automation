"""
schedule_resume.py — a generation the queue runs again after a deploy picks
up where its earlier try stopped (scheduler audit 10/10/26: SCALE-2,
SCALE-22, RM-7).

A deploy used to kill every generation in flight: the boot sweep failed its
job ("Generation was interrupted — please try again"), the owner pressed
again and paid again for model answers already stored in
schedule_model_calls. A generation is now a job_queue row; a try that dies
leaves the row running with a lease that runs out (or, at a clean SIGTERM,
handed back at once — job_queue.shutdown), and the next consumer claims it
again. This module is what that next try does differently:

  * THE SAVED STAGE. Once the draft is saved its job row's checkpoint holds
    the finished answer (checkpoint_saved: the payload the owner's poll
    reads, compressed). A try that finds it finishes the job with that
    payload — nothing is generated again. Since scheduler fix round 10/10/26
    (C2, ARCH-17) the quality gate's passes are held in memory and only the
    winner is saved, once, so the saved checkpoint is the job's last stage
    before its finish; a try that died before it saved nothing and runs
    again (below). The saved draft is NOT put back in force over a newer
    one: the job saved nothing after it, so any later draft is somebody's
    newer work (blind re-audit 10/11/26 BUGSGEN-11 — it used to supersede
    them), and an owner's Stop after the save is honoured
    (schedule_engine._finish_stopped).
  * EACH MODEL ANSWER. Every schedule call is recorded with its queue job
    and call key (schedule_output.record_call: model, dates, output schema —
    schedule_batch.call_key). Under resuming() the call site
    (labor.generate_optimized_schedule, through schedule_batch.answer_for)
    gets the earlier try's answer for the same key, in order, each once —
    a message read from storage, zero tokens, its usage marked `resumed`.
    A call whose key matches nothing stored (the roster changed the schema)
    goes out as usual.
  * THE DETERMINISTIC STAGES (inputs, rules, repair, sweep, score) run
    again: they are the code's own work, seconds not dollars, and given the
    same inputs and the same answers they reach the same draft. Their
    boundaries are recorded as the job's stages (job_queue.checkpoint, from
    schedule_engine._StageClock) so the Jobs page and the resume can say
    how far a try got.
"""
import base64
import contextlib
import contextvars
import json
import threading
import time
import zlib

_STATE = contextvars.ContextVar("schedule_resume_state", default=None)

# How long past a resumed try's start its owner's poll waits before calling
# it dead: a deploy takes a minute or two, and the try that died set its
# deadline for its own run.
RESUME_GRACE_SECONDS = 20 * 60


class StoredMessage:
    """An answer read back from schedule_model_calls, shaped like the SDK's
    Message as far as the schedule call site reads one (content, stop_reason,
    usage). Zero tokens: nothing was spent on it this time."""

    class _Text:
        type = "text"

        def __init__(self, text):
            self.text = text

    class _Usage:
        def __init__(self):
            self.input_tokens = 0
            self.output_tokens = 0
            self.cache_read_input_tokens = 0
            self.cache_creation_input_tokens = 0

    def __init__(self, text, stop_reason=None, model=None, record_id=None):
        self.content = [StoredMessage._Text(text)]
        self.stop_reason = stop_reason or "end_turn"
        self.stop_details = None
        self.model = model
        self.usage = StoredMessage._Usage()
        self._cavnar_resumed = True
        self._cavnar_call_id = None
        self.resumed_from = record_id


@contextlib.contextmanager
def resuming(job_id):
    """Within: the schedule calls of this generation read the answers the
    queue job `job_id`'s earlier tries stored, instead of calling the model
    (stored_answer). `job_id` None: calls go out as usual."""
    if not job_id:
        yield None
        return
    import schedule_output
    answers = schedule_output.stored_answers(job_id)
    state = {"job_id": job_id, "lock": threading.Lock(), "by_key": {}, "used": [], "moved": []}
    for a in answers:
        state["by_key"].setdefault(a["call_key"], []).append(a)
    token = _STATE.set(state)
    try:
        yield state
    finally:
        _STATE.reset(token)


def stored_answer(request):
    """The stored answer for this call when a resumed generation has one
    (the next unused answer with the same call key), else None."""
    state = _STATE.get()
    if not state or not state["by_key"]:
        return None
    try:
        import schedule_batch
        key = schedule_batch.call_key(request)
    except Exception:
        return None
    with state["lock"]:
        queue = state["by_key"].get(key) or []
        if not queue:
            return None
        a = queue.pop(0)
        state["used"].append(a["id"])
    # The answer was written for the prompt the dead try built; the prompt
    # this try builds is read from the database as it is NOW. When they
    # differ the inputs moved meanwhile (time off approved, a person gone,
    # a week published): said on the draft (inputs_moved, ARCH-25).
    try:
        import schedule_output
        if a.get("prompt_sig") and schedule_output.prompt_sig(request) != a["prompt_sig"]:
            with state["lock"]:
                state["moved"].append(a["id"])
    except Exception as e:
        print(f"[schedule] resumed: prompt not compared ({e!r})")
    msg = StoredMessage(a["answer"], stop_reason=a.get("stop_reason"), model=a.get("model"), record_id=a["id"])
    u = a.get("usage") or {}
    if u.get("batch"):
        # An auto-draft's Message Batches answer (schedule_batch, ARCH-15):
        # paid, at the batch rate — recorded with its tokens and as a batch.
        msg._cavnar_resumed = False
        msg._cavnar_batch = True
        msg._cavnar_call_id = u.get("call_id")
        msg.usage.input_tokens = int(u.get("input_tokens") or 0)
        msg.usage.output_tokens = int(u.get("output_tokens") or 0)
        msg.usage.cache_read_input_tokens = int(u.get("cache_read_tokens") or 0)
        msg.usage.cache_creation_input_tokens = int(u.get("cache_write_tokens") or 0)
        print(f"[schedule] the batched answer {a['id']} used for its call")
        return msg
    print(f"[schedule] resumed: call {a['id']}'s stored answer read again, not paid for")
    return msg


def answers_reused() -> list:
    state = _STATE.get()
    return list(state["used"]) if state else []


def inputs_moved() -> list:
    """The stored answers this resumed try reused whose prompt differs from
    the one it built now: written from inputs that have since moved."""
    state = _STATE.get()
    return list(state.get("moved") or []) if state else []


# What a resumed draft says when the answers it reused were written from
# inputs that moved while the job restarted (blind re-audit 10/11/26
# ARCH-25): the rows are re-checked against the inputs as they are now —
# every rule, the repair loop and the score read the live tables — but the
# model wrote them from the earlier ones.
INPUTS_MOVED_LINE = ("⚠ Cavnar AI restarted while writing this draft and reused the AI model's answer it had "
                     "already paid for — but your team's details changed in between, so that answer was written "
                     "from the earlier details. Every rule was checked again against them as they are now; read "
                     "the week through before you publish.")


def resume_facts() -> dict:
    """{"resumed", "answers_reused", "inputs_moved"} for the draft's payload
    and review — {"resumed": False} outside a resumed try."""
    state = _STATE.get()
    if not state:
        return {"resumed": False}
    return {"resumed": True, "answers_reused": len(state["used"]), "inputs_moved": bool(state.get("moved"))}


# ── the saved stage ─────────────────────────────────────────────────────────

def pack(payload) -> str:
    return base64.b64encode(zlib.compress(json.dumps(payload, default=str).encode("utf-8"))).decode("ascii")


def unpack(text):
    try:
        return json.loads(zlib.decompress(base64.b64decode(text)).decode("utf-8"))
    except Exception:
        return None


def checkpoint_saved(history_id, payload, week_start=None) -> bool:
    """The generation's draft is saved (the job's commit point): its job row
    keeps the finished payload, so a later try finishes with it."""
    import job_queue
    return job_queue.checkpoint("saved", history_id=int(history_id) if history_id else None,
                                week_start=week_start, payload_z=pack(payload))


# The line a draft finished from its saved checkpoint carries: the earlier
# try saved it and a restart cut the try off before it said so.
RESUMED_SAVED_LINE = ("Cavnar AI restarted just after it saved this draft; the draft is exactly as it was "
                      "saved.")


def finish_from_checkpoint(job) -> bool:
    """A resumed try whose earlier try reached the saved stage: the job is
    finished with that try's payload (`resumed: true`) — `done`, or, when
    the owner pressed Stop after the save, as the Stop says
    (schedule_engine._finish_stopped puts back the draft in force before the
    press). Nothing else is touched: the saved draft is never put back over
    a later draft (BUGSGEN-11). A job a poll or the boot sweep already
    finished keeps its verdict, unless that verdict is the provisional
    "saved but didn't finish" (ops.SAVED_UNFINISHED), which this replaces.
    True when the job was settled here (nothing to generate again)."""
    import ops
    cp = job.checkpoint if hasattr(job, "checkpoint") else (job.get("checkpoint") or {})
    if "saved" not in (cp.get("stages") or []) or not cp.get("payload_z"):
        return False
    payload = unpack(cp["payload_z"])
    if not isinstance(payload, dict):
        return False
    p = job.payload if hasattr(job, "payload") else (job.get("payload") or {})
    job_id, rid = p.get("job_id"), p.get("restaurant_id")
    hid = cp.get("history_id") or payload.get("history_id")
    payload = dict(payload, resumed=True)
    review = dict(payload.get("review") or {})
    review["lines"] = list(review.get("lines") or []) + [RESUMED_SAVED_LINE]
    payload["review"] = review
    state = ops.job_stop_state(job_id) if job_id else "running"
    if state == "stop_requested":
        # The owner's Stop landed after the save: what is in force is the
        # Stop's to say, as the job itself would have said it.
        import types
        import schedule_engine
        clock = types.SimpleNamespace(prior_draft_id=p.get("prior_draft_id"))
        schedule_engine._finish_stopped(job_id, rid, clock, int(hid) if hid else None, payload)
        return True
    ops.finish_async_job(job_id, "done", payload)
    return True


def extend_deadline(job_id, seconds=RESUME_GRACE_SECONDS) -> None:
    """A resumed try gets its own time: the dead try's deadline would have
    a poll call it dead while it starts."""
    import ops
    ops.set_async_job_deadline(job_id, time.time() + seconds)
