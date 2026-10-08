"""ai_workflows — every AI workflow's policy, in one registry (AI orchestration
design, owner-approved 10/7/26).

A workflow is one kind of model output the platform produces: a labor read,
a review reply, the nightly DSR narrative, a week's schedule. Its POLICY says
how the orchestrator (ai_orchestrator) runs it:

  * which agent (prompt family) writes it,
  * the tier LADDER it climbs — the cheapest tier able to do the job first,
    a stronger one only on evidence (ESCALATION),
  * what counts as evidence to escalate on, and how many times,
  * who reviews it before anyone reads it (the rules engine, the owner, or a
    Haiku rubric where text reaches guests or staff with no human between),
  * the context sections it reads (restaurant_context) and their budget,
  * per-run caps: model calls, dollars, seconds,
  * whether it may go through Message Batches (unattended, half price).

Defaults live here, in code, reviewed and tested. The admin console can
override any field per workflow (ai_route_overrides, audited); an override is
how a route the learner recommends gets applied — never automatically, except
a revert to the last good route (ai_orchestrator.learn).

Tiers are models, set once: moving every T1 workflow to a better Haiku is one
env var, and the learner measures the change.

    T0  deterministic, no model (forecasts, alerts, the Morning Brief)
    T1  Haiku 4.5
    T2  Sonnet 5
    T3  Sonnet 5.5, adaptive thinking at medium effort
    T4  Opus 5.5, adaptive thinking at medium effort

"default" on a ladder is the call site's own model (ai_utils.model_for of the
policy's `purpose`) with the call site's own thinking and effort — the route
every workflow ran on before this registry, so a policy that starts on
"default" changes nothing until its ladder is edited.

Escalation evidence is DETERMINISTIC ONLY (TRIGGERS): a schema that does not
parse, a validation refusal, a hard rule still broken after the code's own
repair, a reviewer's flag. Missing data never escalates — no model can supply
data that does not exist; the readiness gate holds the call and says what is
missing. A model's opinion of its own confidence is never read.

L2, imports ai_utils only (and that lazily): call sites, the orchestrator and
the admin console read it; it reads nothing above it.
"""
import os
from dataclasses import dataclass, field, replace

# Bumped when any default below changes: every ai_runs row records the policy
# version it ran under, so the learner never compares runs across a change it
# cannot see.
POLICY_VERSION = "2026-10-07.4"

TIERS = ("T0", "T1", "T2", "T3", "T4")
DEFAULT = "default"


def _tier_table():
    import ai_utils as _ai
    return {
        "T1": {"model": os.getenv("AI_TIER_T1_MODEL", _ai.HAIKU), "effort": None},
        "T2": {"model": os.getenv("AI_TIER_T2_MODEL", _ai.SONNET), "effort": None},
        "T3": {"model": os.getenv("AI_TIER_T3_MODEL", _ai.SONNET_55),
               "effort": os.getenv("AI_TIER_T3_EFFORT", "medium")},
        "T4": {"model": os.getenv("AI_TIER_T4_MODEL", _ai.OPUS_55),
               "effort": os.getenv("AI_TIER_T4_EFFORT", "medium")},
    }


# What may move a run up its ladder. Each is decided by code from the output,
# never by asking a model how sure it is.
TRIGGERS = {
    "schema_fail": "the answer did not parse against its schema",
    "validation_refuse": "the validation engine refused the answer",
    "validation_withhold": "the validation engine withheld the answer",
    "rule_breach": "a hard business rule was still broken after the code's repair",
    "reviewer_flag": "the reviewer flagged the answer",
    "uncited": "the answer cited no source line it was given",
}
# The trigger "uncited" was named "not_found" before 10/7/26 (re-audit #3),
# when it also covered a reading that found nothing; a console override
# stored under the old name meant the citation check and is read as it.
LEGACY_TRIGGERS = {"not_found": "uncited"}
# Never a trigger, by design (they are listed so a policy naming one fails
# its test instead of quietly escalating): missing data holds, a cut answer
# is split, thin evidence is caveated, and sources that do not cover the
# question ("not_covered" — a staff answer's found:false) are missing data
# too: a stronger model reading the same lines cannot add the rule that is
# not there (AI cost audit 10/7/26 re-audit #3).
NEVER_TRIGGERS = ("data_missing", "truncated", "low_evidence", "model_confidence", "not_covered")

