"""
schedule_context.py — what one schedule generation runs inside, below
every module that drafts, repairs or scores a week.

  * the two errors a generation ends with (ScheduleGenerationError and
    ScheduleStopped);
  * the generation's clock, as the code running on its thread reads it
    (current_clock — schedule_engine.GenerationClock sets it);
  * its read-once store (frozen_inputs / frozen_read) and the inputs it
    went without (_note_input_failure / _inputs_failed);
  * the model route its calls run on (schedule_route, start_route and the
    schedule call's thinking settings);
  * the restaurant's schedule week (week_start_day_of / _for, and
    check_week_start for a Generate request).

Layer S0 of the schedule modules (ARCHITECTURE_MANIFEST.md §3a): it imports
no schedule module but schedule_time, so labor, the rules and the learners
read the generation's context without importing schedule_engine.
schedule_engine re-exports every name here, so `se.NAME` keeps working.
"""
import base64
import contextlib
import contextvars
import copy
import json
import logging
import os
import zlib

import schedule_time as _stime

log = logging.getLogger(__name__)


class ScheduleGenerationError(ValueError):
    """A generation that must not be saved, with a sentence the owner can
    read. Anything else that fails a job is logged and shown as a plain
    "try again" — never its exception text or a traceback (DATA-46)."""


class ScheduleStopped(ScheduleGenerationError):
    """The owner pressed Stop (10/9/26): the job row is no longer pending, so
    no further model call starts and nothing is saved."""


_CLOCK = contextvars.ContextVar("schedule_generation_clock", default=None)


def current_clock():
    """The GenerationClock of the generation running on this thread, or None
    (a direct call — a test, a script — runs unbounded as before)."""
    return _CLOCK.get()


# ── One frozen context per generation (schedule audit 10/3/26 P-36) ───────
#
# A generation read the same inputs again and again: the Shift Quality
# signals five times (about a dozen reads each, 56 days of hourly sales among
# them), the tail of last week three times, the sales curve three times, and
# every model call re-read attendance, the data-health readiness and who
# holds which role. Inside a generation (_run_schedule_job) every read made
# through frozen_read happens once and is handed out as a copy after that, so
# the prompt, the repair loop and the score see one snapshot of the inputs.
# The rules the week is checked against are deliberately not frozen: they are
# rebuilt after the model call (P-41), so time off approved while the model
# was writing is in the draft. Outside a generation — the Studio's rescore, a
# script — frozen_read is simply the read.
#
# The same store collects the inputs that could not be read (_soft_fail), so
# the review names what the draft was made without (P-17).
_FROZEN = contextvars.ContextVar("schedule_generation_frozen", default=None)


@contextlib.contextmanager
def frozen_inputs():
    """The generation's read-once store ({"reads", "failed"}), entered by
    _run_schedule_job. A nested run — the quality gate's rewrite inside the
    same job — shares the outer run's store."""
    store = _FROZEN.get()
    if store is not None:
        yield store
        return
    store = {"reads": {}, "failed": []}
    token = _FROZEN.set(store)
    # And one read of the client_data row and one parse of the shift history
    # for the whole generation (models.read_scope — scheduler audit 10/10/26,
    # SCALE-6): every stage re-parsed it, ~25 times per inputs build. The
    # restaurant row and the roster reads (capabilities, staff settings, the
    # hand-added team, the held roles) are read once too (blind re-audit
    # 10/11/26 SCALE-24: 207 reads of the row and 132 of the capabilities in
    # one 60-person generation) — and read again, once, where the generation
    # must see what the owner changed while it ran: the rules after the
    # model (_rules_after_the_model → models.refresh_scope, P-41). A write
    # through this process drops what it changed (models._note_write).
    import models as _m_frozen
    try:
        with _m_frozen.read_scope(restaurant_rows=True, roster=True):
            yield store
    finally:
        _FROZEN.reset(token)


def frozen_read(key, fn):
    """fn() once per generation for `key` (a tuple naming the read and its
    arguments) and a copy of that first answer for every later read; outside
    a generation simply fn(). A read that raises is not stored, so the
    caller's own fallback runs, as before."""
    store = _FROZEN.get()
    if store is None:
        return fn()
    reads = store["reads"]
    if key not in reads:
        reads[key] = fn()
    return copy.deepcopy(reads[key])


