"""
schedule_job.py — one schedule generation as a pipeline of named stages
(scheduler audit 10/10/26 ARCH-16, ARCH-17, ARCH-26).

The job used to be one 1,700-line function (`_run_schedule_job`) that
re-entered itself for the quality gate's fill, its model rewrite, a redo
drafted in code first and a redo run again over an edited draft, steered by
private keyword flags, `locals().get(...)` and `inspect.signature`, and that
SAVED each attempt: the first draft superseded the owner's, the gate's fill
and rewrite were saved over it, and the loser was put back with
`_restore_draft`. For minutes another device saw a draft that could still be
replaced, one-tap Send could publish it, and history filled with drafts
nobody chose.

Now:

    run(Request)                         the job: one GenerationRun, no recursion
      ├─ _owner_redo / _week             which passes the request needs
      │    └─ _build_pass(PassSpec)      ONE pass, held in memory (a Candidate):
      │         draft    — the model, the code (carry-forward / solver), or an
      │                    answer reused (the gate's fill, a re-run)
      │         scope    — a redo keeps only its dates; the kept days are the
      │                    owner's rows exactly as saved
      │         parse    — typed rows (or the CSV contract's lines), close cap,
      │                    hours from the times, the planned managers restored
      │         rules    — the rules read again after the model (P-41)
      │         signals  — the frozen context: signals, curve, requirements, arms
      │         repair   — the ranked repair loop (repair_week)
      │         review   — one sweep of the finished rows and the review
      │         score    — Shift Quality
      │         finish   — the diff, the stored text, what the week does not meet
      │         note     — Cavnar AI's note (a model call)
      │         inputs   — what the draft went without
      │    └─ _gate (week only)          the quality gate: at most GATE_ATTEMPTS
      │                                  more passes (the fill in code, then the
      │                                  model's rewrite), each judged against
      │                                  the candidate in force — never saved
      └─ _commit(winner)                 the ONE save: the week, its version, its
                                         calls, its arm, in one transaction; then
                                         the after-save bookkeeping and the finish

Every stage boundary checks the Stop (`_check_stop`: the owner's Stop is
ScheduleStopped, a poll that called the job dead is "didn't finish in time")
and records the boundary on the queue row (job_queue.checkpoint — what
schedule_resume reads). Nothing is saved before `_commit`, so a Stop or a
deploy before it leaves the owner's draft exactly as it was; a resumed try
runs the passes again reading every stored model answer once
(schedule_resume), and a try past the save finishes from the saved
checkpoint.

The helpers each stage calls stay in schedule_engine and are reached through
the module (`se.NAME`) at call time, so a test that patches one patches the
job. schedule_engine._run_schedule_job is the entry point every caller uses.
"""
import copy
import time
import traceback
from dataclasses import dataclass, field

import schedule_engine as se
import schedule_time as _stime

# The quality gate's passes after the first draft: the fill in code, then the
# model's rewrite (each at most once). A loop over this, never a recursion.
GATE_ATTEMPTS = ("fill", "rewrite")


# ── the request and the state one generation carries ─────────────────────

@dataclass
class Request:
    """What the press (or the auto-draft) asked for."""
    job_id: object
    restaurant_id: int
    week_start: object = None
    dates: list = None
    base_history_id: object = None
    focus: list = None
    gate: bool = True
    instruction: str = None
    keep_locked: bool = True

    @property
    def redo(self) -> bool:
        return bool(self.dates and self.base_history_id)


@dataclass
class Base:
    """The draft a redo pass writes over: its rows and its text as read, and
    the saved draft they came from (an owner's redo — the save goes ahead
    only over exactly this text, PIPE-2) or None (a candidate held in memory:
    the quality gate's passes read the first draft before anything is saved)."""
    rows: list
    csv: str
    history_id: object = None
    week_start: object = None
    # A candidate held in memory: what its own pass said about the whole
    # week (notes, where each day came from, days not written, stage
    # failures) — carried into a gate pass that wins (BUGSGEN-10).
    facts: dict = None


@dataclass
class PassSpec:
    """One pass of the pipeline: what it drafts and over what.

    kind — "week" (the whole week), "redo" (an owner's chosen days),
    "gate_fill" (the gate's days re-run through the repair loop from the
    draft's own rows, no model), "gate_rewrite" (the gate's days rewritten by
    the model with what was wrong with them)."""
    kind: str
    dates: list = None
    base: Base = None
    answer: dict = None          # a model answer reused: nothing paid twice
    focus: list = None
    code_redo: bool = False      # drafted in code first (AI-28)
    gate: dict = None            # the gate this pass answers
    # How a reused answer was drafted ("code" / "model"): a re-run over an
    # edited draft reuses a code redo's answer, and its saved redo said
    # "model" (blind re-audit 10/11/26 BUGSGEN-15).
    redo_how: str = None

    @property
    def redo(self) -> bool:
        return bool(self.dates and self.base is not None)

    @property
    def by(self) -> str:
        return "gate" if (self.focus or self.kind in ("gate_fill", "gate_rewrite")) else "owner"


@dataclass
class Candidate:
    """One finished pass, held in memory until the job chooses what it saves."""
    spec: PassSpec
    result: dict
    rows: list
    payload: dict
    timer: object
    dropped_rows: list = field(default_factory=list)
    hours_scheduled: float = 0.0
    metrics_in: dict = None
    gen_attr: dict = None
    pass_sig: object = None
    pass_w: object = None
    answer: dict = None          # the answer as drafted, for a re-run over an edited draft
    gate_answer: dict = None     # the week's answer before any pass, for the gate's fill
    code_redo_on: bool = False
    pinned: list = field(default_factory=list)
    noted: bool = False          # Cavnar AI's note written (the winner only — ARCH-13)
    _rules: dict = None

    def view(self) -> dict:
        """The finished week as the gate reads it (P-43): its rows beside its
        sweep, its rules and its quality."""
        return dict(self.result, rows=self.rows)

    @property
    def score(self):
        return (self.result.get("quality") or {}).get("score")

    @property
    def quality(self) -> dict:
        return self.result.get("quality") or {}

    def rules(self) -> dict:
        """{"hard", "unmanaged"} of the finished week (_gate_rules), read once."""
        if self._rules is None:
            self._rules = se._gate_rules(self.view())
        return self._rules


@dataclass
class InForce:
    """The candidate the gate's next pass is judged against, and the payload
    the job finishes with if nothing beats it."""
    cand: Candidate
    payload: dict


class GenerationRun:
    """The state one job carries through its stages: the request, the clock,
    every pass built (for the save's links and metrics), and what was saved."""

    def __init__(self, req: Request, clock):
        self.req = req
        self.clock = clock
        self.passes = []
        self.history_id = None
        self.payload = None
        self.week_dates = []


class _CodeRedoDeclined(Exception):
    """The code's redo could not draft the days, or changed nothing on them:
    the model redoes them (AI-28)."""


# ── stage boundaries ─────────────────────────────────────────────────────

def _check_stop(clock) -> None:
    """Between two stages: the owner's Stop is ScheduleStopped (said as
    stopped — BUGSGEN-12); a job a poll already called dead starts nothing
    more and is "didn't finish in time" (P-22)."""
    if clock is None:
        return
    if clock.stop_requested():
        raise se.ScheduleStopped(se.STOPPED_MESSAGE)
    if clock.stopped():
        raise se.ScheduleGenerationError(se._TIMED_OUT_MESSAGE)


def _boundary(run: GenerationRun, name: str) -> None:
    """A stage boundary: the Stop checked, and the queue row told how far the
    job got (job_queue.checkpoint — a no-op outside the queue)."""
    _check_stop(run.clock)
    try:
        import job_queue as _jq
        _jq.checkpoint(name)
    except Exception as e:
        print(f"[schedule] checkpoint {name} not kept: {e!r}")


# ── the job ──────────────────────────────────────────────────────────────

def run(req: Request) -> None:
    """One generation, start to finish; reports its own outcome into the
    job store (ops.finish_async_job). Never re-enters itself."""
    if req.dates and not req.focus:
        req.gate = False
    if req.job_id and not req.focus and not se._ops.job_still_pending(req.job_id):
        # Called dead while it waited for a slot (a poll past its age, the
        # boot sweep): the owner was told it didn't finish, so nothing is
        # paid for now (re-audit 10/4/26 PIPE-9).
        print(f"[schedule] job {req.job_id} was failed before it started — not run")
        return
    job = GenerationRun(req, se.current_clock())
    import learning_stats as _ls_scope
    # The learning_v2 switchback's off week turns every learned input off
    # for this generation only (blind re-audit 10/11/26 LEARNING-14; set in
    # schedule_engine._build_schedule_result where the week's arm is known).
    with _ls_scope.learning_scope():
        try:
            winner = _owner_redo(job) if req.redo else _week(job)
            _finish(job, winner)
        except Exception as e:
            _fail(job, e)


def _finish(job: GenerationRun, winner: Candidate) -> None:
    """After the one save: a Stop that landed while it was written says what
    is in force (ARCH-2); otherwise the job is done with the saved draft —
    even when a poll called it dead while the after-save steps ran: the
    draft is saved and in force, so only the OWNER's Stop changes what the
    job says (blind re-audit 10/11/26 BUGSGEN-13: the poll's verdict made the
    owner read "didn't finish in time" over a draft that had replaced
    theirs). The poll yields to a saved job (ops.read_async_job, saved_ref)
    and the finish replaces its provisional verdict."""
    se._raise_if_stopped(job.clock, owner_only=True)
    se._ops.finish_async_job(job.req.job_id, "done", se._json_ready(job.payload))


def _fail(job: GenerationRun, e: Exception) -> None:
    req = job.req
    if isinstance(e, se.ScheduleStopped):
        # The owner's Stop (ARCH-2, BUGSGEN-12): the job, which knows what it
        # saved, says what is in force.
        se._finish_stopped(req.job_id, req.restaurant_id, job.clock, job.history_id, job.payload)
        return
    print(f"[schedule job] FAILED:\n{traceback.format_exc()}")
    se._run_failed(e)
    try:
        se._ops.capture(e, job="schedule_generate", context=f"restaurant_id={req.restaurant_id}")
    except Exception:
        pass
    if job.history_id:
        # The draft was saved before this failed (re-audit 10/4/26 PIPE-8):
        # it is the week's draft in force now, so the owner is never told
        # "nothing was saved" and sent to pay for another.
        se._finish_after_save(req.job_id, req.restaurant_id, job.history_id, job.payload, job.week_dates)
        return
    # The owner gets a sentence that says what stopped it (P-44), never the
    # exception or the traceback.
    se._ops.finish_async_job(req.job_id, "error",
                             {"ok": False, "error": se.generation_error_message(e, job.clock)})


# ── which passes a request needs ─────────────────────────────────────────

def _read_base(req: Request) -> Base:
    """The draft an owner's redo writes over, exactly as read now: the save
    goes ahead only over this (PIPE-2 — models.save_schedule_history
    `base`)."""
    from models import get_schedule_history_detail as _gshd
    b = _gshd(int(req.base_history_id), req.restaurant_id) or {}
    text = b.get("schedule_csv") or ""
    return Base(rows=se._versions.rows_from_csv(text), csv=text, history_id=int(req.base_history_id),
                week_start=b.get("week_start"))


def _refuse_dates_outside(base: Base, dates) -> None:
    """A redo's days must be days of the draft it rewrites (BUGSGEN-2)."""
    if not base.week_start or not dates:
        return
    week = set(se.week_dates_from(se._date_of(str(base.week_start)[:10])))
    outside = sorted({str(d)[:10] for d in dates} - week)
    if outside:
        from time_utils import mdy
        raise se.ScheduleGenerationError(
            "The days you picked (" + ", ".join(mdy(d) for d in outside[:3]) + ("…" if len(outside) > 3 else "")
            + f") aren't in this draft's week of {mdy(str(base.week_start)[:10])}, so nothing was redone. "
            "Open the week those days belong to and redo them there.")


def _owner_redo(job: GenerationRun) -> Candidate:
    """An owner's redo of some days (or a focused redo named by the caller):
    drafted in code first when the reason reads as rules (AI-28), else — or
    when the code changed nothing — by the model; saved only over the draft
    it read, and run again from the same answer over the draft as it now
    stands when only a kept day was edited meanwhile (PIPE-2), at most
    REDO_RERUNS_MAX times. No recursion: each turn is one more pass."""
    req = job.req
    base = _read_base(req)
    # A redo belongs to its draft's own week, whatever week the client named
    # or the restaurant's week start is now (blind re-audit 10/11/26
    # BUGSGEN-2: EJ's moved Monday -> Wednesday with drafts on file, and a
    # redo saved rows outside the week it was filed under, or refused as
    # "closed"); a day outside that week is refused, never drafted.
    week_start = base.week_start or req.week_start
    req.week_start = week_start
    _refuse_dates_outside(base, req.dates)
    code_first = False
    if not req.focus:
        # An owner's redo is drafted in code first (scheduler audit 10/10/26
        # AI-28): a one-day redo used to pay a whole week's thinking for a
        # few rows. The days are re-shaped and solved against every rule —
        # their reason read as typed rules — and go to the model only when
        # that changes nothing, or when the reason is a judgement code cannot
        # read (_redo_reads_as_rules). Never the quality gate's redo, never a
        # re-run over an edited draft.
        code_first = se._redo_reads_as_rules(req.restaurant_id, req.instruction, week_start)
    spec = PassSpec(kind="redo", dates=sorted(set(req.dates)), base=base, focus=req.focus, code_redo=code_first)
    reruns = 0
    while True:
        if spec.code_redo:
            try:
                cand = _build_pass(job, spec)
            except _CodeRedoDeclined as why:
                print(f"[schedule] {why} — the model redoes the days")
                spec = PassSpec(kind="redo", dates=spec.dates, base=spec.base, focus=spec.focus, code_redo=False)
                continue
        else:
            cand = _build_pass(job, spec)
        from models import DraftChanged as _DraftChanged
        try:
            _commit(job, cand)
            return cand
        except _DraftChanged as moved:
            verdict = se._redo_base_moved(moved, cand.spec.base.rows, spec.dates, cand.answer, reruns)
            if verdict != "again":
                raise se.ScheduleGenerationError(verdict)
            print("[schedule] the draft changed on a kept day while the redo ran — running again over it")
            reruns += 1
            # The model's answer as it came back, over the draft as it now
            # stands (PIPE-2): nothing is paid twice.
            spec = PassSpec(kind="redo", dates=spec.dates, base=_read_base(req), answer=cand.answer,
                            focus=spec.focus, code_redo=False,
                            redo_how=((cand.result.get("redo") or {}).get("how")
                                      or ("code" if cand.code_redo_on else "model")))


