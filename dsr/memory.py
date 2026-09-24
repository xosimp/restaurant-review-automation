"""
dsr.memory — the nightly reports as the rest of the product remembers them
(docs/plans/DSR_ENGINE_PLAN.md §2, §8, phase 6): Ask's tools, Ask's context
and the morning brief.

Every read here is a compact view of the ONE stored snapshot, through
dsr.access for the login asking — never a second copy of the figures and
never a third prose document. "Ask context is the facts JSON plus the
narrative": the headline metrics by fact key and the narrative's own
(verified) lead and actions, redacted exactly as the screen redacts them, so
a manager never reads the budget, prime cost, loss lines or food cost here
either.

  finished(rid, day)            the night's latest FINISHED version (final or
                                provisional), else the latest in progress
  compact(report, user)         read_dsr's payload: metrics per block, a
                                capped detail, the manager's words (as
                                untrusted text), the narrative as strings
  context_block(rid, user, day) Ask's "LAST NIGHT'S DSR" section, or ""
  last_night(rid, day, user)    the morning brief's figures for last night

`user` is a current_user-shaped dict; OWNER_USER stands in for a caller with
no login behind it (the scheduler, admin tooling) — the owner's view, as
everywhere else a None viewer is unrestricted.
"""
from datetime import date, timedelta

import dsr as _dsr
from dsr import access, store

OWNER_USER = {"is_admin": True, "role": "owner"}

# The figures a one-glance read carries, per block, in this order — the
# night's headline, not every metric (read_dsr has the rest).
HEADLINE = {
    "sales": ("net", "gross", "transactions", "avg_ticket", "guests", "vs_forecast_pct",
              "vs_budget_net", "vs_budget_net_pct"),
    "labor": ("pct", "cost", "target_pct", "vs_target_pts", "hours", "overtime_hours", "no_shows"),
    "food": ("est_food_cost_pct", "waste_logged", "low_count", "critical_count"),
    "reviews": ("received", "avg_rating", "urgent", "drafts_awaiting"),
    "marketing": ("posts_published", "campaigns_sent"),
    "intel": ("weather_high_f",),
}
# Detail a read_dsr answer may carry, per block — the lists an owner asks
# about ("what sold", "who was late"), never the whole structure. Reviews'
# own rows (guest names, summaries of guests' words) stay with read_reviews.
DETAIL_KEYS = {
    "sales": ("top_items", "bottom_items", "categories", "unmapped"),
    "labor": ("coverage", "observations", "shift_quality"),
    "food": ("estimate", "stock", "waste", "recoverable", "drivers_at_stake"),
    "reviews": ("themes",),
    "marketing": ("posts", "campaigns", "upcoming"),
    "intel": ("weather", "events", "competitors", "traffic"),
}
LIST_CAP = 5
TEXT_CAP = 160
NOTE_CAP = 500
CONTEXT_DAYS = 3        # "last night" in Ask's context: a report older than this is not last night


def _user(user):
    return OWNER_USER if user is None else user


def finished(restaurant_id, day, db_path=None):
    """The night as readers see it after close: its latest final or
    provisional version, else whatever version is running (None: no report)."""
    import models
    db_path = db_path or models.DB_PATH
    return (store.get_finished_report(restaurant_id, day, db_path=db_path)
            or store.get_report(restaurant_id, day, db_path=db_path))


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _fmt(key, v):
    from dsr.narrative import _fmt as fmt, _is_pct
    return f"{fmt(v)}{'%' if _is_pct(key) else ''}"


def _clip(s, cap=TEXT_CAP):
    s = " ".join(str(s).split())
    return s if len(s) <= cap else s[:cap - 1] + "…"


def _compact(v, depth=0):
    """A detail value, capped: lists to LIST_CAP entries (with how many
    more), strings to TEXT_CAP, three levels deep."""
    if v is None or isinstance(v, bool) or _num(v):
        return v
    if isinstance(v, str):
        return _clip(v)
    if depth >= 3:
        return None
    if isinstance(v, dict):
        out = {}
        for k, x in list(v.items())[:12]:
            c = _compact(x, depth + 1)
            if c not in (None, "", [], {}):
                out[str(k)] = c
        return out
    if isinstance(v, (list, tuple)):
        items = [c for c in (_compact(x, depth + 1) for x in v[:LIST_CAP]) if c not in (None, "", [], {})]
        if len(v) > LIST_CAP:
            items.append({"more": len(v) - LIST_CAP})
        return items
    return None


