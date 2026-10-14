"""
schedule_metrics.py — what each schedule generation cost and what the
model's rows were worth (scheduler audit 10/10/26: AI-2's headline metric,
the measurement half of AI-23, the stage attribution the evaluation plan
asks for, and the post-model context the golden eval set replays — RM-1).

One row a finished generation (a week, an owner's redo, the gate's fill or
rewrite), in schedule_generation_metrics:

  * cost_usd and calls — every schedule_model_calls row of the generation,
    priced at ai_utils' own rates (cache writes and reads included).
  * model_rows / model_kept / survival — how many rows the model wrote
    (not the manager plan, not a kept day, not a day code drafted), and the
    share of them still in the saved week unchanged (person, date, role,
    start, end). Without it nobody can say what the model adds.
  * by_stage — which repair stage changed, trimmed, staggered or added how
    many rows (the repair loop's own records).
  * role_violations — model rows in a role the person does not hold
    (`role_not_held`, AI-23: measured here before any schema change).
  * seconds — the generation's model seconds and its whole wall clock.
  * context_z — the post-model context the job scored the rows in (the
    week's figures, the Constraints, the quality signals, the requirements,
    the curve), encoded with schedule_output.encode, so the eval
    (schedule_eval.freeze → score_against_published) replays the same
    production post-model path on the model's own rows. Scrubbed as
    record_call scrubs a request (ai_utils.redact_pii); staff names and
    figures kept, as a replay needs them.

A week is ACCEPTED when the owner published it (a schedule_history row of
the same restaurant and week with published_at). summary() reads cost per
accepted week — every generation's cost in the window over the weeks that
were published — and p50/p95 seconds: AI-2's headline, so a saving that
makes owners redo more is not counted as a saving.

The table is created at boot (init_schedule_metrics, from models.init_db)
and kept RETAIN_SCHEDULE_METRICS_DAYS (365) by ops' retention sweep; the
eval copies what it needs into its own retention-exempt store.
"""
import json
import zlib
import logging

log = logging.getLogger(__name__)

from schedule_context import degraded as _degraded  # noqa: E402  (the one degradation event)


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


_DDL = (
    """CREATE TABLE IF NOT EXISTS schedule_generation_metrics (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        restaurant_id   INTEGER,
        history_id      INTEGER,
        generation_id   TEXT,
        week_start      TEXT,
        kind            TEXT,
        draft_source    TEXT,
        model           TEXT,
        contract        TEXT,
        roster_size     INTEGER,
        calls           INTEGER,
        cost_usd        REAL,
        model_seconds   REAL,
        seconds         REAL,
        model_rows      INTEGER,
        model_kept      INTEGER,
        survival        REAL,
        role_violations INTEGER,
        final_rows      INTEGER,
        by_stage_json   TEXT,
        context_z       BLOB
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sched_gen_metrics_r ON schedule_generation_metrics(restaurant_id, week_start)",
    "CREATE INDEX IF NOT EXISTS idx_sched_gen_metrics_created ON schedule_generation_metrics(created_at)",
)

KINDS = ("week", "redo", "gate_fill", "gate_rewrite")
_SIG = ("employee", "date", "role", "shift_start", "shift_end")


def init_schedule_metrics(db_path=None):
    """At boot (models.init_db), never on a call."""
    conn = get_conn(db_path)
    try:
        for stmt in _DDL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


# ── what the model's rows were worth ───────────────────────────────────────

def _sig(r) -> tuple:
    return tuple(" ".join(str(r.get(k) or "").split()).lower() for k in _SIG)


def model_rows_of(rows, kept_dates=(), code_dates=(), source="model") -> list:
    """The rows the model wrote, out of the rows a generation handed the
    repair loop: not the manager plan (schedule_skeleton.PLAN_NOTE), not a
    kept day of a redo, not a day code drafted (draft_source code_dates),
    and none at all when the draft was code's."""
    if source and source != "model":
        return []
    try:
        from schedule_skeleton import PLAN_NOTE
    except Exception:
        PLAN_NOTE = "Cavnar AI: manager plan"
    skip = set(kept_dates or ()) | set(code_dates or ())
    return [dict(r) for r in rows or []
            if r.get("date") not in skip and not str(r.get("notes") or "").startswith(PLAN_NOTE)]


def survival(model_rows, final_rows) -> tuple:
    """(kept, total): how many of the model's rows are in the final week
    unchanged — the same person, date, role, start and end — counted as a
    multiset, so two identical rows survive only if two are still there."""
    left = {}
    for r in final_rows or []:
        k = _sig(r)
        left[k] = left.get(k, 0) + 1
    kept = 0
    for r in model_rows or []:
        k = _sig(r)
        if left.get(k):
            left[k] -= 1
            kept += 1
    return kept, len(model_rows or [])


def by_stage(loop) -> dict:
    """{stage: {"changed", "trimmed", "staggered", "added"}} from the
    repair loop's own result (repair_week): its kept records' fix lines,
    trims and staggers, and the rows each stage added still in the week."""
    out = {}
    for rec in (loop or {}).get("records") or []:
        st = rec.get("stage") or "?"
        e = out.setdefault(st, {"changed": 0, "trimmed": 0, "staggered": 0, "added": 0})
        e["changed"] += len(rec.get("fixes") or [])
        e["trimmed"] += len(rec.get("trimmed") or [])
        e["staggered"] += len(rec.get("staggered") or [])
    for st, dates in ((loop or {}).get("rows_added") or {}).items():
        e = out.setdefault(st, {"changed": 0, "trimmed": 0, "staggered": 0, "added": 0})
        e["added"] += int(sum((dates or {}).values()))
    return out


def role_violations(model_rows, c) -> int:
    """How many of the model's rows put a person in a role they do not hold
    (schedule_rules' role_not_held — AI-23's measurement)."""
    if c is None or not model_rows:
        return 0
    try:
        import schedule_rules as _sr
        return sum(1 for v in _sr.violations(model_rows, c) if v.get("kind") == "role_not_held")
    except Exception as e:
        _degraded(f"role check skipped: {e!r}", e)
        return 0


def generation_cost(generation_id, db_path=None) -> dict:
    """{"calls", "cost_usd", "model_seconds", "model", "contract"} over every
    stored call of `generation_id`, priced at ai_utils' rates."""
    out = {"calls": 0, "cost_usd": 0.0, "model_seconds": 0.0, "model": None, "contract": None}
    if not generation_id:
        return out
    from ai_utils import _estimate_cost
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT model, contract, seconds, usage_json FROM schedule_model_calls "
                            "WHERE generation_id=? ORDER BY id", (generation_id,)).fetchall()
    finally:
        conn.close()
    for r in rows:
        try:
            u = json.loads(r["usage_json"] or "{}") or {}
        except (TypeError, ValueError):
            u = {}
        out["calls"] += 1
        out["cost_usd"] += _estimate_cost(r["model"], int(u.get("input_tokens") or 0),
                                          int(u.get("output_tokens") or 0), int(u.get("cache_write_tokens") or 0),
                                          int(u.get("cache_read_tokens") or 0), batch=bool(u.get("batch")))
        out["model_seconds"] += float(r["seconds"] or 0)
        out["model"] = out["model"] or r["model"]
        out["contract"] = out["contract"] or r["contract"]
    out["cost_usd"] = round(out["cost_usd"], 6)
    out["model_seconds"] = round(out["model_seconds"], 2)
    return out