REVIEWERS = ("none", "rules", "owner", "haiku_gate", "haiku_shadow")


@dataclass(frozen=True)
class Caps:
    calls: int = 4           # model calls in one run, escalations and reviewer included
    usd: float = 0.50        # dollars one run may spend (ledger cost, list prices)
    seconds: float = 120.0   # wall clock for an interactive run; a job's own deadline wins


@dataclass(frozen=True)
class Policy:
    workflow: str            # the ledger action (ai_usage.action) this workflow writes
    agent: str               # the prompt family — see AGENTS
    purpose: str             # ai_utils.MODELS key: the call site's own model ("default")
    ladder: tuple = (DEFAULT,)
    escalate_on: tuple = ()
    max_escalations: int = 0
    reviewer: str = "rules"
    # The reviewer when nobody approves before the text goes out (an
    # auto-approved review reply): unset means the same as `reviewer`.
    reviewer_unattended: str = ""
    shadow_rate: float = 0.0     # share of runs a haiku_shadow reviewer scores
    context: tuple = ()          # restaurant_context sections it reads
    context_tokens: int = 0
    caps: Caps = field(default_factory=Caps)
    batch: bool = False          # may run through Message Batches (unattended)
    delivery: str = "interactive"   # interactive | background | batch
    note: str = ""


AGENTS = {
    "scheduling": "the week's schedule: the model decides its shape; requirements, the manager plan, "
                  "repair, the solver and the quality gate are code",
    "cfo": "Ask Cavnar AI and the weekly plan — the one tool-using agent; executive depth is the CFO",
    "analyst": "the reads and diagnoses, the DSR narrative, the digest — facts in, cited prose out",
    "reviews": "review analysis and reply drafts",
    "marketing": "posts, the calendar, guest texts and email",
    "intel": "the competitor read and menu extraction (AI visibility is a search vendor, not a model)",
    "documents": "invoices, recipe photos and recipe drafts",
    "staff": "the pre-shift brief, translations, house-rules answers, task-sheet starters",
    "reviewer": "the Haiku rubric that checks tone, brand and restaurant sense where no human does",
}

# Deliberately not agents (design, 10/7/26): forecasts are statistics and a
# model must never write a forecast figure; notifications and the Morning
# Brief are deterministic and correct; interface copy and prompt changes are
# dev-time work through the eval harness, never self-modified in production.
NOT_AGENTS = ("forecasting", "notifications", "morning_brief", "ui_writing", "prompt_optimizer")


def _p(workflow, agent, purpose, **kw):
    return Policy(workflow=workflow, agent=agent, purpose=purpose, **kw)


_ANALYST_CTX = ("profile", "owner_rules", "kpis", "data_state")
_ESC_COPY = ("schema_fail", "validation_refuse")

