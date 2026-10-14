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
    (schedule_engine._finish_stopped, through schedule_job.finish_from_checkpoint).
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
import time

# A resumed try's stored answers (resuming, stored_answer, what it reused) live
# in schedule_output beside the call records; re-exported here.
from schedule_output import (  # noqa: F401
    _STATE, INPUTS_MOVED_LINE, StoredMessage, answers_reused, inputs_moved, resume_facts, resuming, stored_answer)

from schedule_context import pack, unpack  # noqa: F401  (a checkpoint's payload; re-exported)


# How long past a resumed try's start its owner's poll waits before calling
# it dead: a deploy takes a minute or two, and the try that died set its
# deadline for its own run.
RESUME_GRACE_SECONDS = 20 * 60


# ── the saved stage ─────────────────────────────────────────────────────────


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


def extend_deadline(job_id, seconds=RESUME_GRACE_SECONDS) -> None:
    """A resumed try gets its own time: the dead try's deadline would have
    a poll call it dead while it starts."""
    import ops
    ops.set_async_job_deadline(job_id, time.time() + seconds)
