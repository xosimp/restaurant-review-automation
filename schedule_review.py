"""
schedule_review.py — the stored review of a week, as a save refreshes it,
and the review's lines about days nobody wrote or nobody can work.

`refresh_review` keeps what the generation did and found (its fixes, the
manager plan and its question, the rules it could not check) and refreshes
only what the edited rows decide, from their sweep. Pure: a function of the
saved review, the sweep and the rows — schedule_versions calls it inside a
save's transaction, so it lives below the history store, beside the rules
it reads (schedule layer S2, ARCHITECTURE_MANIFEST.md §3a). schedule_engine
re-exports every name.
"""
import logging

import schedule_rules as _rules
import schedule_time as _stime

log = logging.getLogger(__name__)

from schedule_context import degraded as _degraded  # noqa: E402  (the one degradation event)


def _unstaffable_line(items) -> str:
    """The review's warning for days nobody can work (E-20) — a "⚠" line, so
    the publish gate holds the week until the owner has seen it."""
    from time_utils import mdy
    days = [f"{it['day']} {mdy(it['date'])}" for it in items]
    which = days[0] if len(days) == 1 else ", ".join(days[:-1]) + " and " + days[-1]
    one = len(days) == 1
    return (f"⚠ Nobody on the team can work {which} (time off or availability), so {'it was' if one else 'they were'} "
            f"left empty — mark {'it' if one else 'them'} closed or fix availability, then redo "
            f"{'that day' if one else 'those days'}.")


_UNWRITTEN_WHY = {
    "skipped": "the model left them empty even when asked again",
    "cut": "the model ran out of room writing them, even in smaller parts",
    "calls": "the generation used every call it is allowed",
    "time": "the generation ran out of time",
    "timeout": "the model took too long on them",
    "budget": "AI is paused — this account has reached its AI limit",
    "provider_down": "Cavnar AI's model provider stopped responding partway",
    "provider": "Cavnar AI's model provider failed partway",
    "data": "the data behind them stopped being current partway",
    "refused": "the model declined to write them",
    "capped": "the generation reached the spending cap a draft is allowed",
    "config": "Cavnar AI couldn't reach its model partway (we've been alerted)",
    "generation": "they couldn't be written",
    "other": "something failed partway (we've been alerted)",
}


def _gap_groups(items) -> list:
    """[(days words, why words)] for unwritten dates, grouped by why, in
    _UNWRITTEN_WHY's order: "Saturday 10/10/26 and Sunday 10/11/26" with
    "the model left them empty even when asked again"."""
    from time_utils import mdy
    by = {}
    for it in items:
        by.setdefault(it["why"], []).append(f"{it['day']} {mdy(it['date'])}")
    order = list(_UNWRITTEN_WHY)
    out = []
    for why in sorted(by, key=lambda w: order.index(w) if w in order else len(order)):
        days = by[why]
        words = _UNWRITTEN_WHY.get(why, _UNWRITTEN_WHY["other"])
        if len(days) == 1:
            words = words.replace("them", "it")
        out.append((days[0] if len(days) == 1 else ", ".join(days[:-1]) + " and " + days[-1], words, len(days)))
    return out


def _unwritten_line(items) -> str:
    """The review's warning for days the generation could not write — a "⚠"
    line, so the publish gate holds the week until they are redone. The
    rest of the week is kept (P-34): the days already paid for are not
    thrown away for the ones that failed."""
    groups = _gap_groups(items)
    one = len(items) == 1
    tail = f" The rest of the week is here; redo {'that day' if one else 'those days'} before you publish."
    if len(groups) == 1:
        days, why, n = groups[0]
        return f"⚠ {days} {'was' if n == 1 else 'were'} not written — {why}.{tail}"
    return "⚠ Not written: " + "; ".join(f"{days} — {why}" for days, why, _n in groups) + "." + tail