def _note_input_failure(what) -> None:
    """An input the draft went ahead without, for the review (P-17)."""
    store = _FROZEN.get()
    if store is not None and what not in store["failed"]:
        store["failed"].append(what)


def _inputs_failed() -> list:
    store = _FROZEN.get()
    return list(store["failed"]) if store is not None else []


# ── The eval's verdicts the pre-router reads (schedule_eval writes them) ─────
#
# The newest stored verdict of T4 against T3 for a roster size band or a kind
# of week (schedule_eval.record_verdict); a week starts on T4 only on one.

# ── One orchestrated run per generation (AI orchestration, 10/7/26) ───────
#
# The week runs on the ai_workflows "labor_schedule" ladder (T3 Sonnet 5.5,
# then T4 Opus 5.5, both at medium thinking): owner decision 3, 10/7/26 —
# "Sonnet 5.5 first if the eval clears 80%, hard weeks straight to Opus".
# On Simple EJ's one stored week (scripts/schedule_model_eval.py, prod
# 10/7/26) Sonnet 5.5 medium matched Opus 5.5 medium, the setting it
# replaces (quality 78 v 79, 0 hard rules after the repair for both) at a
# third of the cost.
#
# One job (_run_schedule_job, the quality gate's fill and rewrite inside it)
# is ONE run: every model call of it — the week, its slices, retries and
# splits, the gate's rewrite — carries the run id as its ai_usage
# correlation_id, and the run is written to ai_runs when the job ends (its
# start and final tier, the escalation, the gate's reasons, the cost read
# from the ledger). The job's recursive shape (the gate re-enters the job)
# does not fit ai_orchestrator.generate's attempt/check loop, so the run is
# kept here and recorded through the orchestrator's own record path.
#
#   * The route: a SCHEDULE_MODEL env pin wins and skips the ladder (that
#     model, SCHEDULE_EFFORT) — the operator's override, as before.
#     Otherwise the policy's first rung, or T4 when hard_week_reasons finds
#     the week hard (the pre-router, deterministic, thresholds below).
#   * The escalation is the quality gate: the code's fill runs first
#     (_gate_fill_answer, no model); when what tripped the gate survives it,
#     the gate's model rewrite runs on the NEXT rung (trigger "rule_breach",
#     the gate's reasons as its notes — they are already its `focus`). A
#     week already on the top rung (pre-routed, or pinned) rewrites on it.
#     The run's caps (calls, dollars) are checked before the escalation, as
#     generate() checks them before each rung: a capped run keeps its draft.
WORKFLOW = "labor_schedule"


_SCHED_RUN = contextvars.ContextVar("schedule_generation_run", default=None)


def schedule_pinned() -> bool:
    """Whether SCHEDULE_MODEL pins the schedule's model (no ladder)."""
    return bool((os.getenv("SCHEDULE_MODEL") or "").strip())


def _pinned_route():
    """The call site's own route: ai_utils.model_for("schedule") (the
    SCHEDULE_MODEL pin, else Opus 5.5) at SCHEDULE_EFFORT on a model
    that thinks — the route every week ran on before the ladder."""
    import ai_utils as _ai
    import ai_workflows as _wf
    model = _ai.model_for("schedule")
    return _wf.Route(tier=_wf.DEFAULT, model=model,
                     effort=SCHEDULE_EFFORT if schedule_model_thinks(model) else None)


# No case for the $10/$50 tier on the schedule (scheduler audit 10/10/26
# AI-30): on the 10/7/26 eval every medium-or-higher arm ended at 0 hard
# breaches after the repair, and Opus high (62k tokens, $1.39) scored 83 to
# Sonnet medium's 78 at $0.41 — a few unverified points for multiplying the
# dominant cost. A route that would put the week on Claude Fable or Mythos
# (an env pin, a tier override) runs on Opus 5.5 instead, said in the run's
# steps, unless SCHEDULE_ALLOW_TOP_TIER=1 — set only once the eval
# (schedule_eval) shows a measured ceiling below it.
SCHEDULE_BARRED_MODELS = ("claude-fable", "claude-mythos")