# ── the post-model context, for the eval ───────────────────────────────────

# What RepairContext and the scorer read from a generation's result; the
# rest (the rows, the CSV, the review) is the week, not its context.
_RESULT_KEYS = ("week_dates", "week_days", "roster", "roster_roles", "hours_budget", "daily_target_hours",
                "labor_target", "closed_dates", "budget_basis", "weather_forecast", "shift_profiles",
                "blended_rate", "role_rates", "elsewhere", "last_week_tail", "unwritten_dates",
                "shift_requirements", "hourly_profile_ready", "hours_budget_dollars", "labor_budget_dollars",
                "requirements_by_date", "open_times")
_RESTAURANT_KEYS = ("trim_to_budget", "section_count", "week_start_day", "timezone")


def snapshot(result, constraints, restaurant=None, close_times=None, role_buffers=None, signals=None,
             weights=None, requirements=None, curve=None, solver_on=True, model_rows=None, editable=None) -> bytes:
    """The post-model context of one generation, encoded and compressed —
    what schedule_engine.post_model_rows needs to run the production
    repair loop and the gate's fill again on any arm's rows."""
    from schedule_output import encode, _scrub
    from ai_utils import guest_names_in
    ctx = {
        "result": {k: (result or {}).get(k) for k in _RESULT_KEYS if (result or {}).get(k) is not None},
        "constraints": constraints,
        "restaurant": {k: getattr(restaurant, k, None) for k in _RESTAURANT_KEYS} if restaurant is not None else None,
        "close_times": close_times or {}, "role_buffers": role_buffers or {},
        "signals": signals, "weights": weights, "requirements": requirements or [], "curve": curve or {},
        "solver_on": bool(solver_on), "editable": sorted(editable) if editable else None,
        "model_rows": [{k: r.get(k) for k in _SIG + ("notes",)} for r in model_rows or []],
    }
    enc = encode(ctx)
    try:
        enc = _scrub(enc, guest_names_in(json.dumps(enc, default=str)[:200000]))
    except Exception as e:
        _degraded(f"context not scrubbed, not kept: {e!r}", e)
        return None
    return zlib.compress(json.dumps(enc, default=str).encode("utf-8", "replace"))


def load_context(blob) -> dict:
    """snapshot()'s dict back, the Constraints a dataclass again."""
    if not blob:
        return None
    from schedule_output import decode
    return decode(json.loads(zlib.decompress(blob).decode("utf-8")))


# ── the row a generation leaves ────────────────────────────────────────────