def _safe_hours_sum(rows: list) -> float:
    """Total scheduled_hours across rows, skipping any row whose value
    isn't actually numeric instead of raising — a live generation
    occasionally has one malformed row out of 100+ (see
    test_schedule_row_repair.py) where scheduled_hours ends up holding a
    stray time string from a column shift. The original per-row parsing
    loop already tolerates this per-row; the backstop passes' own
    hours_scheduled recomputes need the same tolerance, since a single bad
    row previously aborted the recompute (and by extension the CSV
    rebuild) via an uncaught ValueError from float() inside sum()."""
    total = 0.0
    for r in rows:
        try:
            total += float(r.get("scheduled_hours") or 0)
        except (ValueError, TypeError):
            pass
    return round(total, 1)


def _unfixed_from(viols, rows, editable, why) -> list:
    """What the week still breaks on a row after the loop, from the FINAL
    sweep (P-15: it came from the first fix pass and was reported after
    later passes had changed the rows), each with why it was left."""
    import shift_quality as _sq
    out = []
    for v in viols or []:
        if not v.get("hard") or v.get("day_level") or not isinstance(v.get("index"), int):
            continue
        r = rows[v["index"]]
        if editable is not None and r.get("date") not in editable:
            continue
        label = v.get("label") or v.get("kind")
        reason = (why or {}).get(((v.get("employee") or "").strip().lower(), r.get("date"), v.get("kind")))
        if not reason:
            reason = (f"{label} — nobody who could legally take the shift was found"
                      if v.get("kind") in _sq.PERSON_FIXABLE
                      else f"{label} — not something a different person on the shift would change.")
        out.append({"index": v["index"], "row_id": r.get("_rid"), "employee": v.get("employee"),
                    "date": v.get("date"), "shift_start": v.get("shift_start"), "kind": v.get("kind"),
                    "reason": reason})
    return out


# ── A Studio save keeps the generation's review (schedule audit 10/3/26 P-26) ──
#
# The Studio's save overwrote the week's review with a bare summarize() of
# the edited rows, so what the generation did and found — the fix lines, the
# optimizer's changes, the trims, the manager stretches nobody could cover
# and why, the owner's rules it could not check, the over-budget line —
# vanished at the first save. refresh_review keeps all of it and refreshes
# only what the rows decide.
_REVIEW_RULE_KEYS = ("hard", "soft", "over_limit", "by_kind", "hard_rows", "hard_days")


def _fix_sig(r) -> tuple:
    return ((r.get("employee") or "").strip().lower(), r.get("date") or "", r.get("shift_start") or "",
            r.get("shift_end") or "")


def _span_of(frm, to):
    """(start, end) of a stored stretch's clock words on the business
    timeline (schedule_time.place): a stretch starting in the small hours is
    that night's, as manager_gaps reads it (BUGSGEN-10). For a review saved
    before its entries carried their minutes."""
    return _stime.place(_rules.parse_minutes(frm or ""), _rules.parse_minutes(to or ""))


def _left_span(entry):
    """(start, end) minutes of a manager stretch the backstop left: its own
    gap_start/gap_end when it carries them (BUGSGEN-10), else read from its
    words."""
    try:
        if entry.get("gap_start") is not None and entry.get("gap_end") is not None:
            return int(entry["gap_start"]), int(entry["gap_end"])
    except (TypeError, ValueError):
        pass
    return _span_of(entry.get("from"), entry.get("to"))


