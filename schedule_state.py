"""
schedule_state.py — the state one schedule generation carries from its draft
to its save, declared (blind re-audit 10/11/26 ARCH-5).

A pass's result was an untyped dict that thirty modules wrote and read by
string key (~130 of them): a misspelt key read None and a safeguard was
skipped without a word, and no stage said what it needed or produced.
GenerationResult is still a dict — the job serialises it at the save, the
queue's checkpoints and the Studio keep reading it with .get, and an old
checkpoint resumes as it was — but every key it may hold is declared below,
grouped by the stage that writes it, with its type:

    result.review            typed attribute access to a declared key
    result["review"]         the dict's own access, unchanged
    result["reviw"] = ...    an undeclared key: logged and counted
                             (`undeclared()`); under SCHEDULE_STATE_STRICT=1
                             (the test suite) it raises KeyError, so a
                             misspelling fails a test instead of a week.

Layer S0 of the schedule modules (ARCHITECTURE_MANIFEST.md §3a): stdlib only.
"""
import logging
import os
import threading

log = logging.getLogger(__name__)

# ── the declared keys, by the stage that writes them ──────────────────────
#
# draft    — the model call (labor.generate_optimized_schedule: the week's
#            figures and the answer), the parts and the code's draft
#            (schedule_engine._generate_in_parts, schedule_fallback)
# inputs   — what the input builder hands the passes (schedule_engine
#            _result_facts and the rules read after the model)
# repair   — the repair loop and the passes (schedule_engine.repair_week)
# review   — the sweep, the review and the score (schedule_job._stage_review,
#            _stage_score)
# finish   — the payload's figures and the bookkeeping before the save
STAGES = {
    "draft": {
        "schedule_csv": str, "rows": list, "dropped_rows": list, "summary": list, "narrative": list,
        "decisions": list, "truncated": bool, "stop_reason": str, "structured": bool, "complete_dates": list,
        "partial_dates": list, "salvaged": bool, "json_recovered": bool, "contract": str, "shape_rows": list,
        "model_call": dict, "generation_id": str, "generation_seconds": float, "generated_dates": list,
        "pinned_rows": list, "pinned_dropped": list, "prepared": bool, "request": dict, "prompt": str,
        "week_dates": list, "week_days": list, "projected_revenue": float, "hours_budget": float,
        "labor_budget_dollars": float, "labor_target": float, "daily_target_hours": dict, "revenue_basis": str,
        "budget_basis": dict, "daily_target_basis": dict, "daily_target_reasons": dict, "requirements": list,
        "requirements_by_date": dict, "roster": list, "operational_scores": dict, "strength_thresholds": dict,
        "leader_rules": list, "shift_profiles": list, "typical_headcount": dict, "late_headcount": dict,
        "published_headcount": dict, "headcount_trends": list, "headcount_provenance": dict,
        "cross_trained": dict, "concurrent": dict, "concurrent_family": dict, "concurrent_trends": list,
        "crew_answers": dict, "learned_headcount": dict, "chunked": int, "departments": list, "slices": list,
        "unwritten_dates": list, "generation_notes": list, "draft_source": dict, "objective_need": dict,
        "closed_dates": list, "open_unwritten": list, "empty_accepted": list, "planned_calls": list,
        "draft_fallback": dict, "call_kind": str, "tier": str, "readiness": dict,
        # A hand-built result's own flag (the test fixtures carry it); no stage reads it.
        "ok": bool,
        # The gate's fill in code: the answer it re-ran (schedule_engine._gate_fill_answer).
        "gate_fill": bool,
        # Read by the pass pricing (schedule_job._price_week, RepairContext.pricing) but
        # written by no stage: the pricing then uses its own blended rate (a hand-built
        # result in the tests may carry one).
        "blended_rate": float,
        # Read as a fallback, written by no stage (ARCH-5 found them): the payload's gate
        # starts as not run and the gate writes it there (schedule_job._stage_payload); the
        # optimizer's section cap is the pass's own (RepairContext.section_count).
        "gate": dict, "section_count": int,
        # Read into the eval's replay context (schedule_metrics._RESULT_KEYS) when present;
        # no stage writes them on the result (they live in the analysis, the call's
        # arguments and the signals), so the replay context goes without them.
        "role_rates": dict, "last_week_tail": list, "hours_budget_dollars": float, "open_times": dict,
    },
    "inputs": {
        "experiment_arms": list, "instruction_rules": list, "manager_plan": dict, "learned_preferences": dict,
        "unstaffable_dates": list, "rotation_plan": dict, "splh_objective": dict, "date_demand": dict,
        "labor_standards": dict, "starting_headcount": dict, "holiday_lift": dict, "splh_by_daypart": dict,
        "outcomes_by_daypart": dict, "fairness_ledger": dict, "could_hold": dict, "projected_revenue_source": str,
        "forecast_basis": dict, "learned_worse": list, "role_scores": dict, "labor_target_label": str,
        "labor_target_source": str, "demand_data_through": dict, "restaurant_name": str, "demand_by_day": dict,
        "demand_by_date": dict, "role_minimums": dict, "constraints": object, "roster_roles": dict,
        "dormant": dict, "prior_pattern": dict, "pairs": dict, "reliability": dict, "learned_patterns": list,
        "pattern_conflicts": list, "soft_requirements": list, "_soft_typical": dict,
        "prior_published_rows": list, "weather_forecast": list, "pending_time_off": dict,
        "leader_rules_status": dict, "reservation_feed": dict, "staff_constraints": list, "partial_week": dict,
        "started_week": dict, "capability_version": object, "time_off_since_inputs": object,
        "hourly_profile_ready": bool, "resumed_inputs": dict, "redo": dict, "dates": list,
        "regenerated_dates": list, "week_start": str, "_rate_book": object, "schema_enums": bool,
        "elsewhere": dict,
    },
    "repair": {
        "repair": dict, "optimizer": dict, "solver": dict, "overtime_rebalance": dict, "staggered": list,
        "trimmed": list, "hours_trimmed": float, "role_times": dict, "role_windows": dict, "stations": dict,
        "min_hours": dict, "manager_coverage": dict, "end_overruns": list, "standby_days": list,
        "overtime_forecast": dict, "likely_edits": list, "cap_floor_conflicts": list,
        "section_cap_conflicts": list, "floor_suggestions": list, "budget_conflict": dict,
        "rows_needing_review": list, "flagged_rows": list, "objective": dict, "assignment_reasons": dict,
    },
    "review": {
        "review": dict, "quality": dict, "rule_violations": list, "what_if": dict, "strength": dict,
        "diff_vs_published": dict, "not_scheduled": list, "regulars_not_scheduled": list,
        "ratings_off_roster": list, "shift_requirements": list, "demand_accuracy": dict,
        "week_projection_accuracy": dict, "starting_point": dict, "stage_failures": list,
    },
    "finish": {
        "projected_cost": dict, "hours_hourly": float, "hours_salaried": float, "over_budget_dollars": float,
        "splh_report": dict, "prior_week_assignments": dict, "stage_seconds": dict,
    },
}
FIELDS = {k: t for keys in STAGES.values() for k, t in keys.items()}
STAGE_OF = {k: s for s, keys in STAGES.items() for k in keys}