def record(restaurant_id, history_id, generation_id, week_start, kind, draft_source=None, roster_size=None,
           model_rows=None, final_rows=None, loop=None, constraints=None, seconds=None, context=None,
           db_path=None):
    """One generation's metrics row. Never raises past the caller's own
    soft-fail; returns the row's dict (without the context)."""
    kept, total = survival(model_rows, final_rows)
    cost = generation_cost(generation_id, db_path=db_path)
    row = {"restaurant_id": restaurant_id, "history_id": history_id, "generation_id": generation_id,
           "week_start": week_start, "kind": kind if kind in KINDS else "week", "draft_source": draft_source,
           "model": cost["model"], "contract": cost["contract"], "roster_size": roster_size,
           "calls": cost["calls"], "cost_usd": cost["cost_usd"], "model_seconds": cost["model_seconds"],
           "seconds": round(float(seconds), 2) if seconds is not None else None,
           "model_rows": total, "model_kept": kept, "survival": round(kept / total, 4) if total else None,
           "role_violations": role_violations(model_rows, constraints),
           "final_rows": len(final_rows or []), "by_stage": by_stage(loop)}
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO schedule_generation_metrics (restaurant_id, history_id, generation_id, week_start, kind, "
            "draft_source, model, contract, roster_size, calls, cost_usd, model_seconds, seconds, model_rows, "
            "model_kept, survival, role_violations, final_rows, by_stage_json, context_z) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (restaurant_id, history_id, generation_id, week_start, row["kind"], draft_source, row["model"],
             row["contract"], roster_size, row["calls"], row["cost_usd"], row["model_seconds"], row["seconds"],
             total, kept, row["survival"], row["role_violations"], row["final_rows"],
             json.dumps(row["by_stage"]), context))
        conn.commit()
    finally:
        conn.close()
    return row


def _pct(values, q):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    i = min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))
    return round(v[i], 2)


def summary(restaurant_id=None, days=90, db_path=None) -> dict:
    """The headline figures over the last `days`: generations, their cost,
    the weeks the owner published among the weeks generated, cost per
    accepted week, p50/p95 seconds, mean model-row survival, the role
    violation rate and each stage's totals. A restaurant's own, or the
    fleet's when restaurant_id is None."""
    where, args = "m.created_at >= datetime('now', ?)", [f"-{int(days)} days"]
    if restaurant_id is not None:
        where += " AND m.restaurant_id=?"
        args.append(restaurant_id)
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            f"SELECT m.* FROM schedule_generation_metrics m WHERE {where}", args).fetchall()]
        weeks = {(r["restaurant_id"], r["week_start"]) for r in rows if r["week_start"]}
        accepted = 0
        for rid, ws in weeks:
            if conn.execute("SELECT 1 FROM schedule_history WHERE restaurant_id=? AND week_start=? "
                            "AND published_at IS NOT NULL LIMIT 1", (rid, ws)).fetchone():
                accepted += 1
    finally:
        conn.close()
    cost = round(sum(float(r["cost_usd"] or 0) for r in rows), 4)
    model_rows = sum(int(r["model_rows"] or 0) for r in rows)
    stages = {}
    for r in rows:
        try:
            for st, e in (json.loads(r["by_stage_json"] or "{}") or {}).items():
                t = stages.setdefault(st, {"changed": 0, "trimmed": 0, "staggered": 0, "added": 0})
                for k in t:
                    t[k] += int((e or {}).get(k) or 0)
        except (TypeError, ValueError):
            continue
    return {"generations": len(rows), "cost_usd": cost, "weeks": len(weeks), "accepted_weeks": accepted,
            "cost_per_accepted_week": round(cost / accepted, 4) if accepted else None,
            "seconds_p50": _pct([r["seconds"] for r in rows], 0.5),
            "seconds_p95": _pct([r["seconds"] for r in rows], 0.95),
            "survival_mean": (round(sum(int(r["model_kept"] or 0) for r in rows) / model_rows, 4)
                              if model_rows else None),
            "role_violation_rate": round(sum(int(r["role_violations"] or 0) for r in rows) / model_rows, 4)
            if model_rows else None,
            "by_stage": stages}


def context_for(generation_id=None, history_id=None, db_path=None, kind=None):
    """The newest stored context blob of a generation (or of a history row's
    generation — of its pass of `kind`, when named: the one the job saved),
    for schedule_eval.freeze and the queued learning ablation."""
    conn = get_conn(db_path)
    try:
        if kind and history_id:
            r = conn.execute("SELECT context_z FROM schedule_generation_metrics WHERE history_id=? AND kind=? "
                             "AND context_z IS NOT NULL ORDER BY id DESC LIMIT 1", (history_id, kind)).fetchone()
        elif generation_id:
            r = conn.execute("SELECT context_z FROM schedule_generation_metrics WHERE generation_id=? "
                             "AND context_z IS NOT NULL ORDER BY id DESC LIMIT 1", (generation_id,)).fetchone()
        else:
            r = conn.execute("SELECT context_z FROM schedule_generation_metrics WHERE history_id=? "
                             "AND context_z IS NOT NULL ORDER BY id DESC LIMIT 1", (history_id,)).fetchone()
    finally:
        conn.close()
    return r["context_z"] if r else None