def refresh_review(saved, violations, rows, constraints=None, hours_budget=None, extras=None) -> dict:
    """The review a Studio save stores for edited `rows`: the generation's
    own — what Cavnar AI fixed, trimmed, staggered and optimized, the
    manager plan and its question, the setup, the rules it could not check,
    the repair stages that did not run, the settings it could not read —
    kept, and only what the rows decide refreshed from `violations` (their
    sweep): the counts and the rule lines, the rows still broken, the
    rows needing review, the hours over the budget, the manager stretches
    and minimum hours still short, and `extras` (what the week does not
    meet, its staffing asks — schedule_output.week_review_extras). A fix
    line keeps pointing at its row while the shift it describes is still in
    the week and points at none once the edit took it out. A review saved
    before its lines were told apart keeps its non-⚠ lines; its ⚠ lines were
    the sweep's and the budget's, and are read fresh."""
    saved = dict(saved or {})
    fresh = _rules.summarize(violations or [])
    out = dict(saved)
    for k in _REVIEW_RULE_KEYS:
        out[k] = fresh.get(k)
    out.update(extras or {})
    out["unfixed"] = _unfixed_from(violations, rows, None, {})
    out["needs_review"] = [{"index": u["index"], "row_id": None, "reason": u["reason"]} for u in out["unfixed"]]
    at = {}
    for i, r in enumerate(rows or []):
        at.setdefault(_fix_sig(r), i)
    fixes = []
    for f in saved.get("fixes") or []:
        f = dict(f)
        f["index"] = at.get(_fix_sig(f.get("row") or {})) if f.get("row") else None
        f.pop("row_id", None)            # ids live with a generation's rows, not a saved CSV
        fixes.append(f)
    out["fixes"] = fixes
    # What the backstops could not do, while it is still so.
    if constraints is not None and isinstance(saved.get("manager_coverage"), dict):
        gaps = _rules.manager_gaps(rows or [], constraints)
        mc = dict(saved["manager_coverage"])
        still = []
        for e in mc.get("left") or []:
            want = _left_span(e)
            if want and any(gs < want[1] and want[0] < ge for gs, ge, _i in gaps.get(e.get("date"), [])):
                still.append(e)
        mc["left"] = still
        if not still:
            mc["shortfall"] = None
        out["manager_coverage"] = mc
    # The closing stays the owner's verdict assumes, as the rows now stand
    # (TRUST-21): an edit that wrote a manager to the last person out, or
    # moved the last person, changes them.
    old_stay_lines = list(saved.get("stay_lines") or [])
    if constraints is not None:
        try:
            stays = _rules.assumed_stays(rows or [], constraints)
        except Exception as _sx:
            _degraded(f"assumed stays unavailable: {_sx!r}", _sx)
            stays = None
        if stays is not None:
            out["assumed_stays"] = stays
            out["stay_lines"] = [s["text"] for s in stays]
    if isinstance(saved.get("min_hours"), dict):
        short = {(v.get("employee") or "").strip().lower() for v in violations or [] if v.get("kind") == "under_min_hours"}
        mh = dict(saved["min_hours"])
        mh["left"] = [e for e in mh.get("left") or [] if (e.get("employee") or "").strip().lower() in short]
        out["min_hours"] = mh
    # The lines: the budget's, then what the generation had to say first,
    # then the sweep's, then the rest of what it said.
    told_apart = "rule_lines" in saved
    rule_lines = set(saved.get("rule_lines") or [])
    budget_old = list(saved.get("budget_lines") or [])
    lead = list(saved.get("lead_lines") or []) if told_apart else []
    if told_apart:
        tail = [ln for ln in saved.get("lines") or []
                if ln not in rule_lines and ln not in set(budget_old) and ln not in set(lead)]
    else:
        tail = [ln for ln in saved.get("lines") or [] if not str(ln).startswith("⚠")]
    if "stay_lines" in out and out.get("stay_lines") != old_stay_lines:
        tail = [ln for ln in tail if ln not in set(old_stay_lines)] + list(out["stay_lines"])
    # A day the generation could not write, or nobody could work, that the
    # owner has since filled is not said as empty any more.
    on = {r.get("date") for r in rows or [] if (r.get("employee") or "").strip()}
    for key, line_of in (("unwritten_dates", _unwritten_line), ("unstaffable_dates", _unstaffable_line)):
        items = list(saved.get(key) or [])
        if not items:
            continue
        try:
            old = line_of(items)
        except Exception:
            continue
        left = [u for u in items if u.get("date") not in on]
        out[key] = left
        if old in lead:
            i = lead.index(old)
            if left:
                lead[i] = line_of(left)
            else:
                lead.pop(i)
    budget = []
    try:
        hb = float(hours_budget or 0)
        hs = _rules.hours_split(rows or [], constraints)["hourly"] if constraints is not None else \
            _safe_hours_sum(rows or [])
    except (TypeError, ValueError, KeyError):
        hb = hs = 0.0
    if hb > 0 and hs > hb * 1.02:
        out["over_budget_hours"] = round(hs - hb, 1)
        budget.append(f"⚠ {hs:,.0f}h scheduled against a {hb:,.0f}h budget — {hs - hb:,.0f}h over the ceiling")
        if saved.get("budget_conflict"):
            try:
                bcl = budget_conflict_line(saved["budget_conflict"], over_by=round(hs - hb, 1), budget=hb)
                if bcl:
                    budget.append(bcl)
            except Exception as _bx:
                _degraded(f"budget conflict line unavailable: {_bx}", _bx)
        budget += [ln for ln in budget_old[1:] if ln.startswith("The rules that come before the budget")]
    else:
        out.pop("over_budget_hours", None)
    out["lines"] = budget + lead + list(fresh.get("lines") or []) + tail
    out["rule_lines"], out["budget_lines"], out["lead_lines"] = list(fresh.get("lines") or []), budget, lead
    out["edited"] = True
    return out