POLICIES = {p.workflow: p for p in (
    # ── scheduling ──────────────────────────────────────────────────────
    # Sonnet 5.5 first (owner decision 3, 10/7/26: "Sonnet 5.5 first if the
    # eval clears 80%, hard weeks straight to Opus"). On Simple EJ's one
    # stored week (scripts/schedule_model_eval.py, prod 10/7/26) Sonnet 5.5
    # medium matched Opus 5.5 medium, the production setting: quality 78 v
    # 79, hard rules 3 v 4 before the repair and 0 after it, at a third of
    # the cost ($0.41 v $1.23) and 278s v 456s. The quality gate is the
    # rules engine: its fill in code runs first, and only a gate that still
    # trips after it (rule_breach) sends the gate's days to T4 — that
    # rewrite IS the escalation, with the gate's reasons as its notes.
    # schedule_engine.hard_week_reasons pre-routes a hard week to T4; a
    # SCHEDULE_MODEL env pin skips the ladder (schedule_engine.
    # schedule_route). Caps: a week's planned calls, the generation's own
    # MAX_EXTRA_CALLS (8) retries and splits, and the gate's rewrite with
    # its own; ~$6; the job's longest deadline (SCHEDULE_JOB_MAX_SECONDS).
    _p("labor_schedule", "scheduling", "schedule", ladder=("T3", "T4"), reviewer="rules",
       escalate_on=("rule_breach",), max_escalations=1,
       caps=Caps(calls=16, usd=6.0, seconds=2400), delivery="background",
       context=("profile", "owner_rules", "roster", "sales_trend", "labor_trend", "events", "weather",
                "memory", "data_state"),
       note="hard weeks (schedule_engine.hard_week_reasons) start on T4; a SCHEDULE_MODEL pin skips the ladder"),
    # ── cfo ─────────────────────────────────────────────────────────────
    _p("ask_cavnar", "cfo", "ask_cavnar", ladder=("T2",), reviewer="rules",
       caps=Caps(calls=8, usd=0.60, seconds=90),
       context=("profile", "owner_rules", "kpis", "sales_trend", "labor_trend", "findings", "events",
                "weather", "memory", "alerts", "data_state"),
       note="no interactive escalation: a second full turn doubles the wait; the figure check gates it"),
    _p("weekly_plan", "cfo", "ask_cavnar", ladder=("T2",), reviewer="rules",
       caps=Caps(calls=8, usd=0.80, seconds=600), delivery="background"),
    _p("ask_summary", "cfo", "ask_summary", ladder=("T1",), reviewer="rules",
       caps=Caps(calls=1, usd=0.02, seconds=30), delivery="background"),
    # ── analyst ─────────────────────────────────────────────────────────
    _p("labor_insight", "analyst", "labor_insight", ladder=("T1", "T2"),
       escalate_on=_ESC_COPY + ("validation_withhold",), max_escalations=1,
       caps=Caps(calls=2, usd=0.05, seconds=45),
       context=_ANALYST_CTX + ("labor_trend", "memory")),
    _p("inventory_insight", "analyst", "inventory_insight", ladder=("T2",),
       escalate_on=(), caps=Caps(calls=1, usd=0.05, seconds=45),
       context=_ANALYST_CTX + ("memory",)),
    _p("review_insight", "analyst", "review_insight", ladder=("T2",),
       caps=Caps(calls=1, usd=0.05, seconds=45), context=_ANALYST_CTX + ("memory",)),
    _p("marketing_insight", "analyst", "marketing_insight", ladder=("T1", "T2"),
       escalate_on=_ESC_COPY + ("validation_withhold",), max_escalations=1,
       caps=Caps(calls=2, usd=0.03, seconds=45), context=_ANALYST_CTX + ("memory",)),
    _p("review_diagnosis", "analyst", "review_diagnosis", ladder=("T2",), batch=True,
       caps=Caps(calls=1, usd=0.05, seconds=120), delivery="background"),
    _p("food_cost_diagnosis", "analyst", "food_cost_diagnosis", ladder=("T2",), batch=True,
       caps=Caps(calls=1, usd=0.05, seconds=120), delivery="background"),
    _p("dsr_narrative", "analyst", "dsr_narrative", ladder=("T2", "T3"), batch=True,
       escalate_on=("schema_fail",), max_escalations=1, reviewer="rules", shadow_rate=0.2,
       caps=Caps(calls=3, usd=0.25, seconds=300), delivery="background",
       context=_ANALYST_CTX + ("sales_trend", "labor_trend", "alerts", "memory"),
       note="a Haiku rubric scores ~20% in shadow (do the priorities follow from the night)"),
    _p("weekly_digest", "analyst", "reporter", ladder=("T2",), batch=True,
       caps=Caps(calls=1, usd=0.05, seconds=120), delivery="background"),
    # ── reviews ─────────────────────────────────────────────────────────
    _p("review_analysis", "reviews", "review_analysis", ladder=("T1",),
       caps=Caps(calls=1, usd=0.01, seconds=60), delivery="background"),
    _p("draft_response", "reviews", "drafter", ladder=("T1", "T2"),
       escalate_on=_ESC_COPY, max_escalations=1, reviewer="owner", reviewer_unattended="haiku_gate",
       shadow_rate=0.1, caps=Caps(calls=3, usd=0.05, seconds=60), delivery="background",
       context=("profile", "owner_rules"),
       note="4-5 stars with no complaint start on T1; 3 stars or less, or a flagged review, on T2"),
    # ── marketing ───────────────────────────────────────────────────────
    _p("marketing_content", "marketing", "marketing", ladder=("T2", "T3"),
       escalate_on=_ESC_COPY, max_escalations=1, reviewer="owner", shadow_rate=0.1, batch=True,
       caps=Caps(calls=3, usd=0.10, seconds=60), context=("profile", "owner_rules", "events", "memory"),
       note="the scheduled quiet-night post batches (ai_batches 'quiet_night_post', AI cost audit 10/7/26 "
            "#62); a post the owner asks for never does"),
    _p("content_calendar", "marketing", "marketing", ladder=("T2", "T3"),
       escalate_on=_ESC_COPY, max_escalations=1, reviewer="owner",
       caps=Caps(calls=3, usd=0.15, seconds=90), context=("profile", "owner_rules", "events", "memory")),
    _p("guest_campaign_draft", "marketing", "guest_marketing", ladder=("T2", "T3"),
       escalate_on=_ESC_COPY + ("reviewer_flag",), max_escalations=1, reviewer="haiku_gate",
       caps=Caps(calls=4, usd=0.10, seconds=60), context=("profile", "owner_rules", "memory"),
       note="reaches many guests and cannot be recalled (owner, 10/7/26: gate it)"),
    _p("guest_newsletter_draft", "marketing", "guest_marketing", ladder=("T2", "T3"),
       escalate_on=_ESC_COPY + ("reviewer_flag",), max_escalations=1, reviewer="haiku_gate",
       caps=Caps(calls=4, usd=0.15, seconds=90), context=("profile", "owner_rules", "memory")),
    _p("email_personalization", "marketing", "email_personalise", ladder=("T1",),
       caps=Caps(calls=1, usd=0.01, seconds=30), delivery="background"),
    # ── intel ───────────────────────────────────────────────────────────
    _p("competitor_insight", "intel", "competitor_insight", ladder=("T2",), batch=True,
       caps=Caps(calls=1, usd=0.06, seconds=120), delivery="background"),
    _p("menu_extract_url", "intel", "competitor_extract", ladder=("T1",),
       caps=Caps(calls=1, usd=0.02, seconds=60), delivery="background"),
    _p("menu_extract_pdf", "intel", "competitor_extract", ladder=("T1",),
       caps=Caps(calls=1, usd=0.02, seconds=60), delivery="background"),
    # ── documents ───────────────────────────────────────────────────────
    # The call site's own Opus until an eval on stored invoices clears a
    # cheaper tier; arithmetic (lines sum to the total) is the reviewer.
    _p("invoice_extract", "documents", "invoices", ladder=(DEFAULT,), reviewer="rules",
       escalate_on=("rule_breach",), caps=Caps(calls=2, usd=0.60, seconds=180)),
    _p("recipe_photo", "documents", "recipes", ladder=("T2",), reviewer="owner",
       caps=Caps(calls=1, usd=0.08, seconds=120)),
    _p("recipe_draft", "documents", "recipes", ladder=("T2",), reviewer="owner", batch=True,
       caps=Caps(calls=1, usd=0.05, seconds=120), delivery="background"),
    # ── staff ───────────────────────────────────────────────────────────
    _p("task_sheet_starter", "staff", "task_sheets", ladder=("T1", "T2"),
       escalate_on=_ESC_COPY, max_escalations=1, reviewer="owner",
       caps=Caps(calls=2, usd=0.05, seconds=60)),
    _p("staff_brief", "staff", "staff_brief", ladder=("T1",), reviewer="owner",
       caps=Caps(calls=1, usd=0.01, seconds=60), delivery="background"),
    _p("staff_translation", "staff", "staff_translation", ladder=("T1",), reviewer="rules",
       caps=Caps(calls=1, usd=0.01, seconds=60)),
    # T2 reads again only on evidence the first reading was wrong: an answer
    # that cited no line it was given, unparseable JSON, the staff check's
    # refusal, the reviewer's flag. "The lines don't cover it" (found:false)
    # is final — the same lines read by a stronger model still do not hold
    # the rule (re-audit #3: every uncovered question cost Haiku + Sonnet).
    _p("staff_answer", "staff", "staff_answer", ladder=("T1", "T2"),
       escalate_on=_ESC_COPY + ("uncited", "reviewer_flag"), max_escalations=1, reviewer="haiku_gate",
       caps=Caps(calls=4, usd=0.06, seconds=45), context=("profile", "owner_rules"),
       note="read by staff with no manager between; 'ask your manager' is the safe fallback; "
            "not covered by the lines is final, never escalated"),
    # ── reviewer ────────────────────────────────────────────────────────
    # The Haiku rubric itself (ai_reviewer). It never escalates and is never
    # reviewed: a reviewer that cannot run passes (the rules engine already did).
    _p("ai_review", "reviewer", "", ladder=("T1",), reviewer="none",
       caps=Caps(calls=1, usd=0.01, seconds=30)),
    # ── admin ───────────────────────────────────────────────────────────
    _p("audit_notes_read", "analyst", "sales_audit_notes", ladder=(DEFAULT,), reviewer="rules",
       caps=Caps(calls=1, usd=0.10, seconds=120)),
)}