def _week(job: GenerationRun) -> Candidate:
    """A whole week: one pass, then the quality gate's passes held in memory,
    then the one save of the winner (ARCH-17)."""
    req = job.req
    started = _started_week(req)
    if started is None:
        first = _build_pass(job, PassSpec(kind="week", focus=req.focus))
    else:
        # The week has begun (blind re-audit 10/11/26 BUGSGEN-24): only the
        # days from today's business date are drafted and paid for; the days
        # already worked keep the working copy's rows.
        ahead, kept = started
        first = _build_pass(job, PassSpec(kind="week", focus=req.focus, dates=ahead, base=kept))
    winner = first
    if req.gate:
        try:
            winner = _gate(job, first)
        except se.ScheduleGenerationError:
            raise                      # a Stop, or a job called dead: said as that
        except Exception as _gx:
            # The gate is an improvement on a sound draft: one that crashed
            # leaves the draft as it was, captured (P-17) — never a lost week.
            print(f"[schedule] quality gate failed: {_gx!r}")
            se._ops.capture(_gx, job="schedule_quality_gate", context=f"restaurant_id={req.restaurant_id}")
            first.payload["gate"] = _gate_not_run("error")
            winner = first
    _commit(job, winner)
    return winner


def _started_week(req: Request):
    """(the week's days from today's business date on, Base of the days
    already worked) when a whole-week Generate names a week that has begun,
    else None (BUGSGEN-24: a week three days gone was drafted whole, its
    past days paid for and saved over what was worked). The past days keep
    the week's working copy's rows (none: they are closed to every pass)."""
    if req.dates or not req.week_start:
        return None                  # the default week is the first one still ahead
    import time_utils
    restaurant = se.get_restaurant(req.restaurant_id)
    now = time_utils.restaurant_now(restaurant, naive=True)
    try:
        today = time_utils.business_date(restaurant, now).isoformat()
    except Exception:
        today = now.date().isoformat()
    monday = se._week_monday(now, req.week_start, start_day=se.week_start_day_for(restaurant, req.week_start))
    week = se.week_dates_from(monday)
    past = [d for d in week if d < today]
    if not past or len(past) == len(week):
        return None
    rows = []
    try:
        import models
        conn = models.get_conn()
        try:
            row = models.week_draft(conn, req.restaurant_id, week[0])
        finally:
            conn.close()
        if row is not None:
            rows = [r for r in se._versions.rows_from_csv(row["schedule_csv"] or "") if r.get("date") in set(past)]
    except Exception as e:
        se._soft_fail("the week's working copy", e, req.restaurant_id)
    return [d for d in week if d not in past], Base(rows=rows, csv="", history_id=None, week_start=week[0])


# ── one pass ─────────────────────────────────────────────────────────────

def _build_pass(job: GenerationRun, spec: PassSpec) -> Candidate:
    """One pass of the pipeline, every stage in order, nothing saved."""
    req = job.req
    rid = req.restaurant_id
    timer = se._StageClock()
    _boundary(job, "start")
    result = _stage_draft(job, spec, timer)
    cand = Candidate(spec=spec, result=result, rows=[], payload={}, timer=timer, code_redo_on=spec.code_redo)
    if spec.redo:
        try:
            cand.answer = copy.deepcopy(result)
        except Exception as _cpx:
            # Without a copy an edit on a kept day refuses the save instead
            # of running again (said, never lost).
            se._ops.capture(_cpx, job="schedule_generate", context=f"restaurant_id={rid} — redo answer copy")
    if spec.kind == "week" and req.gate and spec.answer is None:
        # The model's answer as it came back, before any pass changed it: the
        # quality gate's fill in code re-runs the gate's dates from it (AI
        # cost audit 10/7/26 #21). Without a copy the gate goes straight to
        # the model's rewrite, as it did before.
        try:
            cand.gate_answer = copy.deepcopy(result)
        except Exception as _gcx:
            print(f"[schedule] gate: answer not copied, no fill in code: {_gcx!r}")
    editable, kept_rows = _stage_scope(cand)
    if spec.kind in ("gate_fill", "gate_rewrite") and spec.base is not None and spec.base.facts:
        # The days the gate keeps were built by the first pass: what it said
        # about them leads this pass's review too (BUGSGEN-10).
        _inherit_week_facts(cand.result, spec.base.facts, spec.dates)
    try:
        ctx = _stage_parse(job, cand, editable, kept_rows)
        _boundary(job, "parsed")
        _stage_rules(cand, ctx)
        _boundary(job, "rules_read")
        _stage_signals(cand, ctx)
        _boundary(job, "signals_read")
        _stage_repair(job, cand, ctx, editable)
        _boundary(job, "repaired")
        _stage_review(cand, ctx)
        _boundary(job, "reviewed")
        _stage_score(cand, ctx)
        _boundary(job, "scored")
        _stage_rows_out(cand, ctx)
    except se.ScheduleGenerationError:
        raise                      # already a sentence the owner can read
    except _CodeRedoDeclined:
        raise
    except Exception as _csv_ex:
        # Every safeguard lives inside these stages. A crash there used to
        # fall through to save and ship the raw model text as a finished week
        # with "0 hard"; now it is the generation's failure.
        se._ops.capture(_csv_ex, job="schedule_checks", context=f"restaurant_id={rid}")
        raise se.ScheduleGenerationError("The draft was written but its checks failed, so nothing was saved. "
                                         "Try again.")
    _stage_requirements_met(cand, rid)
    # No note here: it is a model call, written for the pass the job saves
    # (_commit), never for a gate pass that loses (ARCH-13).
    _stage_inputs(cand)
    _boundary(job, "built")
    _stage_payload(cand, ctx)
    # How long the pass took outside the model, for the gate's fill (its
    # expected seconds — _gate_fill_seconds) and the stored timings.
    cand.result["stage_seconds"] = timer.as_dict()
    job.passes.append(cand)
    return cand


def _stage_draft(job: GenerationRun, spec: PassSpec, timer) -> dict:
    """The inputs and the draft: the model (and the code where the model
    could not write — schedule_fallback), the code alone for a redo drafted
    in code first (AI-28), or an answer reused as it came back."""
    req = job.req
    build_kw = {"week_start": req.week_start or (spec.base.week_start if spec.base is not None else None)}
    if req.keep_locked is False:
        build_kw["keep_locked"] = False
    if spec.focus:
        build_kw["focus"] = list(spec.focus)
    if req.instruction:
        build_kw["instruction"] = req.instruction
    if spec.redo:
        # A redo (the owner's, or the quality gate's) writes only its dates,
        # with the kept days handed to every call as the rest of the week
        # (schedule audit 10/3/26 P-9, E-21, PR-18). The manager plan of a
        # redo covers only its days, with the kept days' rows counted for
        # hours, rest and runs (schedule_skeleton).
        pinned = [r for r in spec.base.rows if r.get("date") not in set(spec.dates)]
        build_kw.update(dates=sorted(set(spec.dates)), prior_rows=pinned)
    if spec.code_redo:
        build_kw["code_redo"] = True
    if spec.kind == "week" and spec.dates:
        build_kw["started_week"] = True          # BUGSGEN-24
    with timer.stage("inputs_and_model"):
        if spec.answer is not None:
            # An answer reused as it came back (the gate's fill, a re-run
            # over a draft edited while the model wrote — PIPE-2).
            return copy.deepcopy(spec.answer)
        if spec.code_redo:
            try:
                return se._build_schedule_result(req.restaurant_id, **build_kw)
            except se.ScheduleStopped:
                raise
            except se.ScheduleGenerationError as _crx:
                # Code could not redraft the days (nothing it could legally
                # staff): the model redoes them (AI-28).
                raise _CodeRedoDeclined(f"the code's redo drafted nothing ({_crx})")
        return se._build_schedule_result(req.restaurant_id, **build_kw)


def _stage_scope(cand: Candidate):
    """A redo keeps only the dates it was asked for; the rest come from the
    draft the owner is keeping — and so does any asked-for day the model
    could not write, said in the review. (editable dates, kept rows)."""
    spec, result = cand.spec, cand.result
    if not spec.redo:
        return None, []
    dates = spec.dates
    cand.pinned = [r for r in spec.base.rows if r.get("date") not in set(dates)]
    unwritten = {u["date"] for u in (result.get("unwritten_dates") or [])}
    keep = set(dates) - unwritten
    lines = [ln for ln in result["schedule_csv"].split("\n")[1:] if ln.split(",", 1)[0].strip() in keep]
    result["schedule_csv"] = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + "\n".join(lines)
    if result.get("rows") is not None:
        result["rows"] = [r for r in result["rows"] if str(r.get("date") or "")[:10] in keep]
    # The days kept are the owner's rows, never model text (re-audit 10/4/26
    # PIPE-1): added after the parse exactly as saved, each pinned, a stale
    # NEEDS REVIEW mark taken off (PIPE-6).
    import schedule_skeleton as _skel_keep
    kept_rows = _skel_keep.kept_rows(cand.pinned + [r for r in spec.base.rows if r.get("date") in unwritten])
    if spec.kind == "week":
        # A week begun before today (BUGSGEN-24): a week, drafted from
        # today; its empty days are the generation's own "not written" line.
        past = sorted(set(result.get("week_dates") or []) - set(dates))
        result["started_week"] = {"kept_dates": past, "drafted_dates": sorted(set(dates))}
        result.setdefault("generation_notes", []).append(_started_week_line(past, sorted(set(dates))))
        return keep, kept_rows
    result["regenerated_dates"] = sorted(keep)
    if unwritten:
        result.setdefault("generation_notes", []).insert(0, se._not_redone_line(sorted(unwritten)))
    # The owner's reason rides `instruction` into the prompt and is recorded
    # from the request (schedule_versions.record_rejection).
    result["redo"] = {"dates": sorted(set(dates)),
                      # The saved draft it was written over; None for the
                      # gate's passes, whose base is the draft held in memory
                      # (ARCH-17 — nothing is saved before the gate decides).
                      "base_history_id": int(spec.base.history_id) if spec.base.history_id else None,
                      "by": spec.by,
                      # Redone in code, no model call (AI-28), or by the model.
                      "how": spec.redo_how or ("code" if cand.code_redo_on else "model")}
    return keep, kept_rows


def _started_week_line(past, ahead) -> str:
    from time_utils import mdy
    first_past, last_past = past[0], past[-1]
    gone = mdy(first_past) if first_past == last_past else f"{mdy(first_past)}–{mdy(last_past)}"
    return (f"This week had already begun, so only the days from {mdy(ahead[0])} were drafted — {gone} "
            f"{'is' if len(past) == 1 else 'are'} kept as {'it was' if len(past) == 1 else 'they were'}.")


class _PassContext:
    """What the stages after the draft share (one frozen context, P-36)."""

    def __init__(self, rid):
        self.rid = rid
        self.editable = None
        self.restaurant = None
        self.close_times = None
        self.role_buffers = None
        self.staff_constraints = {}
        self.mplan = {}
        self.constraints = None
        self.curve = None
        self.reqs = None
        self.solver_on = True
        self.reassign_on = True
        self.pass_editable = None
        self.repair_ctx = None
        self.loop = None
        self.hours_added = 0.0
        self.added_dates = {}
        self.extended_dates = {}
        self.trimmed_dates = {}
        self.floor_added_dates = {}
        self.split = {"hourly": None, "salaried": None}


def _stage_parse(job: GenerationRun, cand: Candidate, editable, kept_rows) -> _PassContext:
    """The answer's rows: typed as the structured contract's parse wrote them
    (ARCH-10) — or, on the CSV contract alone, its text through the legacy
    line repairer — each capped at the close, its hours read from its times;
    the planned manager rows restored exactly as planned (PR-1, PR-32)."""
    rid = job.req.restaurant_id
    result = cand.result
    ctx = _PassContext(rid)
    from models import get_staff_notes as _gsn_sched, get_close_times as _gct_sched, \
        get_role_close_buffers as _grcb_sched
    _raw_notes = se.frozen_read(("staff_notes", rid), lambda: _gsn_sched(rid)) or []
    ctx.staff_constraints = {n["employee_name"]: n["notes"] for n in _raw_notes if n.get("employee_name")}
    ctx.close_times = _gct_sched(rid)
    ctx.role_buffers = _grcb_sched(rid)
    ctx.restaurant = se.get_restaurant(rid)
    ctx.editable = editable
    preview_rows = []
    hours_scheduled = 0.0
    _hours_drift_total = 0.0
    _hours_drift_rows = 0
    _t_parse = time.monotonic()
    _csv_lines = result["schedule_csv"].split("\n")
    # The planned manager rows of the dates this pass planned come from the
    # plan itself (restore_pinned, below), never through this parser: its
    # close cap would re-time a manager staying until the last role's
    # after-close stay, and a planned row must not make an unreadable answer
    # look like a week (SCHED-42).
    import schedule_skeleton as _skeleton
    ctx.mplan = result.get("manager_plan") or {}
    _plan_dates = set(ctx.mplan.get("dates") or ())
    if editable is not None:
        # A redo day the model could not write keeps the owner's rows as they
        # were — their planned manager rows included, which nothing restores
        # below (it restores only the days rewritten).
        _plan_dates &= editable
    if _plan_dates:
        _csv_lines = _csv_lines[:1] + [ln for ln in _csv_lines[1:]
                                       if not (ln.split(",", 1)[0].strip() in _plan_dates
                                               and _skeleton.is_plan_line(ln))]
    print(f"[schedule] csv lines={len(_csv_lines)} first3={_csv_lines[:3]}")
    # A malformed row from the model used to be skipped in silence; counted
    # now, and surfaced.
    _dropped_rows = []
    _typed_in = result.get("rows")
    if _typed_in is not None:
        if _plan_dates:
            _typed_in = [r for r in _typed_in
                         if not (str(r.get("date") or "")[:10] in _plan_dates
                                 and (r.get("_pinned") == _skeleton.PLAN_SOURCE
                                      or _skeleton.is_plan_note(r.get("notes"))))]
        _dropped_rows = [str(d)[:120] for d in (result.get("dropped_rows") or [])]
        _answer_rows = se._typed_answer_rows(_typed_in)
    else:
        _answer_rows = se._legacy_answer_rows(_csv_lines)
    for _row, _real_day, _line in _answer_rows:
        if _row is None:
            _dropped_rows.append(_line[:120])
            continue
        # A row with no shift times, or a weekday where the role belongs, is
        # not a shift (owner, 9/26/26): dropped and counted, never saved.
        # A start equal to its end ("5:00pm-5:00pm") is a typo, never a
        # 24-hour shift (sched re-audit 10/11/26 BUGSGEN-19): unreadable.
        if (not (_row.get("shift_start") or "").strip() or not (_row.get("shift_end") or "").strip()
                or (_row.get("role") or "").strip() in se._WEEKDAYS
                or _stime.span(_row) is None):
            _dropped_rows.append(_line[:120])
            continue
        # The sweep's own ceiling (close_cap_minutes) with the rules the
        # prompt was built from: a manager stays as long as any role does
        # (BUGSGEN-15).
        se._enforce_close_time(_row, _real_day, ctx.close_times, ctx.role_buffers,
                               constraints=result.get("constraints"))
        # Times win over the model's own arithmetic, in real hours, so the
        # night the clocks go back counts its extra hour (E-23).
        _drift = se._reconcile_scheduled_hours(
            _row, tz=(getattr(ctx.restaurant, "timezone", None) or "").strip() or None)
        if abs(_drift) >= 0.1:
            _hours_drift_total += abs(_drift)
            _hours_drift_rows += 1
        preview_rows.append(_row)
        try:
            hours_scheduled += float(_row.get("scheduled_hours") or 0)
        except (ValueError, TypeError):
            pass
    if _hours_drift_rows:
        print(f"[schedule] corrected scheduled_hours on {_hours_drift_rows} row(s), "
              f"{round(_hours_drift_total, 1)}h total drift")
        # The model's hours disagreed with its own shift times and were
        # corrected: an AI-quality finding (fix round G #58), not a failing job.
        import ai_utils as _ai_q
        _ai_q.record_quality_event(
            "labor_schedule", "figures", restaurant_id=rid, action="labor_schedule",
            n=_hours_drift_rows,
            detail=(f"{_hours_drift_rows} schedule rows had scheduled_hours that disagreed "
                    f"with their shift times ({round(_hours_drift_total, 1)}h total)"))
    if kept_rows:
        # A redo's kept days, exactly as the owner saved them (PIPE-1).
        preview_rows.extend(kept_rows)
        hours_scheduled = se._safe_hours_sum(preview_rows)
    print(f"[schedule] parsed {len(preview_rows)} rows, first={preview_rows[0] if preview_rows else None}")
    if not preview_rows:
        # Every line was unreadable: saving it would publish an empty week as
        # if it were a schedule (SCHED-42).
        raise se.ScheduleGenerationError(
            "The generated schedule came back in a form we couldn't read, so nothing was saved. "
            "Try generating again.")
    # Every planned manager row, exactly as planned and carrying "_pinned"
    # (schedule audit 10/3/26 PR-1, PR-32): no pass after this removes,
    # re-times or re-assigns it, and none lands on a day the generation
    # accepted as closed. A redo restores only the days it rewrote.
    _plan_rows = [r for r in (ctx.mplan.get("rows") or []) if editable is None or r.get("date") in editable]
    if _plan_rows:
        preview_rows = _skeleton.restore_pinned(preview_rows, _plan_rows, closed=result.get("closed_dates") or ())
        hours_scheduled = se._safe_hours_sum(preview_rows)
    cand.timer.add("parse", time.monotonic() - _t_parse)
    cand.rows = preview_rows
    cand.dropped_rows = _dropped_rows
    cand.hours_scheduled = hours_scheduled
    return ctx