def day_label(day):
    from time_utils import mdy
    d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    return f"{d.strftime('%a')} {mdy(d)}"


def _fiscal_label(facts):
    f = (facts or {}).get("fiscal") or {}
    if f.get("period"):
        return f"Period {f['period']} · Week {f.get('week')}"
    return None


def _narrative_strings(n):
    """The view's narrative as plain strings (and actions as do/why), so a
    reader quotes it without the fact-key plumbing."""
    if not isinstance(n, dict):
        return None

    def text(it):
        return it.get("text") if isinstance(it, dict) and isinstance(it.get("text"), str) else None
    out = {}
    if text(n.get("executive_summary")):
        out["lead"] = text(n["executive_summary"])
        if n.get("lead_from"):
            out["lead_from"] = n["lead_from"]
    for f in ("went_well", "needs_attention"):
        items = [t for t in (text(i) for i in n.get(f) or []) if t]
        if items:
            out[f] = items
    for f in ("biggest_risk", "biggest_win", "biggest_financial_opportunity", "biggest_staffing_concern"):
        if text(n.get(f)):
            out[f] = text(n[f])
    acts = [{"do": a.get("text"), "why": a.get("why"), "urgency": a.get("urgency")}
            for a in n.get("actions_tomorrow") or [] if isinstance(a, dict) and a.get("text")]
    if acts:
        out["actions_tomorrow"] = acts
    return out or None


def _closeout_notes(block):
    """The manager's own words as "Label: words" strings — people's words,
    which Ask fences as untrusted (they arrive under `notes`)."""
    if not block or block.get("status") != _dsr.READY:
        return []
    detail = block.get("detail") or {}
    fields = detail.get("fields") or {}
    labels = detail.get("labels") or {}
    order = detail.get("order") or list(fields)
    return [f"{labels.get(k, k)}: {_clip(fields[k], NOTE_CAP)}" for k in order
            if isinstance(fields.get(k), str) and fields[k].strip()]


def compact(report, user):
    """One night for read_dsr, as `user` may read it."""
    user = _user(user)
    view = access.view_for(user)
    facts, hidden = access.redact(report.get("facts") or {}, user)
    blocks = {}
    for name in _dsr.BLOCKS:
        b = (facts.get("blocks") or {}).get(name)
        if b is None or name == "closeout":
            continue
        entry = {"status": b.get("status")}
        if b.get("reason"):
            entry["reason"] = b["reason"]
        if b.get("source"):
            entry["source"] = b["source"]
        metrics = {k: v for k, v in (b.get("metrics") or {}).items() if _num(v)}
        if metrics:
            entry["metrics"] = metrics
        detail = {k: _compact(v) for k, v in (b.get("detail") or {}).items()
                  if k in DETAIL_KEYS.get(name, ()) and v not in (None, "", [], {})}
        detail = {k: v for k, v in detail.items() if v not in (None, "", [], {})}
        if detail:
            entry["detail"] = detail
        blocks[name] = entry
    out = {
        "date": report.get("business_date"), "label": day_label(report.get("business_date")),
        "fiscal": _fiscal_label(facts), "view": view, "status": report.get("status"),
        "provisional": bool(report.get("provisional")), "version": report.get("version"),
        "blocks": blocks, "missing": facts.get("missing") or [], "withheld": facts.get("withheld") or [],
        "summary": _narrative_strings(access.narrative_for(report.get("narrative"), hidden)),
    }
    notes = _closeout_notes((facts.get("blocks") or {}).get("closeout"))
    if notes:
        out["notes"] = notes
    if report.get("status") == "failed":
        out["failed"] = "This night's report couldn't be finished; only what was collected is here."
    elif report.get("status") not in store.FINISHED:
        out["in_progress"] = "This night's report is still being put together; figures may change."
    elif out["provisional"]:
        out["provisional_note"] = "Provisional: some data was still syncing when it went out; a later version replaces it."
    if not out["summary"]:
        out.pop("summary")
    return out