def _route_allowed(route):
    """`route`, or Opus 5.5 at the route's effort when it names a barred model."""
    model = str(getattr(route, "model", "") or "").lower()
    if not model.startswith(SCHEDULE_BARRED_MODELS) or os.getenv("SCHEDULE_ALLOW_TOP_TIER") == "1":
        return route
    import ai_utils as _ai
    import ai_workflows as _wf
    print(f"[schedule] {route.model} is barred from the schedule route (AI-30) — running on {_ai.OPUS_55}")
    run = _SCHED_RUN.get()
    if run is not None:
        run.setdefault("steps", []).append({"barred_model": route.model, "ran_on": _ai.OPUS_55})
    return _wf.Route(tier=route.tier, model=_ai.OPUS_55, effort=route.effort or "medium")


def start_route(start=None):
    """The route a generation starts on: the pin when SCHEDULE_MODEL is set;
    else the policy's rung `start` (a pre-routed tier on its ladder) or its
    first. A "default" rung (a console override) is the call site's own.
    Never a barred model (AI-30, _route_allowed)."""
    import ai_workflows as _wf
    if schedule_pinned():
        return _route_allowed(_pinned_route())
    r = _wf.route_for(_wf.policy(WORKFLOW), 0, start)
    return _route_allowed(_pinned_route() if r.tier == _wf.DEFAULT else r)


def schedule_route():
    """The route the next schedule call runs on: the run's (its start, or
    the rung the gate escalated it to), else the start route — a direct
    call outside a job (a test, a script) runs on the policy's first rung."""
    run = _SCHED_RUN.get()
    if run is not None and run.get("route") is not None:
        return run["route"]
    return start_route()


def current_schedule_run():
    """The generation's run state (a copy), or None outside a job."""
    run = _SCHED_RUN.get()
    return dict(run) if run is not None else None


def week_start_day_for(restaurant, week_start=None) -> int:
    """The weekday a week starts on when a date in it is named: a draft
    already saved under `week_start` keeps the week it was built on (a
    restaurant that moved its week start — Simple EJ's, Monday to Wednesday
    on 10/10/26 — still redoes and reopens its older drafts on their own
    dates; blind re-audit 10/11/26), else the restaurant's own week start."""
    if week_start:
        try:
            from models import get_conn as _gc_anchor
            conn = _gc_anchor()
            try:
                hit = conn.execute("SELECT 1 FROM schedule_history WHERE restaurant_id=? AND week_start=? LIMIT 1",
                                   (getattr(restaurant, "id", restaurant), str(week_start)[:10])).fetchone()
            finally:
                conn.close()
            if hit:
                return _stime._date_of(week_start).weekday()
        except Exception:
            pass
    return week_start_day_of(restaurant)


def week_start_day_of(restaurant) -> int:
    """The weekday a restaurant's schedule week starts on (0 = Monday) —
    restaurants.week_start_day, its payroll week (_week_monday). Takes the
    restaurant or its id."""
    if isinstance(restaurant, int):
        return get_week_start_day(restaurant) % 7
    try:
        return int(getattr(restaurant, "week_start_day", 0) or 0) % 7
    except (TypeError, ValueError):
        return 0


def check_week_start(restaurant_id, raw):
    """(week_start or None, error or None) for a Generate request. An
    unreadable date used to fall back silently to next Monday, and a week
    that had already happened generated and could be published (SCHED-37).
    Any date in the wanted week is accepted, as _week_monday reads it."""
    from datetime import date as _date, timedelta as _t
    raw = (raw or "").strip()[:10]
    if not raw:
        return None, None
    try:
        d = _date.fromisoformat(raw)
    except ValueError:
        return None, "That week isn't a date we can read — pick the week again."
    try:
        from time_utils import restaurant_now_by_id
        today = restaurant_now_by_id(restaurant_id, naive=True).date()
    except Exception:
        today = _date.today()
    # A draft already saved under this week keeps the week it was built on
    # (week_start_day_for — blind re-audit 10/11/26 BUGSGEN-2): a redo of a
    # Monday-week draft after the move to Wednesday was read as the week
    # before and refused as already happened.
    monday = d - _t(days=(d.weekday() - week_start_day_for(restaurant_id, raw)) % 7)
    if monday + _t(days=6) < today:
        return None, "That week has already happened — pick this week or a later one."
    if monday > today + _t(days=7 * 12):
        return None, "That week is more than twelve weeks out — pick a nearer one."
    return raw, None