# ── overrides ──────────────────────────────────────────────────────────────
#
# The console's edits (ai_orchestrator owns the table). Only these fields may
# be overridden; anything else in a stored override is ignored, so a typo in
# the console can never change a policy's identity.
OVERRIDABLE = ("ladder", "escalate_on", "max_escalations", "reviewer", "reviewer_unattended",
               "shadow_rate", "batch", "caps")


def _coerce(name, value, base):
    if name in ("ladder", "escalate_on"):
        vals = tuple(str(v) for v in (value or ()))
        if name == "escalate_on":
            vals = tuple(dict.fromkeys(LEGACY_TRIGGERS.get(v, v) for v in vals))
        if name == "ladder":
            if not vals or any(v not in TIERS and v != DEFAULT for v in vals):
                raise ValueError(f"ladder must name tiers {TIERS} or '{DEFAULT}'")
        elif any(v not in TRIGGERS for v in vals):
            raise ValueError(f"escalation triggers must be among {tuple(TRIGGERS)}")
        return vals
    if name == "max_escalations":
        n = int(value)
        if not 0 <= n <= 2:
            raise ValueError("max_escalations is 0, 1 or 2")
        return n
    if name in ("reviewer", "reviewer_unattended"):
        v = str(value or "")
        if v and v not in REVIEWERS:
            raise ValueError(f"reviewer must be one of {REVIEWERS}")
        return v if v or name == "reviewer_unattended" else base.reviewer
    if name == "shadow_rate":
        r = float(value)
        if not 0.0 <= r <= 1.0:
            raise ValueError("shadow_rate is between 0 and 1")
        return r
    if name == "batch":
        return bool(value)
    if name == "caps":
        d = dict(value or {})
        caps = Caps(calls=int(d.get("calls", base.caps.calls)), usd=float(d.get("usd", base.caps.usd)),
                    seconds=float(d.get("seconds", base.caps.seconds)))
        if caps.calls < 1 or caps.calls > 20 or caps.usd <= 0 or caps.usd > 20 or caps.seconds <= 0:
            raise ValueError("caps: 1-20 calls, $0-20, seconds > 0")
        return caps
    raise ValueError(f"{name} cannot be overridden")