_SEEN = set()
_SEEN_LOCK = threading.Lock()


def _strict() -> bool:
    return os.getenv("SCHEDULE_STATE_STRICT") == "1"


def _undeclared(key, how) -> None:
    if _strict():
        raise KeyError(f"GenerationResult has no declared key {key!r} ({how}) — declare it in schedule_state.STAGES")
    with _SEEN_LOCK:
        if key in _SEEN:
            return
        _SEEN.add(key)
    log.warning("schedule_state: undeclared key %r %s on a generation result", key, how)


def undeclared() -> set:
    """The undeclared keys this process has seen written to a GenerationResult."""
    with _SEEN_LOCK:
        return set(_SEEN)


class GenerationResult(dict):
    """A generation pass's state: a dict whose keys are declared in FIELDS,
    each readable as an attribute (None when not set)."""

    __slots__ = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for k in self:
            if k not in FIELDS:
                _undeclared(k, "carried in")

    @classmethod
    def of(cls, value):
        """`value` as a GenerationResult (itself when it already is one)."""
        if isinstance(value, cls):
            return value
        return cls(value or {})

    def __setitem__(self, key, value):
        if key not in FIELDS:
            _undeclared(key, "written")
        super().__setitem__(key, value)

    def get(self, key, default=None):
        # A read of a key nobody declares is a misspelling, or a key no stage
        # writes: it would read None and skip whatever it guards without a word.
        if key not in FIELDS:
            _undeclared(key, "read")
        return super().get(key, default)

    def __getitem__(self, key):
        if key not in FIELDS:
            _undeclared(key, "read")
        return super().__getitem__(key)

    def setdefault(self, key, default=None):
        if key not in self and key not in FIELDS:
            _undeclared(key, "written")
        return super().setdefault(key, default)

    def update(self, *args, **kwargs):
        for k in dict(*args, **kwargs):
            if k not in FIELDS:
                _undeclared(k, "written")
        super().update(*args, **kwargs)

    def copy(self):
        return GenerationResult(self)

    def __getattr__(self, name):
        if name in FIELDS:
            return self.get(name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in FIELDS:
            self[name] = value
            return
        raise AttributeError(f"GenerationResult has no declared field {name!r}")

    def __reduce__(self):
        return (GenerationResult, (dict(self),))