def _stage_rules(cand: Candidate, ctx: _PassContext) -> None:
    """The rules the week is checked against, read again now the model has
    written (schedule audit 10/3/26 P-41): time off approved while the model
    wrote is in them. The roster the prompt was built from stays the roster
    the rows are checked against."""
    with cand.timer.stage("rules"):
        ctx.constraints, newly_off = se._rules_after_the_model(ctx.rid, cand.result, ctx.restaurant)
    cand.result["constraints"] = ctx.constraints
    cand.result["time_off_since_inputs"] = newly_off


def _stage_signals(cand: Candidate, ctx: _PassContext) -> None:
    """One frozen context for the loop, the score and the review (P-36): the
    quality signals, last week's tail, the other sites' shifts, the sales
    curve and the SHIFT REQUIREMENTS, read once; the owner's weights; this
    week's experiment arm."""
    rid, result = ctx.rid, cand.result
    result["staff_constraints"] = ctx.staff_constraints
    with cand.timer.stage("signals"):
        result["prior_week_assignments"] = se._prior_week_assignments(
            rid, before=(result.get("week_dates") or [None])[0])
        try:
            from models import sibling_location_shifts as _sibs
            result["elsewhere"] = _sibs(rid, sorted(set(result.get("week_dates") or [])))
        except Exception as _sbx:
            se._soft_fail("other sites' shifts", _sbx, rid)
            result["elsewhere"] = {}
        cand.pass_sig, cand.pass_w = None, None
        try:
            cand.pass_sig, cand.pass_w = se._quality_signals(rid, result)
        except Exception as _psx:
            se._soft_fail("quality signals", _psx, rid)
        # The sales curve and the SHIFT REQUIREMENTS the draft was written
        # to, built once and shared: the top-up fills only what is short of
        # them and no trim cuts under them (P-20, SQ-7).
        ctx.curve = se._safe_hourly_profile(rid)
        result["hourly_profile_ready"] = bool(ctx.curve)
        ctx.reqs = se.week_requirements(rid, result, ctx.constraints, curve=ctx.curve)
        result["shift_requirements"] = ctx.reqs
    # The owner's lighter and heavier days (owner_rules weights — RM-9,
    # AI-9): each day's hours target moved by its share.
    if getattr(ctx.constraints, "owner_weights", None):
        try:
            import owner_rules as _owr_w
            result["daily_target_hours"] = _owr_w.apply_weights(result.get("daily_target_hours"), ctx.constraints)
        except Exception as _owx:
            se._soft_fail("owner weights", _owx, rid)
    # This week's arm of the live experiment (schedule_experiments): whether
    # the solver re-solves who works each shift (P-21, L-11).
    ctx.solver_on = True
    ctx.reassign_on = True
    try:
        import schedule_experiments as _sx
        # Read before the draft when the generation read them (the draft's
        # own source is an arm — contract #7).
        result["experiment_arms"] = result.get("experiment_arms") or \
            _sx.arms_for(rid, (result.get("week_dates") or [None])[0])
        ctx.solver_on = _sx.flag(result["experiment_arms"], "solver")
        # The model arm is clean (LOGIC-27): the optimizer's replace / swap /
        # trade moves are off with the solver.
        ctx.reassign_on = _sx.flag(result["experiment_arms"], "reassign")
        # learning_v1's off week (LEARNING-25): the learned memory binds none
        # of this week's passes; learning_v2's (LEARNING-14) nothing learned
        # does.
        import learning_stats as _ls_off
        if (not _sx.flag(result["experiment_arms"], "learned") or _ls_off.learning_off()) \
                and cand.pass_sig is not None:
            cand.pass_sig = dict(cand.pass_sig, learned=[])
    except Exception as _arx:
        se._soft_fail("experiment arm", _arx, rid)


def _stage_repair(job: GenerationRun, cand: Candidate, ctx: _PassContext, editable) -> None:
    """The ranked repair loop (P-47, E-18): every pass a stage at the rank of
    the rule it repairs, cycle after cycle until nothing changes, no change
    ever paid for by a rule ranked above it — and, beside it, the learning
    ablation (RM-4)."""
    rid, result, clock = ctx.rid, cand.result, job.clock
    # An open day the model left empty (BUGSGEN-5) is open to the sweep and
    # closed to the passes, as a day it did not write.
    _frozen = set(result.get("open_unwritten") or ())
    ctx.pass_editable = editable
    if _frozen:
        ctx.pass_editable = set(editable if editable is not None else (result.get("week_dates") or [])) - _frozen
    x = se.RepairContext(rid, result, ctx.constraints, editable=ctx.pass_editable,
                         restaurant=ctx.restaurant, close_times=ctx.close_times,
                         role_buffers=ctx.role_buffers, signals=cand.pass_sig, weights=cand.pass_w,
                         requirements=ctx.reqs, curve=ctx.curve, clock=clock, solver_on=ctx.solver_on)
    x.reassign_on = ctx.reassign_on
    ctx.repair_ctx = x
    # The rows as they come into the loop, each with its id, for the row
    # origins (AI-29) and the learning ablation (RM-4).
    x.give_ids(cand.rows)
    _pre_repair = [dict(r) for r in cand.rows]
    with cand.timer.stage("repair"):
        loop = se.repair_week(cand.rows, x)
    ctx.loop = loop
    # The model's own rows as they came in, and the context they are
    # repaired in — for the generation's metrics and the eval's replay
    # (scheduler audit 10/10/26 AI-2, AI-23, RM-1).
    cand.metrics_in = {"rows": _pre_repair, "ctx": x, "loop": loop}
    cand.gen_attr = {"pre": _pre_repair, "info": None, "ablation": None}
    try:
        import schedule_scorecard as _ssc
        cand.gen_attr["info"] = _ssc.loop_info(_pre_repair, loop.get("rows"), loop, x.origin)
        # The learning ablation (schedule audit 10/10/26 RM-4, LEARNING-7,
        # LEARNING-25) — the same loop over the same rows with the learned
        # memory off — is research no customer sees: it runs OFF the owner's
        # wait, as a bulk queue row after the save with this pass's frozen
        # context (schedule_scorecard.enqueue_ablation; blind re-audit
        # 10/11/26 ARCH-14 — it was a second repair loop, up to 20s a pass,
        # inline before the owner saw the draft). Here only whether it is
        # wanted. Off with SCHEDULE_LEARNING_ABLATION=0.
        _learned_now = (cand.pass_sig or {}).get("learned") or []
        if _ssc.ablation_wanted(_learned_now):
            cand.gen_attr["ablation_wanted"] = {"learned": len(_learned_now), "reassign_on": bool(ctx.reassign_on)}
    except Exception as _abx:
        # Not an input the draft went without — the draft is whole — so it
        # is recorded for us, never said in the owner's review.
        print(f"[schedule] learning ablation/row origins failed for restaurant {rid}: {_abx}")
        try:
            se._ops.capture(_abx, job="schedule_learning_ablation", context=f"restaurant_id={rid}")
        except Exception as _cx:
            print(f"[schedule] could not record that failure: {_cx}")
    for _k, _s in (loop.get("seconds") or {}).items():
        cand.timer.add(f"repair.{_k}", _s)
    cand.rows = loop["rows"]
    if cand.code_redo_on and not se._redo_changed(cand.spec.base.rows, cand.rows, cand.spec.dates):
        # The code's redo changed nothing on the owner's days: the model
        # redoes them after all (AI-28), nothing saved here.
        raise _CodeRedoDeclined("the code's redo changed nothing")
    cand.hours_scheduled = se._safe_hours_sum(cand.rows)
    se._apply_repair_reports(result, loop)
    ctx.hours_added = round(float((loop.get("hours_added") or {}).get("top_up") or 0.0), 1)
    ctx.added_dates = dict((loop.get("rows_added") or {}).get("top_up") or {})
    ctx.floor_added_dates = dict((loop.get("rows_added") or {}).get("floors") or {})
    ctx.trimmed_dates = dict(loop.get("section_cap_dates") or {})
    print(f"[schedule] repair loop: {loop['cycles']} cycle(s), "
          f"{'converged' if loop['converged'] else 'stopped at its bound'}, {len(loop['fixes'])} fixes, "
          f"{len(loop['refused'])} refused, {len(loop['failures'])} failed")


def _price_week(cand: Candidate, ctx: _PassContext, rows) -> None:
    """The one set of pricing inputs (schedule_economics.week_pricing) the
    reopened week and the live edit line use too (SQ-6). Overtime is priced
    from the 40h line, never the owner's hours ceiling (NS3 H5); an hourly
    closing manager's assumed stay is paid time (TRUSt-21)."""
    import schedule_economics as _econ
    result = cand.result
    try:
        _pricing = _econ.week_pricing(ctx.rid, ctx.constraints, blended_rate=result.get("blended_rate"))
        _priced = _econ.price_week(se._rules.with_assumed_stays(rows, ctx.constraints), _pricing)
        result["projected_cost"] = _priced
        _lbd = float(result.get("labor_budget_dollars") or 0)
        result["over_budget_dollars"] = round(_priced["total"] - _lbd, 0) if _lbd else None
    except Exception as _px:
        # Said, not printed (schedule audit 10/3/26 P-17).
        se._soft_fail("pricing", _px, ctx.rid)


ASSIGNMENT_REASONS_MAX = 400


def final_assignment_reasons(result, rows) -> dict:
    """The final solve's reasons kept with the draft (contract #4): the
    typed entries the solve produced — `result["assignment_reasons"]` (the
    final solve's, schedule_cpsat.assignment_reasons) else the solver-first
    draft's own (draft_source.solver.reasons) — matched to the FINISHED rows
    by date, start and person, so a reason is kept only while the row still
    carries the pick it explains (a repair pass that moved the shift makes
    it a comparison after the draft, never the engine's). {source, reasons:
    [{shift_id, date, shift_start, employee, runner_up, facts, text}],
    recorded, changed_after}; reasons [] when no solve recorded any."""
    def k(d, s, e):
        return (str(d or "")[:10], " ".join(str(s or "").split()).lower(), " ".join(str(e or "").split()).lower())
    items = result.get("assignment_reasons")
    source = "solver"
    if isinstance(items, dict):
        source = items.get("source") or source
        items = items.get("reasons")
    if not items:
        items = (((result.get("draft_source") or {}).get("solver") or {}).get("reasons")) or []
    on = {}
    for r in rows or []:
        if (r.get("employee") or "").strip():
            on.setdefault(k(r.get("date"), r.get("shift_start"), r.get("employee")), r)
    kept, changed = [], 0
    for x in items:
        if not isinstance(x, dict) or not x.get("text") or not (x.get("employee") or "").strip():
            continue
        row = on.get(k(x.get("date"), x.get("shift_start"), x.get("employee")))
        if row is None:
            changed += 1
            continue
        ru = x.get("runner_up")
        kept.append({"shift_id": x.get("shift_id") or row.get("shift_id"), "date": str(x.get("date"))[:10],
                     "shift_start": x.get("shift_start"), "employee": x.get("employee"),
                     "runner_up": (ru.get("employee") if isinstance(ru, dict) else ru) or x.get("instead"),
                     "facts": list(x.get("facts") or [])[:8], "text": str(x["text"])[:300]})
        if len(kept) >= ASSIGNMENT_REASONS_MAX:
            break
    return {"source": source, "reasons": kept, "recorded": len(kept) + changed, "changed_after": changed}