def get_week_start_day(restaurant_id: int) -> int:
    """Payroll workweek start, 0=Monday .. 6=Sunday.

    FLSA overtime is computed on the employer's own designated 7-day
    workweek. This module used to hardcode Monday for both the overtime
    bucket and the generated schedule's start, which silently mis-stated
    overtime for every restaurant that runs a Sunday- or Wednesday-start
    payroll week.
    """
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id)
        return int(getattr(r, "week_start_day", 0) or 0) if r else 0
    except Exception:
        return 0


# The schedule call's thinking (schedule audit 10/3/26 PR-6, PR-30). Opus
# 5.5 (the default, ai_utils.MODELS["schedule"]) thinks adaptively and
# cannot be turned off; it is run at medium effort. High (to 10/6/26) spent
# 62,353 of the 64,000 output tokens on Simple EJ's first week - nearly all
# thinking, eight minutes, a hair from a cut answer - while the repair, the
# manager plan, the solver and the quality gate do the rule-keeping after it
# (owner, 10/6/26: "yes switch to medium"). Watch the drafts' quality score
# and seconds (schedule_model_calls) before changing it back. A
# SCHEDULE_MODEL override to an older model keeps the old call shape.
# Since 10/7/26 the model and effort are the route's (AI orchestration,
# owner decision 3): the ai_workflows "labor_schedule" ladder's T3 (Sonnet
# 5.5) or T4 (Opus 5.5), both adaptive thinking at medium — both in
# SCHEDULE_THINKING_MODELS (a test holds the tier table to it).
# SCHEDULE_EFFORT is the effort of the pinned route: a SCHEDULE_MODEL env
# pin skips the ladder and runs that model at this effort
# (schedule_engine.schedule_route).
SCHEDULE_THINKING_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable", "claude-mythos")


SCHEDULE_EFFORT = "medium"


# 128K, streamed (scheduler audit 10/10/26 AI-17): both schedule models write
# up to 128,000 output tokens. At 64K, Opus 5.5 medium's measured ~48k of
# thinking left ~16k for rows and a summary, so a large roster's week was
# cut into three to five calls, each paying the thinking again.
SCHEDULE_MAX_TOKENS_THINKING = 128000


def schedule_model_thinks(model) -> bool:
    """Whether the schedule call runs `model` with adaptive thinking and an
    effort level (the 5.5 generation and later), or the old thinking-off
    shape (16k tokens, no effort)."""
    return str(model or "").lower().startswith(SCHEDULE_THINKING_MODELS)


# The paired metric the router decides on (scheduler re-audit 10/11/26
# AI-3, AI-4): an OUTCOME of the arm's own rows, read BEFORE the repair loop
# and the gate's fill touch them — the hard breaches the model wrote plus
# the requirement items its week left unmet. It was agreement with the week
# the owner published (F1), which is an edit of whatever production drafted
# (or of last week, carried forward): the arm that drafted it and the
# carry-forward arm won by anchoring, and a better-than-habit week scored
# worse. And it was read after the repair, which flattened Sonnet and Opus
# to the same week (78 v 79, 0 hard after repair, 10/7/26) — the eval could
# only say whether repair could fix a model, never whether a model was
# better. Agreement with the published week is kept as a SECONDARY metric,
# read only on weeks a third arm drafted (ANCHORED_METRICS, anchored).
ROUTE_METRIC = "pre_repair_defects"