def apply_override(base: Policy, override: dict) -> Policy:
    """`base` with the console's override applied; raises ValueError on a
    field that is not OVERRIDABLE or a value out of range."""
    changes = {}
    for name, value in (override or {}).items():
        if name not in OVERRIDABLE:
            raise ValueError(f"{name} cannot be overridden")
        changes[name] = _coerce(name, value, base)
    pol = replace(base, **changes)
    if pol.max_escalations > max(0, len(pol.ladder) - 1) and pol.ladder != (DEFAULT,):
        pol = replace(pol, max_escalations=max(0, len(pol.ladder) - 1))
    return pol


def policy(workflow: str, db_path=None) -> Policy:
    """The policy in force for `workflow`: its default with the console's
    override applied (cached, ai_orchestrator.overrides). An unknown workflow
    gets a pass-through policy on its call site's own model, so a new call
    site is never broken by the registry — and tests/test_ai_workflows.py
    fails until it is registered."""
    base = POLICIES.get(workflow)
    if base is None:
        return Policy(workflow=workflow, agent="unregistered", purpose="", ladder=(DEFAULT,))
    try:
        import ai_orchestrator
        ov = ai_orchestrator.overrides(db_path).get(workflow)
    except Exception:
        ov = None
    if not ov:
        return base
    try:
        return apply_override(base, ov)
    except ValueError:
        return base