def _stage_review(cand: Candidate, ctx: _PassContext) -> None:
    """Every rule the week is checked against, in one sweep of the FINISHED
    rows (schedule_rules.violations) — nothing changes a row after this, so
    the review, the NEEDS REVIEW marks and the score all describe the week
    that is saved (P-11, SQ-21) — and the review assembled from it."""
    import schedule_economics as _econ
    import schedule_skeleton as _skeleton
    rid, result, _constraints = ctx.rid, cand.result, ctx.constraints
    preview_rows = cand.rows
    _loop, _mplan = ctx.loop, ctx.mplan
    with cand.timer.stage("sweep"):
        _viols = se._rules.violations(preview_rows, _constraints)
    # Priced once, on the rows that are saved.
    _price_week(cand, ctx, preview_rows)
    _fixes = list(_loop["fixes"])
    # What is still broken on a row comes from that final sweep, each with
    # why it was left (P-15).
    _unfixed = se._unfixed_from(_viols, preview_rows, ctx.pass_editable, ctx.repair_ctx.unfixed_why)
    for _v in _viols:
        # A breach about a DAY (no manager on, a floor short) is shown on the
        # day (review.hard_days), never as "needs review" on a row (E-13).
        # A policy limit that is not law is "over your limit" to the owner
        # (LOGIC-20), never a NEEDS REVIEW row the gate holds as must-fix.
        if not se._rules.owner_hard(_v) or _v.get("day_level"):
            continue
        _r = preview_rows[_v["index"]]
        _r["needs_review"] = True
        _r["review_reason"] = _v["detail"] if _v["kind"] == "over_max_hours" else _v["label"]
    result["rule_violations"] = _viols
    result["review"] = se._rules.summarize(_viols)
    review = result["review"]
    # Which lines are the rule sweep's, so a Studio save refreshes them and
    # keeps the rest (P-26: refresh_review).
    review["rule_lines"] = list(review["lines"])
    review["fixes"] = _fixes
    review["unfixed"] = _unfixed
    # Each flagged row by its id as well as its place (P-15).
    review["needs_review"] = [
        {"row_id": _r.get("_rid"), "index": _i, "reason": _r.get("review_reason") or "could not be auto-checked"}
        for _i, _r in enumerate(preview_rows) if _r.get("needs_review")]
    # What the generation itself has to say leads the review: days it could
    # not write (P-34), days nobody could work (E-20), a first week drafted
    # with no history (E-30), a repair stage of a hard rule or the manager
    # rule that did not run (P-3, P-17) and a setting the rules could not
    # read (P-1). A "⚠" line holds the publish gate.
    _lead = list(result.get("generation_notes") or [])
    _lead += [_f["line"] for _f in (result.get("stage_failures") or []) if _f.get("blocks_publish")]
    _inprob = [dict(_p) for _p in (getattr(_constraints, "input_problems", None) or [])]
    _lead += ["⚠ " + se._rules.input_problem_text(_p) for _p in _inprob]
    review["lines"][0:0] = _lead
    review["lead_lines"] = _lead
    review["stage_failures"] = list(result.get("stage_failures") or [])
    review["input_problems"] = [{k: _p.get(k) for k in ("source", "name")} for _p in _inprob]
    review["repair"] = {k: v for k, v in (result.get("repair") or {}).items() if k != "seconds"}
    review["unwritten_dates"] = result.get("unwritten_dates") or []
    # Kept with the saved review: which days were built without the model,
    # and why (contract #7, AI-11).
    review["draft_source"] = se._json_safe(result.get("draft_source") or {})
    # The final solve's own reason for each assignment, kept with the draft
    # for the pre-publish "why" and the Shift pane (schedule re-audit
    # 10/11/26 contract #4, TRUST-2) — only the reasons whose row still
    # carries the solve's pick after every later pass; the rest are counted.
    try:
        _ar = final_assignment_reasons(result, preview_rows)
        if _ar["recorded"]:
            review["assignment_reasons"] = _ar
    except Exception as _arx:
        se._soft_fail("assignment reasons", _arx, rid)
    # The draft's instruction as the rules code held it — chips — and every
    # typed rule of the owner's words in force (owner_rules, RM-9).
    try:
        import owner_rules as _owr_c
        review["instruction_rules"] = _owr_c.chips(preview_rows, _constraints, result.get("instruction_rules"))
        review["typed_rules"] = [
            {"words": r.get("words"), "quote": r.get("quote"), "confirmed": bool(r.get("confirmed")),
             "source": r.get("source")} for r in (getattr(_constraints, "typed_rules", None) or [])][:40]
    except Exception as _ocx:
        se._soft_fail("owner words", _ocx, rid)
    review["unstaffable_dates"] = result.get("unstaffable_dates") or []
    # A role the requirements need with nobody of it on all day, and why
    # nobody can work it (blind re-audit 10/11/26 STRESS-9) — read against
    # the rows as they stand by the publish gate too.
    try:
        import schedule_shapes as _shp_u
        review["uncovered_roles"] = _shp_u.uncovered_roles(
            preview_rows, result.get("requirements") or [], _constraints, result.get("roster_roles"))
        _ul = _shp_u.uncovered_line(review["uncovered_roles"])
        if _ul:
            review["lines"].insert(0, _ul)
            review["lead_lines"] = [_ul] + list(review.get("lead_lines") or [])
        # A role drafted with no history and no floor (STRESS-20): said, so
        # the owner sets its floor before the next draft.
        _as = [a for a in (((result.get("draft_source") or {}).get("solver") or {}).get("assumed") or [])
               if isinstance(a, dict) and a.get("role")]
        if _as:
            review["assumed_roles"] = _as
            review["lines"].append(
                "Drafted with no history and no floor: " + ", ".join(a["role"] for a in _as[:6])
                + " — one on through the open hours. Set their floors in Rules so the next draft knows.")
    except Exception as _ucx:
        se._soft_fail("uncovered roles", _ucx, rid)
        review["uncovered_roles"] = []
    # A staffing rule, or a line of the RESTAURANT HOURS & SHIFT RULES, the
    # code could not read is named, so the owner checks the draft against it
    # (PROMPTS-1, PROMPT-1).
    _unchecked = list(dict.fromkeys(list(getattr(_constraints, "owner_rules_unchecked", None) or [])
                                    + list(getattr(_constraints, "hours_rules_unchecked", None) or [])))
    review["owner_rules_unchecked"] = _unchecked
    for _txt in _unchecked[:3]:
        review["lines"].append(
            f"Your rule “{_txt[:120]}” isn't one Cavnar AI can check automatically — check this "
            f"draft against it")
    # A fact filed under a name nobody on the roster goes by applies to
    # nobody (D-8): named, with a similar roster name to check.
    _unm = list(getattr(_constraints, "unmatched", None) or [])
    review["unmatched_names"] = _unm
    if _unm:
        review["lines"].append(
            f"{len(_unm)} fact{'s' if len(_unm) != 1 else ''} on file name{'' if len(_unm) != 1 else 's'} "
            f"nobody on the roster, so {'they apply' if len(_unm) != 1 else 'it applies'} to nobody: "
            + "; ".join(u["detail"] for u in _unm[:3]) + ("…" if len(_unm) > 3 else ""))
    # Who is who, as the shift history reads it (schedule re-audit 10/11/26
    # STRESS-7, STRESS-8): one name worn by two people is scheduled as one
    # until split; one person under two names is held to one week's hours,
    # overtime and rest until answered. Said — never a silent merge or split.
    _idq = list(getattr(_constraints, "identity_concerns", None) or [])
    review["identity_concerns"] = _idq
    for _q in _idq[:3]:
        review["lines"].append(_q["text"])
    # The manager plan, said (D-5, PR-1). Kept with the review so a reopened
    # week still asks.
    review["lines"].extend(_skeleton.review_lines(_mplan, gaps=se._rules.manager_gaps(preview_rows, _constraints)))
    review["manager_plan"] = {
        "question": _mplan.get("question"), "unknown_pattern": list(_mplan.get("unknown_pattern") or []),
        "uncovered": _skeleton.payload(_mplan)["uncovered"]}
    # The closing manager the owner's verdict assumes stays past their
    # scheduled end until the last person leaves: said per day, priced for
    # an hourly manager (TRUST-21).
    try:
        _stays = se._rules.assumed_stays(preview_rows, _constraints)
    except Exception as _sx:
        se._soft_fail("assumed stays", _sx, rid)
        _stays = []
    review["assumed_stays"] = _stays
    review["stay_lines"] = [_s["text"] for _s in _stays]
    review["lines"].extend(review["stay_lines"])
    # Why the manager rule could not be met, once for the week, and who is
    # still under their minimum hours, with why (E-12, E-13, P-4).
    _short = ((result.get("manager_coverage") or {}).get("shortfall") or {}).get("text")
    if _short:
        review["lines"].append(_short)
    review["manager_coverage"] = se._json_safe(result.get("manager_coverage") or {})
    review["min_hours"] = se._json_safe(result.get("min_hours") or {})
    for _x in ((result.get("min_hours") or {}).get("left") or [])[:4]:
        review["lines"].append(
            f"{_x['employee']} is {_x['short_by']:g}h under the {_x['min']:g}h minimum you set — {_x['reason']}")
    # The setup the week was drafted against, for the owner to confirm (F1).
    try:
        _setup_r = __import__("schedule_setup").setup_review(
            _constraints, leader_status=result.get("leader_rules_status"),
            # the manager plan already asks the managers' days (D-5)
            omit=("standing_missing",))
        review["setup"] = _setup_r["items"]
        review["lines"].extend(_setup_r["lines"])
    except Exception as _stx:
        se._soft_fail("setup review", _stx, rid)
    # Who on the roster got nothing, and ratings that name nobody on it.
    _on = {(_r.get("employee") or "").strip().lower() for _r in preview_rows}
    _not = [n for n in (result.get("roster") or []) if n and n.strip().lower() not in _on]
    result["not_scheduled"] = _not
    _gone = result.get("dormant") or {}
    if _gone:
        from time_utils import mdy as _mdy_gone
        _gl = sorted(_gone.items(), key=lambda kv: kv[1], reverse=True)
        review["lines"].append(
            f"Left off this draft — no shift in {se._staff.DORMANT_WEEKS}+ weeks: "
            + ", ".join(f"{n} (last {_mdy_gone(d)})" for n, d in _gl[:6]) + ("…" if len(_gl) > 6 else "")
            + ". Deactivate anyone who has left, or mark them still here on the Team page.")
    # A regular left with no shift at all (D-37): their usual week, said
    # beside the name.
    _pp = result.get("prior_pattern") or {}
    _wk = list(getattr(_constraints, "week_dates", None) or [])

    def _away_all_week(n):
        _b = (getattr(_constraints, "blocked_dates", None) or {}).get(_constraints.key(n)
                                                                       if hasattr(_constraints, "key")
                                                                       else n.lower()) or {}
        return bool(_wk) and all(d in _b for d in _wk)
    _reg = [(n, (_pp.get(n) or {}).get("avg_hours")) for n in _not
            if float((_pp.get(n) or {}).get("avg_hours") or 0) >= 16 and not _away_all_week(n)]
    result["regulars_not_scheduled"] = [{"name": n, "usual_hours": h} for n, h in _reg]
    if _reg:
        review["lines"].append(
            f"{len(_reg)} regular{'s' if len(_reg) != 1 else ''} with no shift this week: "
            + ", ".join(f"{n} (usually ~{float(h):g}h)" for n, h in _reg[:6]) + ("…" if len(_reg) > 6 else ""))
    if _not:
        _full = [n for n in _not if (_constraints.employment.get(n.lower()) == "full")]
        review["lines"].append(
            f"{len(_not)} on the roster have no shift this week: " + ", ".join(_not[:8]) + ("…" if len(_not) > 8 else "")
            + (f" — {len(_full)} of them full-time" if _full else ""))
    _roster_low = {n.strip().lower() for n in (result.get("roster") or [])}
    # A rating under another spelling of a roster person is theirs (D-8).
    _known = (lambda n: _constraints.key(n) in _constraints.display) if getattr(_constraints, "display", None) \
        else (lambda n: n.strip().lower() in _roster_low)
    _off = sorted(n for n in (result.get("operational_scores") or {}) if not _known(n)) if _roster_low else []
    result["ratings_off_roster"] = _off
    # The section cap against what this restaurant's own history runs ON AT
    # ONCE (labor.concurrent_patterns — LOGIC-7).
    try:
        import staffing_curve as _stc
        _cap_roles = getattr(_constraints, "foh_roles", None) or {"server"}
        _conf = _stc.cap_conflicts(result.get("concurrent") or {},
                                   getattr(_constraints, "section_cap", 0), _cap_roles,
                                   getattr(_constraints, "role_families", None) or None)
        result["section_cap_conflicts"] = _conf
        _line = _stc.cap_conflict_line(
            _conf, "servers" if set(_cap_roles) == {"server"} else "front-of-house staff")
        if _line:
            review["lines"].append(_line)
    except Exception as _scx:
        se._soft_fail("section cap check", _scx, rid)
        result["section_cap_conflicts"] = []
    if _off:
        review["lines"].append(
            f"{len(_off)} Operational Score{'s' if len(_off) != 1 else ''} belong to names not on the roster "
            f"({', '.join(_off[:4])}{'…' if len(_off) > 4 else ''}) — they judge nobody until the names match")
    # The budget is a ceiling: what is still over after the trim is named at
    # the top of the review (P-45), in dollars when the week is priced
    # (LOGIC-13).
    try:
        _hs = se._hourly_hours_sum(preview_rows, _constraints)
        _hb = float(result.get("hours_budget") or 0)
    except (TypeError, ValueError):
        _hs = _hb = 0.0
    _budget_lines = []
    _lbd = float(result.get("labor_budget_dollars") or 0)
    _cost = float(((result.get("projected_cost") or {}).get("total")) or 0)
    _over_d = _lbd > 0 and _cost > _lbd * 1.02
    if _over_d:
        review["over_budget_dollars"] = round(_cost - _lbd, 0)
        review["over_budget_hours"] = round(max(0.0, _hs - _hb), 1)
        # Which budget binds (blind re-audit 10/11/26 BUGSGEN-22): the dollar
        # budget prices each person at their own rate, the hours budget the
        # same dollars at the blended rate — "$298 over (988h scheduled
        # against 988h)" read as a contradiction.
        _bind = _econ.binding_budget(_cost, _lbd, _hs, _hb,
                                     ((result.get("projected_cost") or {}).get("overtime_premium")))
        review["budget_binds"] = _bind.get("binds")
        _budget_lines.append(
            f"⚠ ${_cost:,.0f} of hourly pay against a ${_lbd:,.0f} hourly budget — ${_cost - _lbd:,.0f} over"
            + (f" ({_bind['note']})" if _bind.get("note") else ""))
    elif (_lbd <= 0 or not _cost) and _hb > 0 and _hs > _hb * 1.02:
        review["over_budget_hours"] = round(_hs - _hb, 1)
        _budget_lines.append(
            f"⚠ {_hs:,.0f}h scheduled against a {_hb:,.0f}h budget — {_hs - _hb:,.0f}h over the ceiling")
    if _budget_lines:
        # Why the trim left it there, said once beside it (SQ-7).
        _bc = result.get("budget_conflict")
        if _bc:
            review["budget_conflict"] = _bc
            _bcl = _econ.budget_conflict_line(_bc, over_by=round(_hs - _hb, 1), budget=_hb)
            if _bcl:
                _budget_lines.append(_bcl)
        # And which rules ranked above the budget spent the hours (E-18).
        _above = se._added_above_budget_line(((result.get("repair") or {}).get("budget") or {})
                                             .get("added_above_budget"))
        if _above:
            _budget_lines.append(_above)
    review["lines"][0:0] = _budget_lines
    review["budget_lines"] = _budget_lines
    # A budget mostly priced at the assumed wage is not trimmed to (E-24) —
    # said, with what to set, and held for the owner then.
    _basis = result.get("budget_basis") or {}
    if _basis.get("caveat"):
        _cav = ("⚠ " if _basis.get("trim_ok") is False else "") + str(_basis["caveat"])
        review["lines"].insert(len(_budget_lines), _cav)
        review["lead_lines"] = [_cav] + list(review.get("lead_lines") or [])
    # Nights left over the section count because a cut would have gone
    # under a floor or broken a rule (P-29), named once.
    review["cap_floor_conflicts"] = result.get("cap_floor_conflicts") or []
    _cfl = se.cap_floor_conflict_line(result.get("cap_floor_conflicts") or [])
    if _cfl:
        review["lines"].append(_cfl)
    review["trimmed"] = result.get("trimmed") or []
    review["hours_trimmed"] = result.get("hours_trimmed") or 0.0
    review["staggered"] = result.get("staggered") or []
    for _line in _econ.trim_lines(result.get("trimmed") or [], result.get("hours_trimmed") or 0.0, _hb)[:3]:
        review["lines"].append(_line)
    # Who this draft puts past the ceiling in the payroll week, and the days
    # most likely to lose somebody to a no-show (schedule_learning).
    try:
        import schedule_learning as _sl
        from time_utils import mdy as _mdy
        _ot = _sl.overtime_forecast(preview_rows, constraints=_constraints,
                                    roster_roles=result.get("roster_roles") or None)
        try:
            from models import get_role_rates as _grr_ot, get_restaurant as _gr_ot
            _sl.price_overtime_moves(_ot, _grr_ot(rid), getattr(_gr_ot(rid), "hourly_rate", None) or None)
        except Exception as _px:
            print(f"[schedule] overtime moves unpriced: {_px}")
        result["overtime_forecast"] = _ot
        for _o in _ot[:3]:
            review["lines"].append(_o["text"])
        # Expected attendance against each shift's requirement (RM-18): a
        # busy shift likely to come up short asks for a standby.
        _busy_sb = set()
        try:
            import schedule_requirements as _req_sb
            _busy_sb = _req_sb.busy_shifts(result.get("week_dates") or [], result.get("shift_profiles") or None,
                                           result.get("demand_by_day") or {}, result.get("demand_by_date") or {})
        except Exception as _bx:
            print(f"[schedule] busy shifts unread for the standby: {_bx}")
        _sb = _sl.standby_days(rid, result.get("week_dates") or [], rows=preview_rows,
                               requirements=result.get("shift_requirements") or result.get("requirements"),
                               busy=_busy_sb)
        result["standby_days"] = _sb
        for _d in _sb:
            _who = (_d.get("standby") or {})
            _sh = _d.get("standby_shift")
            if _sh:
                review["lines"].append(
                    f"A standby is needed on {_sh['text']}."
                    + (f" {_who['employee']} ({_who['role'] or 'same role'}) is off and could be on call"
                       if _who.get("employee") else " Nobody in the role is free to be on call")
                    + " — or add one more if the budget has room.")
                continue
            review["lines"].append(
                f"Worth a standby on {_d['day']} {_mdy(_d['date'])}: about a "
                f"{int(round(_d['chance_of_a_no_show'] * 100))}% chance somebody scheduled doesn't show, "
                f"from their own attendance."
                + (f" {_who['employee']} ({_who['role'] or 'same role'}) is off and could be on call."
                   if _who.get("employee") else ""))
    except Exception as _slx:
        # Said, not printed (schedule audit 10/3/26 P-17).
        se._soft_fail("overtime forecast", _slx, rid)
    # A floor the work never needed (MARKET-33): the evidence, said; the
    # floor stays the owner's to change.
    try:
        import staffing_model as _smf
        _fs = _smf.floor_suggestions(rid)
        result["floor_suggestions"] = _fs
        for _f in _fs[:2]:
            review["lines"].append(_f["text"])
    except Exception as _fsx:
        se._soft_fail("floor suggestions", _fsx, rid)
    # Rows the manager has repeatedly edited away and the draft put back,
    # then every row their edit history says they are likely to change
    # (schedule_learning.predict_row_edits) — in the Likely edits block.
    try:
        result["likely_edits"] = se.likely_edits(rid, preview_rows)
        for _le in [x for x in result["likely_edits"] if x.get("kind") != "predicted"][:3]:
            review["lines"].append(_le["text"])
    except Exception as _lex:
        se._soft_fail("likely edits", _lex, rid)
    # How far this restaurant's demand forecasts have been off (K8).
    try:
        import demand as _demand_acc
        result["demand_accuracy"] = _demand_acc.demand_accuracy(rid)
        result["week_projection_accuracy"] = _demand_acc.week_projection_accuracy(rid)
    except Exception as _dax:
        se._soft_fail("demand accuracy", _dax, rid)
    # Sales per labor hour against the objective the draft was written to,
    # and the borrowed starting headcount, said.
    try:
        _sr = _econ.splh_report(result.get("splh_objective") or {}, preview_rows, result.get("demand_by_date") or {})
        result["splh_report"] = _sr
        if _sr.get("line"):
            review["lines"].append(_sr["line"])
    except Exception as _srx:
        se._soft_fail("splh report", _srx, rid)
    _start = result.get("starting_headcount") or {}
    if _start.get("available"):
        review["lines"].append(
            f"Staffing numbers marked borrowed come from {_start.get('n')}+ "
            f"{(_start.get('cohort_label') or 'Restaurants on Cavnar AI')[:1].lower()}"
            f"{(_start.get('cohort_label') or 'Restaurants on Cavnar AI')[1:]} (people on the floor per $1k of "
            f"sales, scaled to {_start.get('basis')})"
            + (" — the type was inferred from the restaurant's name, not set" if _start.get("inferred") else "")
            + " — this restaurant has no schedule history of its own yet.")
    _pc = result.get("projected_cost") or {}
    if _pc.get("overtime_hours"):
        review["lines"].append(
            f"{_pc['overtime_hours']:g}h of overtime priced at {_pc['multiplier']}× — ${_pc['overtime_premium']:,.0f} "
            f"of premium in a ${_pc['total']:,.0f} week")
    # The repair stages that did not run but hold nothing (P-17), the time
    # off approved while the model wrote (P-41), a search the job's clock
    # left no time for, and how current the sales behind the week are (D-33).
    for _f in result.get("stage_failures") or []:
        if not _f.get("blocks_publish") and _f.get("line"):
            review["lines"].append(_f["line"])
    _tos = se._time_off_since_line(result.get("time_off_since_inputs"))
    if _tos:
        review["lines"].append(_tos)
    _skipped = [s["stage"] for s in ((result.get("repair") or {}).get("skipped") or [])]
    if _skipped:
        review["lines"].append(
            "To finish in time, " + " and ".join(
                {"solver": "the assignment solver", "optimizer": "the Shift Quality optimizer"}.get(k, k)
                for k in _skipped) + " didn't run on this draft.")
    _emergency = se.repair_emergency_line((result.get("repair") or {}))
    if _emergency:
        review["lines"].append(_emergency)
    # Every solve an emergency cap ended — the clock or CP-SAT's memory
    # bound, in the draft or the repair loop — named on the week, so the
    # platform check can alert on their rate (ops.schedule_solve_capped;
    # blind re-audit 10/11/26 SCALE-15, SCALE-16).
    _capped = se.solve_capped(result)
    review["solve_capped"] = _capped
    if any(k.endswith(":draft") for k in _capped):
        review["lines"].append("The server was busy while this draft was built, so the solver stopped before "
                               "finishing — generate again for the full search.")
    _ddt = (result.get("demand_data_through") or {}).get("line")
    if _ddt:
        review["lines"].append(_ddt)
    result["rows_needing_review"] = sum(1 for _r in preview_rows if _r.get("needs_review"))
    # The legacy strength banner is deliberately gone: dim_leadership and
    # dim_operational_strength cover everything it reported.
    result["strength"] = {"checked": False, "superseded_by": "quality"}