def headline(facts):
    """[(fact key, value)] — the night's headline figures in HEADLINE order,
    from already-redacted facts, ready blocks only."""
    out = []
    for name, keys in HEADLINE.items():
        b = (facts.get("blocks") or {}).get(name)
        if not b or b.get("status") != _dsr.READY:
            continue
        m = b.get("metrics") or {}
        out += [(f"{name}.{k}", m[k]) for k in keys if _num(m.get(k))]
    return out


def context_block(restaurant_id, user, today, db_path=None):
    """Ask's section for last night's report: the headline figures by fact
    key and the narrative's lead and actions, redacted for `user`. "" when
    there is no finished report in the last CONTEXT_DAYS nights."""
    import models
    db_path = db_path or models.DB_PATH
    user = _user(user)
    if access.view_for(user) is None:
        return ""
    today = today if isinstance(today, date) else date.fromisoformat(str(today)[:10])
    report = store.latest_finished_report(restaurant_id, today - timedelta(days=1),
                                          since=today - timedelta(days=CONTEXT_DAYS), db_path=db_path)
    if not report:
        return ""
    facts, hidden = access.redact(report.get("facts") or {}, user)
    fiscal = _fiscal_label(facts)
    state = "provisional — some data was still syncing" if report.get("provisional") else "final"
    if (report.get("version") or 1) > 1:
        state += f", version {report['version']}"
    lines = [f"LAST NIGHT'S DAILY SALES REPORT ({day_label(report['business_date'])}"
             f"{' · ' + fiscal if fiscal else ''}; {state}). These are the report's own figures — quote them as they "
             "are, never recompute them; use the read_dsr tool for the full night, find_days for history."]
    figs = headline(facts)
    if figs:
        lines.append("Figures: " + " · ".join(f"{k} {_fmt(k, v)}" for k, v in figs))
    for reason in facts.get("missing") or []:
        lines.append(f"Missing: {reason}")
    n = _narrative_strings(access.narrative_for(report.get("narrative"), hidden)) or {}
    if n.get("lead"):
        lines.append(f"Summary: {_clip(n['lead'], 700)}")
    for a in (n.get("actions_tomorrow") or [])[:3]:
        lines.append(f"- To do: {_clip(a['do'], 240)}")
    return "\n".join(lines) + "\n"


def last_night(restaurant_id, today, user=None, db_path=None):
    """Last night's figures for the morning brief from its finished DSR, as
    `user` may read them — None when there is no finished report for last
    night or its sales were not measured (the brief then falls back to what
    it read before). Every figure is the report's own; nothing recomputed."""
    import models
    db_path = db_path or models.DB_PATH
    user = _user(user)
    if access.view_for(user) is None:
        return None
    day = (today if isinstance(today, date) else date.fromisoformat(str(today)[:10])) - timedelta(days=1)
    report = store.get_finished_report(restaurant_id, day, db_path=db_path)
    if not report:
        return None
    facts, _hidden = access.redact(report.get("facts") or {}, user)
    blocks = facts.get("blocks") or {}
    sales = blocks.get("sales") or {}
    sm = sales.get("metrics") or {}
    if sales.get("status") != _dsr.READY or not _num(sm.get("net")):
        return None
    labor = blocks.get("labor") or {}
    lm = (labor.get("metrics") or {}) if labor.get("status") == _dsr.READY else {}
    return {"date": day.isoformat(), "weekday": day.strftime("%A"), "status": report.get("status"),
            "provisional": bool(report.get("provisional")), "version": report.get("version"),
            "net": sm["net"],
            "forecast_net": sm.get("forecast_net") if _num(sm.get("forecast_net")) else None,
            "vs_forecast_pct": sm.get("vs_forecast_pct") if _num(sm.get("vs_forecast_pct")) else None,
            "labor_pct": lm.get("pct") if _num(lm.get("pct")) else None,
            "labor_target_pct": lm.get("target_pct") if _num(lm.get("target_pct")) else None,
            "missing": facts.get("missing") or []}