def binding_budget(cost, dollar_budget, hours, hours_budget, overtime_premium=0.0) -> dict:
    """Which budget binds when the dollar budget is over (blind re-audit
    10/11/26 BUGSGEN-22). The dollar budget prices each person at their own
    rate; the hours budget is the same dollars at the blended rate. So a week
    can be dollars over with its hours exactly at budget, and "$298 over
    (988h scheduled against 988h)" read as a contradiction. Pure.

    {"binds": "both" | "dollars" | None, "over_dollars", "paid_rate",
    "assumed_rate", "overtime_premium", "note"} — `note` the words that go in
    brackets after "$X over": the hours beside it when they are over too,
    else that the hours are within budget and why the dollars are over all
    the same (the crew is paid above the blended rate the hours budget
    assumes, and/or the overtime premium) — the dollar budget binds."""
    try:
        cost, lbd = float(cost or 0), float(dollar_budget or 0)
        hrs, hb = float(hours or 0), float(hours_budget or 0)
        prem = max(0.0, float(overtime_premium or 0))
    except (TypeError, ValueError):
        return {"binds": None, "note": ""}
    out = {"binds": None, "over_dollars": round(cost - lbd, 0) if lbd > 0 else None, "paid_rate": None,
           "assumed_rate": round(lbd / hb, 2) if (lbd > 0 and hb > 0) else None,
           "overtime_premium": round(prem, 0), "note": ""}
    if lbd <= 0 or cost <= lbd:
        return out
    if hb > 0 and hrs > hb + 0.5:
        out["binds"] = "both"
        out["note"] = f"{hrs:,.0f}h scheduled against {hb:,.0f}h"
        return out
    out["binds"] = "dollars"
    straight = cost - prem
    out["paid_rate"] = round(straight / hrs, 2) if hrs > 0 else None
    why = []
    if out["paid_rate"] and out["assumed_rate"] and out["paid_rate"] > out["assumed_rate"] + 0.005:
        why.append(f"this crew is paid ${out['paid_rate']:,.2f}/h on average against the "
                   f"${out['assumed_rate']:,.2f}/h blended rate the hours budget assumes")
    if prem >= 1:
        why.append(f"${prem:,.0f} of it is overtime premium")
    hours_words = f"{hrs:,.0f}h of a {hb:,.0f}h hours budget" if hb > 0 else f"{hrs:,.0f}h scheduled"
    out["note"] = (f"the hours are within budget, {hours_words}; the dollar budget is the one that binds"
                   + (" — " + " and ".join(why) if why else ""))
    return out