def _stage_score(cand: Candidate, ctx: _PassContext) -> None:
    """Shift Quality over the finished rows. Wrapped whole: a schedule that
    generated fine is never lost because a scoring dimension raised — it
    degrades to "not scored", said and held at publish (P-16)."""
    rid, result = ctx.rid, cand.result
    preview_rows = cand.rows
    _t_score = time.monotonic()
    se.reach_stage("score")
    try:
        result["staff_constraints"] = ctx.staff_constraints
        # Only the flags that mean the person is not really on that shift.
        # A row flagged "over 40h for the week" still has a real person on
        # the floor — it is a cost problem, not a coverage one — and
        # excluding it made a fully staffed week read as zero coverage.
        _NO_SHOW_REASONS = tuple(sorted(se._rules.NO_SHOW))  # noqa: F841
        # schedule_rules.NO_SHOW, each on its own line so a reader can grep
        # for the phrase the owner sees:
        #   not on the staff list · no longer on the roster
        #   date is outside next week
        #   double-booked at the same start time · two shifts overlap
        #   on approved time off · marked unavailable that day
        #   not available for that daypart · already scheduled at another location
        # A row over the hours ceiling is NOT here: the person is still on
        # the floor.
        result["flagged_rows"] = se._flagged_rows(result.get("rule_violations"))
        _quality, _whatif = se._score_schedule_quality(
            rid, preview_rows, result,
            frozen=(cand.pass_sig, cand.pass_w) if cand.pass_sig is not None else None,
            rows_needing_review=result.get("rows_needing_review", 0),
            dropped_rows=len(cand.dropped_rows),
        )
        if isinstance(_quality, dict):
            _quality["optimizer"] = result.get("optimizer") or {"ran": False}
            # The per-day hour targets belong to this generation and cannot
            # be re-derived; kept with the stored verdict (stored_daily_targets).
            _quality["daily_target_hours"] = result.get("daily_target_hours") or {}
        result["quality"] = _quality
        result["what_if"] = _whatif
        from models import capability_version as _capver
        result["capability_version"] = _capver(rid)
        if _quality.get("checked"):
            print(f"[schedule] shift quality {_quality['score']}/100 "
                  f"({_quality['band']}), confidence {_quality['confidence']['level']}"
                  + (f", what-if {_whatif['improvement']:+d}" if _whatif.get("ran") else ""))
    except Exception as _qx:
        print(f"[schedule] quality engine failed: {_qx!r}")
        try:
            se._ops.capture(_qx, job="schedule_stage_quality", context=f"restaurant_id={rid}")
        except Exception as _cqx:
            print(f"[schedule] could not record that failure: {_cqx}")
        result["quality"] = {"checked": False,
                             "error": "the scoring engine hit an error on this week — we've been alerted"}
        result["what_if"] = {"ran": False}
        _qf = {"stage": "quality", "tier": None, "blocks_publish": False, "error": type(_qx).__name__,
               "line": "Shift Quality couldn't score this draft — read it yourself before it goes to staff."}
        result.setdefault("stage_failures", []).append(_qf)
        if isinstance(result.get("review"), dict):
            result["review"].setdefault("stage_failures", []).append(_qf)
            result["review"]["lines"].append(_qf["line"])
    cand.timer.add("score", time.monotonic() - _t_score)
    _flagged = sum(1 for _r in preview_rows if _r.get("needs_review"))
    result["rows_needing_review"] = _flagged
    if _flagged:
        print(f"[schedule] {_flagged} row(s) flagged for review")


def _stage_rows_out(cand: Candidate, ctx: _PassContext) -> None:
    """"What changed" from the diff against the last published week, the
    station report, the NEEDS REVIEW marks in the notes and the stored text
    (schedule_rows.to_csv, the one writer)."""
    rid, result = ctx.rid, cand.result
    preview_rows = cand.rows
    try:
        _prior_rows = result.get("prior_published_rows") or []
        if _prior_rows:
            _d = se._versions.diff(_prior_rows, preview_rows)
            result["summary"] = se._versions.diff_lines(_d)
            result["diff_vs_published"] = {k: _d[k] for k in ("changes", "hours_before", "hours_after")}
        else:
            _people = {(_r.get("employee") or "").strip() for _r in preview_rows if _r.get("employee")}
            _by_day = {}
            for _r in preview_rows:
                try:
                    _by_day[_r.get("day")] = _by_day.get(_r.get("day"), 0.0) + float(_r.get("scheduled_hours") or 0)
                except (TypeError, ValueError):
                    pass
            _busiest = max(_by_day.items(), key=lambda kv: kv[1])[0] if _by_day else None
            result["summary"] = [f"{len(preview_rows)} shifts, {round(cand.hours_scheduled):,}h across "
                                 f"{len(_people)} people." + (f" Busiest day {_busiest}." if _busiest else ""),
                                 "First published week on file — no prior week to compare against."]
    except Exception as _sx:
        print(f"[schedule] summary diff failed: {_sx}")
    if preview_rows:
        # Each cook's station named for the screens — a read of the finished
        # rows (the station pass itself runs inside the repair loop).
        try:
            if getattr(ctx.constraints, "stations", None):
                _stations = se.station_report(preview_rows, ctx.constraints, result.get("week_dates", []))
                result["stations"] = _stations
                if isinstance(result.get("review"), dict):
                    result["review"]["stations"] = _stations
        except Exception as _stx:
            se._soft_fail("station report", _stx, rid)
        for _r in preview_rows:
            if _r.get("needs_review"):
                _n = (_r.get("notes") or "").strip()
                _why = _r.get("review_reason") or "could not be auto-checked"
                _mark = f"NEEDS REVIEW: {_why}"
                # Always today's reason (PIPE-6).
                _n = se._rules.strip_review_mark(_n)
                _r["notes"] = f"{_n} — {_mark}" if _n else _mark
        # The stored text: schedule_rows.to_csv, the one writer — quoted, with
        # each row's person id; save_schedule_history gives each row the
        # shift id it keeps (ARCH-6, ARCH-31, SCALE-31).
        import schedule_rows as _srows_out
        se._versions.stamp_people(rid, preview_rows)
        result["schedule_csv"] = _srows_out.to_csv(preview_rows)