# The least max_tokens a call on a thinking tier (T3, T4) is given: room for
# medium-effort thinking before a short answer (Route.apply).
THINKING_MIN_MAX_TOKENS = 8000


@dataclass(frozen=True)
class Route:
    tier: str
    model: str
    effort: str | None = None

    def apply(self, kwargs: dict) -> dict:
        """The call's kwargs on this route: the model, and for a thinking
        tier its effort (adaptive thinking). The default route leaves the
        call site's own model, thinking and effort exactly as written."""
        out = dict(kwargs)
        if self.tier == DEFAULT:
            return out
        out["model"] = self.model
        if self.effort:
            out.pop("thinking", None)
            out["thinking"] = {"type": "adaptive"}
            out["output_config"] = dict(out.get("output_config") or {}, effort=self.effort)
            # Thinking shares max_tokens with the answer. A call site sized
            # for a plain model (a 150-token guest text, a 600-token reply)
            # would spend it all thinking on an escalated rung and come back
            # truncated — the one rung meant to rescue it. Only what is used
            # is billed (AI cost audit 10/7/26, orchestration Phase 3).
            if out.get("max_tokens"):
                out["max_tokens"] = max(int(out["max_tokens"]), THINKING_MIN_MAX_TOKENS)
        else:
            # A plain tier: the gateway's own default (thinking off where the
            # model allows it) — never a thinking setting written for another model.
            if (out.get("thinking") or {}).get("type") == "adaptive":
                out.pop("thinking", None)
            oc = dict(out.get("output_config") or {})
            oc.pop("effort", None)
            if oc:
                out["output_config"] = oc
            else:
                out.pop("output_config", None)
        return out


def route_for(pol: Policy, step: int = 0, start: str | None = None) -> Route:
    """The route of rung `step` on the policy's ladder, starting from
    `start` (a tier the workflow's own pre-router chose) when given."""
    ladder = list(pol.ladder)
    if start and start in ladder:
        ladder = ladder[ladder.index(start):]
    tier = ladder[min(step, len(ladder) - 1)]
    if tier == DEFAULT:
        import ai_utils as _ai
        return Route(tier=DEFAULT, model=_ai.model_for(pol.purpose) if pol.purpose else "")
    t = _tier_table()[tier]
    return Route(tier=tier, model=t["model"], effort=t["effort"])


def rungs(pol: Policy, start: str | None = None) -> int:
    ladder = list(pol.ladder)
    if start and start in ladder:
        ladder = ladder[ladder.index(start):]
    return len(ladder)