def budget_conflict_line(conflict: dict, over_by: float = None, budget: float = None) -> str:
    """One sentence for the review when the trim stopped above the budget:
    what holds the remaining hours — the shifts' own requirement, days and
    dayparts kept from cuts, the rules — and what the owner can change
    (schedule audit 10/3/26 SQ-7: the requirement and the budget disagree,
    said once). "" when there is no conflict."""
    if not conflict:
        return ""
    over = conflict.get("over_by") if over_by is None else over_by
    b = conflict.get("budget") if budget is None else budget
    held = conflict.get("held") or {}
    ex = conflict.get("examples") or []

    def _first(reason):
        return next((x for x in ex if x.get("reason") == reason), None)

    def _part(p):
        return {"morning": "lunch", "night": "dinner"}.get(p, p or "")
    bits = []
    if held.get("requirement"):
        from shift_quality import role_words
        x = _first("requirement")
        what = ""
        if x and x.get("required"):
            what = (f" — {x['day']} {_part(x.get('daypart'))} needs {x['required']} "
                    f"{role_words(x.get('role'), int(x['required']), 'people')}"
                    + (f" at {x['at']}" if x.get("at") else ""))
        bits.append("what the shifts need (your usual staffing, floors and demand" + what + ")")
    if held.get("objective"):
        # LOGIC-6: the cut would lose more, at the stated value of a person
        # short on that half hour, than the dollars it saves past the budget.
        from shift_quality import role_words
        x = _first("objective")
        what = ""
        if x and x.get("required"):
            what = (f" — {x['day']} {_part(x.get('daypart'))} needs {x['required']} "
                    f"{role_words(x.get('role'), int(x['required']), 'people')}")
        elif x and x.get("day") and x.get("role"):
            what = f" — {x['day']} {x['role']}"
        bits.append("what the shifts need, worth more in service than the cut would save" + what)
    if held.get("learned"):
        x = _first("learned")
        bits.append(f"{x['day']}s, where staffing cuts measured worse here" if x and x.get("day")
                    else "days where staffing cuts measured worse here")
    if held.get("troubled"):
        x = _first("troubled")
        bits.append(f"{x['day']} {_part(x.get('daypart'))}, which has gone wrong before" if x and x.get("day")
                    else "dayparts that have gone wrong before")
    if held.get("rule"):
        x = _first("rule")
        bits.append("the rules" + (f" — {x['label']}" if x and x.get("label") else ""))
    if held.get("floor") or held.get("last_of_role") or held.get("station"):
        bits.append("your floors and the last of each role")
    if held.get("min_hours") or held.get("only_shift"):
        bits.append("minimum hours and people's only shifts")
    if held.get("kept"):
        bits.append("the days you kept as they were")
    if conflict.get("timed_out"):
        bits.insert(0, "its time limit")
    if not bits:
        return ""
    if conflict.get("over_by_dollars") is not None and conflict.get("budget_dollars"):
        # The budget is held in dollars (LOGIC-13): said that way, the
        # hours beside it.
        # Which budget binds, never "(988h against 988h)" beside a dollar
        # overrun (BUGSGEN-22).
        _bd = float(conflict["budget_dollars"])
        _bb = binding_budget(_bd + float(conflict["over_by_dollars"]), _bd, conflict.get("scheduled"),
                             conflict.get("budget"), conflict.get("overtime_premium"))
        return (f"The trim stopped ${float(conflict['over_by_dollars']):,.0f} over the "
                f"${_bd:,.0f} hourly budget" + (f" ({_bb['note']})" if _bb.get("note") else "")
                + ": the rest is held by "
                + "; ".join(bits[:3]) + ". Raise the budget, or lower the floors or staffing you set, to close it.")
    return (f"The trim stopped {over:,.0f}h over the {b:,.0f}h budget: the rest is held by "
            + "; ".join(bits[:3]) + ". Raise the hours budget, or lower the floors or staffing you set, to close it.")