def _stage_requirements_met(cand: Candidate, rid) -> None:
    """Which soft requirements the final rows honoured, and everything the
    finished week does not meet (PR-11) — read from the rows, never from
    what the model said it did."""
    result, preview_rows = cand.result, cand.rows
    try:
        import staffing_signals as _stsig_ap
        _typ = {tuple(k.split("|", 1)): v for k, v in (result.pop("_soft_typical", None) or {}).items()}
        result["soft_requirements"] = _stsig_ap.applied(result.get("soft_requirements") or [], preview_rows, _typ)
        if isinstance(result.get("review"), dict) and result["soft_requirements"]:
            result["review"]["soft_requirements"] = result["soft_requirements"]
        # Each review "+1" is a recommendation the owner is shown here
        # (staffing_signals.record_published, CROSSMODULE-10).
        _stsig_ap.present_requirements(rid, result["soft_requirements"])
    except Exception as _sax:
        se._soft_fail("soft requirement check", _sax, rid)
        result.pop("_soft_typical", None)
    try:
        import schedule_output as _so_unmet
        if isinstance(result.get("review"), dict):
            result["review"]["unmet"] = _so_unmet.unmet_items(
                preview_rows, constraints=result.get("constraints"),
                violations=result.get("rule_violations"), quality=result.get("quality"),
                soft_requirements=result.get("soft_requirements"), hours_budget=result.get("hours_budget"),
                station_gaps=(result.get("stations") or {}).get("gaps"),
                owner_rules_unchecked=result["review"].get("owner_rules_unchecked"))
    except Exception as _ux:
        se._soft_fail("unmet list", _ux, rid)


def _stage_note(job: GenerationRun, cand: Candidate) -> None:
    """"Cavnar AI's note", written from the FINISHED week and checked against
    its rows (TRUST-15, AI-21). It is a model call: a stopped run starts no
    further one (10/9/26) — stopped by the owner is said as stopped, called
    dead by a poll as out of time. Written once, for the candidate the job
    saves, just before the save (_commit — blind re-audit 10/11/26 ARCH-13:
    every pass used to pay for a note before the gate chose, up to three a
    generation for two that were thrown away)."""
    result, preview_rows = cand.result, cand.rows
    rid = job.req.restaurant_id
    _boundary(job, "note")
    cand.noted = True
    try:
        import schedule_narrative as _snar
        _note = _snar.write(rid, preview_rows, changes=result.get("summary") or [],
                            unmet=(result.get("review") or {}).get("unmet") or [],
                            chips=(result.get("review") or {}).get("instruction_rules") or [],
                            model_bullets=list(result.get("narrative") or []),
                            roster=result.get("roster") or [],
                            role_floors=getattr(result.get("constraints"), "role_floors", None),
                            # Why the week looks as it does (re-audit 10/11/26
                            # AI-16, AI-17): the model's decisions still in the
                            # final rows, requirement reasons, solver picks,
                            # the repair's changes.
                            reasons=_snar.reasons_for_note(result, preview_rows))
        result["narrative"] = _note["bullets"]
        if isinstance(result.get("review"), dict):
            result["review"]["note"] = {"source": _note["source"], "dropped": _note["dropped"][:6]}
    except Exception as _nx:
        se._soft_fail("note", _nx, rid)
        result["narrative"] = []
        # Said as the inputs stage would have said it (it ran before this).
        review = result.get("review")
        if isinstance(review, dict):
            review["inputs_missing"] = list(dict.fromkeys(list(review.get("inputs_missing") or []) + ["note"]))
            review.setdefault("lines", []).append(se._input_failure_line(["note"]))
    if isinstance(cand.payload, dict):
        cand.payload["narrative"] = result.get("narrative") or []
        if isinstance(result.get("review"), dict):
            cand.payload["review"] = result["review"]


def _stage_inputs(cand: Candidate) -> None:
    """Everything the draft was made or checked without (P-17): a required
    one fails the job; a safety-relevant one leads the review with a "⚠"
    line, so the publish gate and the auto-publish hold the week; the rest
    are one line at the end (ARCH-25)."""
    result = cand.result
    # A resumed try that reused answers written from inputs that have since
    # moved says so, held at publish (blind re-audit 10/11/26 ARCH-25).
    import schedule_resume as _sres_in
    _rf = _sres_in.resume_facts()
    result["resumed_inputs"] = _rf
    if _rf.get("inputs_moved") and isinstance(result.get("review"), dict):
        review = result["review"]
        if _sres_in.INPUTS_MOVED_LINE not in review.get("lines", []):
            review["lines"].insert(0, _sres_in.INPUTS_MOVED_LINE)
            review["lead_lines"] = [_sres_in.INPUTS_MOVED_LINE] + list(review.get("lead_lines") or [])
        review["resumed_inputs"] = _rf
    _failed_inputs = se._inputs_failed()
    _required_gone = [w for w in _failed_inputs if se.input_class(w) == "required"]
    if _required_gone:
        raise se.ScheduleGenerationError(
            "The schedule couldn't be made: " + ", ".join(se._input_words(_required_gone))
            + " couldn't be read, so nothing was saved. Try again in a few minutes.")
    _ifl = se._input_failure_line(_failed_inputs)
    _ihl = se._input_hold_line(_failed_inputs)
    if isinstance(result.get("review"), dict) and _failed_inputs:
        review = result["review"]
        review["inputs_missing"] = _failed_inputs
        review["inputs_held"] = [w for w in _failed_inputs if se.input_class(w) == "safety"]
        if _ihl:
            review["lines"].insert(0, _ihl)
            review["lead_lines"] = [_ihl] + list(review.get("lead_lines") or [])
        if _ifl:
            review["lines"].append(_ifl)


def _stage_payload(cand: Candidate, ctx: _PassContext) -> None:
    """The week's hours split by pay (E-7, P-6) and the payload the owner's
    poll reads — everything but the history id and version, which only the
    save gives it."""
    result, preview_rows = cand.result, cand.rows
    _split = {"hourly": None, "salaried": None}
    try:
        _split = se._rules.hours_split(preview_rows, result.get("constraints"))
    except Exception as _hsx:
        se._soft_fail("hours split", _hsx, ctx.rid)
    ctx.split = _split
    result["hours_hourly"], result["hours_salaried"] = _split.get("hourly"), _split.get("salaried")
    import schedule_skeleton as _skeleton
    _dropped_rows = cand.dropped_rows
    cand.payload = dict(
        ok=True,
        history_id=None,
        # The version of the rows below; a save names it (UI-1).
        version=None,
        # Every rule breach, hard and soft, with the person and the date;
        # what the fix pass repaired; what it could not.
        rule_violations=result.get("rule_violations") or [],
        review=result.get("review") or {"hard": 0, "soft": 0, "lines": [], "fixes": [], "unfixed": []},
        pending_time_off=result.get("pending_time_off") or {},
        narrative=result.get("narrative") or [],
        generation_seconds=result.get("generation_seconds"),
        # Whether the week took more than one model call — a bool, as both
        # clients read it; the count itself is `calls`.
        chunked=int(result.get("chunked") or 1) > 1,
        calls=int(result.get("chunked") or 1),
        roster=result.get("roster") or [],
        dropped_rows=len(_dropped_rows),
        dropped_row_note=(
            f"{len(_dropped_rows)} line(s) of the generated schedule could not be read and were "
            "left out. Check the week before you publish it."
        ) if _dropped_rows else None,
        schedule_csv=result["schedule_csv"],
        summary=result.get("summary", []),
        preview_rows=preview_rows,
        # How many rows failed a check the model cannot be trusted to make
        # for itself; the owner must see this before publishing to staff.
        rows_needing_review=result.get("rows_needing_review", 0),
        # Retained as a key so an older build of either client keeps
        # decoding; the verdict itself now lives in `quality`.
        strength=result.get("strength") or {"checked": False},
        # The Shift Quality Engine's verdict.
        quality=result.get("quality") or {"checked": False},
        # Moves whenever a rating, target, profile or weighting moves.
        capability_version=result.get("capability_version"),
        # Alternative arrangements of the same people, and why this one won.
        what_if=result.get("what_if") or {"ran": False},
        # What Cavnar changed to raise the score before the owner saw it.
        optimizer=result.get("optimizer") or {"ran": False},
        week_dates=result.get("week_dates", []),
        week_days=result.get("week_days", []),
        projected_revenue=result.get("projected_revenue", 0),
        hours_budget=result.get("hours_budget", 0),
        labor_budget_dollars=result.get("labor_budget_dollars", 0),
        hours_scheduled=round(cand.hours_scheduled, 1),
        labor_target=result.get("labor_target", 30),
        # Needed by the live re-score a manager triggers by moving a shift.
        daily_target_hours=result.get("daily_target_hours") or {},
        staff_constraints=ctx.staff_constraints,
        hours_added_by_backstop=ctx.hours_added,
        backstop_added_dates=ctx.added_dates,
        backstop_extended_dates=ctx.extended_dates,
        backstop_trimmed_dates=ctx.trimmed_dates,
        backstop_floor_added_dates=ctx.floor_added_dates,
        trimmed=result.get("trimmed") or [],
        hours_trimmed=result.get("hours_trimmed") or 0.0,
        staggered=result.get("staggered") or [],
        projected_cost=result.get("projected_cost"),
        over_budget_dollars=result.get("over_budget_dollars"),
        projected_revenue_source=result.get("projected_revenue_source"),
        hourly_profile_ready=result.get("hourly_profile_ready", False),
        demand_data_through=result.get("demand_data_through"),
        reservation_feed=result.get("reservation_feed"),
        holiday_lift=result.get("holiday_lift") or {},
        could_hold=result.get("could_hold") or {},
        departments=result.get("departments") or [],
        regenerated_dates=result.get("regenerated_dates") or [],
        slices=result.get("slices") or [],
        not_scheduled=result.get("not_scheduled") or [],
        ratings_off_roster=result.get("ratings_off_roster") or [],
        # Shifts whose usual front-of-house crew is over the section count.
        section_cap_conflicts=result.get("section_cap_conflicts") or [],
        overtime_forecast=result.get("overtime_forecast") or [],
        standby_days=result.get("standby_days") or [],
        likely_edits=result.get("likely_edits") or [],
        demand_accuracy=result.get("demand_accuracy"),
        week_projection_accuracy=result.get("week_projection_accuracy"),
        # What the draft read beside the rows (memory round 9/29/26; TRUST-1).
        soft_requirements=se._json_safe(result.get("soft_requirements") or []),
        pattern_conflicts=se._json_safe(result.get("pattern_conflicts") or []),
        labor_target_label=result.get("labor_target_label"),
        labor_target_source=result.get("labor_target_source"),
        # What schedule learning set the draft against.
        rotation_plan=result.get("rotation_plan") or {},
        splh_objective=result.get("splh_objective") or {"available": False},
        splh_report=result.get("splh_report") or {},
        starting_headcount=result.get("starting_headcount") or {"available": False},
        gate=result.get("gate") or {"ran": False},
        # The managers' shifts planned before the draft (schedule_skeleton).
        manager_plan=_skeleton.payload(result.get("manager_plan")),
        # The manager rule's backstop and who is still under their minimum
        # hours (E-12, E-13, P-4).
        manager_coverage=result.get("manager_coverage") or {"extended": 0, "added": 0, "left": []},
        min_hours=result.get("min_hours") or {"moved": 0, "added": 0, "left": []},
        # What the generation could not do, said (P-34, E-20, E-30).
        # A gate pass that won carries the first pass's empty days
        # (BUGSGEN-10): the week is partial for them as it was.
        partial=(bool(result.get("unwritten_dates")) and not result.get("redo")) or bool(result.get("partial_week")),
        unwritten_dates=result.get("unwritten_dates") or [],
        unstaffable_dates=result.get("unstaffable_dates") or [],
        starting_point=result.get("starting_point") or {"no_history": False},
        redo=result.get("redo"),
        # Where the draft came from (contract #7, AI-11).
        draft_source=se._json_safe(result.get("draft_source") or {"chosen": "model", "source": "model",
                                                                   "code_dates": [], "why": {}}),
        # The draft's instruction read as rules, each held or not (RM-9).
        instruction_rules=(result.get("review") or {}).get("instruction_rules") or [],
        # The hours split by pay (E-7, P-6).
        hours_hourly=result.get("hours_hourly"),
        hours_salaried=result.get("hours_salaried"),
        # Which budget the hours are and how it was built (D-1, D-2, E-24),
        # each day's target and why it moved (D-24), each date's demand
        # number (D-23), the SHIFT REQUIREMENTS (P-19), the standards (D-25).
        budget_basis=se._json_safe(result.get("budget_basis")),
        daily_target_basis=se._json_safe(result.get("daily_target_basis")),
        daily_target_reasons=se._json_safe(result.get("daily_target_reasons") or {}),
        date_demand=se._json_safe(result.get("date_demand") or {}),
        requirements=se._json_safe(result.get("requirements") or []),
        requirements_by_date=se._json_safe(result.get("requirements_by_date") or {}),
        labor_standards=se._json_safe(result.get("labor_standards") or {}),
        # Whether the leader rules judge anybody (D-10).
        leader_rules_status=se._json_safe(result.get("leader_rules_status")),
        # The repair loop (P-47, E-18) and the stages that did not run.
        repair=se._json_safe(result.get("repair") or {}),
        stage_failures=se._json_safe(result.get("stage_failures") or []),
        stage_seconds=cand.timer.as_dict(),
        # Whether this draft was finished by a resumed try and whether the
        # model answers it reused were written from inputs since moved
        # (ARCH-25).
        resumed_inputs=result.get("resumed_inputs") or {"resumed": False},
    )
    if cand.spec.focus:
        cand.payload["gate"] = {"ran": True, "kept": "regenerated", "focus": list(cand.spec.focus)[:12],
                                "dates": list(cand.spec.dates or []),
                                "reason": "The weakest days were regenerated with what was wrong with them."}


# ── the quality gate: passes held in memory, the winner chosen ───────────

def _judge(in_force: InForce, cand: Candidate, dates) -> tuple:
    """(keep, why) for a gate pass against the candidate in force (P-43):
    never one with more hard breaches or more unmanaged minutes; one with
    fewer is kept; otherwise only a better score on the gate's days with the
    week no worse (_gate_keeps)."""
    before_local = se._gate_local(in_force.cand.quality, dates)
    after_local = se._gate_local(cand.quality, dates)
    return se._gate_keeps(before_local, after_local, cand.score, in_force.cand.score,
                          in_force.cand.rules(), cand.rules())