# A verdict names a winner only from at least this many weeks from at least
# this many learning-eligible restaurants in its band or tag (AI-5) — the
# live experiments' own floor (schedule_experiments.MIN_WEEKS_PER_ARM,
# MIN_RESTAURANTS_PER_ARM). cluster_mean_ci gives an interval from two
# restaurants, so Simple EJ's and its demo copy (the same data) could have
# sent a whole roster band to Opus.
MIN_VERDICT_WEEKS = 20


MIN_VERDICT_RESTAURANTS = 5


def latest_verdict(metric, arm_a, arm_b, roster_size=None, tag=None, db_path=None):
    """The newest stored verdict of arm_a against arm_b on `metric` — for
    the band holding `roster_size`, or (with `tag`) for that kind of week —
    or None."""
    import models
    conn = models.get_conn(db_path) if db_path else models.get_conn()
    try:
        if tag:
            r = conn.execute(
                "SELECT * FROM schedule_eval_verdicts WHERE metric=? AND arm_a=? AND arm_b=? AND tag=? "
                "ORDER BY id DESC LIMIT 1", (metric, arm_a, arm_b, tag)).fetchone()
        else:
            n = int(roster_size or 0)
            r = conn.execute(
                "SELECT * FROM schedule_eval_verdicts WHERE metric=? AND arm_a=? AND arm_b=? AND band_lo<=? "
                "AND band_hi>=? AND tag IS NULL ORDER BY id DESC LIMIT 1", (metric, arm_a, arm_b, n, n)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def _decisive(v) -> bool:
    """A stored verdict the router may act on: T4 named the winner on
    ROUTE_METRIC, from at least the floors (re-checked on read, so a row
    stored before the floor existed is never evidence)."""
    return bool(v and v.get("winner") == "T4" and int(v.get("weeks") or 0) >= MIN_VERDICT_WEEKS
                and int(v.get("restaurants") or 0) >= MIN_VERDICT_RESTAURANTS)


def _evidence_text(v, what) -> str:
    return (f"the eval showed T4 better for {what} ({float(v['diff'] or 0):+.3f} {ROUTE_METRIC} T4 minus T3, "
            f"90% CI {float(v['ci_lo']):.3f} to {float(v['ci_hi']):.3f}, {v['weeks']} weeks from "
            f"{v['restaurants']} restaurants)")


def route_evidence(roster_size, db_path=None):
    """Why a week of this roster size starts on T4, or None (AI-3: route by
    measured evidence). A reason only when the newest stored verdict of T4
    against T3 for the roster's size band named T4 the winner at 90% on
    ROUTE_METRIC, from the floors (_decisive). Never raises: a store that
    cannot be read is no evidence, so the week starts where the ladder
    starts it."""
    try:
        v = latest_verdict(ROUTE_METRIC, "T4", "T3", roster_size, db_path=db_path)
    except Exception as e:
        print(f"[schedule_eval] route evidence not read: {e!r}")
        return None
    if not _decisive(v):
        return None
    return _evidence_text(v, f"rosters of {v['band_lo']}-{v['band_hi']}")


def tag_evidence(tag, db_path=None):
    """Why a week carrying `tag` (ROUTE_TAGS) starts on T4, or None — the
    same rule as route_evidence, for a kind of week (AI-27: holidays, first
    weeks, many rules and a week whose last draft escalated were sent to
    Opus on assertion). Never raises."""
    try:
        v = latest_verdict(ROUTE_METRIC, "T4", "T3", tag=tag, db_path=db_path)
    except Exception as e:
        print(f"[schedule_eval] tag evidence not read: {e!r}")
        return None
    if not _decisive(v):
        return None
    return _evidence_text(v, f"{str(tag).replace('_', ' ')} weeks")


# ── a checkpoint's payload, as the queue row keeps it ─────────────────────

def pack(payload) -> str:
    """`payload` as compressed JSON text (a queue row's checkpoint, a scorecard's kept rows)."""
    return base64.b64encode(zlib.compress(json.dumps(payload, default=str).encode("utf-8"))).decode("ascii")


def unpack(text):
    try:
        return json.loads(zlib.decompress(base64.b64decode(text)).decode("utf-8"))
    except Exception:
        return None