def _gate(job: GenerationRun, first: Candidate) -> Candidate:
    """The quality gate (_quality_gate) over the first draft, held in memory
    (ARCH-17): its days solved exactly first (LOGIC-26) — a provable conflict
    is explained and nothing is paid for; then at most GATE_ATTEMPTS passes —
    the fill in code (AI cost audit 10/7/26 #21) and the model's rewrite on
    the run's next rung — each judged against the candidate in force. The
    candidate returned is the one the job saves, with what the gate did on
    its payload. A loop, never a recursion; nothing saved inside it."""
    req, clock = job.req, job.clock
    rid = req.restaurant_id
    view = first.view()
    gate = se._quality_gate(view)
    if not gate:
        return first
    if first.spec.kind == "week" and first.spec.dates:
        # A week begun before today (BUGSGEN-24): the gate reworks only the
        # days still ahead.
        ahead = set(first.spec.dates)
        if not set(gate.get("dates") or ()) & ahead:
            first.payload["gate"] = _gate_not_run("past", gate)
            return first
        gate = dict(gate, dates=[d for d in gate["dates"] if d in ahead])
    # The gate's days solved exactly before any model call (LOGIC-26): a
    # breach an added shift could answer is offered to the solver; where no
    # legal arrangement exists, the smallest set of rules in the way is said
    # and no rewrite is paid for.
    exact = se._gate_exact(view, gate, rid)
    if exact is not None:
        gate = dict(gate, exact={k: exact.get(k) for k in ("conflicts", "probed", "unprobed")},
                    exact_fixes=list(exact.get("fixes") or []))
        if se._gate_settled_by_solver(gate, exact):
            print(f"[schedule] quality gate: {gate['dates']} explained by the solver — no rewrite")
            se._record_gate_path(rid, "solver", gate["dates"])
            se._run_gate(gate, "explained")
            first.payload["gate"] = {"ran": True, "kept": "explained", "path": "solver",
                                     "dates": list(gate["dates"]), "conflicts": list(exact["conflicts"])[:12],
                                     "reason": "No legal arrangement of your team fixes "
                                               + se._dates_label(gate["dates"])
                                               + " with every rule as it stands — relax one of the rules named "
                                                 "and it can be filled."}
            return first
    if clock is not None and clock.model_seconds_left() < se.GATE_MIN_MODEL_SECONDS:
        # Not enough of the job's time left to rewrite the weak days and
        # check the rewrite (P-22): the draft stands as it is.
        print(f"[schedule] quality gate skipped: {int(clock.model_seconds_left())}s of model time left")
        se._run_gate(gate, "skipped")
        first.payload["gate"] = _gate_not_run("time", gate)
        return first
    se._run_gate(gate)
    _boundary(job, "gate")
    try:
        # The fill only when it can change something and still leave the
        # rewrite its time (re-audit 10/7/26 #3).
        no_fill = se._gate_fill_skip(first.result, clock)
        if no_fill == "converged" and gate.get("exact_fixes"):
            # the exact solve found shifts the loop did not: the fill takes
            # them in before any rewrite (LOGIC-26)
            no_fill = None
        fill_answer = None if no_fill else \
            se._gate_fill_answer(first.gate_answer, rid, None, gate["dates"], extra_rows=gate.get("exact_fixes"),
                                 csv_text=first.payload.get("schedule_csv"))
        if no_fill:
            print(f"[schedule] quality gate: no fill in code ({no_fill}) — straight to the rewrite")
            gate = dict(gate, fill={"tried": False, "skipped": no_fill})
        in_force = InForce(first, dict(first.payload, gate={"ran": True, **gate}))
        attempts = list(GATE_ATTEMPTS) if fill_answer is not None else ["rewrite"]
        escalated_here = False
        if fill_answer is None:
            # No fill in code to try: the model's rewrite is the escalation
            # straight away, on the next rung (_escalate_for_gate) — unless
            # the run is at its caps, when the draft stands as it was.
            if se._escalate_for_gate(gate) == "capped":
                print("[schedule] quality gate rewrite skipped: the run is at its caps")
                first.payload["gate"] = _gate_not_run("capped", gate)
                return first
            escalated_here = True
    except se.ScheduleGenerationError:
        raise
    except Exception as _gx:
        # The draft stands; the failed gate is captured (P-17) and said on
        # the payload (BUGSGEN-1, BUGSGEN-16 — it used to carry no verdict).
        print(f"[schedule] quality gate failed: {_gx!r}")
        se._ops.capture(_gx, job="schedule_quality_gate", context=f"restaurant_id={rid}")
        first.payload["gate"] = _gate_not_run("error", gate)
        return first
    for attempt in attempts:
        if attempt == "fill":
            outcome = _gate_fill(job, gate, in_force, fill_answer)
            if isinstance(outcome, Candidate):
                return outcome                       # settled in code
            in_force = outcome                       # the rewrite goes on from here
            continue
        return _gate_rewrite(job, gate, in_force, escalated=escalated_here)
    return in_force.cand


def _gate_fill(job: GenerationRun, gate: dict, in_force: InForce, fill_answer: dict):
    """The gate's fill in code (#21): the gate's dates re-run through the
    repair loop from the draft's own rows, nothing paid. Returns the filled
    Candidate when what tripped the gate is gone (the job saves it), else the
    InForce the model's rewrite starts from — the fill when it was kept, the
    draft when it was not (or when the fill failed)."""
    rid = job.req.restaurant_id
    dates = list(gate["dates"])
    print(f"[schedule] quality gate: filling {dates} in code ({gate['reason']})")
    first = in_force.cand
    base = _held_base(first)
    try:
        fill = _build_pass(job, PassSpec(kind="gate_fill", dates=dates, base=base, answer=fill_answer, gate=gate))
    except se.ScheduleStopped:
        raise
    except Exception as e:
        # The code's fill failed (#21): the model's rewrite is still owed the
        # gate's days, from the draft as it was.
        print(f"[schedule] quality gate fill failed:\n{traceback.format_exc()}")
        try:
            se._ops.capture(e, job="schedule_generate", context=f"restaurant_id={rid}")
        except Exception:
            pass
        return in_force
    better, _why = _judge(in_force, fill, dates)
    # Done when what tripped the gate on those days is gone, else the model
    # rewrites them.
    survives = (not better) or se._gate_survives(gate, se._quality_gate(fill.view()))
    fill_info = {"tried": True, "kept": bool(better), "resolved": not survives, "dates": dates}
    if not survives:
        se._record_gate_path(rid, "code", dates)
        se._run_gate(gate, "filled")
        fill.payload["gate"] = {"ran": True, "kept": "filled", "path": "code", "fill": fill_info, "dates": dates,
                                "reason": "Cavnar AI filled the weakest days from your own team, without "
                                          "rewriting them."}
        return fill
    if better:
        return InForce(fill, dict(fill.payload, gate={"ran": True, "kept": "filled", "path": "code",
                                                      "fill": fill_info, "dates": dates,
                                                      "reason": "Cavnar AI filled what it could of the "
                                                                "weakest days from your own team."}))
    return InForce(first, dict(in_force.payload, gate=dict((in_force.payload or {}).get("gate") or {},
                                                           fill=fill_info)))


# Why the quality gate did not run on a draft that tripped it (BUGSGEN-16):
# the payload always carries a verdict, so the owner and the clients can tell
# a gate skipped or failed from one that found nothing.
_GATE_NOT_RUN = {
    "time": "Cavnar AI ran out of time to rework the weakest days, so the draft is as it was written.",
    "capped": "Cavnar AI reached this draft's limit on AI model calls before it could rework the weakest days, "
              "so the draft is as it was written.",
    "error": "Cavnar AI couldn't check this draft's weakest days just now, so the draft is as it was written.",
    "past": "The weakest days have already happened, so Cavnar AI left them as they were.",
}


def _gate_not_run(why: str, gate: dict = None) -> dict:
    out = {"ran": False, "why": why, "reason": _GATE_NOT_RUN.get(why, _GATE_NOT_RUN["error"])}
    if gate:
        out["dates"] = list(gate.get("dates") or [])
        if gate.get("reason"):
            out["tripped"] = gate.get("reason")
    return out


def _rewrite_skipped(in_force: InForce, why: str) -> InForce:
    """The rewrite was owed but not run (time, the run's caps): the payload in
    force says so beside what the fill did (BUGSGEN-16)."""
    g = dict((in_force.payload or {}).get("gate") or {})
    g.update(rewrite={"tried": False, "skipped": why})
    if not g.get("kept"):
        g.update(kept="original", reason=_GATE_NOT_RUN.get(why, _GATE_NOT_RUN["error"]))
    return InForce(in_force.cand, dict(in_force.payload, gate=g))


def _held_base(cand: Candidate) -> Base:
    """A candidate held in memory as the base a gate pass writes over: its
    stored text (schedule_rows.to_csv) read back as rows, exactly as a saved
    draft's would be — never saved itself (ARCH-17)."""
    text = cand.payload.get("schedule_csv") or ""
    return Base(rows=se._versions.rows_from_csv(text), csv=text, history_id=None,
                week_start=(cand.result.get("week_dates") or [None])[0], facts=_week_facts(cand.result))


# What a pass says about the whole week, beside its rows (BUGSGEN-10).
_WEEK_FACT_KEYS = ("week_dates", "draft_source", "unwritten_dates", "unstaffable_dates", "stage_failures",
                   "empty_accepted", "closed_dates", "open_unwritten", "generation_notes")


def _week_facts(result: dict) -> dict:
    return copy.deepcopy({k: result.get(k) for k in _WEEK_FACT_KEYS if result.get(k) is not None})


def _inherit_week_facts(result: dict, facts: dict, gate_dates) -> None:
    """A gate pass over a candidate held in memory keeps that candidate's days
    outside its own: what the first pass said about them is this pass's to
    say too — which days were built without the model and why, the days not
    written, the days nobody could work, a stage that did not run, the empty
    days accepted, the days closed to the passes. A gate rewrite's result
    used to describe only its own days, and when it won the review lost
    "Built without the AI model ..." for six of the week's seven days (blind
    re-audit 10/11/26 BUGSGEN-10). Mutates `result`."""
    gate = {str(d)[:10] for d in gate_dates or ()}
    week = list(facts.get("week_dates") or result.get("week_dates") or [])
    kept = set(week) - gate

    def _on(items, dates):
        return [dict(u) for u in items or [] if str(u.get("date") or "")[:10] in dates]
    own_ds = dict(result.get("draft_source") or {})
    first_ds = dict(facts.get("draft_source") or {})
    first_code = sorted(d for d in first_ds.get("code_dates") or [] if d in kept)
    own_code = sorted(d for d in own_ds.get("code_dates") or [] if d in gate)
    code_line = None
    if own_code and own_ds.get("line"):
        # The pass drafted its own days in code (the gate's fill reuses the
        # first answer): its line names them, unless it misses a kept day.
        ds = dict(own_ds)
        if not (set(first_code) - set(own_ds.get("code_dates") or ())):
            code_line = own_ds.get("line")
    elif first_code:
        ds = dict(first_ds)
        if first_code == sorted(first_ds.get("code_dates") or []):
            code_line = first_ds.get("line")
    else:
        ds = dict(own_ds or first_ds)
    code = sorted(set(first_code) | set(own_code))
    why = {d: (own_ds.get("why") or {}).get(d) or (first_ds.get("why") or {}).get(d) for d in code}
    ds["code_dates"] = code
    ds["why"] = why
    if first_ds.get("chosen"):
        ds["chosen"] = first_ds["chosen"]               # the arm the week drew
    if code and code_line is None:
        try:
            import schedule_fallback as _sfb
            reasons = [w for w in why.values() if w]
            main = max(set(reasons), key=reasons.count) if reasons else "other"
            src = ds.get("code_source") or ds.get("source") or "solver"
            code_line = _sfb.note_line(src, main, code, ds.get("base") if src == "carry_forward" else None)
        except Exception as e:
            print(f"[schedule] code-built days line not rebuilt: {e!r}")
            se._ops.capture(e, job="schedule_generate", context="code-built days line")
    if code_line:
        ds["line"] = code_line
    else:
        ds.pop("line", None)
    result["draft_source"] = ds
    # The days the first pass left empty stay empty: a gate day keeps the
    # first draft's rows, so it is unwritten only if it was empty there and
    # the pass did not write it either.
    own_notes = list(result.get("generation_notes") or [])
    own_recon = _recon_lines(own_ds, result)
    own_unwritten = {str(u.get("date") or "")[:10] for u in result.get("unwritten_dates") or []}
    first_unwritten = list(facts.get("unwritten_dates") or [])
    week_unwritten = _on(first_unwritten, kept) + [dict(u) for u in first_unwritten
                                                   if str(u.get("date") or "")[:10] in own_unwritten & gate]
    result["unwritten_dates"] = sorted(week_unwritten, key=lambda u: u.get("date") or "")
    result["partial_week"] = bool(week_unwritten)
    result["unstaffable_dates"] = _on(facts.get("unstaffable_dates"), kept) +         _on(result.get("unstaffable_dates"), gate)
    accepted = _on(facts.get("empty_accepted"), kept)
    if accepted:
        result["empty_accepted"] = accepted + _on(result.get("empty_accepted"), gate)
    result["closed_dates"] = sorted({d for d in facts.get("closed_dates") or [] if d in kept}
                                    | {d for d in result.get("closed_dates") or [] if d in gate})
    result["open_unwritten"] = sorted({d for d in facts.get("open_unwritten") or [] if d in kept}
                                      | {d for d in result.get("open_unwritten") or [] if d in gate})
    own_fail = list(result.get("stage_failures") or [])
    seen = {(f.get("stage"), f.get("line")) for f in own_fail}
    result["stage_failures"] = own_fail + [dict(f, from_first_pass=True) for f in facts.get("stage_failures") or []
                                           if (f.get("stage"), f.get("line")) not in seen]
    # The week's lead lines rebuilt from the merged facts, in the order the
    # generation writes them; then the first pass's other lines (a first week
    # with no history), then what this pass said of its own days (a gate day
    # it could not redo).
    first_notes = list(facts.get("generation_notes") or [])
    first_recon = _recon_lines(first_ds, facts)
    notes = []
    if result["unstaffable_dates"]:
        notes.append(se._unstaffable_line(result["unstaffable_dates"]))
    if code_line:
        notes.append(code_line)
    if week_unwritten:
        notes.append(se._unwritten_line(result["unwritten_dates"]))
    if accepted:
        notes.append(se._accepted_empty_line(result["empty_accepted"]))
    for n in [x for x in first_notes if x not in first_recon] + \
            [x for x in own_notes if x not in own_recon and x not in first_notes]:
        if n and n not in notes:
            notes.append(n)
    result["generation_notes"] = notes


def _recon_lines(ds: dict, facts: dict) -> set:
    """The lead lines a pass's own facts produce (the code-built days, the
    days not written, the empty days accepted, the days nobody could work),
    so a merge rebuilds them from the merged facts instead of keeping both
    versions (BUGSGEN-10)."""
    out = {ds.get("line")}
    try:
        if facts.get("unwritten_dates"):
            out.add(se._unwritten_line(facts["unwritten_dates"]))
        if facts.get("empty_accepted"):
            out.add(se._accepted_empty_line(facts["empty_accepted"]))
        if facts.get("unstaffable_dates"):
            out.add(se._unstaffable_line(facts["unstaffable_dates"]))
    except Exception as e:
        print(f"[schedule] week lines not rebuilt: {e!r}")
        se._ops.capture(e, job="schedule_generate", context="week lines")
    out.discard(None)
    return out


def _standing(in_force: InForce) -> Candidate:
    """The candidate in force, finishing with the payload the gate left it."""
    in_force.cand.payload = in_force.payload
    return in_force.cand


def _gate_rewrite(job: GenerationRun, gate: dict, in_force: InForce, escalated=False) -> Candidate:
    """The quality gate's model rewrite of its days over the candidate in
    force, as the run's escalation (the next rung, the gate's reasons as its
    notes). With too little of the job's model time left (P-22), or the run
    at its caps, the candidate in force stands. A rewrite kept only by the
    gate's own test (P-43); one that failed leaves the draft as it was."""
    req, clock = job.req, job.clock
    rid = req.restaurant_id
    dates = list(gate["dates"])
    if not escalated:
        _check_stop(clock)
        if clock is not None and clock.model_seconds_left() < se.GATE_MIN_MODEL_SECONDS:
            print(f"[schedule] quality gate rewrite skipped: {int(clock.model_seconds_left())}s of model time left")
            se._run_gate(gate, "skipped")
            return _standing(_rewrite_skipped(in_force, "time"))
        if se._escalate_for_gate(gate) == "capped":
            print("[schedule] quality gate rewrite skipped: the run is at its caps")
            return _standing(_rewrite_skipped(in_force, "capped"))
    print(f"[schedule] quality gate: regenerating {dates} ({gate.get('reason')})")
    se._record_gate_path(rid, "model", dates)
    base_cand = in_force.cand
    base = _held_base(base_cand)
    focus = gate.get("focus") or [gate.get("reason")]
    try:
        rewrite = _build_pass(job, PassSpec(kind="gate_rewrite", dates=dates, base=base, focus=focus, gate=gate))
    except se.ScheduleStopped:
        raise
    except Exception as e:
        # The rewrite failed, but the draft it was improving is sound: the
        # owner gets that draft, not a failure it did not have.
        print(f"[schedule] quality gate rewrite failed:\n{traceback.format_exc()}")
        se._run_gate(None, "original")
        try:
            se._ops.capture(e, job="schedule_generate", context=f"restaurant_id={rid}")
        except Exception:
            pass
        fb = dict(in_force.payload)
        fb["gate"] = {"ran": True, "kept": "original", "dates": dates,
                      "reason": "Cavnar AI tried to rewrite the weakest days but couldn't just now, so your draft "
                                "is as it was."}
        return _standing(InForce(base_cand, fb))
    better, why = _judge(in_force, rewrite, dates)
    se._run_gate(None, "rewritten" if better else "original")
    if better:
        return rewrite
    fb = dict(in_force.payload)
    fb["gate"] = {"ran": True, "kept": "original", "dates": dates,
                  "reason": ("Cavnar AI rewrote the weakest days to fix them, but the rewrite broke more of your "
                             "rules or left more time with no manager, so your original draft was kept."
                             if why == "rules" else
                             "Cavnar AI rewrote the weakest days to fix them, but the rewrite was no better on "
                             "those days, so your original draft was kept.")}
    return _standing(InForce(base_cand, fb))


# ── the one save ─────────────────────────────────────────────────────────

def _metrics_kind(spec: PassSpec) -> str:
    """The schedule_metrics kind of a pass: the gate's fill, a focused redo
    (the gate's rewrite), an owner's redo, else the week."""
    if spec.kind == "gate_fill":
        return "gate_fill"
    if spec.kind == "week":
        return "week"                          # a week begun before today too
    if spec.focus and spec.redo:
        return "gate_rewrite"
    return "redo" if spec.redo else "week"


def _commit(job: GenerationRun, winner: Candidate) -> int:
    """The job's commit point (ARCH-3, ARCH-17): the winner saved ONCE — the
    week, its review, timings, weather and economics, its `generated`
    version, every model call of every pass linked to it and its experiment
    arm, in one transaction (PIPE-7, ARCH-29, BUGSLIFE-25) — then the
    after-save bookkeeping, none of which can cost the week anything it
    needs. A job past its deadline, called dead or stopped saves nothing."""
    req, clock = job.req, job.clock
    rid = req.restaurant_id
    result, spec = winner.result, winner.spec
    job.week_dates = list(result.get("week_dates") or [])
    if not winner.noted:
        # Cavnar AI's note: for the winner only, before its save (ARCH-13).
        _stage_note(job, winner)
    # A job past its own deadline, or one a poll has already called dead,
    # saves nothing (P-22); checked with half the store's grace in hand.
    # Stopped by the owner is said as stopped (BUGSGEN-12); the save itself
    # re-checks the row in its own write.
    se._raise_if_stopped(clock, owner_only=True)
    if (clock is not None and clock.seconds_left() < -se._ops.ASYNC_DEADLINE_GRACE_SECONDS / 2) \
            or not se._ops.job_still_pending(req.job_id):
        raise se.ScheduleGenerationError(se._TIMED_OUT_MESSAGE)
    timer = winner.timer
    _wd = result.get("week_dates", [])
    # How long each stage took, end to end (P-24), kept with the week.
    _model_s = float(result.get("generation_seconds") or 0)
    timer.add("model", _model_s)
    timer.seconds["inputs"] = round(max(0.0, timer.seconds.get("inputs_and_model", 0.0) - _model_s), 3)
    timer.seconds.pop("inputs_and_model", None)
    _t_save = time.monotonic()
    se.reach_stage("save")
    gen_box = {}
    generation_ids = [c.result.get("generation_id") for c in job.passes if c.result.get("generation_id")]
    if result.get("generation_id") and result.get("generation_id") not in generation_ids:
        generation_ids.append(result.get("generation_id"))

    def _with_the_week(_conn, _new_id):
        import schedule_output as _so_link
        import schedule_experiments as _sx_rec
        _text = _conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (_new_id,)).fetchone()[0]
        gen_box["csv"] = _text
        gen_box["version"] = se._versions._insert_version(
            _conn, rid, _new_id, "generated", _text, result.get("quality"), "Cavnar AI", "system")[1]
        # Every model call of every pass this job made — the week, the gate's
        # rewrite — keyed to the one week it produced (PR-31).
        for gid in generation_ids:
            _so_link.link_calls(rid, _new_id, gid, conn=_conn)
        _sx_rec.record(rid, _new_id, result.get("experiment_arms") or [],
                       (result.get("quality") or {}).get("score"),
                       solver_applied=(result.get("solver") or {}).get("applied")
                       if (result.get("solver") or {}).get("ran") else None, conn=_conn)
    # An owner's redo writes only over the draft it read (PIPE-2): a
    # DraftChanged goes back to _owner_redo. The gate's passes were never
    # saved, so the week is one ordinary save.
    base = spec.base if (spec.redo and spec.base is not None and spec.base.history_id) else None
    history_id = se._save_durably(
        req.job_id, clock, rid, _wd[0] if _wd else None, _wd[-1] if _wd else None,
        round(winner.hours_scheduled, 1), result.get("hours_budget", 0), result.get("labor_target", 30),
        result["schedule_csv"], result.get("summary", []),
        quality=result.get("quality"), what_if=result.get("what_if"),
        hours_hourly=result.get("hours_hourly"), hours_salaried=result.get("hours_salaried"),
        review=se._json_safe(result.get("review")),
        generation_seconds=result.get("generation_seconds"),
        weather=se._json_safe(result.get("weather_forecast")),
        economics={"projected_revenue": result.get("projected_revenue"),
                   "projected_revenue_source": result.get("projected_revenue_source"),
                   # The sales the crew was sized on and what Home states
                   # (ARCH-28).
                   "forecast_basis": se._json_safe(result.get("forecast_basis")),
                   "labor_budget_dollars": result.get("labor_budget_dollars"),
                   # The rate the week was priced at (SQ-6).
                   "blended_rate": result.get("blended_rate"),
                   "daily_target_hours": result.get("daily_target_hours") or {},
                   "demand_data_through": result.get("demand_data_through"),
                   # What the draft was ASKED for (LEARNING-3).
                   "requirements_by_date": se._json_safe(result.get("requirements_by_date") or {})},
        stage_seconds=timer.as_dict(),
        base=(int(base.history_id), base.csv) if base is not None else None,
        in_transaction=_with_the_week,
    )
    if not history_id:
        # The draft is the job's commit point (ARCH-3): no durable id, no
        # "ok" — a draft on screen that can never be saved or sent.
        raise se.ScheduleGenerationError(se._SAVE_FAILED_MESSAGE)
    job.history_id = history_id
    payload = dict(winner.payload, history_id=history_id, version=gen_box.get("version"))
    job.payload = payload
    try:
        _after_save(job, winner, history_id, gen_box, _t_save)
    except Exception as _hist_ex:
        # Something after the save failed (the draft is saved and in force):
        # captured, and the week goes on with its id.
        print(f"[schedule history] after the save: {_hist_ex}")
        se._ops.capture(_hist_ex, job="schedule_generate",
                        context=f"restaurant_id={rid} — a step after the history save failed")
    return history_id


def _after_save(job: GenerationRun, winner: Candidate, history_id: int, gen_box: dict, t_save: float) -> None:
    """Everything after the one save — timings, metrics, the next week marked
    built, the scorecard's tags, the account event, the check snapshot, the
    model's dropped rows, the queue's saved checkpoint. Each soft."""
    req = job.req
    rid = req.restaurant_id
    result, preview_rows, timer = winner.result, winner.rows, winner.timer
    payload = job.payload
    # The rows as stored, with the shift id each kept or was given: the
    # client holds them and sends them back on save.
    if gen_box.get("csv"):
        import schedule_rows as _srows_out
        result["schedule_csv"] = gen_box["csv"]
        payload["schedule_csv"] = gen_box["csv"]
        _stored = se._versions.rows_from_csv(gen_box["csv"])
        if len(_stored) == len(preview_rows):
            for _pr, _sr_ in zip(preview_rows, _stored):
                _pr["shift_id"], _pr["person_id"] = _sr_["shift_id"], _sr_["person_id"]
                _pr["locked"] = _srows_out.is_locked(_sr_)
    timer.add("save", time.monotonic() - t_save)
    result["stage_seconds"] = timer.as_dict()
    payload["stage_seconds"] = result["stage_seconds"]
    # The save's own seconds join the stored timings afterwards: only
    # timings, so a failure here costs the week nothing it needs.
    try:
        se._store_stage_seconds(history_id, rid, result["stage_seconds"])
    except Exception as _tsx:
        print(f"[schedule] stage timings not updated for history {history_id}: {_tsx}")
        se._ops.capture(_tsx, job="schedule_generate", context=f"restaurant_id={rid} — stage timings")
    # What each pass cost and what the model's rows were worth (AI-2, AI-23,
    # the eval's context) — every pass this job built, against the one week
    # it saved.
    for c in job.passes:
        se._record_generation_metrics(
            rid, history_id, c.result, c.metrics_in, kind=_metrics_kind(c.spec),
            kept_dates=(set(c.result.get("week_dates") or []) - set(c.spec.dates or [])) if c.spec.redo else (),
            seconds=sum(c.timer.seconds.values()), final_rows=c.rows)
    # A whole week built discharges "next week's schedule isn't built"; an
    # owner's redo of a few days does not, nor a week with days the
    # generation could not write (P-34).
    if req.redo:
        whole = not winner.pinned and not winner.result.get("unwritten_dates")
    else:
        week = next((c for c in job.passes if c.spec.kind == "week"), None)
        whole = week is not None and not week.result.get("unwritten_dates")
    if whole:
        se.mark_next_week_built(rid, history_id)
    try:
        # What made this draft, kept for the Schedule Learning Scorecard (RM-4,
        # AI-29, LEARNING-7). Never blocks the save.
        import schedule_scorecard as _ssc_tag
        _ga = winner.gen_attr or {}
        _want = _ga.get("ablation_wanted")
        _ssc_tag.tag_generation(
            rid, history_id, generation_id=result.get("generation_id"),
            arms=result.get("experiment_arms") or [],
            origins=_ssc_tag.classify_rows(_ga.get("pre") or [], preview_rows, _ga.get("info"))
            if _ga.get("pre") else {},
            ablation={"ran": False, "queued": True, "learned": _want["learned"]} if _want else None,
            rules=_ssc_tag.rules_honoured(result.get("constraints"), result.get("rule_violations")))
        if _want:
            # Off the owner's wait (ARCH-14): queued after the tags exist, with
            # the pass's rows; the bulk worker re-runs the loop in the context
            # stored with this week's metrics and writes its result over the
            # placeholder.
            _ab = _ssc_tag.enqueue_ablation(
                rid, history_id, kind=_metrics_kind(winner.spec), pre_rows=_ga.get("pre") or [],
                rows_on=((winner.metrics_in or {}).get("loop") or {}).get("rows") or preview_rows,
                learned=_want["learned"], reassign_on=_want["reassign_on"])
            if not _ab.get("queued"):
                _ssc_tag.record_ablation(history_id, _ab)
    except Exception as _tgx:
        print(f"[schedule] generation tags not kept: {_tgx}")
    try:
        from models import log_account_event
        _wd = result.get("week_dates") or []
        log_account_event(rid, "schedule_generated", None,
                          detail=f"week of {_wd[0] if _wd else '?'} · {len(preview_rows)} shifts · "
                                 f"{result.get('rows_needing_review', 0)} flagged")
    except Exception as _lx:
        print(f"[schedule] account event not logged: {_lx}")
    try:
        # The rules, requirements and signals this week was checked and
        # scored against, kept with it (ARCH-35).
        import schedule_output as _so_snap
        _so_snap.record_check_snapshot(rid, history_id, result.get("generation_id"), result.get("constraints"),
                                       result.get("shift_requirements"), winner.pass_sig, winner.pass_w)
    except Exception as _snx:
        se._ops.capture(_snx, job="schedule_check_snapshot", context=f"restaurant_id={rid}")
    for c in job.passes:
        if c.dropped_rows:
            # Rows the model wrote that could not be read: an AI-quality
            # finding (fix round G #58), not a failing job.
            print(f"[schedule] dropped {len(c.dropped_rows)} malformed row(s): {c.dropped_rows[:3]}")
            import ai_utils as _ai_q
            _ai_q.record_quality_event("labor_schedule", "line_dropped", restaurant_id=rid,
                                       action="labor_schedule", n=len(c.dropped_rows),
                                       detail=f"{len(c.dropped_rows)} malformed schedule row(s) dropped")
    try:
        # The job's commit point as a queue checkpoint (SCALE-2, RM-7): a try
        # a deploy kills from here on is finished by the next try with this
        # draft (schedule_resume), never generated and paid for again.
        import schedule_resume as _sres
        _sres.checkpoint_saved(history_id, se._json_ready(payload),
                               week_start=(result.get("week_dates") or [None])[0] or req.week_start)
    except Exception as _cpx:
        print(f"[schedule] saved checkpoint not kept: {_cpx!r}")
